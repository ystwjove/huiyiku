# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""Validate GitHub Release zip layout against docs/开发方案.md 2.10 / 2.11."""

from __future__ import annotations

import zipfile
from pathlib import Path

UI_REQUIRED = (
    "Huiyiku/Huiyiku.exe",
    "Huiyiku/LICENSE",
    "Huiyiku/NOTICE",
    "Huiyiku/THIRD_PARTY_NOTICES.md",
    "Huiyiku/README.txt",
    # 再分发组件的许可正文（MIT/BSD 要求随二进制附声明）
    "Huiyiku/licenses/python-packages.txt",
    "Huiyiku/licenses/cpython-and-native-libs.txt",
    "Huiyiku/licenses/frontend.txt",
)
ASR_NOTICES = "third_party_notices_asr.md"
ASR_REQUIRED = (
    "licenses/python-packages.txt",
    "licenses/cpython-and-native-libs.txt",
)
UI_FORBIDDEN_SUBSTR = (
    "portable.txt",
    "secrets.json",
    "meeting-data",
    ".env",
    "torch",
    "funasr",
    "modelscope",
    "numpy",
)
UI_FORBIDDEN_SUFFIX = (".wav", ".mp3", ".m4a", ".mp4", ".aac", ".flac")
UI_MAX_BYTES = 120 * 1024 * 1024


def _norm(name: str) -> str:
    return name.replace("\\", "/")


def iter_zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return [_norm(n) for n in zf.namelist()]


def check_ui_zip(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"missing {path}"]
    size = path.stat().st_size
    if size > UI_MAX_BYTES:
        errors.append(f"UI zip is {size} bytes; plan target < 120MB")
    names = iter_zip_names(path)
    for req in UI_REQUIRED:
        if req not in names:
            errors.append(f"UI zip missing {req}")
    if not any(n.startswith("Huiyiku/ffmpeg/") for n in names):
        errors.append("UI zip missing ffmpeg/ directory")
    for name in names:
        lowered = name.lower()
        for bad in UI_FORBIDDEN_SUBSTR:
            if bad in lowered:
                errors.append(f"UI zip contains forbidden path {name}")
                break
        if lowered.endswith(UI_FORBIDDEN_SUFFIX):
            errors.append(f"UI zip contains media {name}")
    return errors


def check_asr_zip(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"missing {path}"]
    names = [n.lower() for n in iter_zip_names(path)]
    if "huiyiku-asr.exe" not in names:
        errors.append("ASR zip missing root-level huiyiku-asr.exe")
    if ASR_NOTICES not in names:
        errors.append(f"ASR zip missing {ASR_NOTICES} (third-party attribution)")
    for req in ASR_REQUIRED:
        if req not in names:
            errors.append(f"ASR zip missing {req} (license texts)")
    for name in names:
        if name.startswith("huiyiku-asr/"):
            errors.append(f"ASR zip must not nest {name} (exe belongs at zip root)")
        if "meeting-data" in name or name.endswith("secrets.json") or name.endswith(".wav"):
            errors.append(f"ASR zip contains forbidden path {name}")
    return errors
