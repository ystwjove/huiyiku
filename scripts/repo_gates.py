# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""Repository gates used by CI: lockfile, licenses, hygiene."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_LOCK_PACKAGES = frozenset(
    {
        "torch",
        "torchvision",
        "torchaudio",
        "funasr",
        "modelscope",
        "numpy",
        "pyinstaller",
    }
)

LOCK_FILES = ("requirements.lock", "requirements-dev.lock")
SIZE_EXEMPT_LOCKS = frozenset(
    {
        "requirements.lock",
        "requirements-dev.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "uv.lock",
    }
)
MAX_FILE_BYTES = 1_000_000

FORBIDDEN_BASENAMES = frozenset({"app.json", "secrets.json"})
FORBIDDEN_SUFFIXES = (
    ".wav",
    ".mp3",
    ".m4a",
    ".mp4",
    ".aac",
    ".flac",
    ".ogg",
    ".wma",
    ".webm",
    ".opus",
    ".db",
)
FORBIDDEN_PATH_PARTS = ("meeting-data",)
FORBIDDEN_TRACKED_PREFIXES = ("web/dist/",)

ENV_NAME_RE = re.compile(r"^\.env(?:\..+)?$")
# Real-looking keys. README placeholders such as sk-... must not match.
KEY_RE = re.compile(
    r"""(?x)
    (?:
        sk-[A-Za-z0-9]{16,}
      | sk-proj-[A-Za-z0-9]{16,}
      | sk-or-[A-Za-z0-9]{16,}
      | AKIA[0-9A-Z]{16}
    )
    """
)
TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".txt",
        ".toml",
        ".yml",
        ".yaml",
        ".json",
        ".in",
        ".cfg",
        ".ini",
        ".ps1",
        ".sh",
        ".css",
        ".js",
        ".ts",
        ".tsx",
        ".html",
        ".svg",
    }
)

ALLOWED_LICENSE_TOKENS = frozenset(
    {
        "MIT",
        "MIT-0",
        "APACHE-2.0",
        "APACHE-2",
        "APACHE",
        "BSD-2-CLAUSE",
        "BSD-3-CLAUSE",
        "BSD-2",
        "BSD-3",
        "BSD",
        "ISC",
        "0BSD",
        "PSF",
        "PSFL",
        "PYTHON-SOFTWARE-FOUNDATION",
        "PYTHON",
        "UNLICENSE",
        "BLESSING",
        "PUBLIC-DOMAIN",
    }
)

FORBIDDEN_LICENSE_TOKENS = frozenset(
    {
        "GPL",
        "AGPL",
        "SSPL",
        "LGPL",
        "MPL",
        "MOZILLA",
        "BSD-4-CLAUSE",
        "BSD-4",
        "UNKNOWN",
        "PROPRIETARY",
    }
)

REQ_LINE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)\s*(?:\[.*\])?\s*(?:===|==|>=|<=|~=|!=|>|<)")


def iter_requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    if not path.is_file():
        return names
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = REQ_LINE_RE.match(line)
        if not match:
            continue
        names.add(_normalize_dist_name(match.group("name")))
    return names


