# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import re
from pathlib import Path

_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9]{8,})|(Bearer\s+\S+)|(api[_-]?key\s*[:=]\s*\S+)", re.I)


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        if _SECRET_RE.search(msg):
            record.msg = "[redacted log line]"
            record.args = ()
        return True


def setup_logging(log_dir: Path | None = None) -> None:
    redactor = _RedactFilter()
    process_root = logging.getLogger()
    if not any(isinstance(f, _RedactFilter) for f in process_root.filters):
        process_root.addFilter(redactor)
    for existing in process_root.handlers:
        if not any(isinstance(f, _RedactFilter) for f in existing.filters):
            existing.addFilter(redactor)
    root = logging.getLogger("huiyiku")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler: logging.Handler = logging.StreamHandler()
    handler.setFormatter(fmt)
    handler.addFilter(redactor)
    root.addHandler(handler)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "huiyiku.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)
