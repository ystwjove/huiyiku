# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import zipfile
from pathlib import Path

from release_zip import check_asr_zip, check_ui_zip


def _write_zip(path: Path, names: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, b"x")


def test_ui_zip_happy(tmp_path: Path) -> None:
    z = tmp_path / "ui.zip"
    _write_zip(
        z,
        [
            "Huiyiku/Huiyiku.exe",
            "Huiyiku/LICENSE",
            "Huiyiku/NOTICE",
            "Huiyiku/THIRD_PARTY_NOTICES.md",
            "Huiyiku/README.txt",
            "Huiyiku/ffmpeg/README.txt",
            "Huiyiku/_internal/base_library.zip",
            "Huiyiku/licenses/python-packages.txt",
            "Huiyiku/licenses/cpython-and-native-libs.txt",
            "Huiyiku/licenses/frontend.txt",
        ],
    )
    assert check_ui_zip(z) == []


def test_ui_zip_rejects_torch_and_portable(tmp_path: Path) -> None:
    z = tmp_path / "ui.zip"
    _write_zip(
        z,
        [
            "Huiyiku/Huiyiku.exe",
            "Huiyiku/LICENSE",
            "Huiyiku/NOTICE",
            "Huiyiku/THIRD_PARTY_NOTICES.md",
            "Huiyiku/README.txt",
            "Huiyiku/ffmpeg/README.txt",
            "Huiyiku/portable.txt",
            "Huiyiku/_internal/torch/__init__.py",
        ],
    )
    errors = check_ui_zip(z)
    assert any("portable" in e for e in errors)
    assert any("torch" in e for e in errors)


def test_ui_zip_requires_license_texts(tmp_path: Path) -> None:
    """再分发组件的许可正文必须随包：缺了要拦住。"""
    z = tmp_path / "ui.zip"
    _write_zip(
        z,
        [
            "Huiyiku/Huiyiku.exe",
            "Huiyiku/LICENSE",
            "Huiyiku/NOTICE",
            "Huiyiku/THIRD_PARTY_NOTICES.md",
            "Huiyiku/README.txt",
            "Huiyiku/ffmpeg/README.txt",
        ],
    )
    errors = check_ui_zip(z)
    assert any("licenses/" in e for e in errors)


def test_asr_zip_requires_exe(tmp_path: Path) -> None:
    z = tmp_path / "asr.zip"
    _write_zip(z, ["LICENSE"])
    assert check_asr_zip(z)
    _write_zip(z, ["huiyiku-asr.exe", "LICENSE"])
    assert check_asr_zip(z)  # 缺第三方声明与许可正文
    _write_zip(
        z,
        [
            "huiyiku-asr.exe",
            "LICENSE",
            "THIRD_PARTY_NOTICES_ASR.md",
            "licenses/python-packages.txt",
            "licenses/cpython-and-native-libs.txt",
        ],
    )
    assert check_asr_zip(z) == []


def test_asr_zip_rejects_nested_layout(tmp_path: Path) -> None:
    # 文档要求解压到 asr\ 后直接看到 huiyiku-asr.exe；嵌套目录会导致 asr_ready 误判未安装
    z = tmp_path / "asr.zip"
    _write_zip(z, ["huiyiku-asr/huiyiku-asr.exe", "LICENSE"])
    errors = check_asr_zip(z)
    assert any("root-level" in e for e in errors)
    assert any("nest" in e for e in errors)


def test_write_sums_merges_instead_of_overwriting(tmp_path: Path) -> None:
    """UI 与 ASR zip 分两次构建：第二次写哈希不能抹掉第一次的条目。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from package_windows import write_sums

    a = tmp_path / "huiyiku-windows-x64.zip"
    b = tmp_path / "huiyiku-asr-windows-x64.zip"
    a.write_bytes(b"ui")
    b.write_bytes(b"asr")

    first = write_sums([a], dist=tmp_path)
    lines1 = first.read_text(encoding="utf-8").splitlines()
    assert len(lines1) == 1 and lines1[0].endswith("huiyiku-windows-x64.zip")

    second = write_sums([b], dist=tmp_path)
    lines2 = second.read_text(encoding="utf-8").splitlines()
    names = [ln.split("  ", 1)[1] for ln in lines2]
    assert names == ["huiyiku-asr-windows-x64.zip", "huiyiku-windows-x64.zip"]

    # 陈旧条目（对应文件已不存在）应被清掉，不残留误导校验
    (tmp_path / "huiyiku-windows-x64.zip").unlink()
    third = write_sums([b], dist=tmp_path)
    assert third.read_text(encoding="utf-8").count("  ") == 1
