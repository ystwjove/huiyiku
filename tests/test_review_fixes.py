# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from huiyiku.api.app import create_app
from huiyiku.asr.client import AsrError, transcribe_wav
from huiyiku.db.models import MeetingChunk
from huiyiku.db.session import make_engine, session_factory
from huiyiku.domain.chunks import build_transcript_chunks, split_text_sentences
from huiyiku.domain.citations import verify_citation
from huiyiku.domain.stats import compute_speaker_stats
from huiyiku.paths import safe_filename, safe_join
from huiyiku.store import create_meeting, persist_transcript, rebuild_stats_and_chunks
from huiyiku.timeutil import now_iso


def test_safe_join_blocks_traversal(tmp_path: Path) -> None:
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("ok", encoding="utf-8")
    with pytest.raises(ValueError):
        safe_join(root, "..", "secrets.json")
    assert safe_join(root, "index.html").name == "index.html"


def test_safe_filename_strips_path() -> None:
    assert safe_filename(r"..\..\evil.wav") == "evil.wav"
    with pytest.raises(ValueError):
        safe_filename("..")


def test_upload_strips_path_components(runtime) -> None:
    client = TestClient(create_app(runtime))
    meeting = client.post("/api/meetings", json={"title": "u"}).json()
    r = client.post(
        f"/api/meetings/{meeting['id']}/media",
        content=b"RIFF" + b"\x00" * 32,
        headers={"x-filename": r"..\..\x.wav"},
    )
    assert r.status_code == 202
    dests = list(runtime.layout["media_original"].glob("*"))
    assert dests
    for item in dests:
        item.resolve().relative_to(runtime.layout["media_original"].resolve())
        assert ".." not in item.name


def test_pending_not_in_stats_denominator() -> None:
    speakers = [
        {"id": 1, "status": "confirmed"},
        {"id": 2, "status": "pending"},
    ]
    segments = [
        {"speaker_id": 1, "start_ms": 0, "end_ms": 1000},
        {"speaker_id": 2, "start_ms": 0, "end_ms": 9000},
    ]
    rows = {r["speaker_id"]: r for r in compute_speaker_stats(segments, speakers)}
    assert 2 not in rows
    assert rows[1]["speech_ratio"] == 1.0


def test_sentence_split_used() -> None:
    parts = split_text_sentences("甲。" + "乙" * 400 + "。丙。", 50)
    assert len(parts) >= 2
    assert all(len(p) <= 50 or "。" not in p[50:] for p in parts)


def test_build_chunks_non_overlapping() -> None:
    speakers = {1: {"id": 1, "status": "confirmed"}}
    segs = [
        {"speaker_id": 1, "start_ms": 0, "end_ms": 40000, "text": "第一段。"},
        {"speaker_id": 1, "start_ms": 40000, "end_ms": 80000, "text": "第二段。"},
    ]
    chunks = build_transcript_chunks(segs, speakers)
    assert chunks
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a["end_ms"] <= b["start_ms"] or a["end_ms"] == b["start_ms"]


def test_asr_timeout_does_not_reset_on_silence() -> None:
    class _Pipe:
        def __iter__(self):
            return iter(())

        def readline(self):
            return ""

        def read(self, n=-1):
            time.sleep(0.05)
            return "[]"

    class _Proc:
        def __init__(self) -> None:
            self.stderr = _Pipe()
            self.stdout = _Pipe()
            self.returncode = None

        def poll(self):
            return None

        def kill(self):
            self.returncode = -9

        def terminate(self):
            self.kill()

        def wait(self, timeout=None):
            return self.returncode

    heartbeats: list[dict] = []

    def popen(*_a, **_k):
        return _Proc()

    with pytest.raises(AsrError, match="timed out"):
        transcribe_wav(
            ["false"],
            Path("x.wav"),
            heartbeat=heartbeats.append,
            timeout_seconds=0.4,
            popen=popen,
        )
    assert heartbeats == []


def test_patch_segment_rebuilds_active_chunks(runtime) -> None:
    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="c")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": 0,
                    "end_ms": 40000,
                    "text": "hello world meeting notes",
                    "speaker_label": "SPEAKER_00",
                }
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        from huiyiku.db.models import MeetingSpeaker

        speaker = session.scalars(
            select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == m.id)
        ).first()
        speaker.status = "confirmed"
        conn = session.connection().connection
        rebuild_stats_and_chunks(session, m, conn)
        session.commit()
        mid = m.id
    client = TestClient(create_app(runtime))
    segs = client.get(f"/api/meetings/{mid}/transcript").json()
    client.patch(f"/api/transcript-segments/{segs[0]['id']}", json={"text": "修正后的内容。"})
    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        active = list(
            session.scalars(
                select(MeetingChunk).where(
                    MeetingChunk.meeting_id == mid,
                    MeetingChunk.chunk_type == "transcript",
                    MeetingChunk.is_active == 1,
                )
            )
        )
        assert any("修正后的内容" in (c.content or "") for c in active)


def test_qa_citation_not_auto_true() -> None:
    assert not verify_citation("这段原文", ["模型胡诌的答案"])
    assert verify_citation("这段原文", ["回答里引用了这段原文作为依据"])


def test_same_speaker_overlap_not_double_counted() -> None:
    speakers = [{"id": 1, "status": "confirmed"}]
    segments = [
        {"speaker_id": 1, "start_ms": 0, "end_ms": 1000},
        {"speaker_id": 1, "start_ms": 500, "end_ms": 1500},
    ]
    rows = compute_speaker_stats(segments, speakers)
    assert rows[0]["speech_ms"] == 1500


