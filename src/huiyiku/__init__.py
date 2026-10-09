# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""会议库：本机会议录音知识库。"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]


def _version_from_pyproject() -> str:
    """Read [project].version when the package is not installed yet."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    return "0.0.0"


try:
    __version__ = version("huiyiku")
except PackageNotFoundError:
    __version__ = _version_from_pyproject()
