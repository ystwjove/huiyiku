# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from huiyiku import __version__
from huiyiku.api.auth import (
    clear_session_cookie,
    require_auth,
    set_session_cookie,
    tokens_match,
)
from huiyiku.api.state import AppState
from huiyiku.asr.client import asr_exe_installed, asr_python_ready, asr_ready
from huiyiku.config import (
    RuntimeContext,
    load_runtime,
    save_secrets,
    save_settings,
    settings_public_dict,
    wizard_completed,
)
from huiyiku.db.models import (
    ActionItem,
    Group,
    GroupDigest,
    Job,
    Meeting,
    MeetingParticipant,
    MeetingSpeaker,
    ModelRegistry,
    Person,
    QaMessage,
    Report,
    ReportVersion,
    SpeakerStat,
    TranscriptSegment,
)
from huiyiku.llm.client import ChatError
from huiyiku.media.ffmpeg import ALLOWED_SUFFIXES, find_ffmpeg
from huiyiku.paths import (
    executable_dir,
    is_local_client,
    looks_like_sync_or_network_path,
    safe_filename,
    safe_join,
)
from huiyiku.store import (
    accept_report_actions,
    attach_original,
    create_meeting,
    delete_meeting_contents,
    enqueue_job,
    mark_reports_stale,
    next_manual_label,
    rebuild_stats_and_chunks,
)
from huiyiku.timeutil import now_iso


class SetupBody(BaseModel):
    legal_confirmed: bool = False
    egress_confirmed: bool = False
    allow_cloud: bool = True
    access_token: str = ""
    llm_endpoint: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_context_length: int = 32000
    embedding_endpoint: str = ""
    embedding_model: str = ""
    embedding_api_key: str = ""
    embedding_dim: int | None = None


class SessionBody(BaseModel):
    token: str


class MeetingCreate(BaseModel):
    title: str | None = None
    group_id: int | None = None
    occurred_at: str | None = None


class MeetingPatch(BaseModel):
    title: str | None = None
    occurred_at: str | None = None
    group_id: int | None = None


class ImportLocalBody(BaseModel):
    path: str
    title: str | None = None


class GroupBody(BaseModel):
    name: str
    description: str | None = None
    color: str | None = None


class PersonBody(BaseModel):
    name: str
    aliases_json: str | None = None
    notes: str | None = None


class SpeakerPatch(BaseModel):
    display_name: str | None = None
    person_id: int | None = None
    status: str | None = None


class AssignBody(BaseModel):
    segment_ids: list[int]


class MergeBody(BaseModel):
    into_id: int


class SegmentPatch(BaseModel):
    text: str | None = None
    speaker_id: int | None = None


class ReportPatch(BaseModel):
    content_json: dict[str, Any]


# GET /report 返回的 content 含读取时派生的字段（出处核实、说话人段数），
# 前端编辑回传时必须剥离，避免把它们写进版本快照。
_DERIVED_ITEM_KEYS = {"evidence", "evidence_status", "segment_count", "vector_matched"}
_DERIVED_TOP_KEYS = {"evidence_summary"}
# PATCH 允许写入的顶层键白名单：报告结构自身 + 说话人复核标记，其余拒绝
# （validate_report 只查必需键，不加白名单的话多余键会原样入库）
_REPORT_EDITABLE_KEYS = frozenset(
    {
        "summary",
        "key_points",
        "decisions",
        "action_items",
        "risks",
        "open_questions",
        "participants",
        "auto_speakers_unreviewed",
    }
)


