# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from huiyiku.api.app import create_app
from huiyiku.config import load_runtime


@pytest.fixture()
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app_json = tmp_path / "app.json"
    data_dir = tmp_path / "meeting-data"
    asr_home = tmp_path / "asr"
    app_json.write_text(
        json.dumps(
            {
                "data_dir": str(data_dir),
                "host": "127.0.0.1",
                "port": 8787,
                "ffmpeg_path": "",
                "asr_home": str(asr_home),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HUIYIKU_APP_JSON", str(app_json))
    monkeypatch.delenv("HUIYIKU_ACCESS_TOKEN", raising=False)
    return load_runtime()


@pytest.fixture()
def client(runtime) -> TestClient:
    return TestClient(create_app(runtime))
