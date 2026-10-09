# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""Build huiyiku-windows-x64.zip and optionally huiyiku-asr-windows-x64.zip."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from release_zip import check_asr_zip, check_ui_zip  # noqa: E402

WORK = ROOT / "packaging" / "work"
DIST = ROOT / "packaging" / "dist"


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sums(paths: list[Path], dist: Path | None = None) -> Path:
    """合并式写入 SHA256SUMS：只更新本次构建的文件，保留其余既有条目。

    UI 与 ASR zip 分两次构建（--ui / --asr 各自独立跑），覆盖式写入会把
    上一个 zip 的哈希抹掉——发布时校验会缺行。
    """
    dist = dist or DIST
    dist.mkdir(parents=True, exist_ok=True)
    out = dist / "SHA256SUMS"
    merged: dict[str, str] = {}
    if out.is_file():
        for line in out.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "  " in line:
                digest, name = line.split("  ", 1)
                if (dist / name).is_file():
                    merged[name] = digest
    for p in paths:
        merged[p.name] = sha256_file(p)
    body = "".join(f"{merged[name]}  {name}\n" for name in sorted(merged))
    out.write_text(body, encoding="utf-8", newline="\n")
    return out


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=cwd or ROOT)


# 构建会清空 ui-dist\Huiyiku；用户数据（meeting-data/app.json/webview）若在里面
# 必须先移出、构建后原样放回——2026-09 曾因漏做这步删掉用户数据，脚本级强制保护。
USER_DATA_NAMES = ("meeting-data", "app.json", "webview")


def _protect_user_data(app_dir: Path, backup_root: Path) -> list[Path]:
    backup_root.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for name in USER_DATA_NAMES:
        src = app_dir / name
        if src.exists():
            dst = backup_root / name
            if dst.exists():
                # 回滚已移出的项，避免数据滞留备份目录
                for already in moved:
                    already.replace(app_dir / already.name)
                raise SystemExit(f"备份位置已存在，请先处理: {dst}")
            src.replace(dst)
            moved.append(dst)
            print(f"已保护用户数据: {name} -> {dst}")
    return moved


def _restore_user_data(app_dir: Path, backup_root: Path, moved: list[Path]) -> None:
    for dst in moved:
        src = app_dir / dst.name
        if src.exists() and dst.name == "meeting-data":
            print(f"警告: 构建产生了新 {dst.name}，保留构建产物版本于 {dst}.build")
            src.replace(backup_root / (dst.name + ".build"))
        dst.replace(app_dir / dst.name)
        print(f"已还原用户数据: {dst.name}")


def build_ui() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    app_dir = WORK / "ui-dist" / "Huiyiku"
    backup_root = WORK / "userdata-backup"
    moved = _protect_user_data(app_dir, backup_root) if app_dir.exists() else []
    try:
        return _build_ui_inner()
    finally:
        if moved:
            _restore_user_data(app_dir, backup_root, moved)


def _build_ui_inner() -> Path:
    spec = WORK / "huiyiku.spec"
    spec.write_text(_ui_spec(), encoding="utf-8")
    _run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(WORK / "ui-dist"),
            "--workpath",
            str(WORK / "ui-build"),
            str(spec),
        ]
    )
    built = WORK / "ui-dist" / "Huiyiku"
    exe = built / "Huiyiku.exe"
    if not exe.is_file():
        raise SystemExit(f"PyInstaller did not produce {exe}")
    stage = WORK / "ui-stage" / "Huiyiku"
    if stage.parent.exists():
        shutil.rmtree(stage.parent)
    stage.mkdir(parents=True)
    shutil.copytree(built, stage, dirs_exist_ok=True)
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, stage / name)
    _copy_licenses(stage)
    shutil.copy2(ROOT / "packaging" / "README.txt", stage / "README.txt")
    ffmpeg_dir = stage / "ffmpeg"
    ffmpeg_dir.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "packaging" / "ffmpeg" / "README.txt", ffmpeg_dir / "README.txt")
    _write_sbom("ui")
    zip_path = DIST / "huiyiku-windows-x64.zip"
    _zip_dir(stage.parent, zip_path)
    errors = check_ui_zip(zip_path)
    if errors:
        raise SystemExit("UI zip failed checks:\n" + "\n".join(errors))
    return zip_path


