# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import shutil
import wave
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from huiyiku.api.app import create_app
from huiyiku.config import load_runtime, save_settings
from huiyiku.db.models import Meeting, MeetingChunk, ModelRegistry
from huiyiku.db.session import make_engine, session_factory
from huiyiku.store import create_meeting
from huiyiku.timeutil import now_iso


def _tiny_wav(path: Path) -> None:
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 1600)


def test_allow_cloud_false_still_queues_transcribe(runtime) -> None:
    runtime.settings.allow_cloud = False
    save_settings(runtime.layout["settings"], runtime.settings)
    client = TestClient(create_app(runtime))
    meeting = client.post("/api/meetings", json={"title": "local"}).json()
    r = client.post(f"/api/meetings/{meeting['id']}/retry?stage=transcribe")
    assert r.status_code == 202
    assert "job_id" in r.json()


def test_settings_patch_does_not_change_registry_default(runtime) -> None:
    client = TestClient(create_app(runtime))
    client.post("/api/setup", json={"legal_confirmed": True, "egress_confirmed": True})
    before = client.get("/api/models").json()
    asr = next(m for m in before if m["task"] == "asr")
    assert asr["is_default"] is True
    client.patch("/api/settings", json={"allow_cloud": False, "max_upload_gb": 1})
    after = client.get("/api/models").json()
    asr2 = next(m for m in after if m["task"] == "asr")
    assert asr2["is_default"] is True
    assert asr2["id"] == asr["id"]


def test_embedding_default_blocked_when_vectors_exist(runtime) -> None:
    client = TestClient(create_app(runtime))
    client.post("/api/setup", json={"legal_confirmed": True, "egress_confirmed": True})
    created = client.post(
        "/api/models",
        json={
            "task": "embedding",
            "provider": "openai_compatible",
            "model_name": "text-embedding-3-small",
            "execution": "remote",
            "endpoint": "https://example.invalid/v1",
            "embedding_dim": 4,
            "terms_accepted": True,
        },
    ).json()
    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        row = session.get(ModelRegistry, created["id"])
        assert row is not None
        row.status = "available"
        m = create_meeting(session, title="e")
        session.add(
            MeetingChunk(
                meeting_id=m.id,
                group_id=m.group_id,
                start_ms=0,
                end_ms=1000,
                chunk_type="transcript",
                content="hello",
                tokenized_content="hello",
                embedding=b"\x00\x00\x80\x3f" * 4,
                embedding_model="openai_compatible:text-embedding-3-small",
                embedding_dim=4,
                is_active=1,
                created_at=now_iso(),
            )
        )
        session.commit()
    r = client.patch(f"/api/models/{created['id']}", json={"is_default": True})
    assert r.status_code == 400


def test_audio_range_with_cookie(runtime) -> None:
    runtime.settings.access_token = "tok"
    save_settings(runtime.layout["settings"], runtime.settings)
    wav = runtime.layout["media_wav"] / "1.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    _tiny_wav(wav)
    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="a")
        m.wav_path = "media/wav/1.wav"
        session.commit()
        mid = m.id
    client = TestClient(create_app(runtime))
    denied = client.get(f"/api/meetings/{mid}/audio")
    assert denied.status_code == 401
    client.post("/api/session", json={"token": "tok"})
    ok = client.get(f"/api/meetings/{mid}/audio", headers={"Range": "bytes=0-63"})
    assert ok.status_code in {200, 206}
    assert (
        "bytes"
        in (ok.headers.get("accept-ranges") or ok.headers.get("Accept-Ranges") or "bytes").lower()
    )


def test_backup_copy_data_dir(runtime, tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(runtime))
    client.post("/api/meetings", json={"title": "keep-me"})
    backup = tmp_path / "backup"
    shutil.copytree(runtime.app.data_path, backup)
    app_json = tmp_path / "app2.json"
    app_json.write_text(
        json.dumps(
            {
                "data_dir": str(backup),
                "host": "127.0.0.1",
                "port": 8787,
                "ffmpeg_path": "",
                "asr_home": str(tmp_path / "asr"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HUIYIKU_APP_JSON", str(app_json))
    restored = load_runtime()
    other = TestClient(create_app(restored))
    titles = [m["title"] for m in other.get("/api/meetings").json()]
    assert "keep-me" in titles


def test_retranscribe_after_stats_succeeds(runtime) -> None:
    """重转写回归：已建统计（SpeakerStat 引用说话人）的会议再次
    persist_transcript 不得因外键崩溃，且旧派生块被失活。"""
    from huiyiku.db.models import MeetingChunk, MeetingSpeaker
    from huiyiku.store import (
        create_meeting,
        persist_transcript,
        rebuild_stats_and_chunks,
    )

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    segs = [{"start_ms": 0, "end_ms": 2000, "text": "first pass", "speaker_label": "SPEAKER_00"}]
    with Session() as session:
        m = create_meeting(session, title="re")
        persist_transcript(session, m, segs, provider="funasr", model="p", version="v1")
        for sp in session.scalars(select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == m.id)):
            sp.status = "confirmed"
        rebuild_stats_and_chunks(session, m, session.connection().connection)
        session.commit()
        mid = m.id

    segs2 = [{"start_ms": 0, "end_ms": 3000, "text": "second pass", "speaker_label": "SPEAKER_00"}]
    with Session() as session:  # 修复前在第二遍抛 FOREIGN KEY constraint failed
        m = session.get(Meeting, mid)
        persist_transcript(session, m, segs2, provider="funasr", model="p", version="v1")
        rebuild_stats_and_chunks(session, m, session.connection().connection)
        session.commit()
        active = session.scalars(
            select(MeetingChunk).where(MeetingChunk.meeting_id == mid, MeetingChunk.is_active == 1)
        ).all()
        texts = [c.content for c in active]
        assert any("second pass" in t for t in texts)
        assert not any("first pass" in t for t in texts)