def _strip_derived(content: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in content.items():
        if k in _DERIVED_TOP_KEYS:
            continue
        if isinstance(v, list):
            out[k] = [
                {ik: iv for ik, iv in item.items() if ik not in _DERIVED_ITEM_KEYS}
                if isinstance(item, dict)
                else item
                for item in v
            ]
        else:
            out[k] = v
    return out


ACTION_STATUSES = frozenset({"open", "doing", "blocked", "done", "cancelled"})
SPEAKER_STATUSES = frozenset({"pending", "confirmed", "merged", "invalid"})


class ActionPatch(BaseModel):
    status: str | None = None
    owner_person_id: int | None = None
    due_at: str | None = None
    title: str | None = None


class QaBody(BaseModel):
    session_id: str
    question: str
    scope: dict[str, Any] = Field(default_factory=dict)
    allow_extra: bool = False


class ReportItemVerifyBody(BaseModel):
    meeting_id: int
    text: str


class DigestBody(BaseModel):
    period_start: str | None = None
    period_end: str | None = None


class ModelCreate(BaseModel):
    task: str
    provider: str
    model_name: str
    model_version: str | None = ""
    display_name: str | None = None
    execution: str = "remote"
    endpoint: str | None = None
    secret_ref: str | None = None
    embedding_dim: int | None = None
    license: str | None = None
    usage_class: str | None = "cloud_byok"
    capabilities_json: dict[str, Any] | None = None
    terms_accepted: bool = False


class ModelPatch(BaseModel):
    display_name: str | None = None
    status: str | None = None
    is_default: bool | None = None
    terms_accepted: bool | None = None
    capabilities_json: dict[str, Any] | None = None


class SettingsPatch(BaseModel):
    allow_cloud: bool | None = None
    report_llm_model_id: int | None = None
    qa_llm_model_id: int | None = None
    digest_llm_model_id: int | None = None
    max_upload_gb: float | None = None
    hotwords: str | None = None
    share_lan: bool | None = None


def create_app(runtime: RuntimeContext | None = None) -> FastAPI:
    runtime = runtime or load_runtime()
    state = AppState.from_runtime(runtime)
    app = FastAPI(title="会议库", version=__version__)
    app.state.s = state

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        if request.url.path == "/api/health":
            return await call_next(request)
        try:
            require_auth(request, state.runtime.settings.access_token)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    # 后注册的中间件先执行：共享关闭时，非本机请求在鉴权之前就被拒绝。
    @app.middleware("http")
    async def _share_gate(request: Request, call_next):
        peer = request.client.host if request.client else None
        if not state.runtime.settings.share_lan and not is_local_client(peer):
            return JSONResponse({"detail": "内网共享未开启"}, status_code=403)
        return await call_next(request)

    _register_routes(app, state)
    _mount_frontend(app)
    return app


def _register_routes(app: FastAPI, state: AppState) -> None:
    rt = state.runtime

    def db():
        return state.Session()

    @app.get("/api/health")
    def health():
        hb_path = rt.layout["logs"] / "worker.heartbeat"
        worker_hb = hb_path.read_text(encoding="utf-8") if hb_path.is_file() else None
        return {
            "ok": True,
            "app": "huiyiku",
            "version": __version__,
            "worker_heartbeat_at": worker_hb,
        }

    @app.get("/api/runtime")
    def runtime_info():
        ffmpeg = find_ffmpeg(rt.app.ffmpeg_path, search_root=executable_dir())
        return {
            "version": __version__,
            "data_dir": str(rt.app.data_path),
            "host": rt.app.host,
            "port": rt.app.port,
            "share_lan": rt.settings.share_lan,
            "ffmpeg": bool(ffmpeg),
            "ffmpeg_path": ffmpeg,
            "asr_home": str(rt.app.asr_path),
            "asr_exe": asr_exe_installed(rt.app.asr_path),
            "asr_python": asr_python_ready(rt.app.asr_path),
            "wizard_completed": wizard_completed(rt.settings),
            "worker_heartbeat_at": (
                (rt.layout["logs"] / "worker.heartbeat").read_text(encoding="utf-8").strip()
                if (rt.layout["logs"] / "worker.heartbeat").is_file()
                else None
            ),
            "sync_path_warning": looks_like_sync_or_network_path(rt.app.data_path),
        }

    @app.get("/api/settings")
    def get_settings():
        return settings_public_dict(rt.settings, rt.secrets)

    @app.patch("/api/settings")
    def patch_settings(body: SettingsPatch):
        s = rt.settings
        if body.allow_cloud is not None:
            s.allow_cloud = body.allow_cloud
        for attr, value in (
            ("report_llm_model_id", body.report_llm_model_id),
            ("qa_llm_model_id", body.qa_llm_model_id),
            ("digest_llm_model_id", body.digest_llm_model_id),
        ):
            if value is not None:
                _require_role_llm(value)
                setattr(s, attr, value)
        if body.max_upload_gb is not None:
            s.max_upload_gb = body.max_upload_gb
        if body.hotwords is not None:
            if len(body.hotwords) > 2000:
                raise HTTPException(400, "热词过长（最多 2000 字符）")
            s.hotwords = body.hotwords.strip()
        if body.share_lan is not None:
            s.share_lan = body.share_lan
        save_settings(rt.layout["settings"], s)
        return settings_public_dict(s, rt.secrets)

    def _require_role_llm(model_id: int) -> None:
        with db() as session:
            row = session.get(ModelRegistry, model_id)
            if row is None or row.task != "llm":
                raise HTTPException(400, "找不到该 LLM 模型")
            if row.execution == "remote" and not row.terms_accepted:
                raise HTTPException(400, "需要先确认合规声明")
            if row.execution == "remote" and row.status != "available":
                raise HTTPException(400, "使用该模型前请先做连通测试")

    @app.post("/api/setup")
    def setup(body: SetupBody):
        if not body.legal_confirmed:
            raise HTTPException(400, "需要先确认合规声明")
        ts = now_iso()
        rt.settings.legal_confirmed_at = ts
        if body.egress_confirmed:
            rt.settings.egress_confirmed_at = ts
        rt.settings.allow_cloud = body.allow_cloud
        if body.access_token:
            rt.settings.access_token = body.access_token
        if body.llm_api_key:
            rt.secrets.llm_api_key = body.llm_api_key
        if body.embedding_api_key:
            rt.secrets.embedding_api_key = body.embedding_api_key
        save_settings(rt.layout["settings"], rt.settings)
        save_secrets(rt.layout["secrets"], rt.secrets)
        with db() as session:
            _ensure_local_asr(session)
            llm_probe_error: str | None = None
            if body.llm_endpoint and body.llm_model:
                llm_key = body.llm_api_key or rt.secrets.llm_api_key
                if body.egress_confirmed and llm_key:
                    # 先测通再落库：测不通就完全不动已有配置（含默认项），
                    # 否则一次手误的端点会把本来可用的默认模型改坏。
                    try:
                        _probe_llm_endpoint(rt, body.llm_endpoint, body.llm_model, llm_key)
                    except Exception as exc:  # noqa: BLE001 - 失败只回报原因
                        logging.getLogger("huiyiku.api").warning(
                            "llm probe failed for %s: %s", body.llm_model, exc
                        )
                        llm_probe_error = str(exc) or exc.__class__.__name__
                if llm_probe_error is None:
                    llm_row = _upsert_model(
                        session,
                        task="llm",
                        provider="openai_compatible",
                        model_name=body.llm_model,
                        endpoint=body.llm_endpoint,
                        secret_ref="llm_api_key",
                        execution="remote",
                        caps={"context_length": body.llm_context_length},
                        terms=body.egress_confirmed,
                        make_default=False,
                    )
                    if llm_key:
                        llm_row.status = "available"
                        _mark_available_default(session, llm_row)
            if body.embedding_endpoint and body.embedding_model:
                emb_row = _upsert_model(
                    session,
                    task="embedding",
                    provider="openai_compatible",
                    model_name=body.embedding_model,
                    endpoint=body.embedding_endpoint,
                    secret_ref="embedding_api_key",
                    execution="remote",
                    caps={},
                    terms=body.egress_confirmed,
                    make_default=False,
                    embedding_dim=body.embedding_dim,
                )
                _maybe_probe_and_default(
                    rt,
                    session,
                    emb_row,
                    probe=bool(
                        body.egress_confirmed
                        and (
                            body.embedding_api_key
                            or body.llm_api_key
                            or rt.secrets.embedding_api_key
                        )
                    ),
                )
            session.commit()
        return {"ok": True, "llm_probe_error": llm_probe_error}

    @app.post("/api/session")
    def session_login(body: SessionBody, response: Response):
        if not tokens_match(rt.settings.access_token, body.token):
            raise HTTPException(401, "访问令牌不正确")
        set_session_cookie(response, rt.settings.access_token)
        return {"ok": True}

    @app.delete("/api/session")
    def session_logout(response: Response):
        clear_session_cookie(response)
        return {"ok": True}

    @app.get("/api/llm/models")
    def llm_models(task: str = "llm"):
        """拉取当前端点声明的可用模型列表（OpenAI-compatible /models）。

        task=llm 用默认 LLM 端点；task=embedding 用默认向量模型端点
        （设置页「向量模型」卡片的拉取可用模型）。
        """
        if task not in {"llm", "embedding"}:
            raise HTTPException(400, "不支持的任务类型")
        with db() as session:
            row = session.scalar(
                select(ModelRegistry).where(
                    ModelRegistry.task == task, ModelRegistry.is_default == 1
                )
            )
        if row is None or not (row.endpoint or "").strip():
            raise HTTPException(400, "尚未配置远程端点")
        key = (
            (rt.secrets.embedding_api_key or rt.secrets.llm_api_key)
            if task == "embedding"
            else rt.secrets.llm_api_key
        )
        if not key:
            raise HTTPException(400, "缺少 API Key")
        headers = {"Authorization": f"Bearer {key}"}
        if rt.settings.client_session:
            headers["x-opencode-session"] = rt.settings.client_session
        url = row.endpoint.rstrip("/") + "/models"
        try:
            resp = httpx.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise HTTPException(400, f"拉取模型列表失败：{exc}") from exc
        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        return {"models": ids}

    @app.get("/api/models")
    def list_models():
        with db() as session:
            rows = session.scalars(select(ModelRegistry).order_by(ModelRegistry.id)).all()
            return [_model_json(m) for m in rows]

    @app.post("/api/models")
    def create_model(body: ModelCreate):
        with db() as session:
            row = _upsert_model(
                session,
                task=body.task,
                provider=body.provider,
                model_name=body.model_name,
                endpoint=body.endpoint,
                secret_ref=body.secret_ref,
                execution=body.execution,
                caps=body.capabilities_json or {},
                terms=body.terms_accepted,
                make_default=False,
                embedding_dim=body.embedding_dim,
                version=body.model_version,
                display_name=body.display_name,
                license_=body.license,
                usage_class=body.usage_class,
            )
            session.commit()
            session.refresh(row)
            return _model_json(row)

    @app.patch("/api/models/{model_id}")
    def patch_model(model_id: int, body: ModelPatch):
        with db() as session:
            row = session.get(ModelRegistry, model_id)
            if row is None:
                raise HTTPException(404, "模型不存在")
            if body.is_default:
                if row.task == "embedding":
                    from huiyiku.db.models import MeetingChunk

                    has_vec = session.scalar(
                        select(MeetingChunk.id).where(
                            MeetingChunk.is_active == 1, MeetingChunk.embedding.is_not(None)
                        )
                    )
                    if has_vec:
                        raise HTTPException(400, "已有向量索引；重建功能计划在后续版本提供")
                if row.task == "asr" and row.execution == "remote":
                    raise HTTPException(400, "远程 ASR 暂不能设为默认")
                if row.execution == "remote":
                    if not row.terms_accepted:
                        raise HTTPException(400, "需要先确认合规声明")
                    if row.status != "available":
                        raise HTTPException(400, "设为默认前请先做连通测试")
                session.query(ModelRegistry).filter(ModelRegistry.task == row.task).update(
                    {"is_default": 0}
                )
                row.is_default = 1
            if body.display_name is not None:
                row.display_name = body.display_name
            if body.status is not None:
                if body.status == "available":
                    raise HTTPException(400, "请先做连通测试再标记为可用")
                if body.status not in {"disabled", "failed", "untested"}:
                    raise HTTPException(400, "状态不合法")
                row.status = body.status
            if body.terms_accepted is not None:
                row.terms_accepted = int(body.terms_accepted)
            if body.capabilities_json is not None:
                row.capabilities_json = json.dumps(body.capabilities_json)
            row.updated_at = now_iso()
            session.commit()
            return _model_json(row)

    @app.post("/api/models/{model_id}/test")
    def test_model(model_id: int):
        with db() as session:
            row = session.get(ModelRegistry, model_id)
            if row is None:
                raise HTTPException(404, "模型不存在")
            try:
                payload = _probe_model(rt, row)
            except Exception as exc:  # noqa: BLE001 - 任何失败都回报原因，不 500
                row.status = "failed"
                row.updated_at = now_iso()
                session.commit()
                raise HTTPException(400, str(exc) or exc.__class__.__name__) from exc
            row.status = "available"
            row.updated_at = now_iso()
            session.commit()
            return {"ok": True, **payload}

    @app.get("/api/models/manifest")
    def manifest():
        path = executable_dir() / "docs" / "model-manifest.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"local": [], "remote": []}

    @app.get("/api/groups")
    def list_groups():
        with db() as session:
            return [_group_json(g) for g in session.scalars(select(Group).order_by(Group.id))]

    @app.post("/api/groups")
    def create_group(body: GroupBody):
        with db() as session:
            ts = now_iso()
            g = Group(
                name=body.name,
                description=body.description,
                color=body.color,
                is_system=0,
                created_at=ts,
                updated_at=ts,
            )
            session.add(g)
            session.commit()
            session.refresh(g)
            return _group_json(g)

    @app.get("/api/groups/{gid}")
    def get_group(gid: int):
        with db() as session:
            g = session.get(Group, gid)
            if g is None:
                raise HTTPException(404, "分组不存在")
            return _group_json(g)

    @app.patch("/api/groups/{gid}")
    def patch_group(gid: int, body: GroupBody):
        with db() as session:
            g = session.get(Group, gid)
            if g is None:
                raise HTTPException(404, "分组不存在")
            g.name = body.name
            g.description = body.description
            g.color = body.color
            g.updated_at = now_iso()
            session.commit()
            return _group_json(g)

    @app.delete("/api/groups/{gid}")
    def delete_group(gid: int):
        with db() as session:
            g = session.get(Group, gid)
            if g is None:
                raise HTTPException(404, "分组不存在")
            if g.is_system:
                raise HTTPException(400, "系统分组不能删除")
            n = session.scalar(select(Meeting.id).where(Meeting.group_id == gid))
            if n:
                raise HTTPException(400, "分组下还有会议，无法删除")
            session.delete(g)
            session.commit()
            return {"ok": True}

    @app.get("/api/persons")
    def list_persons():
        with db() as session:
            return [_person_json(p) for p in session.scalars(select(Person).order_by(Person.id))]

    @app.post("/api/persons")
    def create_person(body: PersonBody):
        with db() as session:
            ts = now_iso()
            p = Person(
                name=body.name,
                aliases_json=body.aliases_json,
                notes=body.notes,
                created_at=ts,
                updated_at=ts,
            )
            session.add(p)
            session.commit()
            session.refresh(p)
            return _person_json(p)

    @app.patch("/api/persons/{pid}")
    def patch_person(pid: int, body: PersonBody):
        with db() as session:
            p = session.get(Person, pid)
            if p is None:
                raise HTTPException(404, "人物不存在")
            p.name = body.name
            p.aliases_json = body.aliases_json
            p.notes = body.notes
            p.updated_at = now_iso()
            session.commit()
            return _person_json(p)

    @app.delete("/api/persons/{pid}")
    def delete_person(pid: int):
        with db() as session:
            p = session.get(Person, pid)
            if p is None:
                raise HTTPException(404, "人物不存在")
            session.query(MeetingParticipant).filter(MeetingParticipant.person_id == pid).delete()
            session.query(MeetingSpeaker).filter(MeetingSpeaker.person_id == pid).update(
                {"person_id": None}
            )
            session.delete(p)
            session.commit()
            return {"ok": True}

    @app.get("/api/meetings")
    def list_meetings(
        group_id: int | None = None,
        status: str | None = None,
        person_id: int | None = None,
    ):
        with db() as session:
            q = select(Meeting)
            if group_id:
                q = q.where(Meeting.group_id == group_id)
            if status:
                q = q.where(Meeting.status == status)
            meetings = list(session.scalars(q.order_by(Meeting.occurred_at.desc())))
            if person_id:
                mids = {
                    r.meeting_id
                    for r in session.scalars(
                        select(MeetingParticipant).where(MeetingParticipant.person_id == person_id)
                    )
                }
                meetings = [m for m in meetings if m.id in mids]
            return [_meeting_json(m) for m in meetings]

    @app.post("/api/meetings")
    def post_meeting(body: MeetingCreate):
        with db() as session:
            m = create_meeting(
                session,
                title=body.title or "未命名会议",
                group_id=body.group_id,
                occurred_at=body.occurred_at,
            )
            session.commit()
            session.refresh(m)
            return _meeting_json(m)

    @app.post("/api/meetings/import-local")
    def import_local(body: ImportLocalBody):
        src = Path(body.path)
        if not src.is_file():
            raise HTTPException(400, "文件不存在")
        if src.suffix.lower() not in ALLOWED_SUFFIXES:
            raise HTTPException(400, "不支持的文件类型")
        with db() as session:
            m = create_meeting(
                session,
                title=body.title or src.stem,
                source_type="import",
                occurred_at=None,
            )
            attach_original(
                session, m, src, rt.layout["media_original"], filename=src.name, apply_mtime=True
            )
            job = enqueue_job(session, "preprocess", meeting_id=m.id)
            session.commit()
            return {"meeting": _meeting_json(m), "job_id": job.id}

    @app.post("/api/meetings/{mid}/media")
    async def upload_media(mid: int, request: Request, filename: str | None = None):
        max_bytes = int(rt.settings.max_upload_gb * 1024**3)
        # query 参数已解码；x-filename header 由前端 encodeURIComponent 编码（HTTP header 仅 ISO-8859-1）
        header_name = unquote(request.headers.get("x-filename", ""))
        raw_name = filename or header_name or "upload.bin"
        try:
            name = safe_filename(raw_name)
        except ValueError as exc:
            raise HTTPException(400, "文件名不合法") from exc
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(400, "不支持的文件类型")
        with db() as session:
            m = session.get(Meeting, mid)
            if m is None:
                raise HTTPException(404, "会议不存在")
            dest = safe_join(rt.layout["media_original"], f"{m.id}_{name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            digest = hashlib.sha256()
            with dest.open("wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_bytes:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        raise HTTPException(413, "文件超过大小上限")
                    fh.write(chunk)
                    digest.update(chunk)
            m.original_filename = name
            m.original_path = f"media/original/{dest.name}"
            m.file_size_bytes = size
            m.media_checksum = digest.hexdigest()
            m.source_type = "upload"
            job = enqueue_job(session, "preprocess", meeting_id=m.id, payload={"t": now_iso()})
            session.commit()
            return JSONResponse({"job_id": job.id}, status_code=202)

    @app.get("/api/meetings/{mid}")
    def get_meeting(mid: int):
        with db() as session:
            m = session.get(Meeting, mid)
            if m is None:
                raise HTTPException(404, "会议不存在")
            return _meeting_json(m)

    @app.patch("/api/meetings/{mid}")
    def patch_meeting(mid: int, body: MeetingPatch):
        with db() as session:
            m = session.get(Meeting, mid)
            if m is None:
                raise HTTPException(404, "会议不存在")
            if body.title is not None:
                m.title = body.title
            if body.occurred_at is not None:
                m.occurred_at = body.occurred_at
            if body.group_id is not None:
                m.group_id = body.group_id
                from huiyiku.db.models import MeetingChunk

                session.query(MeetingChunk).filter(MeetingChunk.meeting_id == mid).update(
                    {"group_id": body.group_id}
                )
            m.updated_at = now_iso()
            session.commit()
            return _meeting_json(m)

    @app.delete("/api/meetings/{mid}")
    def delete_meeting(mid: int, confirm: bool = False):
        with db() as session:
            m = session.get(Meeting, mid)
            if m is None:
                raise HTTPException(404, "会议不存在")
            busy = session.scalars(
                select(Job).where(Job.meeting_id == mid, Job.status.in_(["queued", "running"]))
            ).all()
            impact = {
                "title": m.title,
                "jobs": [{"id": j.id, "type": j.type, "status": j.status} for j in busy],
            }
            if not confirm:
                return {"impact": impact, "need_confirm": True}
            if busy:
                for j in busy:
                    j.cancel_requested = 1
                session.commit()
                raise HTTPException(409, "请先取消进行中的任务再重试")
            conn = session.connection().connection
            result = delete_meeting_contents(session, m, rt.app.data_path, conn)
            session.commit()
            for path in result.get("files") or []:
                try:
                    if path.is_file():
                        path.unlink()
                except OSError:
                    pass
            return {"ok": True}

    @app.post("/api/meetings/{mid}/retry")
    def retry_meeting(mid: int, stage: str | None = Query(None)):
        with db() as session:
            m = session.get(Meeting, mid)
            if m is None:
                raise HTTPException(404, "会议不存在")
            chosen = (stage or "").strip() or "auto"
            allowed = {"auto", "preprocess", "transcribe", "stats", "embed", "report"}
            if chosen not in allowed:
                raise HTTPException(400, f"invalid stage: {chosen}")
            if chosen == "auto":
                failed = session.scalar(
                    select(Job)
                    .where(Job.meeting_id == mid, Job.status == "failed")
                    .order_by(Job.id.desc())
                )
                if failed is not None:
                    chosen = failed.type
                elif m.wav_path:
                    chosen = "transcribe"
                else:
                    chosen = "preprocess"
            job = enqueue_job(
                session, chosen, meeting_id=mid, payload={"retry": True, "t": now_iso()}
            )
            session.commit()
            return JSONResponse({"job_id": job.id, "stage": chosen}, status_code=202)

    @app.get("/api/meetings/{mid}/audio")
    def audio(mid: int, request: Request):
        with db() as session:
            m = session.get(Meeting, mid)
            if m is None:
                raise HTTPException(404, "会议不存在")
            rel = m.wav_path
            if not rel:
                raise HTTPException(404, "音频尚未就绪")
            path = _safe_data_path(rt.app.data_path, rel)
        return FileResponse(path, media_type="audio/wav", headers={"Accept-Ranges": "bytes"})

    @app.get("/api/meetings/{mid}/participants")
    def participants(mid: int):
        with db() as session:
            rows = session.scalars(
                select(MeetingParticipant).where(MeetingParticipant.meeting_id == mid)
            ).all()
            return [
                {
                    "id": r.id,
                    "person_id": r.person_id,
                    "source": r.source,
                    "note": r.note,
                    "person": _person_json(session.get(Person, r.person_id))
                    if r.person_id
                    else None,
                }
                for r in rows
            ]

    @app.post("/api/meetings/{mid}/participants")
    def add_participant(mid: int, person_id: int, source: str = "manual"):
        with db() as session:
            if session.get(Meeting, mid) is None or session.get(Person, person_id) is None:
                raise HTTPException(404, "资源不存在")
            row = MeetingParticipant(
                meeting_id=mid, person_id=person_id, source=source, created_at=now_iso()
            )
            session.add(row)
            session.commit()
            return {"id": row.id}

    @app.delete("/api/meetings/{mid}/participants/{person_id}")
    def del_participant(mid: int, person_id: int):
        with db() as session:
            session.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == mid, MeetingParticipant.person_id == person_id
            ).delete()
            session.commit()
            return {"ok": True}

    @app.get("/api/meetings/{mid}/transcript")
    def transcript(mid: int, offset: int = 0, limit: int = 200):
        with db() as session:
            rows = session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.meeting_id == mid)
                .order_by(TranscriptSegment.start_ms)
                .offset(offset)
                .limit(limit)
            ).all()
            return [_seg_json(s) for s in rows]

    @app.patch("/api/transcript-segments/{sid}")
    def patch_segment(sid: int, body: SegmentPatch):
        with db() as session:
            seg = session.get(TranscriptSegment, sid)
            if seg is None:
                raise HTTPException(404, "转写片段不存在")
            if body.speaker_id is not None:
                owner = session.scalar(
                    select(MeetingSpeaker.id).where(
                        MeetingSpeaker.id == body.speaker_id,
                        MeetingSpeaker.meeting_id == seg.meeting_id,
                    )
                )
                if owner is None:
                    raise HTTPException(400, "该说话人不属于这场会议")
            if body.text is not None:
                seg.text = body.text
                seg.normalized_text = body.text
                seg.review_status = "edited"
            if body.speaker_id is not None:
                seg.speaker_id = body.speaker_id
            seg.updated_at = now_iso()
            _rebuild_meeting_chunks(session, seg.meeting_id, rt)
            mark_reports_stale(session, seg.meeting_id, "转写已更新，报告可能过时")
            session.commit()
            return _seg_json(seg)

    @app.get("/api/meetings/{mid}/speakers")
    def speakers(mid: int):
        with db() as session:
            rows = session.scalars(
                select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == mid)
            ).all()
            return [_speaker_json(s) for s in rows]

    @app.post("/api/meetings/{mid}/speakers")
    def create_speaker(mid: int, display_name: str | None = None):
        with db() as session:
            if session.get(Meeting, mid) is None:
                raise HTTPException(404, "会议不存在")
            label = next_manual_label(session, mid)
            row = MeetingSpeaker(
                meeting_id=mid,
                speaker_label=label,
                display_name=display_name or label,
                status="pending",
                updated_at=now_iso(),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _speaker_json(row)

    @app.patch("/api/meeting-speakers/{sid}")
    def patch_speaker(sid: int, body: SpeakerPatch):
        with db() as session:
            row = session.get(MeetingSpeaker, sid)
            if row is None:
                raise HTTPException(404, "说话人不存在")
            dirty = False
            if body.display_name is not None:
                row.display_name = body.display_name
            if body.status is not None:
                if body.status not in SPEAKER_STATUSES:
                    raise HTTPException(400, "说话人状态不合法")
                row.status = body.status
                dirty = True
            if body.person_id is not None:
                row.person_id = body.person_id
                row.status = "confirmed"
                dirty = True
                if (
                    session.scalar(
                        select(MeetingParticipant).where(
                            MeetingParticipant.meeting_id == row.meeting_id,
                            MeetingParticipant.person_id == body.person_id,
                        )
                    )
                    is None
                ):
                    session.add(
                        MeetingParticipant(
                            meeting_id=row.meeting_id,
                            person_id=body.person_id,
                            source="speaker_confirm",
                            created_at=now_iso(),
                        )
                    )
            row.updated_at = now_iso()
            if dirty:
                _rebuild_meeting_chunks(session, row.meeting_id, rt)
                mark_reports_stale(session, row.meeting_id, "说话人已更新，报告可能过时")
            session.commit()
            return _speaker_json(row)

    @app.post("/api/meeting-speakers/{sid}/merge")
    def merge_speaker(sid: int, body: MergeBody):
        with db() as session:
            src = session.get(MeetingSpeaker, sid)
            dst = session.get(MeetingSpeaker, body.into_id)
            if src is None or dst is None:
                raise HTTPException(404, "说话人不存在")
            session.query(TranscriptSegment).filter(TranscriptSegment.speaker_id == src.id).update(
                {"speaker_id": dst.id}
            )
            src.status = "merged"
            src.merged_into_id = dst.id
            src.updated_at = now_iso()
            _rebuild_meeting_chunks(session, src.meeting_id, rt)
            mark_reports_stale(session, src.meeting_id, "说话人已合并，报告可能过时")
            session.commit()
            return {"ok": True}

    @app.post("/api/meeting-speakers/{sid}/assign")
    def assign_segments(sid: int, body: AssignBody):
        with db() as session:
            sp = session.get(MeetingSpeaker, sid)
            if sp is None:
                raise HTTPException(404, "说话人不存在")
            session.query(TranscriptSegment).filter(
                TranscriptSegment.id.in_(body.segment_ids)
            ).update(
                {"speaker_id": sp.id, "review_status": "edited"},
                synchronize_session=False,
            )
            _rebuild_meeting_chunks(session, sp.meeting_id, rt)
            mark_reports_stale(session, sp.meeting_id, "转写已更新，报告可能过时")
            session.commit()
            return {"ok": True}

    @app.post("/api/meetings/{mid}/speakers/approve")
    def approve_speakers(mid: int):
        with db() as session:
            m = session.get(Meeting, mid)
            if m is None:
                raise HTTPException(404, "会议不存在")
            for sp in session.scalars(
                select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == mid)
            ):
                if sp.status == "pending":
                    sp.status = "confirmed"
                    sp.updated_at = now_iso()
            job = enqueue_job(session, "stats", meeting_id=mid)
            session.commit()
            return JSONResponse({"job_id": job.id}, status_code=202)

    @app.get("/api/meetings/{mid}/stats")
    def meeting_stats(mid: int):
        with db() as session:
            rows = session.scalars(select(SpeakerStat).where(SpeakerStat.meeting_id == mid)).all()
            speakers = {
                s.id: s
                for s in session.scalars(
                    select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == mid)
                )
            }
            return {
                "metric_version": "v1",
                "speakers": [
                    {
                        **{
                            "speaker_id": r.speaker_id,
                            "speech_ms": r.speech_ms,
                            "segment_count": r.segment_count,
                            "speech_ratio": r.speech_ratio,
                            "timeline": json.loads(r.timeline_json or "[]"),
                            "display_name": speakers.get(r.speaker_id).display_name
                            if speakers.get(r.speaker_id)
                            else None,
                        }
                    }
                    for r in rows
                ],
            }

    @app.post("/api/meetings/{mid}/report")
    def post_report(mid: int):
        if not rt.settings.allow_cloud:
            raise HTTPException(400, "未开启云端处理：请到设置页打开联网开关")
        if not rt.secrets.llm_api_key:
            raise HTTPException(400, "尚未配置 LLM（缺 API Key 或未开启联网）")
        with db() as session:
            m = session.get(Meeting, mid)
            if m is None:
                raise HTTPException(404, "会议不存在")
            job = enqueue_job(session, "report", meeting_id=mid, payload={"t": now_iso()})
            session.commit()
            return JSONResponse({"job_id": job.id}, status_code=202)

    @app.get("/api/meetings/{mid}/report")
    def get_report(mid: int):
        with db() as session:
            report = session.scalar(select(Report).where(Report.meeting_id == mid))
            if report is None:
                raise HTTPException(404, "该会议还没有报告")
            ver = (
                session.get(ReportVersion, report.current_version_id)
                if report.current_version_id
                else None
            )
            current = _report_ver_json(ver) if ver else None
            if current is not None and isinstance(current.get("content"), dict):
                cache_key = _evidence_cache_key(ver.id, ver.content_json or "")
                cached = _evidence_cache_get(cache_key)
                if cached is not None:
                    current["content"] = cached
                else:
                    from huiyiku.llm.report import attach_report_evidence

                    attach_report_evidence(session, mid, current["content"], runtime=rt)
                    _evidence_cache_put(cache_key, current["content"])
            return {
                "report_id": report.id,
                "current": current,
            }

    @app.patch("/api/meetings/{mid}/report")
    def patch_report(mid: int, body: ReportPatch):
        from huiyiku.domain.report_schema import empty_report, validate_report

        # 先剥离派生键再查白名单：GET /report 回传的顶层派生键不算未知字段
        stripped = _strip_derived(body.content_json)
        unknown = set(stripped) - _REPORT_EDITABLE_KEYS
        if unknown:
            raise HTTPException(400, f"报告不支持的顶层字段：{sorted(unknown)}")
        try:
            clean = validate_report({**empty_report(), **stripped})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with db() as session:
            report = session.scalar(select(Report).where(Report.meeting_id == mid))
            if report is None:
                raise HTTPException(404, "该会议还没有报告")
            cur = session.get(ReportVersion, report.current_version_id)
            ts = now_iso()
            ver = ReportVersion(
                report_id=report.id,
                version=(cur.version + 1) if cur else 1,
                source="manual",
                model=cur.model if cur else None,
                prompt_version=cur.prompt_version if cur else None,
                content_json=json.dumps(clean, ensure_ascii=False),
                content_md=None,
                status="draft",
                created_at=ts,
            )
            session.add(ver)
            session.flush()
            report.current_version_id = ver.id
            report.updated_at = ts
            session.commit()
            session.refresh(ver)
            return _report_ver_json(ver)

    @app.post("/api/report-versions/{vid}/accept")
    def accept_report(vid: int):
        with db() as session:
            ver = session.get(ReportVersion, vid)
            if ver is None:
                raise HTTPException(404, "报告版本不存在")
            report = session.get(Report, ver.report_id)
            if report is None or report.current_version_id != ver.id:
                raise HTTPException(400, "只能接受当前版本的报告")
            ver.status = "accepted"
            meeting = session.get(Meeting, report.meeting_id)
            if meeting:
                accept_report_actions(session, meeting, ver, runtime=rt)
                meeting.status = "ready"
                meeting.updated_at = now_iso()
            session.commit()
            return _report_ver_json(ver)

    @app.get("/api/actions")
    def list_actions(group_id: int | None = None, status: str | None = None):
        with db() as session:
            q = select(ActionItem)
            if group_id:
                q = q.where(ActionItem.group_id == group_id)
            if status:
                q = q.where(ActionItem.status == status)
            return [_action_json(a) for a in session.scalars(q.order_by(ActionItem.id.desc()))]

    @app.patch("/api/actions/{aid}")
    def patch_action(aid: int, body: ActionPatch):
        with db() as session:
            item = session.get(ActionItem, aid)
            if item is None:
                raise HTTPException(404, "行动项不存在")
            if body.status is not None and body.status not in ACTION_STATUSES:
                raise HTTPException(400, "状态不合法")
            from_status = item.status
            if body.status is not None:
                item.status = body.status
            if body.owner_person_id is not None:
                item.owner_person_id = body.owner_person_id
            if body.due_at is not None:
                item.due_at = body.due_at
            if body.title is not None:
                item.title = body.title
            item.user_edited = 1
            item.updated_at = now_iso()
            from huiyiku.db.models import ActionItemEvent

            session.add(
                ActionItemEvent(
                    action_item_id=item.id,
                    from_status=from_status,
                    to_status=item.status,
                    reason="manual",
                    created_at=now_iso(),
                )
            )
            session.commit()
            return _action_json(item)

    @app.post("/api/qa")
    async def qa(body: QaBody):
        if not rt.settings.allow_cloud:
            raise HTTPException(400, "未开启云端处理：请到设置页打开联网开关")
        if not rt.secrets.llm_api_key:
            raise HTTPException(400, "尚未配置 LLM（缺 API Key 或未开启联网）")
        if not (body.question or "").strip():
            raise HTTPException(400, "请先输入问题")

        async def gen():
            with db() as session:
                from huiyiku.llm.qa import stream_answer

                async for event in stream_answer(
                    session,
                    rt,
                    session_id=body.session_id,
                    question=body.question,
                    scope=body.scope,
                    allow_extra=body.allow_extra,
                ):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/qa/threads/{thread_id}/messages")
    def qa_thread_messages(thread_id: str):
        with db() as session:
            rows = session.scalars(
                select(QaMessage)
                .where(QaMessage.session_id == thread_id)
                .order_by(QaMessage.id)
            ).all()
            return [_qa_turn_json(r) for r in rows]

    @app.post("/api/report-items/verify")
    async def verify_report_item(body: ReportItemVerifyBody):
        if not rt.settings.allow_cloud:
            raise HTTPException(400, "未开启云端处理：设置页的联网开关未打开")
        if not rt.secrets.llm_api_key:
            raise HTTPException(400, "尚未配置 LLM API Key")
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(400, "缺少要核实的内容")
        from huiyiku.llm.qa import verify_item

        with db() as session:
            if session.get(Meeting, body.meeting_id) is None:
                raise HTTPException(404, "会议不存在")
            try:
                return await verify_item(
                    session, rt, meeting_id=body.meeting_id, text=text
                )
            except ChatError as exc:
                raise HTTPException(502, str(exc)) from exc

    @app.post("/api/groups/{gid}/digest")
    def post_digest(gid: int, body: DigestBody):
        if not rt.settings.allow_cloud or not rt.secrets.llm_api_key:
            raise HTTPException(400, "尚未配置 LLM（缺 API Key 或未开启联网）")
        with db() as session:
            g = session.get(Group, gid)
            if g is None:
                raise HTTPException(404, "分组不存在")
            ready = session.scalars(
                select(Meeting).where(Meeting.group_id == gid, Meeting.status == "ready")
            ).all()
            if not ready:
                raise HTTPException(400, "先接受至少一份会议报告")
            job = enqueue_job(
                session,
                "digest",
                payload={
                    "group_id": gid,
                    "period_start": body.period_start,
                    "period_end": body.period_end,
                    "t": now_iso(),
                },
            )
            session.commit()
            return JSONResponse({"job_id": job.id}, status_code=202)

    @app.get("/api/groups/{gid}/digests")
    def list_digests(gid: int):
        with db() as session:
            rows = session.scalars(
                select(GroupDigest)
                .where(GroupDigest.group_id == gid)
                .order_by(GroupDigest.id.desc())
            )
            return [_digest_json(d) for d in rows]

    @app.get("/api/groups/{gid}/digests/{did}")
    def get_digest(gid: int, did: int):
        with db() as session:
            d = session.get(GroupDigest, did)
            if d is None or d.group_id != gid:
                raise HTTPException(404, "进展摘要不存在")
            return _digest_json(d)

    @app.get("/api/export/meeting/{mid}.md")
    def export_md(mid: int):
        with db() as session:
            doc, m = _meeting_export_doc(session, mid, transcript=True, runtime=rt)
            from huiyiku.export.markdown import render_meeting_md

            text = render_meeting_md(doc)
            return _export_response(
                text.encode("utf-8"),
                "text/markdown; charset=utf-8",
                m,
                "md",
                rt,
            )

    @app.get("/api/export/meeting/{mid}.html")
    def export_html(mid: int):
        with db() as session:
            doc, m = _meeting_export_doc(session, mid, transcript=True, runtime=rt)
            from huiyiku.export.html import render_meeting_html

            html = render_meeting_html(doc)
            return _export_response(
                html.encode("utf-8"), "text/html; charset=utf-8", m, "html", rt
            )

    @app.get("/api/export/meeting/{mid}.docx")
    def export_docx(mid: int):
        with db() as session:
            doc, m = _meeting_export_doc(session, mid, transcript=True, runtime=rt)
            from huiyiku.export.docx_out import render_meeting_docx

            blob = render_meeting_docx(doc)
            return _export_response(
                blob,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                m,
                "docx",
                rt,
            )

    @app.get("/api/export/actions.csv")
    def export_actions_csv(
        person_id: int | None = None,
        group_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        status: str | None = None,
    ):
        from huiyiku.export.csvout import build_action_rows, render_actions_csv

        with db() as session:
            rows = build_action_rows(
                session,
                person_id=person_id,
                group_id=group_id,
                date_from=date_from,
                date_to=date_to,
                status=status,
                runtime=rt,
            )
            text = render_actions_csv(rows)
            name = f"行动项清单-{now_iso()[:10]}.csv"
            from huiyiku.export.common import export_response

            return export_response(
                text.encode("utf-8-sig"),
                "text/csv; charset=utf-8",
                name,
                rt.layout["exports"],
            )

    @app.get("/api/export/group/{gid}.{ext}")
    def export_group(gid: int, ext: str):
        if ext not in {"md", "html", "docx"}:
            raise HTTPException(400, "不支持的格式")
        with db() as session:
            g = session.get(Group, gid)
            if g is None:
                raise HTTPException(404, "分组不存在")
            digest = session.scalar(
                select(GroupDigest)
                .where(GroupDigest.group_id == gid)
                .order_by(GroupDigest.id.desc())
            )
            digest_md = (digest.content_md if digest else "") or ""
            from huiyiku.export.common import build_meeting_doc, current_report_version

            meeting_docs = []
            for m in session.scalars(
                select(Meeting).where(Meeting.group_id == gid).order_by(Meeting.id)
            ):
                meeting_docs.append(
                    build_meeting_doc(
                        session,
                        m,
                        current_report_version(session, m),
                        transcript=False,
                        runtime=rt,
                    )
                )
            if ext == "md":
                from huiyiku.export.markdown import render_group_md

                blob = render_group_md(g.name, digest_md, meeting_docs).encode("utf-8")
                media = "text/markdown; charset=utf-8"
            elif ext == "html":
                from huiyiku.export.html import render_group_html

                blob = render_group_html(g.name, digest_md, meeting_docs).encode("utf-8")
                media = "text/html; charset=utf-8"
            else:
                from huiyiku.export.docx_out import render_group_docx

                blob = render_group_docx(g.name, digest_md, meeting_docs)
                media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            from huiyiku.export.common import export_filename, export_response

            name = export_filename(f"汇总-{g.name}", now_iso(), ext)
            return export_response(blob, media, name, rt.layout["exports"])

    @app.get("/api/export/qa/{thread_id}.{ext}")
    def export_qa_thread(thread_id: str, ext: str):
        if ext not in {"md", "html"}:
            raise HTTPException(400, "不支持的格式")
        from huiyiku.export.common import export_filename, export_response
        from huiyiku.export.qa import render_qa_html, render_qa_md

        with db() as session:
            rows = session.scalars(
                select(QaMessage)
                .where(QaMessage.session_id == thread_id)
                .order_by(QaMessage.id)
            ).all()
            if not rows:
                raise HTTPException(404, "会话不存在或为空")
            turns = [
                {
                    "question": r.question,
                    "answer": r.answer,
                    "citations": json.loads(r.citations_json or "[]"),
                }
                for r in rows
            ]
            title = f"问询记录-{(rows[0].question or '')[:24]}"
            first_day = (rows[0].created_at or "")[:10]
            name = export_filename(title, first_day, ext)
            if ext == "md":
                blob = render_qa_md(title, turns).encode("utf-8")
                media = "text/markdown; charset=utf-8"
            else:
                blob = render_qa_html(title, turns).encode("utf-8")
                media = "text/html; charset=utf-8"
            return export_response(blob, media, name, rt.layout["exports"])

    @app.get("/api/jobs/{jid}")
    def get_job(jid: int):
        with db() as session:
            job = session.get(Job, jid)
            if job is None:
                raise HTTPException(404, "任务不存在")
            return _job_json(job)

    @app.get("/api/jobs")
    def list_jobs():
        with db() as session:
            rows = session.scalars(select(Job).order_by(Job.id.desc()).limit(50)).all()
            return [_job_json(j) for j in rows]

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: int):
        with db() as session:
            job = session.get(Job, jid)
            if job is None:
                raise HTTPException(404, "任务不存在")
            job.cancel_requested = 1
            session.commit()
            return {"ok": True}


