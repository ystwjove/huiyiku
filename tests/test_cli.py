# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from huiyiku.__main__ import build_parser
from huiyiku_asr.__main__ import main as asr_main


def test_app_help_exits_zero() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0


def test_parser_serve_and_worker_exclusive() -> None:
    from huiyiku.__main__ import main

    with pytest.raises(SystemExit):
        main(["--serve", "--worker"])


def test_asr_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        asr_main(["--help"])
    assert exc.value.code == 0


def test_asr_transcribe_missing_wav() -> None:
    assert asr_main(["transcribe", "--wav", "missing-file.wav"]) == 2


def test_asr_model_dir_defaults_to_project_asr(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from huiyiku_asr.__main__ import resolve_model_dir

    monkeypatch.delenv("HUIYIKU_ASR_MODEL_DIR", raising=False)
    monkeypatch.delenv("HUIYIKU_ASR_HOME", raising=False)
    monkeypatch.setattr("huiyiku_asr.__main__.sys.frozen", False, raising=False)
    got = resolve_model_dir(None)
    assert got.as_posix().endswith("asr/models")
    monkeypatch.setenv("HUIYIKU_ASR_HOME", str(tmp_path / "bundle"))
    assert resolve_model_dir(None) == tmp_path / "bundle" / "models"