def build_asr() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    spec = WORK / "huiyiku-asr.spec"
    spec.write_text(_asr_spec(), encoding="utf-8")
    _run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(WORK / "asr-dist"),
            "--workpath",
            str(WORK / "asr-build"),
            str(spec),
        ]
    )
    built = WORK / "asr-dist" / "huiyiku-asr"
    exe = built / "huiyiku-asr.exe"
    if not exe.is_file():
        raise SystemExit(f"PyInstaller did not produce {exe}")
    stage = WORK / "asr-stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    # 直接平铺到 zip 根：解压到 asr\ 后应看到 asr\huiyiku-asr.exe（asr_ready 按此判断）
    shutil.copytree(built, stage, dirs_exist_ok=True)
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, stage / name)
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES_ASR.md", stage / "THIRD_PARTY_NOTICES_ASR.md")
    _copy_licenses(stage, asr=True)
    _write_sbom("asr")
    (stage / "INSTALL.txt").write_text(
        "解压本 zip 到 Huiyiku.exe 同级的 asr\\ 目录（源码开发则解压到仓库根目录 asr\\）\n"
        "最终应能看到 asr\\huiyiku-asr.exe\n"
        "权重若未打进本包，首次转写会按 docs/model-manifest.json 下载到 asr\\models\\\n",
        encoding="utf-8",
    )
    zip_path = DIST / "huiyiku-asr-windows-x64.zip"
    _zip_dir(stage, zip_path)
    errors = check_asr_zip(zip_path)
    if errors:
        raise SystemExit("ASR zip failed checks:\n" + "\n".join(errors))
    return zip_path


def _copy_licenses(stage: Path, *, asr: bool = False) -> None:
    """把许可正文拷进发布包（含 _upstream 出处说明）。

    asr=True 时用 licenses-asr/（.venv-asr 环境生成，含 torch 等正文），
    在 zip 内统一落在 licenses/ 目录。
    """
    src = ROOT / ("licenses-asr" if asr else "licenses")
    if not src.is_dir():
        hint = (
            ".venv-asr\\Scripts\\python.exe scripts/collect_licenses.py --mode asr"
            if asr
            else "scripts/collect_licenses.py"
        )
        raise SystemExit(f"{src.name}/ missing; run {hint} first")
    shutil.copytree(src, stage / "licenses", dirs_exist_ok=True)


def _write_sbom(scope: str) -> None:
    """生成 CycloneDX SBOM 到 packaging/dist/。

    ASR 侧用 --installed：torch / torchaudio 不在 requirements-asr.txt 里，
    只能枚举当前解释器（须用 .venv-asr 运行本脚本）已安装的发行版。
    """
    cmd = [sys.executable, str(ROOT / "scripts" / "make_sbom.py"), "--scope", scope]
    if scope == "asr":
        cmd += ["--lock", "requirements-asr.txt", "--installed"]
    _run(cmd)


def _zip_dir(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in src.rglob("*"):
            if path.is_dir():
                continue
            rel = path.relative_to(src).as_posix()
            zf.write(path, arcname=rel)


def _ui_spec() -> str:
    root = ROOT.as_posix()
    return f"""
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = collect_data_files("jieba")
tk_datas, tk_binaries, tk_hidden = collect_all("tkinter")
datas += tk_datas
# python-docx 自带默认模板等数据文件，漏了冻结包里生成 .docx 会崩
datas += collect_data_files("docx")
datas += [
    (r"{root}/web/dist", "web/dist"),
    (r"{root}/src/huiyiku/db/schema.sql", "huiyiku/db"),
    (r"{root}/LICENSE", "."),
    (r"{root}/NOTICE", "."),
    (r"{root}/THIRD_PARTY_NOTICES.md", "."),
]

a = Analysis(
    [r"{root}/src/huiyiku/__main__.py"],
    pathex=[r"{root}/src"],
    binaries=tk_binaries,
    datas=datas,
    hiddenimports=[
        "tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        *tk_hidden,
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "sqlalchemy.dialects.sqlite",
        "anyio._backends._asyncio",
        "multipart",
        "jieba",
        "webview",
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "pythonnet",
        "clr_loader",
        "bottle",
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=["torch", "torchvision", "torchaudio", "funasr", "modelscope", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Huiyiku",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Huiyiku",
)
"""


def _asr_spec() -> str:
    root = ROOT.as_posix()
    return f"""
# -*- mode: python ; coding: utf-8 -*-
# funasr/modelscope 运行时要读包内数据文件（version.txt 等），
# 仅 hiddenimports 不够，必须 collect_all 收齐 datas/binaries。
from PyInstaller.utils.hooks import collect_all

asr_datas, asr_binaries, asr_hidden = [], [], []
for _pkg in ("funasr", "modelscope"):
    _d, _b, _h = collect_all(_pkg)
    asr_datas += _d
    asr_binaries += _b
    asr_hidden += _h

a = Analysis(
    [r"{root}/src/huiyiku_asr/__main__.py"],
    pathex=[r"{root}/src"],
    binaries=asr_binaries,
    datas=asr_datas,
    hiddenimports=["funasr", "modelscope", "torchaudio", *asr_hidden],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="huiyiku-asr",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="huiyiku-asr",
)
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package Windows release zips")
    parser.add_argument("--ui", action="store_true", help="Build UI zip (no torch)")
    parser.add_argument("--asr", action="store_true", help="Build ASR zip (contains torch)")
    args = parser.parse_args(argv)
    if not args.ui and not args.asr:
        args.ui = True
    built: list[Path] = []
    if args.ui:
        built.append(build_ui())
        print("UI zip:", built[-1])
    if args.asr:
        built.append(build_asr())
        print("ASR zip:", built[-1])
    sums = write_sums(built)
    print("SHA256SUMS:", sums)
    print(sums.read_text(encoding="utf-8"))
    print("version", project_version())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
