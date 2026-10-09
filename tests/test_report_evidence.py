# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from huiyiku.domain.report_evidence import (
    EvidenceIndex,
    content_tokens,
    resolve_report_evidence,
)


def _seg(i: int, text: str, speaker: str = "张三", start: int = 0) -> dict:
    return {
        "id": i,
        "start_ms": start,
        "end_ms": start + 40000,
        "speaker_id": i + 100,
        "speaker_name": speaker,
        "text": text,
    }


def test_quoted_hits_substring_segment() -> None:
    idx = EvidenceIndex([_seg(1, "我们今天讨论了数据中台的建设方案", start=120000)])
    r = idx.resolve("讨论了数据中台的建设方案")
    assert r["status"] == "quoted"
    assert r["evidence"][0]["segment_id"] == 1
    assert r["evidence"][0]["start_ms"] == 120000
    assert r["evidence"][0]["speaker_name"] == "张三"


def test_paraphrase_matches_by_content_words() -> None:
    seg = "项目组决定在下个季度完成预算审批并启动供应商招标流程"
    idx = EvidenceIndex([_seg(1, seg)])
    r = idx.resolve("会议决定下季度完成预算审批，还启动了招标工作")
    assert r["status"] == "paraphrase"
    assert r["evidence"][0]["segment_id"] == 1


def test_unrelated_text_is_unsupported() -> None:
    idx = EvidenceIndex([_seg(1, "项目组决定在下个季度完成预算审批并启动供应商招标流程")])
    r = idx.resolve("团队计划去滨海城市组织团建活动")
    assert r["status"] == "unsupported"
    assert r["evidence"] == []


def test_stopwords_do_not_inflate_coverage() -> None:
    seg = "因为所以这个那个现在我们已经通过关于"
    idx = EvidenceIndex([_seg(1, seg)])
    # 全是虚词：既不是原句子串，也没有内容词可打分
    r = idx.resolve("这个那个现在已经")
    assert r["status"] == "unsupported"
    # content_tokens 把多字虚词全部过滤
    assert content_tokens("这个数据") == ["数据"]


def test_hint_ids_only_reorder_not_fabricate() -> None:
    seg1 = _seg(1, "会议决定下季度完成预算审批并启动外部招标", start=0)
    seg2 = _seg(2, "会上决定下季度完成预算审批并启动内部招标", start=60000)
    idx = EvidenceIndex([seg1, seg2])
    item = "会议决定下季度完成预算审批，还启动了招标工作"
    r = idx.resolve(item, hint_segment_ids=[2])
    assert r["status"] == "paraphrase"
    assert r["evidence"][0]["segment_id"] == 2

    # 提示 id 指向无关段也不会把 unsupported 洗成有据
    r2 = idx.resolve("团队计划去滨海城市组织团建活动", hint_segment_ids=[2])
    assert r2["status"] == "unsupported"
    assert r2["evidence"] == []


def test_evidence_capped_at_three() -> None:
    line = "一致同意把交付日期定在月底"
    idx = EvidenceIndex([_seg(i, f"第{i}点：{line}", start=i * 40000) for i in range(1, 6)])
    r = idx.resolve(line)
    assert r["status"] == "quoted"
    assert len(r["evidence"]) == 3


def test_resolve_report_evidence_counts_and_attaches() -> None:
    segs = [
        _seg(1, "项目组决定在下个季度完成预算审批并启动供应商招标流程"),
        _seg(2, "一致同意把交付日期定在月底"),
    ]
    idx = EvidenceIndex(segs)
    data = {
        "key_points": [{"content": "一致同意把交付日期定在月底", "segment_ids": []}],
        "decisions": [
            {"content": "会议决定下季度完成预算审批，还启动了招标工作", "segment_ids": []},
            {"content": "团队计划去滨海城市组织团建活动", "segment_ids": []},
        ],
        "action_items": [{"title": "团队计划去滨海城市组织团建活动", "segment_ids": []}],
        "risks": [],
        "open_questions": [],
    }
    counts = resolve_report_evidence(data, idx)
    assert counts == {"quoted": 1, "paraphrase": 1, "unsupported": 2}
    assert data["key_points"][0]["evidence_status"] == "quoted"
    assert data["decisions"][0]["evidence_status"] == "paraphrase"
    assert data["decisions"][1]["evidence_status"] == "unsupported"
    assert data["action_items"][0]["evidence_status"] == "unsupported"