def _mount_frontend(app: FastAPI) -> None:
    candidates = [
        executable_dir() / "web" / "dist",
        executable_dir() / "_internal" / "web" / "dist",
    ]
    dist = next((p for p in candidates if (p / "index.html").is_file()), None)
    if dist is None:
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404)
        if not full_path:
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
        try:
            file = safe_join(dist, full_path)
        except ValueError as exc:
            raise HTTPException(400, "文件路径不合法") from exc
        if file.is_file():
            return FileResponse(file)
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})


def _safe_data_path(data_dir: Path, rel: str) -> Path:
    try:
        full = safe_join(data_dir, rel)
    except ValueError as exc:
        raise HTTPException(400, "文件路径不合法") from exc
    if not full.is_file():
        raise HTTPException(404, "文件不存在")
    return full


def _ensure_local_asr(session) -> None:
    existing = session.scalar(
        select(ModelRegistry).where(ModelRegistry.task == "asr", ModelRegistry.execution == "local")
    )
    if existing:
        return
    ts = now_iso()
    session.add(
        ModelRegistry(
            task="asr",
            provider="funasr",
            model_name="paraformer-zh",
            model_version="v2.0.4",
            display_name="FunASR Paraformer 中文",
            execution="local",
            license="Apache-2.0",
            terms_accepted=1,
            review_status="approved",
            status="available",
            is_default=1,
            capabilities_json=json.dumps({"speaker_labels": "experimental", "vad": "fsmn-vad"}),
            created_at=ts,
            updated_at=ts,
        )
    )


