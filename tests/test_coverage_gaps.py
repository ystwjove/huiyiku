# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from huiyiku.api.app import create_app
from huiyiku.db.models import (
    ActionItem,
    Job,
    Meeting,
    MeetingChunk,
    MeetingSpeaker,
    Report,
    ReportVersion,
    SpeakerStat,
)
from huiyiku.db.session import make_engine, session_factory
from huiyiku.store import (
    accept_report_actions,
    create_meeting,
    enqueue_job,
    persist_transcript,
    rebuild_stats_and_chunks,
)
from huiyiku.timeutil import now_iso


def _session(runtime):
    engine = make_engine(runtime.layout["db"])
    return session_factory(engine)


def _seed_transcript(session, title: str = "t", text: str = "hello world meeting notes"):
    m = create_meeting(session, title=title)
    persist_transcript(
        session,
        m,
        [
            {"start_ms": 0, "end_ms": 40000, "text": text, "speaker_label": "SPEAKER_00"},
            {
                "start_ms": 40000,
                "end_ms": 70000,
                "text": "second speaker line",
                "speaker_label": "SPEAKER_01",
            },
        ],
        provider="funasr",
        model="paraformer-zh",
        version="v2.0.4",
    )
    for sp in session.scalars(select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == m.id)):
        sp.status = "confirmed"
    rebuild_stats_and_chunks(session, m, session.connection().connection)
    session.commit()
    return m


def test_pack_embedding_roundtrip() -> None:
    from huiyiku.llm.embed import pack_embedding, unpack_embedding

    raw = [0.1, -0.2, 0.5]
    assert unpack_embedding(pack_embedding(raw)) == pytest.approx(raw)


def test_completions_and_embeddings_url() -> None:
    from huiyiku.llm.client import completions_url
    from huiyiku.llm.embed import embeddings_url

    assert completions_url("https://api.example/v1").endswith("/chat/completions")
    assert embeddings_url("https://api.example/v1").endswith("/embeddings")
    assert completions_url("https://x/v1/chat/completions").endswith("/chat/completions")
    assert embeddings_url("https://x/v1/embeddings").endswith("/embeddings")


def test_logging_redacts_secrets() -> None:
    from huiyiku.logging_setup import _RedactFilter

    filt = _RedactFilter()
    rec = logging.LogRecord("huiyiku", logging.INFO, __file__, 1, "token sk-abcdefghijk", (), None)
    assert filt.filter(rec)
    assert rec.msg == "[redacted log line]"
    rec2 = logging.LogRecord("huiyiku", logging.INFO, __file__, 1, "plain message", (), None)
    assert filt.filter(rec2)
    assert rec2.msg == "plain message"


def test_pipeline_fmt_and_stats() -> None:
    from huiyiku.pipeline import _fmt_ms, _stats_from_segments

    assert _fmt_ms(3661000) == "01:01:01"
    rows = _stats_from_segments(
        [
            {"speaker_label": "SPEAKER_00", "start_ms": 0, "end_ms": 1000},
            {"speaker_label": "SPEAKER_00", "start_ms": 1000, "end_ms": 3000},
        ]
    )
    assert rows[0]["speech_ms"] == 3000
    assert rows[0]["segment_count"] == 2


