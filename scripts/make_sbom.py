# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""生成 CycloneDX SBOM（JSON），覆盖再分发到发布包里的组件。

覆盖范围（与 docs/open-source-compliance-checklist.md 的发布门禁一致）：

1. Python 运行时依赖：从 requirements.lock 取，许可与版本取自本机 dist-info
2. 前端生产依赖：从 web/package-lock.json 取非 dev 条目
3. 随 CPython Windows 构建分发的本地库与内嵌组件：清单是静态的，因为它们的
   存在由 CPython 构建决定，pip 元数据里查不到（正文见 licenses/）

用法：
    python scripts/make_sbom.py [-o packaging/dist/sbom-huiyiku-<version>.cdx.json]

纯标准库，不联网；同一份锁文件生成的结果确定（时间戳除外）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from repo_gates import _license_tokens, iter_requirement_names  # noqa: E402

SPEC_VERSION = "1.5"
BUILD_TOOLS = frozenset({"pyinstaller", "pyinstaller-hooks-contrib", "pip", "wheel"})
# 字面形态合法的 SPDX 标识（含 copyleft：ASR 侧确实存在 LGPL，必须如实登记）
SPDX_LITERAL = re.compile(
    r"^(MIT|MIT-0|ISC|0BSD|Unlicense|Zlib|PSF-2\.0|MPL-2\.0|Apache-2\.0|BSD-[23]-Clause"
    r"|(?:LGPL|GPL|AGPL)-[0-9.]+(?:-or-later|-only)?)$"
)

# 随 CPython Windows 构建分发的组件：pip/Node 元数据查不到，须显式登记。
# license 字段用 SPDX 标识；正文来源见 licenses/cpython-and-native-libs.txt。
NATIVE_COMPONENTS: tuple[dict[str, str], ...] = (
    {
        "name": "CPython",
        "version": "3.13",
        "license": "PSF-2.0",
        "note": "Python 运行时本体，冻结为 python313.dll",
    },
    {
        "name": "OpenSSL",
        "version": "3.x",
        "license": "Apache-2.0",
        "note": "libcrypto-3.dll / libssl-3.dll",
    },
    {"name": "zlib", "version": "1.3", "license": "Zlib", "note": "zlib1.dll"},
    {"name": "libffi", "version": "3.4", "license": "MIT", "note": "libffi-8.dll"},
    {"name": "Tcl/Tk", "version": "8.6", "license": "TCL", "note": "tkinter 弹窗与文件选择框"},
    {"name": "SQLite", "version": "3.x", "license": "blessing", "note": "sqlite3.dll，public domain"},
    {
        "name": "Microsoft Visual C++ Runtime",
        "version": "14.x",
        "license": "proprietary-redistributable",
        "note": "VCRUNTIME140.dll / VCRUNTIME140_1.dll，按 VS 再分发条款",
    },
    {
        "name": "Microsoft WebView2 SDK",
        "version": "1.x",
        "license": "proprietary-redistributable",
        "note": "webview/lib 下的 WebView2 DLL，按 WebView2 SDK 条款",
    },
)


def _spdx_id(dist_name: str) -> tuple[str, str]:
    """返回 (spdx 标识, 原始声明)。取不到干净标识时回落到原始字符串。"""
    try:
        meta = metadata.metadata(dist_name)
    except metadata.PackageNotFoundError:
        return "", ""
    raw = (meta.get("License-Expression") or meta.get("License") or "").strip()
    if not raw or raw.upper() in {"UNKNOWN", "DUAL LICENSE"} or len(raw) > 60:
        classifiers = [
            c.split(" :: ")[-1]
            for c in meta.get_all("Classifier") or []
            if c.startswith("License ::")
        ]
        raw = " OR ".join(classifiers) if classifiers else raw
    # 分类器名 → SPDX：只映射无歧义的；"BSD License" 之类保留原样（BSD-2 与
    # BSD-3 无法从分类器区分，硬映射会写错）
    classifier_ids = {
        "Apache Software License": "Apache-2.0",
        "MIT License": "MIT",
        "MIT No Attribution License (MIT-0)": "MIT-0",
        "ISC License (ISCL)": "ISC",
        "The Unlicense (Unlicense)": "Unlicense",
        "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
        "Python Software Foundation License": "PSF-2.0",
        "BSD 2-Clause License": "BSD-2-Clause",
        "BSD 3-Clause License": "BSD-3-Clause",
        "Zero-Clause BSD / Free Public License 0.0.0 (0BSD)": "0BSD",
    }
    for part in (p for p in raw.split(" OR ") if p.strip()):
        if part.strip() in classifier_ids:
            return classifier_ids[part.strip()], raw
    # SPDX 表达式直接采用（lib 的 token 归一化会把 "LGPL-2.1-or-later" 拆成
    # LGPL-2-1，故先按字面匹配合法 SPDX 形态）
    first = (raw or "").strip().splitlines()[0].strip() if (raw or "").strip() else ""
    if SPDX_LITERAL.match(first):
        return first, raw
    tokens = _license_tokens(raw)
    known = {
        "MIT", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0",
        "ISC", "0BSD", "MPL-2.0", "PSF-2.0", "Zlib", "LGPL-2.1-or-later",
    }
    for token in sorted(tokens):
        if token in known:
            return token, raw
    return "", raw


