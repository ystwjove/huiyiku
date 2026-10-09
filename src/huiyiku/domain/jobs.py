# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

MEDIA_TYPES = frozenset({"preprocess", "transcribe"})
INFERENCE_TYPES = frozenset({"stats", "embed", "report", "digest"})

DEFAULT_TIMEOUT = {
    "preprocess": 30 * 60,
    "transcribe": 2 * 60 * 60,
    "embed": 60 * 60,
    "report": 45 * 60,
    "digest": 30 * 60,
    "stats": 10 * 60,
}

MEETING_STATUS_FOR_JOB = {
    "preprocess": "preprocessing",
    "transcribe": "transcribing",
    "stats": "indexing",
    "embed": "indexing",
    "report": "report_draft",
}


def idempotency_key(
    meeting_id: int | None,
    job_type: str,
    payload: dict[str, Any],
    model_version: str = "",
) -> str:
    raw = json.dumps(
        {"meeting_id": meeting_id, "type": job_type, "payload": payload, "model": model_version},
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{meeting_id or 0}:{job_type}:{digest}"


def is_heartbeat_stale(
    heartbeat_at: str | None, timeout_seconds: int, now: datetime | None = None
) -> bool:
    if not heartbeat_at:
        return True
    now = now or datetime.now(UTC)
    try:
        hb = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=UTC)
    return now - hb > timedelta(seconds=timeout_seconds)


def recover_status(attempt_count: int, max_attempts: int = 3) -> str:
    if attempt_count + 1 > max_attempts:
        return "failed"
    return "queued"