def test_settings_rejects_missing_llm(runtime) -> None:
    client = TestClient(create_app(runtime))
    r = client.patch("/api/settings", json={"report_llm_model_id": 9999})
    assert r.status_code == 400


def test_forbidden_data_dir_reasons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from huiyiku import paths

    monkeypatch.setattr(paths, "executable_dir", lambda: tmp_path)
    assert paths.forbidden_data_dir_reason(tmp_path / "_internal" / "data")
    assert paths.forbidden_data_dir_reason(tmp_path / "meeting-data") is None
    fake_pf = tmp_path / "Program Files" / "Huiyiku"
    fake_pf.mkdir(parents=True)
    assert paths.forbidden_data_dir_reason(fake_pf)


def test_spa_blocks_path_traversal(
    tmp_path: Path, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("idx", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    monkeypatch.setattr("huiyiku.api.app.executable_dir", lambda: tmp_path)
    client = TestClient(create_app(runtime))
    for url in ("/../secret.txt", "/..%2fsecret.txt", "/%2e%2e/secret.txt"):
        r = client.get(url)
        assert "TOPSECRET" not in (r.text or "")
        assert r.status_code in {200, 400, 404}


def test_report_markdown_flags_unreviewed_speakers() -> None:
    from huiyiku.domain.report_schema import report_to_markdown, validate_report

    data = validate_report(
        {
            "summary": "s",
            "key_points": [{"content": "p"}],
            "decisions": [],
            "action_items": [{"title": "a"}],
            "risks": [],
            "open_questions": [],
            "participants": [],
            "auto_speakers_unreviewed": True,
        }
    )
    md = report_to_markdown(data)
    assert "尚未人工校对" in md
    assert data["action_items"][0]["owner_person_id"] is None
    assert data["key_points"][0]["segment_ids"] == []


def test_accept_old_version_rejected(runtime) -> None:
    from huiyiku.db.models import Report, ReportVersion
    from huiyiku.timeutil import now_iso

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="r")
        ts = now_iso()
        report = Report(meeting_id=m.id, created_at=ts, updated_at=ts)
        session.add(report)
        session.flush()
        payload = {
            "summary": "s",
            "key_points": [],
            "decisions": [],
            "action_items": [],
            "risks": [],
            "open_questions": [],
            "participants": [],
        }
        v1 = ReportVersion(
            report_id=report.id,
            version=1,
            source="llm",
            content_json=json.dumps(payload),
            status="draft",
            created_at=ts,
        )
        session.add(v1)
        session.flush()
        v2 = ReportVersion(
            report_id=report.id,
            version=2,
            source="llm",
            content_json=json.dumps(payload),
            status="draft",
            created_at=ts,
        )
        session.add(v2)
        session.flush()
        report.current_version_id = v2.id
        session.commit()
        vid = v1.id
    client = TestClient(create_app(runtime))
    r = client.post(f"/api/report-versions/{vid}/accept")
    assert r.status_code == 400


def test_remote_model_starts_untested(runtime) -> None:
    client = TestClient(create_app(runtime))
    created = client.post(
        "/api/models",
        json={
            "task": "llm",
            "provider": "openai_compatible",
            "model_name": "deepseek-chat",
            "execution": "remote",
            "endpoint": "https://example.invalid/v1",
            "terms_accepted": True,
        },
    ).json()
    assert created["status"] == "untested"
    assert created["is_default"] is False
    assert (
        client.patch(f"/api/models/{created['id']}", json={"status": "available"}).status_code
        == 400
    )
    assert (
        client.patch(f"/api/models/{created['id']}", json={"is_default": True}).status_code == 400
    )


def test_llm_test_sets_available_and_failed(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    from huiyiku.llm.client import ChatError

    runtime.secrets.llm_api_key = "sk-test"
    client = TestClient(create_app(runtime))
    created = client.post(
        "/api/models",
        json={
            "task": "llm",
            "provider": "openai_compatible",
            "model_name": "deepseek-chat",
            "execution": "remote",
            "endpoint": "https://example.invalid/v1",
            "terms_accepted": True,
        },
    ).json()

    def ok(_self, _messages, **_kwargs):
        return {"content": "pong", "usage": {}}

    monkeypatch.setattr("huiyiku.llm.client.ChatClient.complete", ok)
    r = client.post(f"/api/models/{created['id']}/test")
    assert r.status_code == 200
    row = next(m for m in client.get("/api/models").json() if m["id"] == created["id"])
    assert row["status"] == "available"

    def boom(_self, _messages, **_kwargs):
        raise ChatError("down")

    monkeypatch.setattr("huiyiku.llm.client.ChatClient.complete", boom)
    r = client.post(f"/api/models/{created['id']}/test")
    assert r.status_code == 400
    row = next(m for m in client.get("/api/models").json() if m["id"] == created["id"])
    assert row["status"] == "failed"


def test_embed_text_wraps_http_errors(runtime) -> None:
    from types import SimpleNamespace

    import httpx

    from huiyiku.llm.client import ChatError
    from huiyiku.llm.embed import embed_text

    runtime.settings.allow_cloud = True
    runtime.secrets.embedding_api_key = "sk-test"
    model = SimpleNamespace(
        endpoint="https://example.invalid/v1", model_name="emb", embedding_dim=None
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(ChatError):
        embed_text(runtime, model, "ping", transport=httpx.MockTransport(handler))


def test_mentions_chunk_id_not_prefix() -> None:
    from huiyiku.llm.qa import _mentions_chunk

    assert _mentions_chunk("see [chunk 3] please", 3)
    assert _mentions_chunk("chunk 3 is cited", 3)
    assert not _mentions_chunk("chunk 35 is cited", 3)
    assert not _mentions_chunk("chunk 13", 3)


def test_asr_empty_stdout_is_asr_error() -> None:
    class _Pipe:
        def __iter__(self):
            return iter(())

        def read(self, n=-1):
            return ""

    class _Proc:
        def __init__(self) -> None:
            self.stderr = _Pipe()
            self.stdout = _Pipe()
            self.returncode = 0

        def poll(self):
            return 0

        def kill(self) -> None:
            pass

        def terminate(self) -> None:
            pass

        def wait(self, timeout=None):
            return self.returncode

    with pytest.raises(AsrError, match="not JSON"):
        transcribe_wav(["false"], Path("x.wav"), popen=lambda *_a, **_k: _Proc())


def test_retrieve_falls_back_to_fts_on_embed_error(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from huiyiku.db.models import MeetingSpeaker, ModelRegistry
    from huiyiku.llm.client import ChatError
    from huiyiku.llm.qa import retrieve
    from huiyiku.timeutil import now_iso

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="q")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": 0,
                    "end_ms": 40000,
                    "text": "hello world meeting notes",
                    "speaker_label": "SPEAKER_00",
                }
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        speaker = session.scalars(
            select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == m.id)
        ).first()
        speaker.status = "confirmed"
        rebuild_stats_and_chunks(session, m, session.connection().connection)
        ts = now_iso()
        session.add(
            ModelRegistry(
                task="embedding",
                provider="openai_compatible",
                model_name="emb",
                model_version="",
                execution="remote",
                endpoint="https://example.invalid/v1",
                embedding_dim=4,
                terms_accepted=1,
                review_status="approved",
                status="available",
                is_default=1,
                created_at=ts,
                updated_at=ts,
            )
        )
        session.commit()
        runtime.settings.allow_cloud = True
        runtime.secrets.embedding_api_key = "sk-test"

        def boom(*_a, **_k):
            raise ChatError("down")

        monkeypatch.setattr("huiyiku.llm.qa.embed_text", boom)
        chunks = asyncio.run(retrieve(session, runtime, {"meeting_ids": [m.id]}, "hello"))
        assert any("hello" in (c.content or "") for c in chunks)


def test_assign_rebuilds_chunk_speaker(runtime) -> None:
    from huiyiku.db.models import MeetingSpeaker

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="asg")
        persist_transcript(
            session,
            m,
            [
                {"start_ms": 0, "end_ms": 40000, "text": "甲。", "speaker_label": "SPEAKER_00"},
                {"start_ms": 40000, "end_ms": 80000, "text": "乙。", "speaker_label": "SPEAKER_01"},
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        for sp in session.scalars(select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == m.id)):
            sp.status = "confirmed"
        rebuild_stats_and_chunks(session, m, session.connection().connection)
        session.commit()
        mid = m.id
    client = TestClient(create_app(runtime))
    speakers = client.get(f"/api/meetings/{mid}/speakers").json()
    segs = client.get(f"/api/meetings/{mid}/transcript").json()
    dst = next(s for s in speakers if s["speaker_label"] == "SPEAKER_01")
    src_seg = next(s for s in segs if s["speaker_id"] != dst["id"])
    client.post(f"/api/meeting-speakers/{dst['id']}/assign", json={"segment_ids": [src_seg["id"]]})
    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        active = list(
            session.scalars(
                select(MeetingChunk).where(
                    MeetingChunk.meeting_id == mid,
                    MeetingChunk.chunk_type == "transcript",
                    MeetingChunk.is_active == 1,
                )
            )
        )
        assert any(c.speaker_id == dst["id"] and "甲" in (c.content or "") for c in active)


def test_asr_stderr_carriage_return_progress_is_drained() -> None:
    """回归：modelscope/tqdm 进度条用 \r 无换行，逐行迭代会挂等 \n，
    管道缓冲写满后 ASR 写 stderr 死锁。块读取必须把无换行数据消费掉。"""
    import threading

    progress = "downloading 50%|\u2588\u2588\u2588| 12/12\r" * 3000  # ~78KB 无换行
    # 真实 tqdm 形状：进度条残迹 + 粘在后面的心跳 JSON（同一逻辑行）
    stderr_data = (
        progress + ' 75%|\u2588\u2588\u2588\u2588|{"event": "heartbeat", "phase": "load_models"}\n'
    )

    class _CrPipe:
        def __init__(self, data: str) -> None:
            self._data = data
            self._pos = 0
            self._lock = threading.Lock()

        def __iter__(self):
            return iter(())

        def read(self, n=-1):
            with self._lock:
                if self._pos >= len(self._data):
                    return ""
                size = n if isinstance(n, int) and n > 0 else len(self._data)
                chunk = self._data[self._pos : self._pos + size]
                self._pos += len(chunk)
                return chunk

    class _Proc:
        def __init__(self) -> None:
            self.stderr = _CrPipe(stderr_data)
            # funasr 版本行等 stdout 噪音 + 结果单行 JSON
            self.stdout = _CrPipe("funasr version: 1.4.2.\n[]")
            self.returncode = 0

        def poll(self):
            return 0

        def kill(self):
            self.returncode = -9

        def terminate(self):
            self.kill()

        def wait(self, timeout=None):
            return self.returncode

    heartbeats: list[dict] = []

    def popen(*_a, **_k):
        return _Proc()

    result = transcribe_wav(
        ["fake-asr"],
        Path("x.wav"),
        heartbeat=heartbeats.append,
        timeout_seconds=30,
        popen=popen,
    )
    assert result == []
    # 无换行的 ~78KB 进度被完整消费，心跳 JSON 在其后仍被解析到
    assert {"event": "heartbeat", "phase": "load_models"} in heartbeats


def test_summarize_stderr_filters_noise_and_keeps_real_errors() -> None:
    from huiyiku.asr.client import _summarize_stderr

    hb = '{"event": "heartbeat", "phase": "load_models"}' + "\n"
    info = "2026-08-27 [INFO] Loading ckpt" + "\n"
    dl = "| INFO    | modelscope_hub.download | Downloading 12 files" + "\n"
    tqdm = "Downloading: 100%|" + "\u2588" * 10 + "| 12/12" + "\n"
    jieba = "Building prefix dict from the default dictionary ..." + "\n"
    dump = "Dumping model to binary cache ..." + "\n"
    noise = [hb, info, dl, tqdm, jieba, dump]
    assert _summarize_stderr(noise) == ""
    real = noise + ["sqlite3.OperationalError: database is locked" + "\n"]
    assert _summarize_stderr(real) == "sqlite3.OperationalError: database is locked"
    assert _summarize_stderr([]) == ""


def test_probe_failure_does_not_500_setup(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    """探测失败只能降级为提示：不能 500，也不能把已有配置改成坏的。"""
    from huiyiku import api

    def _boom(*_a, **_k):
        raise RuntimeError("网关无响应")

    monkeypatch.setattr(api.app, "_probe_llm_endpoint", _boom)
    client = TestClient(create_app(runtime))
    r = client.post(
        "/api/setup",
        json={
            "legal_confirmed": True,
            "egress_confirmed": True,
            "llm_endpoint": "https://example.invalid/v1",
            "llm_model": "some-model",
            "llm_api_key": "sk-test",
        },
    )
    assert r.status_code == 200
    assert "网关无响应" in (r.json().get("llm_probe_error") or "")
    # 测不通就不落库：注册表里不该出现这个坏端点
    assert all(m["model_name"] != "some-model" for m in client.get("/api/models").json())


def test_probe_success_sets_default_and_reports_no_error(runtime, monkeypatch) -> None:
    monkeypatch.setattr(
        "huiyiku.llm.client.ChatClient.complete",
        lambda _self, _messages, **_kw: {"content": "pong", "usage": {}},
    )
    client = TestClient(create_app(runtime))
    r = client.post(
        "/api/setup",
        json={
            "legal_confirmed": True,
            "egress_confirmed": True,
            "llm_endpoint": "https://example.invalid/v1",
            "llm_model": "some-model",
            "llm_api_key": "sk-test",
        },
    )
    assert r.status_code == 200
    assert r.json()["llm_probe_error"] is None
    rows = [m for m in client.get("/api/models").json() if m["task"] == "llm"]
    assert [m["model_name"] for m in rows if m["is_default"]] == ["some-model"]
    assert rows[0]["endpoint"] == "https://example.invalid/v1"


def test_transport_error_message_is_chinese() -> None:
    import httpx

    from huiyiku.llm.client import _friendly_transport_error

    req = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    msg = _friendly_transport_error(
        httpx.ConnectError("SSL EOF", request=req), "https://example.invalid/v1"
    )
    assert "无法连接" in msg


def test_parse_response_empty_body_gives_friendly_error() -> None:
    import httpx

    from huiyiku.llm.client import ChatError, _parse_response

    resp = httpx.Response(200, content=b"", headers={"content-type": "application/json"})
    with pytest.raises(ChatError) as ei:
        _parse_response(resp)
    assert "无法解析" in str(ei.value)


def test_retrieve_samples_scope_when_keywords_miss(runtime) -> None:
    """没配 embedding 时，"这场会讨论了什么" 不该空手回话。"""
    import asyncio

    from huiyiku.llm.qa import retrieve

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="generic")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": i * 40000,
                    "end_ms": (i + 1) * 40000,
                    "text": f"第{i}段内容 预算 排期 交付",
                    "speaker_label": "SPEAKER_00",
                }
                for i in range(20)
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        rebuild_stats_and_chunks(session, m, session.connection().connection)
        mid = m.id
        got = asyncio.run(retrieve(session, runtime, {"meeting_ids": [mid]}, "这次沟通主要讨论了什么"))
        assert got, "限定会议范围时应均匀取样兜底"
        assert all(c.meeting_id == mid for c in got)

        # 无范围限定不兜底，避免把整个库塞给模型
        empty = asyncio.run(retrieve(session, runtime, {}, "这次沟通主要讨论了什么"))
        assert empty == []