def _rebuild_meeting_chunks(
    session, meeting_id: int | None, runtime: RuntimeContext | None = None
) -> None:
    if not meeting_id:
        return
    meeting = session.get(Meeting, meeting_id)
    if meeting is None:
        return
    conn = session.connection().connection
    rebuild_stats_and_chunks(session, meeting, conn)
    if runtime is not None:
        from huiyiku.store import enqueue_embed_if_configured

        enqueue_embed_if_configured(session, meeting, allow_cloud=runtime.settings.allow_cloud)


def _mark_available_default(session, row: ModelRegistry) -> None:
    """把该模型标为可用，并设为同任务下唯一的默认项。"""
    row.status = "available"
    session.query(ModelRegistry).filter(ModelRegistry.task == row.task).update({"is_default": 0})
    row.is_default = 1
    row.updated_at = now_iso()


def _probe_llm_endpoint(rt: RuntimeContext, endpoint: str, model_name: str, key: str) -> None:
    """用给定端点/模型/Key 做一次最小连通测试（不落库）。"""
    from huiyiku.llm.client import ChatClient

    client = ChatClient(
        endpoint,
        key,
        model_name,
        timeout=15,
        session_id=rt.settings.client_session,
    )
    client.complete([{"role": "user", "content": "ping"}], max_tokens=8)


