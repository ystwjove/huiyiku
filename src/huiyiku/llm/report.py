# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from huiyiku.config import RuntimeContext
from huiyiku.db.models import (
    Job,
    Meeting,
    MeetingSpeaker,
    ModelRegistry,
    Report,
    ReportVersion,
    TranscriptSegment,
)
from huiyiku.domain.citations import verify_citation
from huiyiku.domain.report_schema import empty_report, report_to_markdown, validate_report
from huiyiku.llm.client import ChatClient, ChatError, estimate_tokens, map_reduce_groups
from huiyiku.timeutil import now_iso, pulse_progress

PROMPT_V1 = "report-v1"
MAP_PROMPT_V1 = "report-map-v1"


def _llm_for_role(
    session: Session, runtime: RuntimeContext, role: str
) -> tuple[ChatClient, ModelRegistry]:
    settings = runtime.settings
    override = {
        "report": settings.report_llm_model_id,
        "qa": settings.qa_llm_model_id,
        "digest": settings.digest_llm_model_id,
    }.get(role)
    model = None
    if override:
        model = session.get(ModelRegistry, override)
    if model is None:
        model = session.scalar(
            select(ModelRegistry).where(
                ModelRegistry.task == "llm",
                ModelRegistry.is_default == 1,
                ModelRegistry.status == "available",
            )
        )
    if model is None:
        raise ChatError("no default LLM configured")
    if model.execution != "local":
        if not runtime.settings.allow_cloud:
            raise ChatError("allow_cloud is false")
        if not model.terms_accepted:
            raise ChatError("terms not accepted")
    key = runtime.secrets.llm_api_key
    if not key:
        raise ChatError("LLM API key missing")
    caps = json.loads(model.capabilities_json or "{}")
    client = ChatClient(
        model.endpoint or "", key, model.model_name, session_id=runtime.settings.client_session
    )
    client.context_length = int(caps.get("context_length") or 32000)  # type: ignore[attr-defined]
    return client, model


def generate_report(
    session: Session,
    runtime: RuntimeContext,
    job: Job,
    progress,
    cancel_check=None,
) -> None:
    if cancel_check and cancel_check():
        raise ChatError("cancelled")
    if not job.meeting_id:
        raise ChatError("report job missing meeting")
    meeting = session.get(Meeting, job.meeting_id)
    if meeting is None:
        raise ChatError("meeting missing")
    client, model = _llm_for_role(session, runtime, "report")
    segs = list(
        session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == meeting.id)
            .order_by(TranscriptSegment.start_ms)
        )
    )
    speakers = {
        s.id: s
        for s in session.scalars(
            select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == meeting.id)
        )
    }
    lines = []
    for seg in segs:
        sp = speakers.get(seg.speaker_id) if seg.speaker_id else None
        name = (sp.display_name if sp else None) or (sp.speaker_label if sp else "?")
        lines.append(f"[{seg.id} | {seg.start_ms}ms | {name}] {seg.normalized_text or seg.text}")
    corpus = [s.text for s in segs]
    ctx = getattr(client, "context_length", 32000)
    progress({"stage": "report", "percent": 15})
    if cancel_check and cancel_check():
        raise ChatError("cancelled")
    if estimate_tokens("\n".join(lines)) < ctx * 0.6:
        with pulse_progress(progress, "report-llm"):
            raw = client.complete(
                [
                    {"role": "system", "content": _system_prompt(False)},
                    {"role": "user", "content": "\n".join(lines)},
                ],
                json_mode=True,
                # 推理模型：思考与正文共享 token 预算，给足 8192
                max_tokens=8192,
            )
        prompt_version = PROMPT_V1
        content = raw["content"]
        usage = raw.get("usage")
    else:
        groups = map_reduce_groups(lines, ctx)
        notes = []
        for i, group in enumerate(groups, start=1):
            if cancel_check and cancel_check():
                raise ChatError("cancelled")
            progress({"stage": "report-map", "i": i, "n": len(groups)})
            with pulse_progress(progress, "report-map-llm"):
                part = client.complete(
                    [
                        {"role": "system", "content": _map_prompt()},
                        {"role": "user", "content": "\n".join(group)},
                    ],
                    json_mode=True,
                    max_tokens=4096,
                )
            notes.append(part["content"])
        with pulse_progress(progress, "report-reduce-llm"):
            raw = client.complete(
                [
                    {"role": "system", "content": _system_prompt(True)},
                    {"role": "user", "content": "\n\n".join(notes)},
                ],
                json_mode=True,
                # 推理模型：思考与正文共享 token 预算，给足 8192
                max_tokens=8192,
            )
        prompt_version = MAP_PROMPT_V1
        content = raw["content"]
        usage = raw.get("usage")
    data = _parse_json_object(content)
    data = validate_report({**empty_report(), **data})
    _verify_report_quotes(data, corpus)
    auto = any(s.status == "pending" for s in speakers.values())
    data["auto_speakers_unreviewed"] = auto
    ts = now_iso()
    report = session.scalar(select(Report).where(Report.meeting_id == meeting.id))
    if report is None:
        report = Report(meeting_id=meeting.id, created_at=ts, updated_at=ts)
        session.add(report)
        session.flush()
    version_no = 1
    if report.current_version_id:
        cur = session.get(ReportVersion, report.current_version_id)
        if cur:
            version_no = cur.version + 1
    ver = ReportVersion(
        report_id=report.id,
        version=version_no,
        source="llm",
        model=f"{model.provider}:{model.model_name}",
        prompt_version=prompt_version,
        content_json=json.dumps(data, ensure_ascii=False),
        content_md=report_to_markdown(data),
        status="draft",
        created_at=ts,
    )
    session.add(ver)
    session.flush()
    report.current_version_id = ver.id
    report.updated_at = ts
    meeting.status = "report_draft"
    meeting.updated_at = ts
    job.usage_json = json.dumps(usage or {})
    progress({"stage": "report", "percent": 100})


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def _system_prompt(reduce: bool) -> str:
    extra = "你正在合并多段抽取结果，必须覆盖全部时间范围，不得丢掉后半场。" if reduce else ""
    return (
        "你是会议记录助手。根据转写生成 JSON："
        '{"summary":"...","key_points":[{"content":"...","speaker_id":null,"person_id":null,'
        '"time_ms":0,"segment_ids":[]}],"decisions":[],"action_items":[{"title":"...","owner_person_id":null,'
        '"due_at":null,"priority":"normal","segment_ids":[]}],"risks":[],"open_questions":[],'
        '"participants":[{"person_id":null,"name":"...","spoke":true}]}。'
        "每条重点/决议/行动项必须引用 segment_ids 与 time_ms。无法确定负责人时 owner_person_id 为 null。"
        "不要编造转写中没有的事实。" + extra
    )


