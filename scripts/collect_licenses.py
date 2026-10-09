# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""把再分发组件的许可正文收集到 licenses/，随 UI zip 一起分发。

来源全部是本机可核验的文件，不联网、不手工转抄：

- Python 包：各发行版 dist-info 里的 LICENSE / COPYING / NOTICE 文件
- CPython 本体及随其 Windows 构建分发的本地库与内嵌组件（OpenSSL、zlib、
  libffi、expat、libmpdec 等）：基础解释器的 LICENSE.txt，以及官方文档
  license.html 中 "Licenses and Acknowledgements for Incorporated Software"
  章节
- Tcl/Tk：基础解释器 tcl 目录下的 license.terms
- 前端生产依赖：web/node_modules/<pkg>/LICENSE*

用法：
    python scripts/collect_licenses.py            # 重新生成 licenses/
    python scripts/collect_licenses.py --check    # 只校验文件存在且非空

源解释器可用 --python-home 指定；默认取当前解释器的 sys.base_prefix。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from repo_gates import iter_requirement_names  # noqa: E402

OUT_DIR = ROOT / "licenses"
ASR_OUT_DIR = ROOT / "licenses-asr"
# 构建工具不随包分发、也不该出现在许可正文与 SBOM 里（口径与 make_sbom.BUILD_TOOLS 一致）
ASR_SKIP = {"pip", "wheel", "huiyiku", "pyinstaller", "pyinstaller-hooks-contrib"}
PY_HEADER = "许可正文（Python 运行时依赖）\n来源：各发行版 dist-info 内的许可文件\n"
NATIVE_HEADER = (
    "许可正文（CPython 本体与随其 Windows 构建分发的本地库/内嵌组件）\n"
    "来源：基础解释器 LICENSE.txt 与官方文档 license.html 的\n"
    "\"{section}\" 章节\n"
)
FRONTEND_HEADER = "许可正文（前端生产依赖）\n来源：web/node_modules 各包的 LICENSE 文件\n"
MAX_BYTES_PER_PACKAGE = 200_000


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _dist_license_text(dist_name: str) -> tuple[str, list[str]]:
    """取一个发行版 dist-info 里的许可文件正文。

    新式 wheel 把许可放在 dist-info/licenses/ 子目录（PEP 639），老式放在
    dist-info 根，两者都要覆盖，所以按名字递归查找。
    """
    try:
        dist = metadata.distribution(dist_name)
    except metadata.PackageNotFoundError:
        return "", []
    base = Path(dist._path)  # type: ignore[attr-defined]
    texts: list[str] = []
    seen: set[str] = set()
    candidates = [
        p
        for p in base.rglob("*")
        if p.is_file()
        and (
            p.name.upper().startswith("LICENSE")
            or p.name.upper().startswith("COPYING")
            or p.name.upper().startswith("NOTICE")
        )
    ]
    for item in sorted(candidates):
        rel = item.relative_to(base).as_posix()
        if rel.upper() in seen or item.stat().st_size > MAX_BYTES_PER_PACKAGE:
            continue
        seen.add(rel.upper())
        texts.append(f"--- {rel} ---\n{_read(item).rstrip()}\n")
    return "\n".join(texts), [p.relative_to(base).as_posix() for p in candidates]


def _installed_package_names() -> set[str]:
    """当前解释器已安装的发行版（ASR 侧组件不在锁文件里）。"""
    return {
        d.metadata["Name"]
        for d in metadata.distributions()
        if d.metadata and d.metadata.get("Name")
    } - ASR_SKIP


def _upstream_override(dist_name: str, out_dir: Path | None = None) -> str:
    """dist-info 不带许可文件时，取 licenses/_upstream/ 下的正文。

    支持一个包多份正文：`<name>.txt` 与 `<name>__*.txt`（例如 LGPL 依赖的
    简短声明 + LGPL 全文），按文件名排序拼接。有 `.sha256` 边车时逐份校验哈希，
    避免正文被无声改写。
    """
    # 先查本模式目录，再回退到 UI 的 licenses/_upstream（公共组件只存一份）
    bases = [(out_dir or OUT_DIR) / "_upstream"]
    if bases[0] != OUT_DIR / "_upstream":
        bases.append(OUT_DIR / "_upstream")
    for base in bases:
        for candidate in (dist_name, dist_name.replace("-", "_")):
            files = [base / f"{candidate}.txt"]
            files += sorted(base.glob(f"{candidate}__*.txt"))
            files = [f for f in files if f.is_file()]
            if not files:
                continue
            chunks: list[str] = []
            for text_file in files:
                side = Path(str(text_file) + ".sha256")
                if side.is_file():
                    expected = side.read_text(encoding="utf-8").split()[0].strip()
                    actual = hashlib.sha256(text_file.read_bytes()).hexdigest()
                    if expected != actual:
                        return f"__HASH_MISMATCH__{text_file.name}"
                chunks.append(f"--- {text_file.name} ---\n{_read(text_file).rstrip()}")
            return "\n".join(chunks) + "\n"
    return ""


