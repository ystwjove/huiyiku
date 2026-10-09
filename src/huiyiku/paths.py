# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_app_json_path() -> Path:
    env = os.environ.get("HUIYIKU_APP_JSON")
    if env:
        return Path(env)
    # Config sits next to the app (repo root in development, exe directory when frozen).
    return executable_dir() / "app.json"


def default_data_dir() -> Path:
    # Recordings, SQLite, transcripts and reports follow the project/install directory.
    return executable_dir() / "meeting-data"


def default_asr_home() -> Path:
    env = os.environ.get("HUIYIKU_ASR_HOME")
    if env:
        return Path(env)
    return executable_dir() / "asr"


def data_layout(data_dir: Path) -> dict[str, Path]:
    return {
        "root": data_dir,
        "db": data_dir / "app.db",
        "settings": data_dir / "settings.json",
        "secrets": data_dir / "secrets.json",
        "media_original": data_dir / "media" / "original",
        "media_wav": data_dir / "media" / "wav",
        "media_waveform": data_dir / "media" / "waveform",
        "transcripts": data_dir / "transcripts",
        "reports": data_dir / "reports",
        "exports": data_dir / "exports",
        "logs": data_dir / "logs",
    }


def ensure_data_layout(data_dir: Path) -> dict[str, Path]:
    layout = data_layout(data_dir)
    for key, path in layout.items():
        if key in {"db", "settings", "secrets"}:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    return layout


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in {"127.0.0.1", "::1", "localhost"}


def is_local_client(host: str | None) -> bool:
    """本机请求，或测试客户端。内网共享关闭时只放行这些来源。"""
    if not host:
        return True
    h = host.strip().lower()
    if h.startswith("::ffff:"):
        h = h[7:]
    return h in {"127.0.0.1", "::1", "localhost", "testclient"}


def lan_ipv4_addresses() -> list[str]:
    """本机非回环 IPv4，供设置页展示同事要打开的地址。"""
    import socket

    found: list[str] = []

    def add(ip: str) -> None:
        if ip and not ip.startswith("127.") and ip not in found:
            found.append(ip)

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        infos = []
    for info in infos:
        add(info[4][0])
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.0.2.1", 9))
            add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    return found


def looks_like_sync_or_network_path(path: Path) -> bool:
    text = str(path)
    lowered = text.lower()
    if "onedrive" in lowered or "dropbox" in lowered or "google drive" in lowered:
        return True
    if text.startswith("\\\\"):
        return True
    return False


def safe_join(root: Path, *parts: str) -> Path:
    """Resolve *parts under root; raise ValueError on traversal."""
    base = root.resolve()
    full = base.joinpath(*parts).resolve()
    full.relative_to(base)
    return full


def safe_filename(name: str) -> str:
    cleaned = (name or "").replace("\\", "/").split("/")[-1]
    if not cleaned or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise ValueError("invalid filename")
    return cleaned


def forbidden_data_dir_reason(path: Path) -> str | None:
    resolved = path.resolve()
    internal = (executable_dir() / "_internal").resolve()
    try:
        resolved.relative_to(internal)
        return "data_dir must not be inside _internal"
    except ValueError:
        pass
    text = str(resolved)
    if "Program Files" in text or "Program Files (x86)" in text:
        return "data_dir must not be in Program Files"
    return None
