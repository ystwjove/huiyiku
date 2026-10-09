# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

from huiyiku.timeutil import now_iso

SCHEMA_VERSION = 1
UNGROUPED_NAME = "未分组"


def _schema_sql() -> str:
    return resources.files("huiyiku.db").joinpath("schema.sql").read_text(encoding="utf-8")


def migrate(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_schema_sql())
        row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
        elif int(row[0]) > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {row[0]} is newer than app {SCHEMA_VERSION}")
        _seed_ungrouped(conn)
        conn.commit()
    finally:
        conn.close()


def _seed_ungrouped(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT id FROM groups WHERE is_system=1 AND name=?", (UNGROUPED_NAME,)
    ).fetchone()
    if row is None:
        ts = now_iso()
        conn.execute(
            "INSERT INTO groups(name, description, color, is_system, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (UNGROUPED_NAME, "系统内置，不可删除", "#6b7280", 1, ts, ts),
        )


def ungrouped_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM groups WHERE is_system=1 AND name=?", (UNGROUPED_NAME,)
    ).fetchone()
    if row is None:
        raise RuntimeError("system group missing")
    return int(row[0])