def _probe_model(rt: RuntimeContext, row: ModelRegistry) -> dict[str, Any]:
    if row.execution == "remote" and not row.terms_accepted:
        raise ChatError("需要先确认合规声明")
    if row.task == "llm":
        key = rt.secrets.llm_api_key
        if not key:
            raise ChatError("尚未配置 LLM API Key，无法做连通测试")
        _probe_llm_endpoint(rt, row.endpoint or "", row.model_name, key)
        return {"sample": "pong"}
    if row.task == "embedding":
        from huiyiku.llm.embed import embed_text

        vec = embed_text(rt, row, "ping")
        if row.embedding_dim and len(vec) != row.embedding_dim:
            raise ChatError(f"embedding dim {len(vec)} != {row.embedding_dim}")
        if not row.embedding_dim:
            row.embedding_dim = len(vec)
        return {"dim": len(vec)}
    if row.task == "asr":
        if row.execution != "local":
            raise ChatError("远程 ASR 暂不能设为默认")
        if not asr_ready(rt.app.asr_path):
            raise ChatError("ASR component not installed")
        return {"asr": True, "speaker_probe": "skipped"}
    raise ChatError(f"unknown task {row.task}")


def _maybe_probe_and_default(
    rt: RuntimeContext, session, row: ModelRegistry, *, probe: bool
) -> str | None:
    """探测通过则设为默认；返回失败原因（None 表示通过或未探测）。

    任何异常都必须在这里收敛：配置保存不能因为一次探测失败返回 500。
    """
    if not probe:
        return None
    if row.execution == "remote" and not row.terms_accepted:
        return None
    try:
        _probe_model(rt, row)
    except Exception as exc:  # noqa: BLE001 - 探测失败只降级，不上抛
        logging.getLogger("huiyiku.api").warning(
            "model probe failed for %s: %s", row.model_name, exc
        )
        row.status = "failed"
        row.is_default = 0
        row.updated_at = now_iso()
        return str(exc) or exc.__class__.__name__
    _mark_available_default(session, row)
    return None