def _python_packages_section(
    names: set[str] | None = None, out_dir: Path | None = None
) -> tuple[str, list[str]]:
    if names is None:
        names = set()
        for rel in ("requirements.lock",):
            names |= iter_requirement_names(ROOT / rel)
        names.discard("huiyiku")
    out = out_dir or OUT_DIR
    parts = [PY_HEADER]
    missing: list[str] = []
    for name in sorted(names):
        text, _ = _dist_license_text(name)
        version = ""
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = "(not installed)"
        if text.strip():
            parts.append(f"===== {name} {version} =====\n{text}")
            continue
        override = _upstream_override(name, out)
        if override.startswith("__HASH_MISMATCH__"):
            missing.append(f"{name}: {override.removeprefix('__HASH_MISMATCH__')} 与边车哈希不符")
            parts.append(f"===== {name} {version}: 覆盖正文哈希校验失败 =====\n")
            continue
        if override.strip():
            parts.append(
                f"===== {name} {version}（dist-info 无许可文件，取 licenses/_upstream/）=====\n"
                f"{override.rstrip()}\n"
            )
            continue
        missing.append(name)
        parts.append(f"===== {name} {version}: 未在 dist-info 或 _upstream 中找到许可正文 =====\n")
    return "\n".join(parts), missing


def _cpython_and_native_section(python_home: Path) -> tuple[str, list[str]]:
    missing: list[str] = []
    parts: list[str] = []
    lic = python_home / "LICENSE.txt"
    if lic.is_file():
        parts.append(f"===== CPython（PSF-2.0）=====\n{_read(lic).rstrip()}\n")
    else:
        missing.append(str(lic))

    section_title = "Licenses and Acknowledgements for Incorporated Software"
    doc = python_home / "Doc" / "html" / "license.html"
    if doc.is_file():
        html = _read(doc)
        idx = html.find(section_title)
        if idx >= 0:
            body = html[idx:]
            body = re.sub(r"(?is)<(script|style).*?</\1>", "", body)
            body = re.sub(r"(?i)<br\s*/?>", "\n", body)
            body = re.sub(r"(?i)</(p|div|li|h[1-6]|pre)>", "\n", body)
            body = re.sub(r"<[^>]+>", "", body)
            body = body.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
            parts.append(f"===== {section_title} =====\n{body}\n")
        else:
            missing.append(f"{doc}（未找到 {section_title} 章节）")
    else:
        missing.append(str(doc))

    for rel in (
        Path("tcl") / "tcl8.6" / "license.terms",
        Path("tcl") / "tk8.6" / "license.terms",
    ):
        p = python_home / rel
        if p.is_file():
            parts.append(f"===== Tcl/Tk（BSD 风格，{rel.as_posix()}）=====\n{_read(p).rstrip()}\n")
    header = NATIVE_HEADER.format(section=section_title)
    return header + "\n".join(parts), missing


def _frontend_section() -> tuple[str, list[str]]:
    lock = ROOT / "web" / "package-lock.json"
    if not lock.is_file():
        return FRONTEND_HEADER, ["web/package-lock.json"]
    data = json.loads(_read(lock))
    parts = [FRONTEND_HEADER]
    missing: list[str] = []
    for name, meta in sorted((data.get("packages") or {}).items()):
        if not name.startswith("node_modules/") or meta.get("dev"):
            continue
        pkg = name.split("node_modules/")[-1]
        pkg_dir = ROOT / "web" / name
        text = ""
        for item in sorted(pkg_dir.iterdir()) if pkg_dir.is_dir() else []:
            upper = item.name.upper()
            if item.is_file() and (upper.startswith("LICENSE") or upper.startswith("COPYING")):
                text = _read(item).rstrip()
                break
        version = meta.get("version", "")
        license_id = meta.get("license", "")
        if text:
            parts.append(f"===== {pkg} {version} ({license_id}) =====\n{text}\n")
        else:
            missing.append(pkg)
            parts.append(f"===== {pkg} {version} ({license_id}): 未找到 LICENSE 文件 =====\n")
    return "\n".join(parts), missing


