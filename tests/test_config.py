# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest

from huiyiku.config import load_runtime
from huiyiku.media.ffmpeg import find_ffmpeg
from huiyiku.paths import (
    default_app_json_path,
    default_asr_home,
    default_data_dir,
    is_loopback_host,
)


def test_recordings_default_to_app_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from huiyiku import paths

    monkeypatch.setattr(paths, "executable_dir", lambda: tmp_path)
    monkeypatch.delenv("HUIYIKU_APP_JSON", raising=False)
    assert default_data_dir() == tmp_path / "meeting-data"
    assert default_app_json_path() == tmp_path / "app.json"
    monkeypatch.delenv("HUIYIKU_ASR_HOME", raising=False)
    assert default_asr_home() == tmp_path / "asr"


def test_asr_ready_detects_project_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from huiyiku.asr import client

    # 隔离机器状态：开发机存在 .venv-asr 或设置 HUIYIKU_ASR_PYTHON 时不算未安装
    monkeypatch.setattr(client, "_dev_asr_python", lambda: None)
    monkeypatch.delenv("HUIYIKU_ASR_PYTHON", raising=False)

    assert client.asr_ready(tmp_path) is False
    (tmp_path / "huiyiku-asr.exe").write_bytes(b"")
    assert client.asr_ready(tmp_path) is True


def test_loopback_hosts() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.2")


def test_legacy_open_host_turns_share_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """旧的 app.json host=0.0.0.0、还没有开关字段时，启动即视为共享已开启。"""
    app_json = tmp_path / "app.json"
    app_json.write_text(
        json.dumps(
            {
                "data_dir": str(tmp_path / "data"),
                "host": "0.0.0.0",
                "port": 8787,
                "ffmpeg_path": "",
                "asr_home": str(tmp_path / "asr"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HUIYIKU_APP_JSON", str(app_json))
    with caplog.at_level("WARNING", logger="huiyiku.config"):
        rt = load_runtime()
    assert rt.settings.share_lan is True
    assert any("内网共享已开启" in r.message for r in caplog.records)
    saved = json.loads((tmp_path / "data" / "settings.json").read_text(encoding="utf-8"))
    assert saved["share_lan"] is True


def test_ffmpeg_lookup_order(tmp_path: Path) -> None:
    configured = tmp_path / "bin" / "ffmpeg.exe"
    configured.parent.mkdir()
    configured.write_text("", encoding="utf-8")
    sibling_root = tmp_path / "app"
    sibling = sibling_root / "ffmpeg" / "ffmpeg.exe"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("", encoding="utf-8")
    assert find_ffmpeg(str(configured), search_root=sibling_root) == str(configured)
    assert find_ffmpeg("", search_root=sibling_root) == str(sibling)
    assert find_ffmpeg("", search_root=tmp_path / "empty", path_env="") is None