def test_retrieve_builds_missing_chunks(runtime) -> None:
    """转写后未确认说话人（因而没建过检索块）的会议，也必须问得到。"""
    import asyncio

    from huiyiku.llm.qa import retrieve

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="no-speaker-confirm")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": 0,
                    "end_ms": 40000,
                    "text": "会议讨论了年度预算与交付排期",
                    "speaker_label": "SPEAKER_00",
                }
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        mid = m.id
        # 故意不调用 rebuild_stats_and_chunks，模拟"只转写、没确认说话人"
        got = asyncio.run(retrieve(session, runtime, {"meeting_ids": [mid]}, "预算"))
        assert got, "检索前应自动补建缺失的转写块"
        assert all(c.meeting_id == mid for c in got)


def test_evidence_cache_is_thread_safe() -> None:
    """缓存驱逐用 min() 迭代字典，并发读写不能抛 RuntimeError。"""
    import threading

    from huiyiku import api

    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(200):
                api.app._evidence_cache_put(n * 1000 + i, {"summary": "x"})
                api.app._evidence_cache_get(n * 1000 + i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_content_disposition_ascii_fallback_readable() -> None:
    from huiyiku.export.common import ascii_fallback_name

    assert ascii_fallback_name("海信数据沟通-20260924.md") == "meeting-20260924.md"


def test_retrieve_empty_question_does_not_crash(runtime) -> None:
    """空问题不能拿空串去 MATCH（FTS5 会报语法错误）。"""
    import asyncio

    from huiyiku.llm.qa import retrieve
    from huiyiku.store import create_meeting, persist_transcript, rebuild_stats_and_chunks

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="空问")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": 0,
                    "end_ms": 30000,
                    "text": "讨论预算与排期",
                    "speaker_label": "SPEAKER_00",
                }
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        rebuild_stats_and_chunks(session, m, session.connection().connection)
        session.commit()
        got = asyncio.run(retrieve(session, runtime, {"meeting_ids": [m.id]}, ""))
        assert got  # 走兜底取样，不抛错


