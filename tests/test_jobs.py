# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from datetime import UTC, datetime, timedelta

from huiyiku.domain.jobs import idempotency_key, is_heartbeat_stale, recover_status


def test_idempotency_stable() -> None:
    a = idempotency_key(1, "transcribe", {"x": 1}, "v1")
    b = idempotency_key(1, "transcribe", {"x": 1}, "v1")
    c = idempotency_key(1, "transcribe", {"x": 2}, "v1")
    assert a == b
    assert a != c


def test_heartbeat_stale() -> None:
    now = datetime.now(UTC)
    fresh = (now - timedelta(seconds=10)).isoformat()
    old = (now - timedelta(seconds=4000)).isoformat()
    assert not is_heartbeat_stale(fresh, 3600, now)
    assert is_heartbeat_stale(old, 3600, now)
    assert is_heartbeat_stale(None, 10, now)


def test_recover_attempts() -> None:
    assert recover_status(0) == "queued"
    assert recover_status(2) == "queued"
    assert recover_status(3) == "failed"