def _map_prompt() -> str:
    return '从这段转写抽取 JSON：{"points":[],"decisions":[],"actions":[],"risks":[],"questions":[]}。引用 segment id。'


def _verify_report_quotes(data: dict[str, Any], corpus: list[str]) -> None:
    for key in ("key_points", "decisions", "risks", "open_questions"):
        for item in data.get(key) or []:
            quote = item.get("content") or ""
            item["verified"] = verify_citation(quote, corpus) if quote else False
    for item in data.get("action_items") or []:
        quote = item.get("title") or ""
        item["verified"] = verify_citation(quote, corpus) if quote else True


def attach_report_evidence(
    session: Session,
    meeting_id: int,
    content: dict[str, Any],
    *,
    runtime: RuntimeContext | None = None,
) -> None:
    """就地给报告 content 补出处（三档标签+证据段+汇总+参会人发言数）。

    供 API 读取与导出共用；不写库（报告版本快照不可变），说话人改名后
    下次读取自动跟随新名字。runtime 提供且配置了可用 embedding 模型时，
    对 unsupported 条目做一次批量向量兜底（见 build_vector_fallback）。
    """
    from huiyiku.domain.report_evidence import (
        EvidenceIndex,
        resolve_report_evidence,
        segment_dicts_from_rows,
    )

    segs = list(
        session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == meeting_id)
            .order_by(TranscriptSegment.start_ms)
        )
    )
    if not segs:
        return
    speakers = {
        s.id: s
        for s in session.scalars(
            select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == meeting_id)
        )
    }
    seg_dicts = segment_dicts_from_rows(segs, speakers)
    fallback = build_vector_fallback(session, runtime, meeting_id, seg_dicts)
    counts = resolve_report_evidence(content, EvidenceIndex(seg_dicts), vector_fallback=fallback)
    # 读路径（GET /report、导出）在此提交自愈落库的 chunk 向量与
    # embedding_dim；accept / 开机修复路径不走这里，由各自调用方提交
    session.commit()
    content["evidence_summary"] = {
        **counts,
        "span_ms": [seg_dicts[0]["start_ms"], max(s["end_ms"] for s in seg_dicts)],
    }
    _attach_participant_counts(content, seg_dicts, speakers)