def test_report_api_returns_evidence(runtime) -> None:
    """GET /report 读取时就地补出处，不写回版本快照。"""
    import json

    from fastapi.testclient import TestClient

    from huiyiku.api.app import create_app
    from huiyiku.db.models import Report, ReportVersion
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.store import create_meeting, persist_transcript
    from huiyiku.timeutil import now_iso

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="溯源会议")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": 0,
                    "end_ms": 40000,
                    "text": "项目组决定在下个季度完成预算审批并启动供应商招标流程",
                    "speaker_label": "SPEAKER_00",
                },
                {
                    "start_ms": 40000,
                    "end_ms": 80000,
                    "text": "另外团队计划去滨海城市组织团建活动",
                    "speaker_label": "SPEAKER_01",
                },
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        ts = now_iso()
        report = Report(meeting_id=m.id, created_at=ts, updated_at=ts)
        session.add(report)
        session.flush()
        payload = {
            "summary": "概要",
            "key_points": [
                {"content": "完成预算审批并启动供应商招标流程", "segment_ids": []}
            ],
            "decisions": [],
            "action_items": [
                {"title": "团队计划去滨海城市组织团建活动", "owner_person_id": None,
                 "due_at": None, "priority": "normal", "segment_ids": []}
            ],
            "risks": [],
            "open_questions": [],
            "participants": [],
        }
        ver = ReportVersion(
            report_id=report.id,
            version=1,
            source="llm",
            content_json=json.dumps(payload, ensure_ascii=False),
            status="draft",
            created_at=ts,
        )
        session.add(ver)
        session.flush()
        report.current_version_id = ver.id
        session.commit()
        mid, vid = m.id, ver.id

    client = TestClient(create_app(runtime))
    cur = client.get(f"/api/meetings/{mid}/report").json()["current"]
    kp = cur["content"]["key_points"][0]
    assert kp["evidence_status"] == "quoted"
    assert kp["evidence"][0]["speaker_name"] in {"SPEAKER_00", "张三"}
    assert cur["content"]["action_items"][0]["evidence_status"] == "quoted"
    summary = cur["content"]["evidence_summary"]
    assert summary["quoted"] == 2 and summary["unsupported"] == 0
    assert summary["span_ms"] == [0, 80000]

    # 版本快照不被改写：content_json 里仍是原始条目
    with Session() as session:
        raw = session.get(ReportVersion, vid)
        stored = json.loads(raw.content_json)
        assert "evidence" not in stored["key_points"][0]
        assert "evidence_summary" not in stored


def _add_default_llm(runtime) -> None:
    """问答/核实走 _llm_for_role，需要一行默认可用的 LLM 注册。"""
    from huiyiku.db.models import ModelRegistry
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.timeutil import now_iso

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        ts = now_iso()
        session.add(
            ModelRegistry(
                task="llm",
                provider="openai_compatible",
                model_name="deepseek-chat",
                model_version="",
                execution="remote",
                endpoint="https://example.invalid/v1",
                terms_accepted=1,
                review_status="approved",
                status="available",
                is_default=1,
                created_at=ts,
                updated_at=ts,
            )
        )
        session.commit()


def _verify_fixture(runtime) -> tuple[int, object]:
    """建一场带转写块的会议，返回 (meeting_id, runtime)。"""
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.store import (
        create_meeting,
        persist_transcript,
        rebuild_stats_and_chunks,
    )

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="核实会议")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": 0,
                    "end_ms": 60000,
                    "text": "项目组决定在下个季度完成预算审批并启动供应商招标流程",
                    "speaker_label": "SPEAKER_00",
                }
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        rebuild_stats_and_chunks(session, m, session.connection().connection)
        session.commit()
        return m.id


