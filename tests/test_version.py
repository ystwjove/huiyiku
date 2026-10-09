# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import tomllib
from pathlib import Path

from huiyiku import __version__
from huiyiku_asr import __version__ as asr_version

ROOT = Path(__file__).resolve().parents[1]


def test_version_matches_pyproject() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__
    assert asr_version == __version__