def _upsert_model(session, **kwargs) -> ModelRegistry:
    task = kwargs["task"]
    provider = kwargs["provider"]
    model_name = kwargs["model_name"]
    version = kwargs.get("version") or ""
    execution = kwargs.get("execution") or "remote"
    row = session.scalar(
        select(ModelRegistry).where(
            ModelRegistry.task == task,
            ModelRegistry.provider == provider,
            ModelRegistry.model_name == model_name,
            ModelRegistry.model_version == version,
        )
    )
    ts = now_iso()
    if row is None:
        row = ModelRegistry(
            task=task,
            provider=provider,
            model_name=model_name,
            model_version=version,
            display_name=kwargs.get("display_name") or model_name,
            execution=execution,
            endpoint=kwargs.get("endpoint"),
            secret_ref=kwargs.get("secret_ref"),
            embedding_dim=kwargs.get("embedding_dim"),
            license=kwargs.get("license_"),
            terms_accepted=int(bool(kwargs.get("terms"))),
            usage_class=kwargs.get("usage_class") or "cloud_byok",
            capabilities_json=json.dumps(kwargs.get("caps") or {}),
            review_status="approved" if kwargs.get("terms") else "pending",
            status="available" if execution == "local" else "untested",
            is_default=0,
            created_at=ts,
            updated_at=ts,
        )
        session.add(row)
        session.flush()
    else:
        # 同名模型重配：endpoint 即时更新；terms 跟随本次确认，否则
        # 从未确认→已确认的再配置会因 terms=0 跳过探测且无法激活
        row.endpoint = kwargs.get("endpoint")
        if kwargs.get("terms"):
            row.terms_accepted = 1
            row.review_status = "approved"
        caps = kwargs.get("caps") or {}
        if caps:
            row.capabilities_json = json.dumps(caps)
        row.updated_at = ts
    if kwargs.get("make_default") and row.status == "available":
        session.query(ModelRegistry).filter(ModelRegistry.task == task).update({"is_default": 0})
        row.is_default = 1
        row.terms_accepted = int(bool(kwargs.get("terms")))
        row.review_status = "approved" if kwargs.get("terms") else row.review_status
        row.updated_at = ts
    return row