def test_verify_item_uses_llm_verdict(runtime, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from huiyiku.api.app import create_app

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    mid = _verify_fixture(runtime)
    _add_default_llm(runtime)
    monkeypatch.setattr(
        "huiyiku.llm.client.ChatClient.complete",
        lambda _self, _messages, **_kw: {
            "content": '{"verdict":"paraphrase","reason":"意思一致但措辞不同"}',
        },
    )
    client = TestClient(create_app(runtime))
    r = client.post(
        "/api/report-items/verify",
        json={"meeting_id": mid, "text": "决定下季度完成预算审批并启动招标"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "paraphrase"
    assert body["verdict_label"] == "释义有据"
    assert "措辞" in body["reason"]
    assert body["citations"] and body["citations"][0]["start_ms"] == 0


def test_verify_item_no_material_skips_llm(runtime, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from huiyiku.api.app import create_app

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"

    def _boom(*_a, **_k):
        raise AssertionError("无材料时不应调用 LLM")

    monkeypatch.setattr("huiyiku.llm.client.ChatClient.complete", _boom)
    client = TestClient(create_app(runtime))
    r = client.post(
        "/api/report-items/verify",
        json={"meeting_id": 99999, "text": "任意内容"},
    )
    assert r.status_code == 404  # 会议不存在


def test_verify_item_unrelated_text_unsupported_without_fabrication(
    runtime, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    from huiyiku.api.app import create_app

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    mid = _verify_fixture(runtime)
    _add_default_llm(runtime)
    monkeypatch.setattr(
        "huiyiku.llm.client.ChatClient.complete",
        lambda _self, _messages, **_kw: {"content": "不是 JSON"},
    )
    client = TestClient(create_app(runtime))
    r = client.post(
        "/api/report-items/verify",
        json={"meeting_id": mid, "text": "团队计划去滨海城市组织团建活动"},
    )
    assert r.status_code == 200
    assert r.json()["verdict"] == "unsupported"


def test_accept_writes_chunk_provenance(runtime) -> None:
    """接受报告时，报告类型 chunk 带真实时间窗与唯一说话人。"""
    import json

    from sqlalchemy import select

    from huiyiku.db.models import MeetingChunk, Report, ReportVersion
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.store import (
        accept_report_actions,
        create_meeting,
        persist_transcript,
    )
    from huiyiku.timeutil import now_iso

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="接受溯源")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": 120000,
                    "end_ms": 160000,
                    "text": "项目组决定在下个季度完成预算审批并启动供应商招标流程",
                    "speaker_label": "SPEAKER_00",
                },
                {
                    "start_ms": 400000,
                    "end_ms": 440000,
                    "text": "一致同意把交付日期定在月底",
                    "speaker_label": "SPEAKER_01",
                },
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        ts = now_iso()
        report = Report(meeting_id=m.id, created_at=ts, updated_at=ts)
        session.add(report)
        session.flush()
        data = {
            "summary": "概要",
            "key_points": [],
            "decisions": [
                {"content": "完成预算审批并启动供应商招标流程", "segment_ids": []},
                {"content": "一致同意把交付日期定在月底", "segment_ids": []},
            ],
            "action_items": [],
            "risks": [],
            "open_questions": [],
            "participants": [],
        }
        ver = ReportVersion(
            report_id=report.id,
            version=1,
            source="llm",
            content_json=json.dumps(data, ensure_ascii=False),
            status="accepted",
            created_at=ts,
        )
        session.add(ver)
        session.flush()
        report.current_version_id = ver.id
        accept_report_actions(session, m, ver)
        session.commit()
        rows = list(
            session.scalars(
                select(MeetingChunk).where(
                    MeetingChunk.meeting_id == m.id,
                    MeetingChunk.chunk_type == "decision",
                )
            )
        )
        assert len(rows) == 2
        by_start = {r.start_ms: r for r in rows}
        assert 120000 in by_start and 400000 in by_start
        assert all(r.end_ms > 0 for r in rows)
        assert all(r.speaker_id is not None for r in rows)


def _qa_stream_fixture(runtime) -> int:
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.store import (
        create_meeting,
        persist_transcript,
        rebuild_stats_and_chunks,
    )

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="补充会议")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": 0,
                    "end_ms": 60000,
                    "text": "项目组决定在下个季度完成预算审批并启动供应商招标流程",
                    "speaker_label": "SPEAKER_00",
                }
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        # 确认说话人后 chunk 才会带 speaker_id（build_transcript_chunks 的既有行为）
        from sqlalchemy import select

        from huiyiku.db.models import MeetingSpeaker

        for sp in session.scalars(
            select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == m.id)
        ):
            sp.status = "confirmed"
            sp.display_name = "张三"
        rebuild_stats_and_chunks(session, m, session.connection().connection)
        session.commit()
        return m.id