def _normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_lockfile(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    found_any = False
    for rel in LOCK_FILES:
        path = root / rel
        if not path.is_file():
            errors.append(f"missing lock file: {rel}")
            continue
        found_any = True
        names = iter_requirement_names(path)
        banned = sorted(names & FORBIDDEN_LOCK_PACKAGES)
        if banned:
            errors.append(f"{rel} contains forbidden packages: {', '.join(banned)}")
    if not found_any:
        errors.append("no default lock files found")
    build_path = root / "requirements-build.txt"
    if build_path.is_file():
        build_names = iter_requirement_names(build_path)
        for rel in LOCK_FILES:
            overlap = iter_requirement_names(root / rel) & build_names
            if overlap:
                errors.append(
                    f"{rel} must not include build-only packages: {', '.join(sorted(overlap))}"
                )
    return errors


def _license_tokens(text: str) -> set[str]:
    cleaned = text.upper().replace("LICENCE", "LICENSE")
    cleaned = cleaned.replace("SOFTWARE LICENSE", "")
    cleaned = cleaned.replace("LICENSE", "")
    cleaned = re.sub(r"[^A-Z0-9]+", "-", cleaned).strip("-")
    if not cleaned:
        return set()
    parts = re.split(r"-(?:AND|OR)-", cleaned)
    tokens = {p.strip("-") for p in parts if p.strip("-")}
    aliases = set(tokens)
    for token in list(tokens):
        if token.startswith("APACHE"):
            aliases.add("APACHE")
            aliases.add("APACHE-2.0")
        if token.startswith("BSD-2"):
            aliases.add("BSD-2-CLAUSE")
            aliases.add("BSD")
        if token.startswith("BSD-3"):
            aliases.add("BSD-3-CLAUSE")
            aliases.add("BSD")
        if token.startswith("PSF") or ("PYTHON" in token and "FOUNDATION" in token):
            aliases.add("PSF")
        if token == "MPL-2" or token.startswith("MPL"):
            aliases.add("MPL")
        if "MOZILLA" in token:
            aliases.add("MPL")
    return aliases


def _exception_licenses(root: Path) -> dict[str, str]:
    path = root / "docs" / "license-exceptions.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for item in data.get("exceptions", []):
        mapping[_normalize_dist_name(item["name"])] = str(item.get("license", ""))
    return mapping


def check_licenses(root: Path = ROOT) -> list[str]:
    from importlib import metadata

    errors: list[str] = check_lockfile(root)
    if errors:
        return errors

    names: set[str] = set()
    for rel in LOCK_FILES:
        names |= iter_requirement_names(root / rel)
    names.discard("huiyiku")

    exceptions = _exception_licenses(root)
    for dist_name in sorted(names):
        try:
            meta = metadata.metadata(dist_name)
        except metadata.PackageNotFoundError:
            errors.append(f"lock package not installed: {dist_name}")
            continue
        license_text = (meta.get("License-Expression") or meta.get("License") or "").strip()
        if not license_text or license_text.upper() in {"UNKNOWN", "DUAL LICENSE"}:
            classifiers = meta.get_all("Classifier") or []
            license_classifiers = [
                c.split(" :: ")[-1] for c in classifiers if c.startswith("License ::")
            ]
            license_text = " OR ".join(license_classifiers)
        if dist_name in exceptions:
            continue
        tokens = _license_tokens(license_text)
        if not tokens:
            errors.append(f"{dist_name}: missing license metadata")
            continue
        if tokens & FORBIDDEN_LICENSE_TOKENS:
            errors.append(f"{dist_name}: license not allowed for default deps ({license_text!r})")
            continue
        if not (tokens & ALLOWED_LICENSE_TOKENS):
            errors.append(f"{dist_name}: license not on whitelist ({license_text!r})")
    errors.extend(check_npm_licenses(root))
    return errors


NPM_LICENSE_MAP = {
    "react": "MIT",
    "react-dom": "MIT",
    "vite": "MIT",
    "@vitejs/plugin-react": "MIT",
    "@tanstack/react-virtual": "MIT",
    "typescript": "Apache-2.0",
    "@types/react": "MIT",
    "@types/react-dom": "MIT",
}


def check_npm_licenses(root: Path = ROOT) -> list[str]:
    pkg_path = root / "web" / "package.json"
    if not pkg_path.is_file():
        return []
    errors: list[str] = []
    lock_path = root / "web" / "package-lock.json"
    if not lock_path.is_file():
        errors.append("missing web/package-lock.json")
    data = json.loads(pkg_path.read_text(encoding="utf-8"))
    names = set(data.get("dependencies") or {}) | set(data.get("devDependencies") or {})
    for name in sorted(names):
        if name not in NPM_LICENSE_MAP:
            errors.append(f"npm package {name} missing from frontend license map")
        elif NPM_LICENSE_MAP[name] not in {
            "MIT",
            "MIT-0",
            "Apache-2.0",
            "BSD-2-Clause",
            "BSD-3-Clause",
            "ISC",
            "0BSD",
        }:
            errors.append(f"npm package {name} license {NPM_LICENSE_MAP[name]} not allowed")
    return errors


NOTICES_FILE = "THIRD_PARTY_NOTICES.md"


def _mentioned(text: str, dist_name: str) -> bool:
    """发行版名在声明文件里出现即可（宽松匹配 -/_/大小写与常见别名）。"""
    haystack = text.lower()
    variants = {
        dist_name.lower(),
        dist_name.lower().replace("-", "_"),
        dist_name.lower().replace("_", "-"),
    }
    aliases = {"pydantic-core": "pydantic-core", "clr-loader": "clr-loader"}
    variants.add(aliases.get(dist_name.lower(), dist_name.lower()))
    return any(v in haystack for v in variants)


def check_notices(root: Path = ROOT) -> list[str]:
    """声明文件必须覆盖两处锁文件里的每个包。

    防止新增依赖后只更新锁文件、忘了登记归属（MIT/BSD 再分发需附声明）。
    """
    notices = root / NOTICES_FILE
    if not notices.is_file():
        return [f"missing {NOTICES_FILE}"]
    text = notices.read_text(encoding="utf-8")
    errors: list[str] = []
    for rel in ("requirements.lock", "web/package-lock.json"):
        path = root / rel
        if not path.is_file():
            errors.append(f"missing {rel}")
            continue
        if rel.endswith(".json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            names = set()
            for item_path, meta in (data.get("packages") or {}).items():
                if not item_path.startswith("node_modules/"):
                    continue
                # 只要求运行时依赖登记；dev 工具不随发布包分发
                if meta.get("dev") or meta.get("devOptional"):
                    continue
                names.add(item_path.split("node_modules/")[-1].split("/")[-1])
        else:
            names = {n for n in iter_requirement_names(path) if n != "huiyiku"}
        for name in sorted(names):
            if not _mentioned(text, name):
                errors.append(f"{rel}: {name} 未在 {NOTICES_FILE} 中登记")
    return errors


def _tracked_files(root: Path) -> list[Path]:
    git_dir = root / ".git"
    if git_dir.exists():
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        return [root / p for p in result.stdout.decode("utf-8").split("\0") if p]
    skip_dirs = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".ruff_cache",
        ".pytest_cache",
        "meeting-data",
        "asr",
    }
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        files.append(path)
    return files


def check_hygiene(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for path in _tracked_files(root):
        rel = path.relative_to(root).as_posix()
        name = path.name
        if name in FORBIDDEN_BASENAMES:
            errors.append(f"forbidden file tracked: {rel}")
        if ENV_NAME_RE.match(name) and name != ".env.example":
            errors.append(f"env file tracked: {rel}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"media/database file tracked: {rel}")
        parts = {p.lower() for p in path.parts}
        if any(part in parts for part in FORBIDDEN_PATH_PARTS):
            errors.append(f"data-dir path tracked: {rel}")
        if any(rel.startswith(prefix) for prefix in FORBIDDEN_TRACKED_PREFIXES):
            errors.append(f"build artifact tracked: {rel}")
        if name not in SIZE_EXEMPT_LOCKS and path.is_file():
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                errors.append(f"file exceeds 1MB ({size} bytes): {rel}")
        if path.suffix.lower() in TEXT_SUFFIXES and path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if KEY_RE.search(text):
                errors.append(f"possible secret pattern in {rel}")
    return errors


def _run(check_name: str, fn) -> int:
    errors = fn()
    if errors:
        print(f"{check_name}: FAIL", file=sys.stderr)
        for item in errors:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"{check_name}: ok")
    return 0