def _group_json(g: Group) -> dict:
    return {
        "id": g.id,
        "name": g.name,
        "description": g.description,
        "color": g.color,
        "is_system": bool(g.is_system),
    }


def _person_json(p: Person) -> dict:
    return {"id": p.id, "name": p.name, "aliases_json": p.aliases_json, "notes": p.notes}


def _meeting_json(m: Meeting) -> dict:
    return {
        "id": m.id,
        "group_id": m.group_id,
        "title": m.title,
        "occurred_at": m.occurred_at,
        "source_type": m.source_type,
        "original_filename": m.original_filename,
        "duration_ms": m.duration_ms,
        "status": m.status,
        "error_json": json.loads(m.error_json) if m.error_json else None,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
        "has_audio": bool(m.wav_path),
    }


def _seg_json(s: TranscriptSegment) -> dict:
    return {
        "id": s.id,
        "meeting_id": s.meeting_id,
        "speaker_id": s.speaker_id,
        "start_ms": s.start_ms,
        "end_ms": s.end_ms,
        "text": s.text,
        "normalized_text": s.normalized_text,
        "review_status": s.review_status,
    }


def _speaker_json(s: MeetingSpeaker) -> dict:
    return {
        "id": s.id,
        "meeting_id": s.meeting_id,
        "speaker_label": s.speaker_label,
        "display_name": s.display_name,
        "person_id": s.person_id,
        "status": s.status,
        "merged_into_id": s.merged_into_id,
    }


