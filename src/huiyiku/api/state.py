# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from huiyiku import __version__
from huiyiku.config import RuntimeContext
from huiyiku.db.session import make_engine, session_factory


@dataclass
class AppState:
    runtime: RuntimeContext
    engine: Engine
    Session: sessionmaker
    started_at: str
    version: str = field(default_factory=lambda: __version__)

    @classmethod
    def from_runtime(cls, runtime: RuntimeContext) -> AppState:
        engine = make_engine(runtime.layout["db"])
        return cls(
            runtime=runtime,
            engine=engine,
            Session=session_factory(engine),
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