def test_qa_rejects_empty_question(runtime) -> None:
    from fastapi.testclient import TestClient

    from huiyiku.api.app import create_app

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    client = TestClient(create_app(runtime))
    r = client.post("/api/qa", json={"session_id": "x", "question": "   ", "scope": {}})
    assert r.status_code == 400
    assert "问题" in r.json()["detail"]


def test_retrieve_person_and_date_filters_survive_limit(runtime) -> None:
    """作用域全部下推到 SQL：人物/日期筛选不会被 LIMIT 挤掉命中。"""
    import asyncio

    from sqlalchemy import select as _select

    from huiyiku.db.models import MeetingSpeaker, Person
    from huiyiku.llm.qa import retrieve
    from huiyiku.store import create_meeting, persist_transcript, rebuild_stats_and_chunks
    from huiyiku.timeutil import now_iso

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="筛选会议")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": i * 30000,
                    "end_ms": (i + 1) * 30000,
                    "text": f"第{i}段：预算 排期 交付 里程碑",
                    "speaker_label": "SPEAKER_00",
                }
                for i in range(30)
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        ts = now_iso()
        person = Person(name="张三", created_at=ts, updated_at=ts)
        session.add(person)
        session.flush()
        for sp in session.scalars(
            _select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == m.id)
        ):
            sp.status = "confirmed"
            sp.display_name = "张三"
            sp.person_id = person.id
        rebuild_stats_and_chunks(session, m, session.connection().connection)
        session.commit()
        mid, pid = m.id, person.id

        hit = asyncio.run(
            retrieve(session, runtime, {"meeting_ids": [mid], "person_id": pid}, "预算 排期")
        )
        assert hit, "人物筛选下不应一条都检索不到"
        day = (session.get(type(m), mid).occurred_at or "")[:10]
        assert asyncio.run(
            retrieve(session, runtime, {"meeting_ids": [mid], "start": day}, "预算")
        )
        assert not asyncio.run(
            retrieve(session, runtime, {"meeting_ids": [mid], "start": "2999-01-01"}, "预算")
        )