def _job_json(j: Job) -> dict:
    return {
        "id": j.id,
        "meeting_id": j.meeting_id,
        "type": j.type,
        "status": j.status,
        "progress": json.loads(j.progress_json) if j.progress_json else None,
        "error": json.loads(j.error_json) if j.error_json else None,
        "cancel_requested": bool(j.cancel_requested),
        "created_at": j.created_at,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
    }


def _model_json(m: ModelRegistry) -> dict:
    return {
        "id": m.id,
        "task": m.task,
        "provider": m.provider,
        "model_name": m.model_name,
        "model_version": m.model_version,
        "display_name": m.display_name,
        "execution": m.execution,
        "endpoint": m.endpoint,
        "embedding_dim": m.embedding_dim,
        "is_default": bool(m.is_default),
        "terms_accepted": bool(m.terms_accepted),
        "status": m.status,
        "capabilities": json.loads(m.capabilities_json) if m.capabilities_json else {},
    }


# 报告出处缓存：版本内容本身不可变，但说话人改名/校对后出处要能跟随，
# 用短 TTL 折中——避免每次页面加载都对大会议重算溯源。
# 同步路由跑在线程池里，字典必须加锁：驱逐用 min() 迭代字典，
# 并发写会抛 "dictionary changed size during iteration"
_REPORT_EVIDENCE_CACHE: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
_EVIDENCE_CACHE_LOCK = threading.Lock()
_EVIDENCE_CACHE_TTL = 30.0
_EVIDENCE_CACHE_MAX = 8


def _evidence_cache_key(version_id: int, content_json: str) -> tuple[int, str]:
    # version 内容不可变，但同进程内可能存在多个数据库（测试），id 会撞；
    # 带上内容指纹保证 key 只在同一份快照上命中
    return (version_id, hashlib.sha256(content_json.encode("utf-8")).hexdigest()[:16])


def _evidence_cache_get(key: tuple[int, str]) -> dict[str, Any] | None:
    with _EVIDENCE_CACHE_LOCK:
        hit = _REPORT_EVIDENCE_CACHE.get(key)
        if hit is None or (time.time() - hit[0]) > _EVIDENCE_CACHE_TTL:
            _REPORT_EVIDENCE_CACHE.pop(key, None)
            return None
        return hit[1]


def _evidence_cache_put(key: tuple[int, str], content: dict[str, Any]) -> None:
    with _EVIDENCE_CACHE_LOCK:
        if len(_REPORT_EVIDENCE_CACHE) >= _EVIDENCE_CACHE_MAX:
            oldest = min(_REPORT_EVIDENCE_CACHE, key=lambda k: _REPORT_EVIDENCE_CACHE[k][0])
            _REPORT_EVIDENCE_CACHE.pop(oldest, None)
        _REPORT_EVIDENCE_CACHE[key] = (time.time(), content)


def _qa_turn_json(rec: QaMessage) -> dict:
    """线程历史消息序列化（独立于 SSE 的 done 载荷）。

    answer 里含【AI 补充（非录音内容）】段的按原文返回，前端拆块渲染。
    """
    try:
        retrieval = json.loads(rec.retrieval_json or "{}")
    except ValueError:
        retrieval = {}
    return {
        "id": rec.id,
        "question": rec.question,
        "answer": rec.answer,
        "citations": json.loads(rec.citations_json or "[]"),
        "allow_extra": bool(retrieval.get("allow_extra")),
        "created_at": rec.created_at,
    }


def _meeting_export_doc(session, mid: int, *, transcript: bool, runtime=None):
    m = session.get(Meeting, mid)
    if m is None:
        raise HTTPException(404, "会议不存在")
    from huiyiku.export.common import build_meeting_doc, current_report_version

    doc = build_meeting_doc(
        session, m, current_report_version(session, m), transcript=transcript, runtime=runtime
    )
    return doc, m


def _export_response(content: bytes, media_type: str, m: Meeting, ext: str, rt) -> Response:
    from huiyiku.export.common import export_filename, export_response

    name = export_filename(m.title, m.occurred_at, ext)
    return export_response(content, media_type, name, rt.layout["exports"])


def _report_ver_json(v: ReportVersion) -> dict:
    return {
        "id": v.id,
        "version": v.version,
        "source": v.source,
        "model": v.model,
        "status": v.status,
        "stale_reason": v.stale_reason,
        "content": json.loads(v.content_json),
        "content_md": v.content_md,
        "created_at": v.created_at,
    }


def _action_json(a: ActionItem) -> dict:
    return {
        "id": a.id,
        "group_id": a.group_id,
        "source_meeting_id": a.source_meeting_id,
        "title": a.title,
        "owner_person_id": a.owner_person_id,
        "status": a.status,
        "priority": a.priority,
        "due_at": a.due_at,
        "origin": a.origin,
        "updated_at": a.updated_at,
    }


def _digest_json(d: GroupDigest) -> dict:
    return {
        "id": d.id,
        "group_id": d.group_id,
        "period_start": d.period_start,
        "period_end": d.period_end,
        "content": json.loads(d.content_json),
        "content_md": d.content_md,
        "created_at": d.created_at,
    }