def _fake_qa_stream(monkeypatch) -> None:
    """两次调用返回不同内容：补充调用的 system 含"补充"。"""

    async def fake_stream(self, messages, **_kw):
        if any("补充" in (m.get("content") or "") for m in messages):
            for t in ("背景甲", "背景乙"):
                yield t
        else:
            for t in ("回答甲", "回答乙"):
                yield t

    monkeypatch.setattr("huiyiku.llm.client.ChatClient.stream", fake_stream)


def test_allow_extra_two_phase_events(runtime, monkeypatch) -> None:
    import asyncio

    from huiyiku.llm.qa import EXTRA_MARKER, stream_answer

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    mid = _qa_stream_fixture(runtime)
    _add_default_llm(runtime)
    _fake_qa_stream(monkeypatch)

    from huiyiku.db.session import make_engine, session_factory

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)

    async def collect(allow_extra: bool):
        events = []
        with Session() as session:
            async for ev in stream_answer(
                session,
                runtime,
                session_id="t",
                question="预算审批怎么安排",
                scope={"meeting_ids": [mid]},
                allow_extra=allow_extra,
            ):
                events.append(ev)
        return events

    events = asyncio.run(collect(True))
    kinds = [(e.get("kind", "ground"), e["text"]) for e in events if e["event"] == "token"]
    assert ("ground", "回答甲") in kinds
    assert ("extra", "背景甲") in kinds and ("extra", "背景乙") in kinds
    done = [e for e in events if e["event"] == "done"][0]["message"]
    assert "回答甲回答乙" in done["answer"]
    assert EXTRA_MARKER in done["answer"] and "背景甲背景乙" in done["answer"]
    cite = done["citations"][0]
    assert cite["chunk_type"] == "transcript"
    assert cite["speaker_name"]

    # 默认关闭：事件序列与现状一致，无 extra
    events_off = asyncio.run(collect(False))
    assert all(e.get("kind") is None for e in events_off if e["event"] == "token")
    done_off = [e for e in events_off if e["event"] == "done"][0]["message"]
    assert EXTRA_MARKER not in done_off["answer"]


def test_allow_extra_covers_no_material_case(runtime, monkeypatch) -> None:
    import asyncio

    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.llm.qa import EXTRA_MARKER, stream_answer
    from huiyiku.store import create_meeting

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        empty = create_meeting(session, title="空会议")
        session.commit()
        empty_id = empty.id
    _add_default_llm(runtime)
    _fake_qa_stream(monkeypatch)

    async def collect():
        events = []
        with Session() as session:
            async for ev in stream_answer(
                session,
                runtime,
                session_id="t",
                question="随便问点录音里没有的",
                scope={"meeting_ids": [empty_id]},
                allow_extra=True,
            ):
                events.append(ev)
        return events

    events = asyncio.run(collect())
    done = [e for e in events if e["event"] == "done"][0]["message"]
    assert "没有找到相关材料" in done["answer"]
    assert EXTRA_MARKER in done["answer"] and "背景甲" in done["answer"]


