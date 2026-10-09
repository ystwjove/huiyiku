# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any


def now_iso() -> str:
    """Local-offset ISO 8601 seconds, as used for meeting.occurred_at defaults."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def file_mtime_iso(path) -> str:
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


@contextmanager
def pulse_progress(
    progress: Callable[[dict[str, Any]], None] | None,
    stage: str,
    interval: float = 15.0,
) -> Iterator[None]:
    """Keep job heartbeat_at fresh during a long blocking LLM call."""
    if progress is None:
        yield
        return
    stop = threading.Event()

    def _run() -> None:
        n = 0
        while not stop.wait(interval):
            n += 1
            try:
                progress({"stage": stage, "pulse": n})
            except Exception:
                return

    thread = threading.Thread(target=_run, daemon=True, name="huiyiku-pulse")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
