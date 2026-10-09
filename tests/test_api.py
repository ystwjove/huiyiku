# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from huiyiku.api.app import create_app
from huiyiku.config import save_settings
from huiyiku.store import create_meeting, persist_transcript
from huiyiku.timeutil import now_iso


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["app"] == "huiyiku"


def test_share_lan_switch_blocks_and_releases(runtime) -> None:
    app = create_app(runtime)
    local = TestClient(app)
    lan = TestClient(app, client=("192.168.1.20", 12345))
    assert local.get("/api/settings").json()["share_lan"] is False
    assert lan.get("/api/health").status_code == 403
    opened = local.patch("/api/settings", json={"share_lan": True})
    assert opened.status_code == 200
    assert opened.json()["share_lan"] is True
    assert lan.get("/api/health").status_code == 200
    assert lan.get("/api/runtime").json()["share_lan"] is True
    closed = lan.patch("/api/settings", json={"share_lan": False})
    assert closed.status_code == 200
    assert closed.json()["share_lan"] is False
    assert lan.get("/api/health").status_code == 403
    assert local.get("/api/health").status_code == 200
    again = json.loads(runtime.layout["settings"].read_text(encoding="utf-8"))
    assert again["share_lan"] is False


def test_setup_and_groups(client: TestClient) -> None:
    r = client.post("/api/setup", json={"legal_confirmed": True, "egress_confirmed": True})
    assert r.status_code == 200
    groups = client.get("/api/groups").json()
    assert any(g["is_system"] for g in groups)
    created = client.post("/api/groups", json={"name": "项目A"}).json()
    assert created["name"] == "项目A"


def test_session_cookie_has_no_secure_or_domain(runtime, monkeypatch) -> None:
    runtime.settings.access_token = "secret-token"
    save_settings(runtime.layout["settings"], runtime.settings)
    client = TestClient(create_app(runtime))
    bad = client.post("/api/session", json={"token": "nope"})
    assert bad.status_code == 401
    ok = client.post("/api/session", json={"token": "secret-token"})
    assert ok.status_code == 200
    cookie = ok.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie or "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    assert "secure" not in cookie.lower()
    assert "domain=" not in cookie.lower()
    # Cookie authenticates subsequent API calls.
    r = client.get("/api/runtime")
    assert r.status_code == 200


def test_speakers_assign_and_stats(runtime) -> None:
    from huiyiku.db.session import make_engine, session_factory

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="t")
        m.duration_ms = 4000
        persist_transcript(
            session,
            m,
            [
                {"start_ms": 0, "end_ms": 1000, "text": "甲说话", "speaker_label": "SPEAKER_00"},
                {"start_ms": 1000, "end_ms": 2500, "text": "乙说话", "speaker_label": "SPEAKER_01"},
                {"start_ms": 2500, "end_ms": 4000, "text": "还是甲", "speaker_label": "SPEAKER_00"},
            ],
            provider="funasr",
            model="paraformer-zh",
            version="v2.0.4",
        )
        session.commit()
        mid = m.id
    client = TestClient(create_app(runtime))
    speakers = client.get(f"/api/meetings/{mid}/speakers").json()
    assert len(speakers) == 2
    a = speakers[0]["id"]
    segs = client.get(f"/api/meetings/{mid}/transcript").json()
    client.post(f"/api/meeting-speakers/{a}/assign", json={"segment_ids": [s["id"] for s in segs]})
    r = client.post(f"/api/meetings/{mid}/speakers/approve")
    assert r.status_code == 202
    # Run stats handler inline
    from huiyiku.db.models import Job
    from huiyiku.worker.handlers import handle_job

    with Session() as session:
        job = session.get(Job, r.json()["job_id"])
        handle_job(session, runtime, job, cancel_check=lambda: False, progress=lambda p: None)
        session.commit()
    stats = client.get(f"/api/meetings/{mid}/stats").json()
    assert stats["metric_version"] == "v1"
    assert sum(s["segment_count"] for s in stats["speakers"]) == 3


def test_action_dedup_same_meeting(runtime) -> None:
    from huiyiku.db.models import Report, ReportVersion
    from huiyiku.db.session import make_engine, session_factory
    from huiyiku.store import accept_report_actions

    engine = make_engine(runtime.layout["db"])
    Session = session_factory(engine)
    with Session() as session:
        m = create_meeting(session, title="t")
        ts = now_iso()
        report = Report(meeting_id=m.id, created_at=ts, updated_at=ts)
        session.add(report)
        session.flush()
        content = {
            "summary": "s",
            "key_points": [],
            "decisions": [],
            "action_items": [{"title": "写周报", "owner_person_id": None, "segment_ids": []}],
            "risks": [],
            "open_questions": [],
            "participants": [],
        }
        v1 = ReportVersion(
            report_id=report.id,
            version=1,
            source="llm",
            content_json=json.dumps(content),
            status="accepted",
            created_at=ts,
        )
        session.add(v1)
        session.flush()
        accept_report_actions(session, m, v1)
        v2 = ReportVersion(
            report_id=report.id,
            version=2,
            source="llm",
            content_json=json.dumps(content),
            status="accepted",
            created_at=ts,
        )
        session.add(v2)
        session.flush()
        accept_report_actions(session, m, v2)
        session.commit()
        from huiyiku.db.models import ActionItem

        items = session.scalars(
            select(ActionItem).where(ActionItem.source_meeting_id == m.id)
        ).all()
        assert len(items) == 1


def test_digest_requires_accepted(client: TestClient) -> None:
    gid = client.get("/api/groups").json()[0]["id"]
    r = client.post(f"/api/groups/{gid}/digest", json={})
    assert r.status_code == 400


def test_allow_cloud_false_blocks_report(runtime) -> None:
    runtime.settings.allow_cloud = False
    save_settings(runtime.layout["settings"], runtime.settings)
    client = TestClient(create_app(runtime))
    m = client.post("/api/meetings", json={"title": "x"}).json()
    r = client.post(f"/api/meetings/{m['id']}/report")
    assert r.status_code == 400