def test_bootstrap_health_and_second_instance(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    from huiyiku import bootstrap

    class _Resp:
        def __init__(self, data: dict) -> None:
            self.status_code = 200
            self.headers = {"content-type": "application/json"}
            self._data = data

        def json(self) -> dict:
            return self._data

    monkeypatch.setattr(
        bootstrap.httpx, "get", lambda *_a, **_k: _Resp({"ok": True, "app": "huiyiku"})
    )
    assert bootstrap._health()["app"] == "huiyiku"
    monkeypatch.setattr(bootstrap.httpx, "get", lambda *_a, **_k: _Resp({"app": "nginx"}))
    assert bootstrap._occupied_by_other() is True

    opened: list[str] = []
    native: list[bool] = []
    monkeypatch.setattr(bootstrap, "_health", lambda: {"app": "huiyiku"})
    monkeypatch.setattr(bootstrap, "_native_window", lambda *_a: native.append(True) or 0)
    monkeypatch.setattr(bootstrap.webbrowser, "open", lambda url: opened.append(url))

    # 已有实例 + WebView2 可用 → 原生窗口，不开浏览器
    monkeypatch.setattr(bootstrap, "_webview2_available", lambda: True)
    assert bootstrap.run_bootstrap() == 0
    assert native == [True]
    assert opened == []

    # 已有实例 + WebView2 不可用 → 回退系统浏览器
    monkeypatch.setattr(bootstrap, "_webview2_available", lambda: False)
    assert bootstrap.run_bootstrap() == 0
    assert opened == ["http://127.0.0.1:8787"]

    # 原生窗口抛异常 → 回退系统浏览器（仍是唯一一个界面）
    def _boom(*_a) -> int:
        raise RuntimeError("no window")

    monkeypatch.setattr(bootstrap, "_native_window", _boom)
    monkeypatch.setattr(bootstrap, "_webview2_available", lambda: True)
    assert bootstrap.run_bootstrap() == 0
    assert opened == ["http://127.0.0.1:8787", "http://127.0.0.1:8787"]


def test_bootstrap_first_launch_native_path(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    from huiyiku import bootstrap

    class _FakeProc:
        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

    procs: list[_FakeProc] = []
    checks = {"n": 0}

    def _health_seq() -> dict | None:
        checks["n"] += 1
        return None if checks["n"] == 1 else {"app": "huiyiku"}

    def _fake_popen(*_a, **_k) -> _FakeProc:
        proc = _FakeProc()
        procs.append(proc)
        return proc

    order: list[str] = []
    monkeypatch.setattr(bootstrap, "_health", _health_seq)
    monkeypatch.setattr(bootstrap, "_occupied_by_other", lambda: False)
    monkeypatch.setattr(bootstrap.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(bootstrap, "_webview2_available", lambda: True)
    monkeypatch.setattr(bootstrap, "_native_window", lambda *_a: order.append("window") or 0)
    monkeypatch.setattr(bootstrap, "_stop", lambda *a: order.append("stop"))
    monkeypatch.setattr(bootstrap, "wal_checkpoint", lambda *a, **_k: order.append("checkpoint"))
    monkeypatch.setattr(bootstrap.webbrowser, "open", lambda url: order.append("browser"))

    assert bootstrap.run_bootstrap() == 0
    # 原生窗口关闭后：停子进程 → WAL checkpoint → 返回；不开浏览器、不进 tkinter
    assert order == ["window", "stop", "checkpoint"]
    assert len(procs) == 2  # serve + worker，无重启


def test_install_excepthook(tmp_path: Path) -> None:
    import sys

    from huiyiku.bootstrap import install_excepthook

    install_excepthook(tmp_path)
    sys.excepthook(ValueError, ValueError("boom"), None)
    assert "ValueError" in (tmp_path / "unhandled.log").read_text(encoding="utf-8")


def test_worker_recovers_stale_and_fails_meeting(runtime) -> None:
    from huiyiku.worker.loop import _recover

    Session = _session(runtime)
    with Session() as session:
        m = create_meeting(session, title="w")
        m.status = "transcribing"
        job = Job(
            meeting_id=m.id,
            type="transcribe",
            status="running",
            payload_json="{}",
            idempotency_key="recover-1",
            attempt_count=3,
            timeout_seconds=10,
            cancel_requested=0,
            heartbeat_at="2000-01-01T00:00:00+00:00",
            created_at=now_iso(),
        )
        session.add(job)
        session.commit()
        mid, jid = m.id, job.id
    _recover(Session)
    with Session() as session:
        job = session.get(Job, jid)
        meeting = session.get(Meeting, mid)
        assert job is not None and job.status == "failed"
        assert meeting is not None and meeting.status == "failed"


def test_worker_once_runs_media_and_infer(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    from huiyiku.worker import loop as loopmod

    started: dict[str, float] = {}

    def fake_handle(session, _runtime, job, cancel_check, progress):
        started[job.type] = time.monotonic()
        time.sleep(0.25)
        progress({"stage": job.type, "percent": 100})

    monkeypatch.setattr(loopmod, "handle_job", fake_handle)
    Session = _session(runtime)
    with Session() as session:
        m = create_meeting(session, title="lanes")
        enqueue_job(session, "preprocess", meeting_id=m.id, payload={"t": "a"})
        enqueue_job(session, "stats", meeting_id=m.id, payload={"t": "b"}, set_meeting_status=False)
        session.commit()
    assert loopmod.run_worker(runtime.app.path, once=True) == 0
    assert "preprocess" in started and "stats" in started
    assert abs(started["preprocess"] - started["stats"]) < 0.2
    with Session() as session:
        jobs = list(session.scalars(select(Job)))
        assert {j.status for j in jobs} == {"success"}


def test_handle_stats_indexes_without_embedding(runtime) -> None:
    from huiyiku.worker.handlers import handle_job

    Session = _session(runtime)
    with Session() as session:
        m = _seed_transcript(session)
        job = enqueue_job(
            session, "stats", meeting_id=m.id, payload={"n": 1}, set_meeting_status=False
        )
        session.commit()
        jid, mid = job.id, m.id
    with Session() as session:
        job = session.get(Job, jid)
        handle_job(session, runtime, job, cancel_check=lambda: False, progress=lambda _p: None)
        session.commit()
        meeting = session.get(Meeting, mid)
        assert meeting is not None and meeting.status == "indexed"
        stats = list(session.scalars(select(SpeakerStat).where(SpeakerStat.meeting_id == mid)))
        assert stats


def test_jobs_cancel_endpoint(runtime) -> None:
    client = TestClient(create_app(runtime))
    Session = _session(runtime)
    with Session() as session:
        m = create_meeting(session, title="c")
        job = enqueue_job(
            session, "report", meeting_id=m.id, payload={"x": 1}, set_meeting_status=False
        )
        session.commit()
        jid = job.id
    r = client.post(f"/api/jobs/{jid}/cancel")
    assert r.status_code == 200
    listed = client.get("/api/jobs").json()
    assert any(j["id"] == jid and j["cancel_requested"] for j in listed)
    one = client.get(f"/api/jobs/{jid}").json()
    assert one["cancel_requested"] is True


def test_qa_sse_no_materials_and_fts_verified(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    from huiyiku.llm import qa as qamod

    class _Model:
        provider = "openai_compatible"
        model_name = "mock-chat"
        embedding_dim = None

    class _Client:
        context_length = 8000

        async def stream(self, _messages):
            yield "引用 [chunk "
            yield "X] hello world meeting notes"

    monkeypatch.setattr(qamod, "_llm_for_role", lambda *_a, **_k: (_Client(), _Model()))
    runtime.secrets.llm_api_key = "sk-test"
    runtime.settings.allow_cloud = True
    client = TestClient(create_app(runtime))
    empty = client.post("/api/qa", json={"session_id": "s", "question": "zzz", "scope": {}})
    assert empty.status_code == 200
    assert "没有找到相关材料" in empty.text

    Session = _session(runtime)
    with Session() as session:
        m = _seed_transcript(session)
        mid = m.id
        chunk = session.scalars(
            select(MeetingChunk).where(MeetingChunk.meeting_id == mid, MeetingChunk.is_active == 1)
        ).first()
        assert chunk is not None
        cid = chunk.id

    class _Client2:
        context_length = 8000

        async def stream(self, _messages):
            yield f"根据 [chunk {cid}] hello world meeting notes 回答。"

    monkeypatch.setattr(qamod, "_llm_for_role", lambda *_a, **_k: (_Client2(), _Model()))
    hit = client.post(
        "/api/qa",
        json={"session_id": "s2", "question": "hello", "scope": {"meeting_ids": [mid]}},
    )
    assert hit.status_code == 200
    assert "hello world" in hit.text
    assert "verified" in hit.text


def test_delete_meeting_removes_files_and_fts(runtime) -> None:
    client = TestClient(create_app(runtime))
    Session = _session(runtime)
    with Session() as session:
        m = _seed_transcript(session)
        wav = runtime.layout["media_wav"] / f"{m.id}.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 32)
        m.wav_path = f"media/wav/{wav.name}"
        session.commit()
        mid = m.id
    preview = client.delete(f"/api/meetings/{mid}")
    assert preview.status_code == 200
    assert preview.json()["need_confirm"] is True
    gone = client.delete(f"/api/meetings/{mid}?confirm=true")
    assert gone.status_code == 200
    assert client.get(f"/api/meetings/{mid}").status_code == 404
    assert not wav.exists()


def test_merge_rebuilds_stats_without_source(runtime) -> None:
    client = TestClient(create_app(runtime))
    Session = _session(runtime)
    with Session() as session:
        m = _seed_transcript(session)
        speakers = list(
            session.scalars(select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == m.id))
        )
        src = next(s for s in speakers if s.speaker_label == "SPEAKER_00")
        dst = next(s for s in speakers if s.speaker_label == "SPEAKER_01")
        sid, did, mid = src.id, dst.id, m.id
    r = client.post(f"/api/meeting-speakers/{sid}/merge", json={"into_id": did})
    assert r.status_code == 200
    stats = client.get(f"/api/meetings/{mid}/stats").json()
    ids = {row["speaker_id"] for row in stats["speakers"]}
    assert did in ids
    assert sid not in ids


def test_export_markdown_contains_title(runtime) -> None:
    client = TestClient(create_app(runtime))
    Session = _session(runtime)
    with Session() as session:
        m = _seed_transcript(session, title="导出会")
        mid = m.id
    r = client.get(f"/api/export/meeting/{mid}.md")
    assert r.status_code == 200
    assert "导出会" in r.text
    assert "hello world" in r.text


def test_move_group_updates_chunk_group_id(runtime) -> None:
    client = TestClient(create_app(runtime))
    g = client.post("/api/groups", json={"name": "项目B"}).json()
    Session = _session(runtime)
    with Session() as session:
        m = _seed_transcript(session)
        mid = m.id
        old = m.group_id
    r = client.patch(f"/api/meetings/{mid}", json={"group_id": g["id"]})
    assert r.status_code == 200
    Session = _session(runtime)
    with Session() as session:
        chunks = list(session.scalars(select(MeetingChunk).where(MeetingChunk.meeting_id == mid)))
        assert chunks
        assert all(c.group_id == g["id"] for c in chunks)
        assert g["id"] != old


def test_action_items_not_deduped_across_meetings(runtime) -> None:
    Session = _session(runtime)
    payload = {
        "summary": "s",
        "key_points": [],
        "decisions": [],
        "action_items": [{"title": "写周报", "owner_person_id": None, "segment_ids": []}],
        "risks": [],
        "open_questions": [],
        "participants": [],
    }
    with Session() as session:
        ts = now_iso()
        items = []
        for title in ("会A", "会B"):
            m = create_meeting(session, title=title)
            report = Report(meeting_id=m.id, created_at=ts, updated_at=ts)
            session.add(report)
            session.flush()
            ver = ReportVersion(
                report_id=report.id,
                version=1,
                source="llm",
                content_json=json.dumps(payload),
                status="accepted",
                created_at=ts,
            )
            session.add(ver)
            session.flush()
            accept_report_actions(session, m, ver)
            items.append(m.id)
        session.commit()
        rows = list(session.scalars(select(ActionItem)))
        assert len(rows) == 2
        assert {r.source_meeting_id for r in rows} == set(items)


def test_invalid_speaker_excluded_from_stats(runtime) -> None:
    client = TestClient(create_app(runtime))
    Session = _session(runtime)
    with Session() as session:
        m = _seed_transcript(session)
        src = session.scalars(
            select(MeetingSpeaker).where(
                MeetingSpeaker.meeting_id == m.id, MeetingSpeaker.speaker_label == "SPEAKER_00"
            )
        ).first()
        assert src is not None
        sid, mid = src.id, m.id
    bad = client.patch(f"/api/meeting-speakers/{sid}", json={"status": "nope"})
    assert bad.status_code == 400
    ok = client.patch(f"/api/meeting-speakers/{sid}", json={"status": "invalid"})
    assert ok.status_code == 200
    stats = client.get(f"/api/meetings/{mid}/stats").json()
    assert sid not in {row["speaker_id"] for row in stats["speakers"]}


def test_groups_and_persons_crud(client: TestClient) -> None:
    g = client.post("/api/groups", json={"name": "一组"}).json()
    assert client.get(f"/api/groups/{g['id']}").status_code == 200
    patched = client.patch(f"/api/groups/{g['id']}", json={"name": "一组改"})
    assert patched.json()["name"] == "一组改"
    p = client.post("/api/persons", json={"name": "张三"}).json()
    client.patch(f"/api/persons/{p['id']}", json={"name": "张三丰"})
    names = [x["name"] for x in client.get("/api/persons").json()]
    assert "张三丰" in names
    assert client.delete(f"/api/persons/{p['id']}").status_code == 200


def test_import_local_and_meeting_get(runtime, tmp_path: Path) -> None:
    import wave

    wav = tmp_path / "in.wav"
    with wave.open(str(wav), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 160)
    client = TestClient(create_app(runtime))
    r = client.post("/api/meetings/import-local", json={"path": str(wav), "title": "本地导入"})
    assert r.status_code == 200
    mid = r.json()["meeting"]["id"]
    got = client.get(f"/api/meetings/{mid}")
    assert got.json()["title"] == "本地导入"


def test_generate_report_and_digest_with_mock_llm(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    from huiyiku.llm import digest as digestmod
    from huiyiku.llm import report as reportmod
    from huiyiku.llm.digest import generate_digest
    from huiyiku.llm.report import generate_report

    class _Model:
        provider = "openai_compatible"
        model_name = "mock-chat"

    class _Client:
        context_length = 32000

        def complete(self, _messages, **_k):
            body = {
                "summary": "hello world meeting notes 的摘要",
                "key_points": [
                    {"content": "hello world meeting notes", "segment_ids": [], "time_ms": 0}
                ],
                "decisions": [],
                "action_items": [{"title": "跟进", "owner_person_id": None, "segment_ids": []}],
                "risks": [],
                "open_questions": [],
                "participants": [],
            }
            return {
                "content": json.dumps(body, ensure_ascii=False),
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            }

    monkeypatch.setattr(reportmod, "_llm_for_role", lambda *_a, **_k: (_Client(), _Model()))
    monkeypatch.setattr(digestmod, "_llm_for_role", lambda *_a, **_k: (_Client(), _Model()))
    runtime.secrets.llm_api_key = "sk-test"
    runtime.settings.allow_cloud = True
    Session = _session(runtime)
    with Session() as session:
        m = _seed_transcript(session)
        m.status = "indexed"
        job = enqueue_job(session, "report", meeting_id=m.id, payload={"n": 1})
        session.commit()
        generate_report(session, runtime, job, progress=lambda _p: None)
        session.commit()
        report = session.scalar(select(Report).where(Report.meeting_id == m.id))
        assert report is not None
        ver = session.get(ReportVersion, report.current_version_id)
        assert ver is not None
        ver.status = "accepted"
        m.status = "ready"
        session.commit()
        gid = m.group_id
        djob = enqueue_job(
            session,
            "digest",
            payload={"group_id": gid, "period_start": "1970-01-01", "period_end": "2999-01-01"},
            set_meeting_status=False,
        )
        session.commit()
        generate_digest(session, runtime, djob, progress=lambda _p: None)
        session.commit()
        from huiyiku.db.models import GroupDigest

        digests = list(session.scalars(select(GroupDigest).where(GroupDigest.group_id == gid)))
        assert digests


def test_digest_period_excludes_out_of_range(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    from huiyiku.llm import digest as digestmod
    from huiyiku.llm.client import ChatError
    from huiyiku.llm.digest import generate_digest

    class _Model:
        provider = "openai_compatible"
        model_name = "mock-chat"

    class _Client:
        context_length = 32000

        def complete(self, _messages, **_k):
            raise AssertionError("should not call LLM")

    monkeypatch.setattr(digestmod, "_llm_for_role", lambda *_a, **_k: (_Client(), _Model()))
    runtime.secrets.llm_api_key = "sk-test"
    Session = _session(runtime)
    with Session() as session:
        m = create_meeting(session, title="old", occurred_at="2020-01-01T00:00:00")
        m.status = "ready"
        ts = now_iso()
        report = Report(meeting_id=m.id, created_at=ts, updated_at=ts)
        session.add(report)
        session.flush()
        ver = ReportVersion(
            report_id=report.id,
            version=1,
            source="llm",
            content_json=json.dumps({"summary": "s"}),
            content_md="md",
            status="accepted",
            created_at=ts,
        )
        session.add(ver)
        session.flush()
        report.current_version_id = ver.id
        job = enqueue_job(
            session,
            "digest",
            payload={
                "group_id": m.group_id,
                "period_start": "2024-01-01",
                "period_end": "2024-12-31",
            },
            set_meeting_status=False,
        )
        session.commit()
        with pytest.raises(ChatError, match="no accepted reports"):
            generate_digest(session, runtime, job, progress=lambda _p: None)


def test_env_inference_slots_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from huiyiku.worker import loop as loopmod

    monkeypatch.setenv("HUIYIKU_INFERENCE_SLOTS", "9")
    monkeypatch.setenv("HUIYIKU_MEDIA_SLOTS", "2")
    lanes = loopmod._Lanes()
    assert lanes.infer_slots == 2
    assert lanes.media_slots == 1
    lanes.shutdown(wait=False)


def test_watch_parent_noop_without_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from huiyiku.__main__ import _watch_parent

    monkeypatch.delenv("HUIYIKU_PARENT_PID", raising=False)
    before = threading.active_count()
    _watch_parent()
    assert threading.active_count() == before
    monkeypatch.setenv("HUIYIKU_PARENT_PID", "not-a-number")
    _watch_parent()
    assert threading.active_count() == before


class _CtypesFunc:
    def __init__(self, impl) -> None:
        self._impl = impl
        self.restype = None
        self.argtypes = None

    def __call__(self, *args, **kwargs):
        return self._impl(*args, **kwargs)


def test_watch_parent_exits_when_openprocess_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    from huiyiku.__main__ import _watch_parent

    if sys.platform != "win32":
        pytest.skip("parent watch is Windows-only")

    class _Kernel:
        OpenProcess = _CtypesFunc(lambda *_a, **_k: 0)
        WaitForSingleObject = _CtypesFunc(lambda *_a, **_k: 0)

    kernel = _Kernel()
    exits: list[int] = []

    def _exit(code: int) -> None:
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setenv("HUIYIKU_PARENT_PID", "4000000")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel)
    monkeypatch.setattr("huiyiku.__main__.os._exit", _exit)
    with pytest.raises(SystemExit) as exc:
        _watch_parent()
    assert exc.value.code == 1
    assert exits == [1]


def test_watch_parent_starts_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes
    import os
    import threading

    from huiyiku.__main__ import _watch_parent

    if sys.platform != "win32":
        pytest.skip("parent watch is Windows-only")

    opened = {"n": 0}
    block = threading.Event()

    class _Kernel:
        OpenProcess = _CtypesFunc(lambda *_a, **_k: opened.__setitem__("n", opened["n"] + 1) or 1)
        WaitForSingleObject = _CtypesFunc(lambda *_a, **_k: block.wait(60) or 0)

    kernel = _Kernel()
    monkeypatch.setenv("HUIYIKU_PARENT_PID", str(os.getpid()))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel)
    monkeypatch.setattr("huiyiku.__main__.os._exit", lambda _code: None)
    before = threading.active_count()
    _watch_parent()
    assert opened["n"] == 1
    assert threading.active_count() == before + 1


def test_spawn_child_passes_parent_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from huiyiku import bootstrap

    captured: dict = {}

    class _Proc:
        pass

    def _fake_popen(cmd, env=None, **_k):
        captured["cmd"] = cmd
        captured["env"] = env
        return _Proc()

    monkeypatch.setattr(bootstrap.subprocess, "Popen", _fake_popen)
    proc = bootstrap._spawn_child("--serve")
    assert isinstance(proc, _Proc)
    assert captured["cmd"] == bootstrap._child_cmd("--serve")
    assert captured["env"]["HUIYIKU_PARENT_PID"] == str(os.getpid())


def test_window_size_clamps_and_floor() -> None:
    from huiyiku.bootstrap import _clamped_window_size, _window_size

    # 常规屏：默认尺寸 + 下限
    assert _clamped_window_size(1920, 1080) == (1280, 820, 960, 540)
    # 小笔电：钳制到 90%/85%，仍保下限
    assert _clamped_window_size(1366, 768) == (1229, 652, 960, 540)
    # 可用区恰好在下限：边界吻合
    assert _clamped_window_size(1067, 636) == (960, 540, 960, 540)
    # 极小屏：放弃下限取可用区，min 同步缩小，窗口完全入屏
    assert _clamped_window_size(1024, 600) == (921, 510, 921, 510)
    # 无效屏尺寸：回退默认
    assert _clamped_window_size(0, 0) == (1280, 820, 960, 540)
    # 不变式：min 永不超过窗口（在任意真实屏幕上也成立）
    w, h, mw, mh = _window_size()
    assert mw <= w and mh <= h


def test_scope_ok_filters_type_time_person(runtime) -> None:
    """5.2 问答范围：类型 / 时间 / 人物过滤组合（前端 QA 面板已接入这些键）。"""
    from huiyiku.llm.qa import _scope_ok

    Session = _session(runtime)
    with Session() as session:
        m = _seed_transcript(session)
        m.occurred_at = "2026-08-01 10:00"
        session.commit()
        chunk = session.scalars(
            select(MeetingChunk).where(MeetingChunk.meeting_id == m.id, MeetingChunk.is_active == 1)
        ).first()
        assert chunk is not None and chunk.chunk_type == "transcript"

        # 类型：命中 / 不命中
        assert _scope_ok(session, chunk, {"chunk_types": ["transcript"]}) is True
        assert _scope_ok(session, chunk, {"chunk_types": ["decision"]}) is False

        # 时间：occurred_at 在区间内 / 早于 start / 晚于 end
        assert _scope_ok(session, chunk, {"start": "2026-07-01", "end": "2026-08-31T23:59"}) is True
        assert _scope_ok(session, chunk, {"start": "2026-09-01"}) is False
        assert _scope_ok(session, chunk, {"end": "2026-07-31"}) is False

        # 人物：chunk.speaker_id 存在时按 speaker.person_id 过滤；无关联则放行
        from huiyiku.db.models import Person

        person = Person(name="张三", created_at="2026-08-01T00:00", updated_at="2026-08-01T00:00")
        session.add(person)
        session.flush()
        sp = session.scalars(
            select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == m.id)
        ).first()
        assert sp is not None
        sp.person_id = person.id
        pid = person.id
        session.commit()
        assert _scope_ok(session, chunk, {"person_id": pid}) is True
        assert _scope_ok(session, chunk, {"person_id": pid + 999}) is False

        # 组合：类型命中 + 时间命中 + 人物命中 → 通过；任一不命中 → 拒绝
        ok = {"chunk_types": ["transcript"], "start": "2026-07-01", "person_id": pid}
        assert _scope_ok(session, chunk, ok) is True
        assert _scope_ok(session, chunk, {**ok, "person_id": pid + 999}) is False
        assert _scope_ok(session, chunk, {**ok, "chunk_types": ["action"]}) is False


def test_recover_reclaims_orphans_immediately(runtime) -> None:
    """worker 重启后 running 孤儿立即回收（单 worker 设计，不等 2 小时心跳超时）；
    cancel_requested 的孤儿直接置 cancelled。"""
    from huiyiku.timeutil import now_iso
    from huiyiku.worker.loop import _recover

    Session = _session(runtime)
    with Session() as session:
        m1 = create_meeting(session, title="orphan-a")
        j1 = enqueue_job(session, "transcribe", meeting_id=m1.id)
        j1.status = "running"
        j1.heartbeat_at = now_iso()  # 心跳新鲜：旧逻辑会等 2 小时超时
        m2 = create_meeting(session, title="orphan-b")
        j2 = enqueue_job(session, "transcribe", meeting_id=m2.id)
        j2.status = "running"
        j2.cancel_requested = 1
        session.commit()
        id1, id2 = j1.id, j2.id

    _recover(Session)

    with Session() as session:
        r1, r2 = session.get(Job, id1), session.get(Job, id2)
        assert r1.status == "queued" and r1.attempt_count == 1
        assert r2.status == "cancelled"