def test_legacy_report_chunk_repair_runs_once(runtime) -> None:
    """开机一次性修复：老构建接受的报告块时间全为 0 → 重建为真实出处。

    新构建的块带真实出处，但 unsupported 条目的块也是零时间，数据本身
    无法区分新老，所以靠标记文件只跑一次。
    """
    from huiyiku.db.models import MeetingChunk, Report, ReportVersion
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.store import accept_report_actions, create_meeting, persist_transcript
    from huiyiku.worker import loop as worker_loop

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="修复会")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": 120000,
                    "end_ms": 160000,
                    "text": "项目组决定在下个季度完成预算审批并启动供应商招标流程",
                    "speaker_label": "SPEAKER_00",
                }
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
                {"content": "完成预算审批并启动供应商招标流程", "segment_ids": []}
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
        mid = m.id
        # 模拟老构建：块时间抹成 0
        for c in session.scalars(
            select(MeetingChunk).where(
                MeetingChunk.meeting_id == mid,
                MeetingChunk.chunk_type != "transcript",
            )
        ):
            c.start_ms = 0
            c.end_ms = 0
        session.commit()

    marker = runtime.layout["root"] / ".report_chunks_fix_v2"
    assert not marker.exists()
    worker_loop._repair_legacy_report_chunks(Session, runtime)
    assert marker.exists()
    with Session() as session:
        rows = list(
            session.scalars(
                select(MeetingChunk).where(
                    MeetingChunk.meeting_id == mid,
                    MeetingChunk.chunk_type == "decision",
                    MeetingChunk.is_active == 1,
                )
            )
        )
        assert rows and all(r.start_ms > 0 for r in rows)
        count = len(rows)
    # 标记已写：再次调用跳过（幂等）
    worker_loop._repair_legacy_report_chunks(Session, runtime)
    with Session() as session:
        count2 = len(
            list(
                session.scalars(
                    select(MeetingChunk).where(
                        MeetingChunk.meeting_id == mid,
                        MeetingChunk.chunk_type == "decision",
                        MeetingChunk.is_active == 1,
                    )
                )
            )
        )
    assert count2 == count


