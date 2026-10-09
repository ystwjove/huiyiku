# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from repo_gates import (  # noqa: E402
    FORBIDDEN_LOCK_PACKAGES,
    _license_tokens,
    check_hygiene,
    check_licenses,
    check_lockfile,
    check_notices,
)


def test_lockfile_gate_clean() -> None:
    assert check_lockfile(ROOT) == []


def test_hygiene_gate_clean() -> None:
    assert check_hygiene(ROOT) == []


def test_forbidden_set_covers_plan() -> None:
    for name in ("torch", "funasr", "modelscope", "numpy"):
        assert name in FORBIDDEN_LOCK_PACKAGES


def test_license_gate_clean() -> None:
    assert check_licenses(ROOT) == []


def test_psf_license_expression_maps() -> None:
    assert "PSF" in _license_tokens("PSF-2.0")
    assert "MIT" in _license_tokens("MIT AND PSF-2.0")


def test_notices_gate_clean() -> None:
    assert check_notices(ROOT) == []


def test_notices_gate_detects_missing_package(tmp_path: Path) -> None:
    """锁文件里新增依赖但忘了登记归属，门禁必须报出来。"""
    (tmp_path / "web").mkdir()
    lock = "httpx==0.28.1" + chr(10) + "unregistered-pkg==1.0" + chr(10)
    (tmp_path / "requirements.lock").write_text(lock, encoding="utf-8")
    (tmp_path / "web" / "package-lock.json").write_text('{"packages": {}}', encoding="utf-8")
    (tmp_path / "THIRD_PARTY_NOTICES.md").write_text("httpx is fine", encoding="utf-8")
    errors = check_notices(tmp_path)
    assert any("unregistered-pkg" in e for e in errors)