def test_junk_hint_ids_are_tolerated() -> None:
    """模型自报的 segment_ids 可能是区间串等垃圾，不能让溯源整体 500。"""
    idx = EvidenceIndex([_seg(1, "项目组决定在下个季度完成预算审批并启动供应商招标流程")])
    r = idx.resolve(
        "完成预算审批并启动供应商招标流程",
        hint_segment_ids=["1303-1307", None, "abc", 1],  # type: ignore[list-item]
    )
    assert r["status"] == "quoted"
    assert r["evidence"][0]["segment_id"] == 1


def test_extra_stream_failure_keeps_ground_answer(runtime, monkeypatch) -> None:
    """补充流中途失败：主回答照常入库，不抛异常（审查发现的降级缺口）。"""
    import asyncio

    from huiyiku.db.models import QaMessage
    from huiyiku.llm.qa import stream_answer

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    mid = _qa_stream_fixture(runtime)
    _add_default_llm(runtime)

    async def fake_stream(self, messages, **_kw):
        if any("补充" in (m.get("content") or "") for m in messages):
            yield "补"
            raise RuntimeError("网关断了")
        for t in ("回答甲", "回答乙"):
            yield t

    monkeypatch.setattr("huiyiku.llm.client.ChatClient.stream", fake_stream)

    from huiyiku.db.session import make_engine, session_factory

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)

    async def run():
        events = []
        with Session() as session:
            async for ev in stream_answer(
                session,
                runtime,
                session_id="t",
                question="预算审批怎么安排",
                scope={"meeting_ids": [mid]},
                allow_extra=True,
            ):
                events.append(ev)
        return events

    events = asyncio.run(run())
    kinds = [(e.get("kind", "ground"), e["text"]) for e in events if e["event"] == "token"]
    assert ("ground", "回答甲") in kinds
    assert ("extra", "（补充生成失败，可重试）") in kinds
    done = [e for e in events if e["event"] == "done"][0]["message"]
    assert "回答甲回答乙" in done["answer"]
    # 失败提示与半截补充都不入库：只留主回答
    assert "网关" not in done["answer"]
    assert "（补充生成失败，可重试）" not in done["answer"]
    assert "【AI 补充（非录音内容）】" not in done["answer"]
    assert "补" not in done["answer"].replace("回答甲回答乙", "")
    with Session() as session:
        assert session.query(QaMessage).count() == 1


def test_window_match_catches_cross_segment_condensation() -> None:
    """单段覆盖不了、时间窗联合才达标时，应判释义并给出窗口内的证据段。"""
    segs = [
        _seg(1, "先说说实时数据这块，Flink 在写", start=100_000),
        _seg(2, "画像存储第一层是 Redis", start=130_000),
        _seg(3, "第二层用 ES 兜底", start=160_000),
        _seg(4, "另外交付日期定在月底", start=900_000),
    ]
    idx = EvidenceIndex(segs)
    item = "实时数据用 Flink 写，画像存储第一层 Redis，第二层 ES 兜底"
    r = idx.resolve(item)
    assert r["status"] == "paraphrase"
    starts = [e["start_ms"] for e in r["evidence"]]
    assert starts and all(100_000 <= s <= 200_000 for s in starts)
    assert len(r["evidence"]) <= 3
    # 窗口之外的无关段不应混进来
    assert 900_000 not in starts


def test_window_match_still_rejects_unrelated() -> None:
    segs = [
        _seg(1, "实时数据用 Flink 写", start=0),
        _seg(2, "画像存储第一层 Redis", start=30_000),
    ]
    idx = EvidenceIndex(segs)
    assert idx.resolve("团队决定去火星建立殖民地并招募志愿者")["status"] == "unsupported"