def _qa_history_fixture(runtime) -> int:
    """建带转写块的会议，返回 meeting id（供多轮追问测试）。"""
    from huiyiku.store import create_meeting, persist_transcript, rebuild_stats_and_chunks

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="追问会议")
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


def test_qa_history_injected_into_llm(runtime, monkeypatch) -> None:
    """多轮追问：历史按 user/assistant 注入，AI 补充段不进历史。"""
    import asyncio

    from huiyiku.db.models import QaMessage
    from huiyiku.llm.qa import EXTRA_MARKER, stream_answer
    from huiyiku.timeutil import now_iso

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    mid = _qa_history_fixture(runtime)
    _add_default_llm(runtime)

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        session.add(
            QaMessage(
                session_id="thread-a",
                scope_json="{}",
                question="预算审批定在什么时候？",
                answer="定在下个季度。\n\n" + EXTRA_MARKER + "\n背景：预算流程常见于财年。",
                citations_json="[]",
                retrieval_json='{"allow_extra": true}',
                model_json="{}",
                created_at=now_iso(),
            )
        )
        session.commit()

    captured: list[list[dict[str, str]]] = []

    async def fake_stream(self, messages, **_kw):
        captured.append(list(messages))
        yield "答"

    monkeypatch.setattr("huiyiku.llm.client.ChatClient.stream", fake_stream)

    async def run():
        with Session() as session:
            async for _ in stream_answer(
                session,
                runtime,
                session_id="thread-a",
                question="它定在哪一季度？",
                scope={"meeting_ids": [mid]},
            ):
                pass

    asyncio.run(run())
    msgs = captured[-1]
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "user"], roles
    assert "背景：预算流程" not in msgs[3]["content"], "AI 补充段不得进历史"
    assert "定在下个季度" in msgs[3]["content"]


def test_qa_no_history_keeps_current_shape(runtime, monkeypatch) -> None:
    """无历史时消息序列与现状一致（回归锁定）。"""
    import asyncio

    from huiyiku.llm.qa import stream_answer

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    mid = _qa_history_fixture(runtime)
    _add_default_llm(runtime)

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)

    captured: list[list[dict[str, str]]] = []

    async def fake_stream(self, messages, **_kw):
        captured.append(list(messages))
        yield "答"

    monkeypatch.setattr("huiyiku.llm.client.ChatClient.stream", fake_stream)

    async def run():
        with Session() as session:
            async for _ in stream_answer(
                session,
                runtime,
                session_id="thread-fresh",
                question="预算审批怎么安排",
                scope={"meeting_ids": [mid]},
            ):
                pass

    asyncio.run(run())
    roles = [m["role"] for m in captured[-1]]
    assert roles == ["system", "user"], roles


def test_qa_thread_messages_endpoint(runtime) -> None:
    from fastapi.testclient import TestClient

    from huiyiku.api.app import create_app
    from huiyiku.db.models import QaMessage
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.timeutil import now_iso

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        for i, q in enumerate(["第一问", "第二问"]):
            session.add(
                QaMessage(
                    session_id="thread-x",
                    scope_json="{}",
                    question=q,
                    answer=f"第{i}答",
                    citations_json='[{"start_ms": 1000}]',
                    retrieval_json='{"allow_extra": true}',
                    model_json="{}",
                    created_at=now_iso(),
                )
            )
        session.commit()

    client = TestClient(create_app(runtime))
    rows = client.get("/api/qa/threads/thread-x/messages").json()
    assert [r["question"] for r in rows] == ["第一问", "第二问"]
    assert rows[0]["allow_extra"] is True
    assert rows[0]["citations"][0]["start_ms"] == 1000
    assert client.get("/api/qa/threads/thread-none/messages").json() == []


def _mk_report(runtime, payload: dict) -> int:
    from huiyiku.db.models import Report, ReportVersion
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.store import create_meeting

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="edit")
        ts = now_iso()
        report = Report(meeting_id=m.id, created_at=ts, updated_at=ts)
        session.add(report)
        session.flush()
        ver = ReportVersion(
            report_id=report.id,
            version=1,
            source="llm",
            content_json=json.dumps(payload, ensure_ascii=False),
            status="accepted",
            created_at=ts,
        )
        session.add(ver)
        session.flush()
        report.current_version_id = ver.id
        session.commit()
        return m.id


def _report_payload() -> dict:
    return {
        "summary": "摘要",
        "key_points": [{"content": "要点", "segment_ids": [], "time_ms": None}],
        "decisions": [{"content": "决议", "segment_ids": [], "time_ms": None}],
        "action_items": [
            {
                "title": "写周报",
                "owner_person_id": None,
                "status": "open",
                "priority": "high",
                "due_at": "2026-10-09",
                "segment_ids": [],
            }
        ],
        "risks": [],
        "open_questions": [],
        "participants": [{"name": "张三"}],
    }


