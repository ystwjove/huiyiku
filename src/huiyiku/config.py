# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import getpass
import json
import logging
import os
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from huiyiku.paths import (
    default_app_json_path,
    default_asr_home,
    default_data_dir,
    ensure_data_layout,
    forbidden_data_dir_reason,
    is_frozen,
    is_loopback_host,
    lan_ipv4_addresses,
)

APP_JSON_KEYS = ("data_dir", "host", "port", "ffmpeg_path", "asr_home")


@dataclass
class AppConfig:
    data_dir: str = ""
    host: str = "127.0.0.1"
    port: int = 8787
    ffmpeg_path: str = ""
    asr_home: str = ""
    path: Path = field(default_factory=lambda: Path("app.json"), repr=False)

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def asr_path(self) -> Path:
        return Path(self.asr_home) if self.asr_home else default_asr_home()


@dataclass
class Settings:
    access_token: str = ""
    allow_cloud: bool = True
    report_llm_model_id: int | None = None
    qa_llm_model_id: int | None = None
    digest_llm_model_id: int | None = None
    max_upload_gb: float = 2
    hotwords: str = ""
    client_session: str = ""
    egress_confirmed_at: str | None = None
    legal_confirmed_at: str | None = None
    # 内网共享。默认关：只有本机可访问。设置页一键切换，不必重启。
    share_lan: bool = False


@dataclass
class Secrets:
    asr_api_key: str = ""
    embedding_api_key: str = ""
    llm_api_key: str = ""


@dataclass
class RuntimeContext:
    app: AppConfig
    settings: Settings
    secrets: Secrets
    frozen: bool
    layout: dict[str, Path]


class StartupError(SystemExit):
    pass


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    for attempt in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, PermissionError):
            if attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))
    return {}


def _write_json(path: Path, data: dict, *, restrict: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if restrict:
        _restrict_acl(tmp)
        if path.exists():
            _restrict_acl(path)
    tmp.replace(path)
    if restrict:
        _restrict_acl(path)


def _windows_user() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or getpass.getuser() or ""


def _restrict_acl(path: Path) -> None:
    try:
        if os.name == "nt":
            user = _windows_user()
            if user:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W,D)"],
                    check=False,
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
        else:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        return


def load_app_config(app_json: Path | None = None) -> AppConfig:
    path = app_json or default_app_json_path()
    env_path = os.environ.get("HUIYIKU_APP_JSON")
    if env_path:
        path = Path(env_path)
    raw = _read_json(path)
    cfg = AppConfig(
        data_dir=str(raw.get("data_dir") or ""),
        host=str(raw.get("host") or "127.0.0.1"),
        port=int(raw.get("port") or 8787),
        ffmpeg_path=str(raw.get("ffmpeg_path") or ""),
        asr_home=str(raw.get("asr_home") or ""),
        path=path,
    )
    dirty = False
    if not cfg.data_dir:
        cfg.data_dir = str(default_data_dir().resolve())
        dirty = True
    if not cfg.asr_home:
        cfg.asr_home = str(default_asr_home().resolve())
        dirty = True
    if cfg.port != 8787:
        # Plan: exclusive 8787. Config may record it, but bootstrap still binds 8787.
        cfg.port = 8787
        dirty = True
    if dirty:
        save_app_config(cfg)
    return cfg


def save_app_config(cfg: AppConfig) -> None:
    data = {k: getattr(cfg, k) for k in APP_JSON_KEYS}
    _write_json(cfg.path, data, restrict=True)


def load_settings(path: Path) -> Settings:
    raw = _read_json(path)
    return Settings(
        access_token=str(raw.get("access_token") or ""),
        allow_cloud=bool(raw.get("allow_cloud", True)),
        report_llm_model_id=raw.get("report_llm_model_id"),
        qa_llm_model_id=raw.get("qa_llm_model_id"),
        digest_llm_model_id=raw.get("digest_llm_model_id"),
        max_upload_gb=float(raw.get("max_upload_gb") or 2),
        hotwords=str(raw.get("hotwords") or ""),
        client_session=str(raw.get("client_session") or ""),
        egress_confirmed_at=raw.get("egress_confirmed_at"),
        legal_confirmed_at=raw.get("legal_confirmed_at"),
        share_lan=_flag(raw, "share_lan", False),
    )


