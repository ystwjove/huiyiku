# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from huiyiku.db.migrate import migrate


def _on_connect(dbapi_conn, _connection_record) -> None:
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA busy_timeout=30000")
    dbapi_conn.execute("PRAGMA foreign_keys=ON")
    dbapi_conn.execute("PRAGMA synchronous=NORMAL")


def make_engine(db_path: Path) -> Engine:
    migrate(db_path)
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
    )
    event.listen(engine, "connect", _on_connect)
    return engine


def connect(db_path: Path) -> sqlite3.Connection:
    migrate(db_path)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def wal_checkpoint(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass  # 有活跃写者时 TRUNCATE 失败可接受（下次退出再试）
    finally:
        conn.close()