def test_patch_report_strips_derived_and_bumps_version(runtime) -> None:
    from huiyiku.db.models import ReportVersion
    from huiyiku.db.session import make_engine, session_factory

    mid = _mk_report(runtime, _report_payload())
    client = TestClient(create_app(runtime))
    content = _report_payload()
    # 模拟前端把 GET /report 派生字段原样回传
    content["decisions"][0]["evidence"] = [{"start_ms": 1}]
    content["decisions"][0]["evidence_status"] = "quoted"
    content["decisions"][0]["vector_matched"] = True
    content["participants"][0]["segment_count"] = 42
    content["evidence_summary"] = {"quoted": 99}
    r = client.patch(f"/api/meetings/{mid}/report", json={"content_json": content})
    assert r.status_code == 200
    assert r.json()["version"] == 2
    assert r.json()["status"] == "draft"
    assert r.json()["source"] == "manual"

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        ver = session.get(ReportVersion, r.json()["id"])
        saved = json.loads(ver.content_json)
    assert "evidence_summary" not in saved
    assert "evidence" not in saved["decisions"][0]
    assert "evidence_status" not in saved["decisions"][0]
    assert "vector_matched" not in saved["decisions"][0]
    assert "segment_count" not in saved["participants"][0]
    # 正常字段往返保留
    assert saved["action_items"][0]["owner_person_id"] is None
    assert saved["action_items"][0]["priority"] == "high"
    assert saved["action_items"][0]["due_at"] == "2026-10-09"