def save_settings(path: Path, settings: Settings) -> None:
    # settings.json 含 access_token，与 secrets.json 同级收紧 ACL
    _write_json(path, asdict(settings), restrict=True)


def load_secrets(path: Path) -> Secrets:
    raw = _read_json(path)
    return Secrets(
        asr_api_key=os.environ.get("HUIYIKU_ASR_API_KEY") or str(raw.get("asr_api_key") or ""),
        embedding_api_key=os.environ.get("HUIYIKU_EMBEDDING_API_KEY")
        or str(raw.get("embedding_api_key") or ""),
        llm_api_key=os.environ.get("HUIYIKU_LLM_API_KEY") or str(raw.get("llm_api_key") or ""),
    )


def save_secrets(path: Path, secrets: Secrets) -> None:
    _write_json(path, asdict(secrets), restrict=True)


def load_runtime(app_json: Path | None = None) -> RuntimeContext:
    frozen = is_frozen()
    app = load_app_config(app_json)
    layout = ensure_data_layout(app.data_path)
    settings = load_settings(layout["settings"])
    token_env = os.environ.get("HUIYIKU_ACCESS_TOKEN")
    if token_env:
        settings.access_token = token_env
    secrets = load_secrets(layout["secrets"])
    if not settings.client_session:
        # 每台安装一个稳定会话 ID：部分网关（opencode zen）要求
        # x-opencode-session 头做路由；标准端点忽略未知头无副作用。
        # 只补写该字段，避免把环境变量令牌意外落盘
        import uuid

        on_disk = _read_json(layout["settings"])
        on_disk["client_session"] = uuid.uuid4().hex
        _write_json(layout["settings"], on_disk, restrict=True)
        settings.client_session = on_disk["client_session"]
    # 旧安装只把 app.json host 改成 0.0.0.0。还没有开关字段时，视为已经打开共享，
    # 并写成显式字段，之后只认设置页的开关。
    on_disk = _read_json(layout["settings"])
    if "share_lan" not in on_disk and not is_loopback_host(app.host):
        settings.share_lan = True
        on_disk["share_lan"] = True
        _write_json(layout["settings"], on_disk, restrict=True)
    if settings.share_lan:
        logging.getLogger("huiyiku.config").warning(
            "内网共享已开启：同网段任何人可访问本库"
        )
    data_resolved = app.data_path.resolve()
    reason = forbidden_data_dir_reason(data_resolved)
    if reason:
        raise StartupError(reason)
    return RuntimeContext(
        app=app,
        settings=settings,
        secrets=secrets,
        frozen=frozen,
        layout=layout,
    )


def settings_public_dict(settings: Settings, secrets: Secrets) -> dict:
    return {
        "allow_cloud": settings.allow_cloud,
        "report_llm_model_id": settings.report_llm_model_id,
        "qa_llm_model_id": settings.qa_llm_model_id,
        "digest_llm_model_id": settings.digest_llm_model_id,
        "max_upload_gb": settings.max_upload_gb,
        "hotwords": settings.hotwords,
        "egress_confirmed_at": settings.egress_confirmed_at,
        "legal_confirmed_at": settings.legal_confirmed_at,
        "access_token_configured": bool(settings.access_token),
        "llm_api_key_configured": bool(secrets.llm_api_key),
        "asr_api_key_configured": bool(secrets.asr_api_key),
        "embedding_api_key_configured": bool(secrets.embedding_api_key),
        "share_lan": settings.share_lan,
        "lan_urls": [f"http://{ip}:8787" for ip in lan_ipv4_addresses()],
    }


def _flag(raw: dict, key: str, default: bool) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def wizard_completed(settings: Settings) -> bool:
    return bool(settings.legal_confirmed_at)