def collect_asr(python_home: Path, out_dir: Path = ASR_OUT_DIR) -> list[str]:
    """ASR 模式：枚举当前解释器已安装的发行版（torch/torchaudio 不在锁文件里）。

    用 .venv-asr 的解释器运行，产出 licenses-asr/：
      .venv-asr\\Scripts\\python.exe scripts/collect_licenses.py --mode asr
    """
    out_dir.mkdir(exist_ok=True)
    missing: list[str] = []
    py_text, py_missing = _python_packages_section(_installed_package_names(), out_dir)
    native_text, native_missing = _cpython_and_native_section(python_home)
    (out_dir / "python-packages.txt").write_text(py_text, encoding="utf-8", newline="\n")
    (out_dir / "cpython-and-native-libs.txt").write_text(
        native_text, encoding="utf-8", newline="\n"
    )
    for name in py_missing:
        missing.append(f"python package without license file in dist-info: {name}")
    missing.extend(f"missing native source: {m}" for m in native_missing)
    return missing


def collect(python_home: Path) -> list[str]:
    OUT_DIR.mkdir(exist_ok=True)
    missing: list[str] = []
    py_text, py_missing = _python_packages_section()
    native_text, native_missing = _cpython_and_native_section(python_home)
    fe_text, fe_missing = _frontend_section()
    (OUT_DIR / "python-packages.txt").write_text(py_text, encoding="utf-8", newline="\n")
    (OUT_DIR / "cpython-and-native-libs.txt").write_text(
        native_text, encoding="utf-8", newline="\n"
    )
    (OUT_DIR / "frontend.txt").write_text(fe_text, encoding="utf-8", newline="\n")
    for name in py_missing:
        missing.append(f"python package without license file in dist-info: {name}")
    missing.extend(f"missing native source: {m}" for m in native_missing)
    missing.extend(f"frontend package without LICENSE: {m}" for m in fe_missing)
    return missing


def _check_dir(out_dir: Path, names: tuple[str, ...]) -> list[str]:
    errors: list[str] = []
    for name in names:
        p = out_dir / name
        if not p.is_file():
            errors.append(f"missing {p.relative_to(ROOT)}")
        elif p.stat().st_size < 200:
            errors.append(f"{p.relative_to(ROOT)} looks empty")
        else:
            text = _read(p)
            for marker in ("未在 dist-info", "未找到 LICENSE 文件", "哈希校验失败"):
                if marker in text:
                    errors.append(f"{p.relative_to(ROOT)} 含缺失正文的条目（{marker}）")
                    break
    return errors


def _section_names(text: str) -> set[str]:
    """解析生成文件里的 `===== name version =====` 章节名。"""
    return set(re.findall(r"^===== (\S+) ", text, flags=re.M))


def check(mode: str = "ui") -> list[str]:
    errors: list[str]
    if mode == "asr":
        errors = _check_dir(ASR_OUT_DIR, ("python-packages.txt", "cpython-and-native-libs.txt"))
    else:
        errors = _check_dir(
            OUT_DIR, ("python-packages.txt", "cpython-and-native-libs.txt", "frontend.txt")
        )
    # 防漂移：正文章节必须覆盖各自口径下的每个包，否则依赖更新后忘了
    # 重新生成 licenses/ 时门禁照样绿（CI 只跑 --check 不重新生成）。
    # ui 比对 requirements.lock；asr 比对当前解释器已安装的发行版
    # （须用 .venv-asr 运行本命令）
    if mode == "asr":
        expected = _installed_package_names()
    else:
        expected = {n for n in iter_requirement_names(ROOT / "requirements.lock") if n != "huiyiku"}
    text_path = ASR_OUT_DIR if mode == "asr" else OUT_DIR
    text = _read(text_path / "python-packages.txt")
    have = _section_names(text)
    for name in sorted(expected - have):
        errors.append(
            f"licenses 缺 {name} 的正文章节：依赖更新后须重新运行 "
            f"scripts/collect_licenses.py{' --mode asr' if mode == 'asr' else ''}"
        )
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python-home", default=sys.base_prefix)
    ap.add_argument("--check", action="store_true", help="只校验，不重新生成")
    ap.add_argument("--mode", choices=("ui", "asr"), default="ui", help="asr 枚举本环境已安装的发行版")
    args = ap.parse_args()
    if args.check:
        errors = check(args.mode)
        for e in errors:
            print(e)
        print("licenses: " + ("FAIL" if errors else "ok"))
        return 1 if errors else 0
    if args.mode == "asr":
        missing = collect_asr(Path(args.python_home))
        target = ASR_OUT_DIR
    else:
        missing = collect(Path(args.python_home))
        target = OUT_DIR
    for m in missing:
        print("warning:", m)
    print(f"licenses written to {target.relative_to(ROOT)}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