def test_patch_report_recovers_missing_sections_and_get_recomputes(runtime) -> None:
    mid = _mk_report(runtime, _report_payload())
    client = TestClient(create_app(runtime))
    # 缺失的节用空值补齐，编辑只改摘要也能保存
    r = client.patch(
        f"/api/meetings/{mid}/report",
        json={"content_json": {"summary": "只改摘要"}},
    )
    assert r.status_code == 200

    # 带转写后 GET /report 应在读取时重算出处（evidence_summary 重新派生）
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.store import persist_transcript

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        from huiyiku.db.models import Meeting

        m = session.get(Meeting, mid)
        persist_transcript(
            session,
            m,
            [
                {"start_ms": 0, "end_ms": 2000, "text": "决议原文", "speaker_label": "SPEAKER_00"},
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        session.commit()

    got = client.get(f"/api/meetings/{mid}/report").json()
    cur = got["current"]
    assert cur["status"] == "draft"
    assert "evidence_summary" in cur["content"]
    assert cur["content"]["summary"] == "只改摘要"


def test_patch_report_rejects_invalid_content(runtime) -> None:
    mid = _mk_report(runtime, _report_payload())
    client = TestClient(create_app(runtime))
    r = client.patch(
        f"/api/meetings/{mid}/report",
        json={"content_json": {"summary": 123}},
    )
    assert r.status_code == 400


def test_embed_texts_batch_aligns_by_index(runtime) -> None:
    import httpx

    from huiyiku.db.models import ModelRegistry
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.llm.embed import embed_texts_batch

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        row = ModelRegistry(
            task="embedding",
            provider="openai_compatible",
            model_name="emb-test",
            execution="remote",
            endpoint="http://emb.test",
            status="available",
            is_default=1,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        session.add(row)
        session.commit()
        mid = row.id

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "emb-test"
        assert body["input"] == ["甲", "乙", "丙"]
        # 故意乱序返回，靠 index 字段对齐
        data = [{"index": i, "embedding": [float(i), 1.0]} for i in [2, 0, 1]]
        return httpx.Response(200, json={"data": data})

    with Session() as session:
        row = session.get(ModelRegistry, mid)
        vecs = embed_texts_batch(
            runtime, row, ["甲", "乙", "丙"], transport=httpx.MockTransport(handler)
        )
    assert vecs == [[0.0, 1.0], [1.0, 1.0], [2.0, 1.0]]


def test_embed_texts_batch_size_mismatch_errors(runtime) -> None:
    import httpx

    from huiyiku.db.models import ModelRegistry
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.llm.client import ChatError
    from huiyiku.llm.embed import embed_texts_batch

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        row = ModelRegistry(
            task="embedding",
            provider="openai_compatible",
            model_name="emb-test",
            execution="remote",
            endpoint="http://emb.test",
            status="available",
            is_default=1,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        session.add(row)
        session.commit()
        mid = row.id
    def handler(request):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})
    with Session() as session:
        row = session.get(ModelRegistry, mid)
        with pytest.raises(ChatError):
            embed_texts_batch(
                runtime, row, ["甲", "乙"], transport=httpx.MockTransport(handler)
            )


def test_accept_and_get_report_share_vector_fallback(runtime, monkeypatch) -> None:
    """双路同源：accept 建的检索块时间与 GET /report 的出处时间一致。"""
    import huiyiku.llm.embed as embed_mod
    from huiyiku.db.models import MeetingChunk, ModelRegistry, Report, ReportVersion
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.store import create_meeting, persist_transcript, rebuild_stats_and_chunks

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"

    def fake_embed(_rt, _model, texts, *, transport=None):
        # 含"预算审批"的是转写块文本，含"团建"的是报告条目文本 → 同向量，余弦 1.0
        return [
            [1.0, 0.0] if ("预算审批" in t or "团建" in t) else [0.0, 1.0] for t in texts
        ]

    monkeypatch.setattr(embed_mod, "embed_texts_batch", fake_embed)

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="vec")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": 30000,
                    "end_ms": 40000,
                    "text": "项目组决定在下个季度完成预算审批并启动供应商招标流程",
                    "speaker_label": "SPEAKER_00",
                }
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        rebuild_stats_and_chunks(session, m, session.connection().connection)
        session.add(
            ModelRegistry(
                task="embedding",
                provider="openai_compatible",
                model_name="emb-test",
                execution="remote",
                endpoint="http://emb.test",
                status="available",
                is_default=1,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
        ts = now_iso()
        report = Report(meeting_id=m.id, created_at=ts, updated_at=ts)
        session.add(report)
        session.flush()
        payload = {
            "summary": "s",
            "key_points": [],
            "decisions": [
                {"content": "团队计划去滨海城市组织团建活动", "segment_ids": [], "time_ms": None}
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
    r = client.post(f"/api/report-versions/{vid}/accept")
    assert r.status_code == 200

    with Session() as session:
        chunk = session.scalar(
            select(MeetingChunk).where(
                MeetingChunk.meeting_id == mid, MeetingChunk.chunk_type == "decision"
            )
        )
        tchunk = session.scalar(
            select(MeetingChunk).where(
                MeetingChunk.meeting_id == mid, MeetingChunk.chunk_type == "transcript"
            )
        )
    assert chunk is not None
    # 向量兜底命中：检索块带真实时间（而非 0）
    assert chunk.start_ms == 30000
    # 自愈：缺失的转写块向量已批量补齐并落库
    assert tchunk is not None and tchunk.embedding is not None

    got = client.get(f"/api/meetings/{mid}/report").json()
    item = got["current"]["content"]["decisions"][0]
    assert item["evidence_status"] == "paraphrase"
    assert item["evidence"][0]["start_ms"] == 30000
    assert item["evidence"][0]["vector_matched"] is True
    # 双路一致
    assert chunk.start_ms == item["evidence"][0]["start_ms"]

    # 导出第三路同源：md 导出带同一向量出处，并标注「时间近似」
    md = client.get(f"/api/export/meeting/{mid}.md").text
    assert "00:00:30" in md
    assert "（时间近似）" in md


def test_patch_report_rejects_unknown_top_keys(runtime) -> None:
    mid = _mk_report(runtime, _report_payload())
    client = TestClient(create_app(runtime))
    content = _report_payload()
    content["sneaky_field"] = "x"
    r = client.patch(f"/api/meetings/{mid}/report", json={"content_json": content})
    assert r.status_code == 400
    assert "sneaky_field" in r.json()["detail"]


def test_embed_texts_batch_duplicate_index_errors(runtime) -> None:
    import httpx

    from huiyiku.db.models import ModelRegistry
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.llm.client import ChatError
    from huiyiku.llm.embed import embed_texts_batch

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"
    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        row = ModelRegistry(
            task="embedding",
            provider="openai_compatible",
            model_name="emb-test",
            execution="remote",
            endpoint="http://emb.test",
            status="available",
            is_default=1,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        session.add(row)
        session.commit()
        mid = row.id
    def handler(request):
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [2.0]}]},
        )
    with Session() as session:
        row = session.get(ModelRegistry, mid)
        with pytest.raises(ChatError, match="duplicate index"):
            embed_texts_batch(
                runtime, row, ["甲", "乙"], transport=httpx.MockTransport(handler)
            )


def test_action_csv_uses_vector_fallback(runtime, monkeypatch) -> None:
    """行动项 CSV 是第五条溯源路径：词法未命中时同样走向量兜底。"""
    import huiyiku.llm.embed as embed_mod
    from huiyiku.db.models import ModelRegistry
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.export.csvout import build_action_rows
    from huiyiku.store import create_meeting, persist_transcript, rebuild_stats_and_chunks

    runtime.settings.allow_cloud = True
    runtime.secrets.llm_api_key = "sk-test"

    def fake_embed(_rt, _model, texts, *, transport=None):
        return [
            [1.0, 0.0] if ("预算审批" in t or "团建" in t) else [0.0, 1.0] for t in texts
        ]

    monkeypatch.setattr(embed_mod, "embed_texts_batch", fake_embed)

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="csv-vec")
        persist_transcript(
            session,
            m,
            [
                {
                    "start_ms": 30000,
                    "end_ms": 40000,
                    "text": "项目组决定在下个季度完成预算审批并启动供应商招标流程",
                    "speaker_label": "SPEAKER_00",
                }
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        rebuild_stats_and_chunks(session, m, session.connection().connection)
        session.add(
            ModelRegistry(
                task="embedding",
                provider="openai_compatible",
                model_name="emb-test",
                execution="remote",
                endpoint="http://emb.test",
                status="available",
                is_default=1,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
        session.commit()
        mid = m.id
        # 造一条词法必然未命中的行动项（靠向量兜底命中）
        from huiyiku.db.models import ActionItem

        session.add(
            ActionItem(
                group_id=m.group_id,
                source_meeting_id=mid,
                title="团队计划去滨海城市组织团建活动",
                status="open",
                origin="report",
                updated_at=now_iso(),
            )
        )
        session.commit()

    with Session() as session:
        rows = build_action_rows(session, runtime=runtime)
    row = next(r for r in rows if r["行动项"] == "团队计划去滨海城市组织团建活动")
    assert row["出处时间点"] == "00:00:30"
    assert row["出处备注"] == "时间近似"

    client = TestClient(create_app(runtime))
    csv_text = client.get("/api/export/actions.csv").text
    assert "时间近似" in csv_text