def build_vector_fallback(
    session: Session,
    runtime: RuntimeContext | None,
    meeting_id: int,
    seg_dicts: list[dict[str, Any]],
):
    """构造向量兜底 callable(texts) -> list[list[候选段]]；不可用时返回 None。

    供 attach_report_evidence（GET /report、导出）与 store._index_report_chunks
    （accept / 开机修复）共用——两条路径必须传同一个兜底，否则出处 chip 标
    释义有据、检索块时间仍是 0。条目文本与转写块（MeetingChunk，type=
    transcript）做余弦相似；chunk 向量复用已落库的 embedding（embedding_model
    匹配才可信），缺失的一次批量补齐并落库（自愈老会议，列早已存在，零迁移）。
    未配置（无 available 的默认 embedding 模型 / allow_cloud=False）返回 None，
    行为与无兜底完全一致。
    """
    if runtime is None or not runtime.settings.allow_cloud:
        return None
    from huiyiku.db.models import MeetingChunk
    from huiyiku.llm.embed import embed_texts_batch, pack_embedding, unpack_embedding

    model = session.scalar(
        select(ModelRegistry).where(
            ModelRegistry.task == "embedding",
            ModelRegistry.is_default == 1,
            ModelRegistry.status == "available",
        )
    )
    if model is None:
        return None
    emb_name = f"{model.provider}:{model.model_name}"

    def fallback(texts: list[str]) -> list[list[dict[str, Any]]]:
        if not texts:
            return []
        chunks = list(
            session.scalars(
                select(MeetingChunk).where(
                    MeetingChunk.meeting_id == meeting_id,
                    MeetingChunk.chunk_type == "transcript",
                    MeetingChunk.is_active == 1,
                )
            )
        )
        if not chunks:
            return [[] for _ in texts]
        missing = [c for c in chunks if not (c.embedding and c.embedding_model == emb_name)]
        if missing:
            try:
                vecs = embed_texts_batch(runtime, model, [c.content or "" for c in missing])
            except ChatError:
                vecs = None
            if vecs is not None:
                dim = model.embedding_dim or len(vecs[0])
                if model.embedding_dim is None:
                    model.embedding_dim = dim
                for c, v in zip(missing, vecs, strict=False):
                    if len(v) != dim:
                        continue
                    c.embedding = pack_embedding(v)
                    c.embedding_model = emb_name
                    c.embedding_dim = dim
                # 不在这里 commit：accept / 开机修复路径由调用方在末尾一次性
                # 提交（保持事务原子）；GET / 导出读路径由 attach_report_evidence
                # 提交（读路径无其他写入，自愈向量随读落库）
        chunk_vecs = [
            (c, unpack_embedding(c.embedding))
            for c in chunks
            if c.embedding and c.embedding_model == emb_name
        ]
        if not chunk_vecs:
            return [[] for _ in texts]
        try:
            item_vecs = embed_texts_batch(runtime, model, texts)
        except ChatError:
            return [[] for _ in texts]
        out: list[list[dict[str, Any]]] = []
        for iv in item_vecs:
            top = sorted(chunk_vecs, key=lambda p: -_cosine(iv, p[1]))[:3]
            cands: list[dict[str, Any]] = []
            for c, _cv in top:
                score = _cosine(iv, _cv)
                for seg in seg_dicts:
                    # chunk → 段按时间窗映射：取与块时间区间重叠的转写段
                    if seg["start_ms"] < c.end_ms and seg["end_ms"] > c.start_ms:
                        cands.append(
                            {
                                "segment_id": seg["id"],
                                "start_ms": seg["start_ms"],
                                "end_ms": seg["end_ms"],
                                "speaker_id": seg["speaker_id"],
                                "speaker_name": seg["speaker_name"],
                                "text": seg["text"],
                                "score": score,
                            }
                        )
            cands.sort(key=lambda x: (-x["score"], x["start_ms"]))
            out.append(cands[:6])
        return out

    return fallback


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _attach_participant_counts(
    content: dict[str, Any],
    seg_dicts: list[dict[str, Any]],
    speakers: dict[int, MeetingSpeaker],
) -> None:
    per_person: dict[int, int] = {}
    per_name: dict[str, int] = {}
    for seg in seg_dicts:
        if seg["speaker_id"] is None:
            continue
        sp = speakers.get(seg["speaker_id"])
        if sp is None:
            continue
        if sp.person_id is not None:
            per_person[sp.person_id] = per_person.get(sp.person_id, 0) + 1
        elif sp.display_name:
            per_name[sp.display_name] = per_name.get(sp.display_name, 0) + 1
    for item in content.get("participants") or []:
        if not isinstance(item, dict):
            continue
        pid = item.get("person_id")
        if pid is not None:
            item["segment_count"] = per_person.get(pid, 0)
        else:
            item["segment_count"] = per_name.get(str(item.get("name") or ""), 0)