def test_verify_materials_no_duplicate_segments(runtime, monkeypatch) -> None:
    """本地证据转材料后 segment_id 不能丢。

    此前 _material_from_segment 只读 "id"，而 resolve() 的 evidence 字典用
    "segment_id"——转出来的材料 segment_id 全是 None，邻域补齐的去重集合
    为空，同一段被再次加入，引用出现重复时间点（实测 144s 出现两次）。
    """
    from fastapi.testclient import TestClient

    from huiyiku.api.app import create_app

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    mid = _verify_fixture(runtime)
    _add_default_llm(runtime)
    monkeypatch.setattr(
        "huiyiku.llm.client.ChatClient.complete",
        lambda _self, _messages, **_kw: {"content": '{"verdict":"paraphrase","reason":"x"}'},
    )
    client = TestClient(create_app(runtime))
    r = client.post(
        "/api/report-items/verify",
        json={"meeting_id": mid, "text": "决定下季度完成预算审批并启动供应商招标"},
    )
    assert r.status_code == 200
    starts = [c["start_ms"] for c in r.json()["citations"]]
    assert starts, "至少要有本地证据一条"
    assert len(starts) == len(set(starts)), f"引用时间点重复: {starts}"


def test_resolve_without_fallback_unchanged() -> None:
    """回归锁定：不传 vector_fallback 时行为与旧版完全一致。"""
    data = {"decisions": [{"content": "团队计划去滨海城市组织团建活动", "segment_ids": []}]}
    idx = EvidenceIndex([_seg(1, "项目组决定在下个季度完成预算审批并启动供应商招标流程")])
    counts = resolve_report_evidence(data, idx)
    assert counts == {"quoted": 0, "paraphrase": 0, "unsupported": 1}
    assert data["decisions"][0]["evidence_status"] == "unsupported"
    assert data["decisions"][0]["evidence"] == []


def test_vector_fallback_upgrades_unsupported() -> None:
    seg = _seg(1, "项目组决定在下个季度完成预算审批并启动供应商招标流程", start=30000)
    data = {"decisions": [{"content": "团队计划去滨海城市组织团建活动", "segment_ids": []}]}
    idx = EvidenceIndex([seg])
    calls: list[list[str]] = []

    def fb(texts: list[str]) -> list[list[dict]]:
        calls.append(list(texts))
        return [[{**seg, "score": 0.9}] for _ in texts]

    counts = resolve_report_evidence(data, idx, vector_fallback=fb)
    # 仅 unsupported 条目触发一次批量调用
    assert calls == [["团队计划去滨海城市组织团建活动"]]
    assert data["decisions"][0]["evidence_status"] == "paraphrase"
    ev = data["decisions"][0]["evidence"][0]
    assert ev["vector_matched"] is True
    assert ev["start_ms"] == 30000
    assert counts == {"quoted": 0, "paraphrase": 1, "unsupported": 0}


def test_vector_low_score_keeps_unsupported() -> None:
    seg = _seg(1, "项目组决定在下个季度完成预算审批并启动供应商招标流程")
    data = {"decisions": [{"content": "团队计划去滨海城市组织团建活动", "segment_ids": []}]}
    idx = EvidenceIndex([seg])
    resolve_report_evidence(
        data,
        idx,
        vector_fallback=lambda texts: [[{**seg, "score": 0.3}] for _ in texts],
    )
    assert data["decisions"][0]["evidence_status"] == "unsupported"


def test_lexical_hits_do_not_call_vector() -> None:
    data = {"decisions": [{"content": "讨论了数据中台的建设方案", "segment_ids": []}]}
    idx = EvidenceIndex([_seg(1, "我们今天讨论了数据中台的建设方案", start=120000)])

    def fb(texts: list[str]) -> list[list[dict]]:
        raise AssertionError("词法命中条目不应触发向量调用")

    counts = resolve_report_evidence(data, idx, vector_fallback=fb)
    assert counts["quoted"] == 1


def test_vector_fallback_exception_degrades_gracefully() -> None:
    seg = _seg(1, "项目组决定在下个季度完成预算审批并启动供应商招标流程")
    data = {"decisions": [{"content": "团队计划去滨海城市组织团建活动", "segment_ids": []}]}
    idx = EvidenceIndex([seg])

    def fb(texts: list[str]) -> list[list[dict]]:
        raise RuntimeError("embedding endpoint down")

    counts = resolve_report_evidence(data, idx, vector_fallback=fb)
    assert counts["unsupported"] == 1
    assert data["decisions"][0]["evidence_status"] == "unsupported"