def _python_components_from_names(names: set[str]) -> list[dict]:
    names = {n for n in names if n and n.lower() != "huiyiku"}
    # 构建工具不进 SBOM：PyInstaller 只以 bootloader 形式嵌入（GPL+冻结例外，
    # 已在 THIRD_PARTY_NOTICES*.md 说明），不是被分发的运行时组件
    names = {n for n in names if n.lower() not in BUILD_TOOLS}
    out = []
    for name in sorted(names):
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = ""
        spdx_id, raw = _spdx_id(name)
        comp: dict = {
            "type": "library",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{name.lower().replace('_', '-')}@{version}",
            "scope": "required",
        }
        comp["licenses"] = [{"license": {"id": spdx_id}}] if spdx_id else [{"license": {"name": _short_name(raw)}}]
        out.append(comp)
    return out


def _short_name(raw: str) -> str:
    """许可字段常整段塞进正文；作为 name 只取首行并截断，避免 SBOM 里出现大段文本。"""
    first = (raw or "").strip().splitlines()[0].strip() if (raw or "").strip() else ""
    if not first:
        return "unknown"
    return first[:60]


def _python_components(lock_path: Path) -> list[dict]:
    return _python_components_from_names(iter_requirement_names(lock_path))


def _installed_components() -> list[dict]:
    """枚举当前解释器已安装的全部发行版（ASR 侧 torch/torchaudio 不在锁文件里）。"""
    names = {
        d.metadata["Name"]
        for d in metadata.distributions()
        if d.metadata and d.metadata.get("Name")
    }
    names -= {"pip", "wheel", "huiyiku"}
    return _python_components_from_names(names)


def _npm_components() -> list[dict]:
    lock = ROOT / "web" / "package-lock.json"
    if not lock.is_file():
        return []
    data = json.loads(lock.read_text(encoding="utf-8"))
    out = []
    for path, meta in sorted((data.get("packages") or {}).items()):
        if not path.startswith("node_modules/") or meta.get("dev"):
            continue
        name = path.split("node_modules/")[-1]
        out.append(
            {
                "type": "library",
                "name": name,
                "version": meta.get("version", ""),
                "purl": f"pkg:npm/{name}@{meta.get('version', '')}",
                "scope": "required",
                "licenses": [{"license": {"id": meta.get("license", "")}}]
                if meta.get("license")
                else [],
            }
        )
    return out


def _native_components() -> list[dict]:
    return [
        {
            "type": "library",
            "name": c["name"],
            "version": c["version"],
            "scope": "required",
            "properties": [{"name": "huiyiku:note", "value": c["note"]}],
            "licenses": [{"license": {"id": c["license"]}}],
        }
        for c in NATIVE_COMPONENTS
    ]


def build(lock_path: Path, scope: str, installed: bool = False) -> dict:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    components = _installed_components() if installed else _python_components(lock_path)
    if scope == "ui":
        components += _npm_components() + _native_components()
    return {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "component": {
                "type": "application",
                "name": "huiyiku",
                "version": version,
                "licenses": [{"license": {"id": "Apache-2.0"}}],
            },
            "tools": [
                {
                    "vendor": "huiyiku contributors",
                    "name": "scripts/make_sbom.py",
                    "version": "1",
                }
            ],
        },
        "components": components,
        "properties": [{"name": "huiyiku:scope", "value": scope}],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="")
    ap.add_argument("--lock", default="requirements.lock", help="组件来源的依赖锁文件")
    ap.add_argument("--scope", choices=("ui", "asr"), default="ui", help="ui 含前端与原生库；asr 只含 Python 组件")
    ap.add_argument("--installed", action="store_true", help="枚举当前解释器已安装的发行版（ASR 侧用）")
    args = ap.parse_args()
    lock_path = ROOT / args.lock
    if not args.installed and not lock_path.is_file():
        print(f"lock file not found: {lock_path}", file=sys.stderr)
        return 1
    bom = build(lock_path, args.scope, installed=args.installed)
    suffix = "-asr" if args.scope == "asr" else ""
    out = Path(args.output) if args.output else ROOT / "packaging" / "dist" / (
        f"sbom-huiyiku{suffix}-{bom['metadata']['component']['version']}.cdx.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for c in bom["components"]:
        counts[c["type"]] = counts.get(c["type"], 0) + 1
    print(f"SBOM: {out} ({len(bom['components'])} components)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
