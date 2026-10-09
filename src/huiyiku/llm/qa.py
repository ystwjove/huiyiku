# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from huiyiku.config import RuntimeContext
from huiyiku.db.models import (
    Meeting,
    MeetingChunk,
    MeetingSpeaker,
    ModelRegistry,
    QaMessage,
    TranscriptSegment,
)
from huiyiku.domain.chunks import cosine, rrf_fuse, tokenize
from huiyiku.domain.citations import verify_citation
from huiyiku.llm.client import ChatError, estimate_tokens
from huiyiku.llm.embed import embed_text, unpack_embedding
from huiyiku.llm.report import _llm_for_role
from huiyiku.timeutil import now_iso


def _mentions_chunk(answer: str, chunk_id: int) -> bool:
    """True if the answer cites this chunk id, not a longer numeric prefix (chunk 3 vs 35)."""
    return bool(re.search(rf"(?i)(?<!\d)chunk\s+{int(chunk_id)}(?!\d)", answer or ""))


def _fts_query(question: str, scope: dict[str, Any]) -> tuple[Any, dict[str, Any]] | None:
    """构造 FTS5 查询：OR 连接各分词 + 按 bm25 相关度排序 + 作用域全部下推。

    以前是隐式 AND（所有分词都要命中）+ 无排序：用户的问句只要有一个词
    没出现就整体落空、退化成按时间取样，模型会拿着不相关材料作答。实测
    "长期打折…海康老师同步" 这类问句 AND 只命中 1 条（还是报告块），
    换成 OR + ORDER BY rank 后最相关的原文段排到了第 1 位。
    作用域（类型/会议/分组/人物/日期）必须全部下推：SQL 带 LIMIT，留在
    Python 侧过滤会被不相关的块挤掉。无有效分词时返回 None——空问题交给
    调用方走兜底，不能拿空串去 MATCH（FTS5 会报语法错误）。
    """
    tokens = [t for t in tokenize(question).split() if t]
    if not tokens:
        return None
    match = " OR ".join('"{}"'.format(t.replace('"', '""')) for t in tokens)
    sql = (
        "SELECT meeting_chunks.id FROM meeting_chunks_fts "
        "JOIN meeting_chunks ON meeting_chunks.id = meeting_chunks_fts.rowid "
        "WHERE meeting_chunks_fts MATCH :q AND meeting_chunks.is_active=1"
    )
    params: dict[str, Any] = {"q": match}
    for key, column in (
        ("chunk_types", "chunk_type"),
        ("meeting_ids", "meeting_id"),
        ("group_ids", "group_id"),
    ):
        values = [v for v in (scope.get(key) or []) if v is not None]
        if not values:
            continue
        names = []
        for i, value in enumerate(values):
            name = f"{key}_{i}"
            names.append(f":{name}")
            params[name] = value
        sql += f" AND meeting_chunks.{column} IN ({', '.join(names)})"
    person_id = scope.get("person_id")
    if person_id:
        # 与 _scope_ok 的软过滤语义一致：没识别出说话人的块照常通过
        sql += (
            " AND (meeting_chunks.speaker_id IS NULL OR meeting_chunks.speaker_id IN "
            "(SELECT id FROM meeting_speakers WHERE person_id = :person_id))"
        )
        params["person_id"] = int(person_id)
    if scope.get("start"):
        sql += (
            " AND meeting_chunks.meeting_id IN "
            "(SELECT id FROM meetings WHERE occurred_at >= :date_from)"
        )
        params["date_from"] = scope["start"]
    if scope.get("end"):
        sql += (
            " AND meeting_chunks.meeting_id IN "
            "(SELECT id FROM meetings WHERE occurred_at <= :date_to)"
        )
        params["date_to"] = scope["end"]
    sql += " ORDER BY meeting_chunks_fts.rank LIMIT 30"
    return text(sql), params


async def retrieve(
    session: Session,
    runtime: RuntimeContext,
    scope: dict[str, Any],
    question: str,
) -> list[MeetingChunk]:
    _build_missing_chunks(session, scope)
    query = _fts_query(question, scope)
    ids = [int(r[0]) for r in session.execute(*query)] if query is not None else []
    chunks = (
        list(session.scalars(select(MeetingChunk).where(MeetingChunk.id.in_(ids)))) if ids else []
    )
    # 按 bm25 相关度重排（SQL 里已 ORDER BY rank，in_() 查询会打乱顺序）
    order = {cid: i for i, cid in enumerate(ids)}
    chunks.sort(key=lambda c: order.get(c.id, len(order)))
    chunks = [c for c in chunks if _scope_ok(session, c, scope)]
    fts_ids = [c.id for c in chunks]
    vec_ids: list[int] = []
    embed_model = session.scalar(
        select(ModelRegistry).where(
            ModelRegistry.task == "embedding",
            ModelRegistry.is_default == 1,
            ModelRegistry.status == "available",
        )
    )
    qvec = None
    if (
        embed_model
        and embed_model.embedding_dim
        and runtime.settings.allow_cloud
        and (runtime.secrets.embedding_api_key or runtime.secrets.llm_api_key)
    ):
        try:
            qvec = await anyio.to_thread.run_sync(embed_text, runtime, embed_model, question)
        except (ChatError, KeyError, IndexError, TypeError, ValueError, httpx.HTTPError):
            qvec = None
        if (
            qvec is not None
            and embed_model.embedding_dim
            and len(qvec) != embed_model.embedding_dim
        ):
            qvec = None
    active = list(
        session.scalars(
            select(MeetingChunk).where(
                MeetingChunk.is_active == 1, MeetingChunk.embedding.is_not(None)
            )
        )
    )
    active = [c for c in active if _scope_ok(session, c, scope)]
    if qvec is not None and active and embed_model is not None:
        expected = f"{embed_model.provider}:{embed_model.model_name}"
        scored = []
        for c in active:
            if c.embedding_dim != embed_model.embedding_dim:
                continue
            if c.embedding_model != expected:
                continue
            scored.append((cosine(qvec, unpack_embedding(c.embedding)), c.id))
        scored.sort(reverse=True)
        vec_ids = [cid for _, cid in scored[:20]]
    fused = rrf_fuse(fts_ids, vec_ids) if vec_ids else fts_ids[:20]
    if not fused:
        # 未配置 embedding 时检索是纯关键词的，"这场会讨论了什么" 这类问题
        # 一个关键词都命中不了，只能答"没有找到相关材料"。范围已限定到具体
        # 会议/项目时按时间均匀取样交给模型，比空手回话有用得多。
        fused = _sample_scope_chunk_ids(session, scope)
    by_id = (
        {c.id: c for c in session.scalars(select(MeetingChunk).where(MeetingChunk.id.in_(fused)))}
        if fused
        else {}
    )
    return [by_id[i] for i in fused if i in by_id]


def _scope_meeting_ids(session: Session, scope: dict[str, Any]) -> list[int]:
    meeting_ids = [int(x) for x in (scope.get("meeting_ids") or [])]
    group_ids = [int(x) for x in (scope.get("group_ids") or [])]
    if not meeting_ids and group_ids:
        meeting_ids = list(
            session.scalars(select(Meeting.id).where(Meeting.group_id.in_(group_ids)))
        )
    return meeting_ids


def _build_missing_chunks(session: Session, scope: dict[str, Any]) -> None:
    """给"已转写但没建过检索块"的会议补建块。

    转写块以前只在确认说话人时才建，于是"转写完直接生成报告"的会议在问答/检索
    里永远是空的。这里在检索前补建一次，让老数据也能被问到。
    """
    from huiyiku.db.models import TranscriptSegment
    from huiyiku.store import rebuild_stats_and_chunks

    for mid in _scope_meeting_ids(session, scope)[:5]:
        has_active = session.scalar(
            select(func.count())
            .select_from(MeetingChunk)
            .where(
                MeetingChunk.meeting_id == mid,
                MeetingChunk.is_active == 1,
                MeetingChunk.chunk_type == "transcript",
            )
        )
        if has_active:
            continue
        has_segments = session.scalar(
            select(func.count())
            .select_from(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == mid)
        )
        if not has_segments:
            continue
        meeting = session.get(Meeting, mid)
        if meeting is None:
            continue
        rebuild_stats_and_chunks(session, meeting, session.connection().connection)
        session.commit()


def _sample_scope_chunk_ids(
    session: Session, scope: dict[str, Any], limit_per_meeting: int = 12
) -> list[int]:
    """范围已限定时，按时间均匀取样会议分片；无范围限定则不兜底。

    无范围时兜底等于把整个库塞给模型，既超 token 又答不准，所以只处理
    scope 明确指向的会议（或项目下的会议）。
    """
    meeting_ids = _scope_meeting_ids(session, scope)
    if not meeting_ids:
        return []
    picked: list[int] = []
    for mid in meeting_ids[:5]:
        chunks = list(
            session.scalars(
                select(MeetingChunk)
                .where(MeetingChunk.meeting_id == mid, MeetingChunk.is_active == 1)
                .order_by(MeetingChunk.id)
            )
        )
        chunks = [c for c in chunks if _scope_ok(session, c, scope)]
        if not chunks:
            continue
        step = max(1, len(chunks) // limit_per_meeting)
        picked.extend(c.id for c in chunks[::step][:limit_per_meeting])
    return picked


def _scope_ok(session: Session, chunk: MeetingChunk, scope: dict[str, Any]) -> bool:
    meeting_ids = scope.get("meeting_ids")
    group_ids = scope.get("group_ids")
    types = scope.get("chunk_types")
    person_id = scope.get("person_id")
    if meeting_ids and chunk.meeting_id not in meeting_ids:
        return False
    if group_ids and chunk.group_id not in group_ids:
        return False
    if types and chunk.chunk_type not in types:
        return False
    start = scope.get("start")
    end = scope.get("end")
    if start or end:
        meeting = session.get(Meeting, chunk.meeting_id)
        if meeting is None:
            return False
        if start and meeting.occurred_at < start:
            return False
        if end and meeting.occurred_at > end:
            return False
    if person_id and chunk.speaker_id:
        from huiyiku.db.models import MeetingSpeaker

        sp = session.get(MeetingSpeaker, chunk.speaker_id)
        if sp is None or sp.person_id != person_id:
            return False
    return True


EXTRA_MARKER = "【AI 补充（非录音内容）】"

_EXTRA_SYSTEM = (
    "下面是用户的问题，以及仅基于会议录音材料的回答。"
    "请只补充回答未覆盖的通用背景知识或合乎常理的推断，用中文，具体、克制。"
    "不得虚构会议事实，不得声称补充内容来自录音，不得引用 chunk。"
    "没有值得补充的内容就一个字也不要输出。"
)


async def _stream_extra(
    client, question: str, ground: str
) -> AsyncIterator[tuple[str, bool]]:
    """开关开启时的第二次调用：只补背景知识，与会议事实严格分开。

    逐段产出 (token, ok)；流中途失败产出 ok=False 后结束——补充是可选
    增强，它的失败只降级提示，绝不能让已经生成的主回答丢失入库。
    """
    try:
        async for token in client.stream(
            [
                {"role": "system", "content": _EXTRA_SYSTEM},
                {"role": "user", "content": f"问题：{question}\n\n已有回答：{ground}"},
            ]
        ):
            yield (token, True)
    except Exception:
        logging.getLogger("huiyiku.qa").warning("extra answer stream failed", exc_info=True)
        yield ("", False)


QA_HISTORY_TURNS = 3
QA_HISTORY_CHARS = 500


def _load_history(session: Session, session_id: str) -> list[dict[str, str]]:
    """取最近几轮问答做多轮对话；历史只保留录音事实。

    答案先切掉【AI 补充】段（背景知识不进对话历史），并各自截断——
    长答案会把本轮材料预算吃光。历史仅用于理解指代，不参与引用。
    """
    if not session_id or session_id == "ui":
        return []
    rows = session.scalars(
        select(QaMessage)
        .where(QaMessage.session_id == session_id)
        .order_by(QaMessage.id.desc())
        .limit(QA_HISTORY_TURNS)
    ).all()
    turns: list[dict[str, str]] = []
    for rec in reversed(rows):
        answer = (rec.answer or "").split(EXTRA_MARKER)[0].strip()
        question = (rec.question or "").strip()
        if not answer or not question:
            continue
        turns.append({"q": question[:QA_HISTORY_CHARS], "a": answer[:QA_HISTORY_CHARS]})
    return turns


async def stream_answer(
    session: Session,
    runtime: RuntimeContext,
    *,
    session_id: str,
    question: str,
    scope: dict[str, Any],
    allow_extra: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    history = _load_history(session, session_id)
    # 检索 query 带上一轮问题：追问里的指代实体（"它/那笔预算"）在旧句里
    retrieve_query = question
    if history:
        retrieve_query = history[-1]["q"] + "\n" + question
    chunks = await retrieve(session, runtime, scope, retrieve_query)
    client, model = _llm_for_role(session, runtime, "qa")
    embed_model = session.scalar(
        select(ModelRegistry).where(
            ModelRegistry.task == "embedding",
            ModelRegistry.is_default == 1,
            ModelRegistry.status == "available",
        )
    )
    speaker_names = _speaker_names(session, chunks)
    ctx = getattr(client, "context_length", 32000)
    budget = int(ctx * 0.8)
    citations = []
    packed = []
    used = estimate_tokens(question) + 400
    used += sum(estimate_tokens(h["q"]) + estimate_tokens(h["a"]) for h in history)
    omitted = False
    for ch in chunks:
        piece = f"[chunk {ch.id} meeting {ch.meeting_id} {ch.start_ms}-{ch.end_ms}ms]\n{ch.content}"
        cost = estimate_tokens(piece)
        if used + cost > budget:
            omitted = True
            break
        packed.append(piece)
        citations.append(
            {
                "chunk_id": ch.id,
                "meeting_id": ch.meeting_id,
                "start_ms": ch.start_ms,
                "end_ms": ch.end_ms,
                "speaker_id": ch.speaker_id,
                "speaker_name": speaker_names.get(ch.speaker_id),
                "chunk_type": ch.chunk_type,
                "text": ch.content,
            }
        )
        used += cost

    if not packed:
        ground = "没有找到相关材料。"
        yield {"event": "token", "text": ground}
        extra_parts: list[str] = []
        if allow_extra:
            async for token, ok in _stream_extra(client, question, ground):
                if not ok:
                    # 补充失败：半截背景话不入库，只给瞬时提示
                    extra_parts.clear()
                    yield {"event": "token", "kind": "extra", "text": "（补充生成失败，可重试）"}
                    break
                extra_parts.append(token)
                yield {"event": "token", "kind": "extra", "text": token}
        extra_text = "".join(extra_parts)
        full = f"{ground}\n\n{EXTRA_MARKER}\n{extra_text}" if extra_text.strip() else ground
        rec = _save(
            session,
            session_id,
            scope,
            question,
            full,
            citations,
            [],
            model,
            omitted,
            embed_model=embed_model,
            allow_extra=allow_extra,
        )
        yield {"event": "done", "message": _qa_public(rec)}
        return
    sys_prompt = (
        "根据引用的会议材料用中文回答。必须引用 chunk id。材料不足时说「没有找到」，不要编造。"
        "对话历史只用于理解指代（如「它/那笔预算」指什么），引用只能来自本轮材料。"
    )
    if omitted:
        sys_prompt += "部分材料因上下文限制已省略。"
    llm_messages: list[dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    for h in history:
        llm_messages.append({"role": "user", "content": h["q"]})
        llm_messages.append({"role": "assistant", "content": h["a"]})
    llm_messages.append(
        {"role": "user", "content": question + "\n\n" + "\n\n".join(packed)}
    )
    answer_parts: list[str] = []
    async for token in client.stream(llm_messages):
        answer_parts.append(token)
        yield {"event": "token", "text": token}
    answer = "".join(answer_parts)
    for cit in citations:
        snippet = (cit.get("text") or "")[:80]
        used = _mentions_chunk(answer, cit["chunk_id"])
        quoted = bool(snippet) and verify_citation(snippet, [answer])
        cit["verified"] = bool(used or quoted)
    extra_parts = []
    if allow_extra:
        async for token, ok in _stream_extra(client, question, answer):
            if not ok:
                # 补充失败：半截背景话不入库，只给瞬时提示
                extra_parts.clear()
                yield {"event": "token", "kind": "extra", "text": "（补充生成失败，可重试）"}
                break
            extra_parts.append(token)
            yield {"event": "token", "kind": "extra", "text": token}
    extra_text = "".join(extra_parts)
    full = f"{answer}\n\n{EXTRA_MARKER}\n{extra_text}" if extra_text.strip() else answer
    rec = _save(
        session,
        session_id,
        scope,
        question,
        full,
        citations,
        packed,
        model,
        omitted,
        embed_model=embed_model,
        allow_extra=allow_extra,
    )
    yield {"event": "done", "message": _qa_public(rec)}


def _speaker_names(session: Session, chunks: list[MeetingChunk]) -> dict[int, str]:
    sids = {c.speaker_id for c in chunks if c.speaker_id is not None}
    if not sids:
        return {}
    rows = session.scalars(select(MeetingSpeaker).where(MeetingSpeaker.id.in_(sids)))
    return {
        r.id: r.display_name or r.speaker_label for r in rows if r.display_name or r.speaker_label
    }


VERDICT_LABELS = {"verbatim": "原文照搬", "paraphrase": "释义有据", "unsupported": "无依据"}

_NEIGHBOUR_MS = 120_000


def _material_from_segment(seg: dict[str, Any]) -> dict[str, Any]:
    # 兼容两种输入：转写段字典用 "id"，resolve() 的 evidence 字典用
    # "segment_id"——此前只读 "id"，把本地证据转成材料后 segment_id 全是
    # None，去重集合为空，同一段会被邻域补齐再次加入（引用出现重复时间点）
    sid = seg.get("id") if seg.get("id") is not None else seg.get("segment_id")
    return {
        "segment_id": sid,
        "start_ms": seg.get("start_ms") or 0,
        "end_ms": seg.get("end_ms") or 0,
        "speaker_name": seg.get("speaker_name") or "?",
        "text": str(seg.get("text") or "")[:120],
    }


def _overlaps_any(start: int, end: int, materials: list[dict[str, Any]]) -> bool:
    """按真实时间重叠判重（不能用固定时间窗：相邻但内容不同的段会被误当重复）。"""
    for m in materials:
        a = int(m.get("start_ms") or 0)
        b = int(m.get("end_ms") or 0)
        overlap = min(int(end or 0), b) - max(int(start or 0), a)
        if overlap <= 0:
            continue
        shorter = max(1, min(int(end or 0) - int(start or 0), b - a))
        if overlap / shorter >= 0.5:
            return True
    return False


async def verify_item(
    session: Session,
    runtime: RuntimeContext,
    *,
    meeting_id: int,
    text: str,
) -> dict[str, Any]:
    """核实一条报告内容是否被录音支持：本地溯源候选 + 检索候选 → 一次 LLM 判定。

    自动溯源（report_evidence）是纯本地的三档标签；这里是对单条的按需深核。
    候选材料必须**先用本地溯源的结果**（与报告页出处 chip 同源），再补检索
    命中——否则会出现"页面标了出处、核实却说无依据"的自相矛盾：条目文本在
    检索里往往只命中报告自己生成的块（被类型过滤掉），于是退化成按时间取样。
    """
    from huiyiku.domain.report_evidence import (
        EvidenceIndex,
        content_tokens,
        segment_dicts_from_rows,
    )
    from huiyiku.llm.report import _parse_json_object

    speakers = {
        s.id: s
        for s in session.scalars(
            select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == meeting_id)
        )
    }

    def _name(sid: int | None) -> str:
        sp = speakers.get(sid) if sid else None
        return (sp.display_name if sp else None) or (sp.speaker_label if sp else "?")

    materials: list[dict[str, Any]] = []
    seg_lookup: dict[int, dict[str, Any]] = {}
    seg_dicts = segment_dicts_from_rows(
        session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == meeting_id)
            .order_by(TranscriptSegment.start_ms)
        ),
        speakers,
    )
    for s in seg_dicts:
        if s.get("id") is not None:
            seg_lookup[int(s["id"])] = s
    if seg_dicts:
        local = EvidenceIndex(seg_dicts).resolve(text)
        item_tokens = set(content_tokens(text))
        for ev in local.get("evidence") or []:
            materials.append(_material_from_segment(ev))
        # 邻域补齐：一条报告条目常横跨相邻几句，只取命中的那 3 句会丢掉
        # 后半句（实测"没做过专题分析"和"和海康老师同步"相隔 12 秒两段）
        picked_ids = {int(m["segment_id"]) for m in materials if m.get("segment_id")}
        for mid_ev in list(materials):
            centre = int(mid_ev["start_ms"] or 0)
            for s in seg_dicts:
                sid = int(s.get("id") or 0)
                if sid in picked_ids or not item_tokens:
                    continue
                start = int(s.get("start_ms") or 0)
                if abs(start - centre) > _NEIGHBOUR_MS:
                    continue
                if not (item_tokens & set(tokenize(str(s.get("text") or "")).split())):
                    continue
                picked_ids.add(sid)
                materials.append(_material_from_segment(s))
                if len(materials) >= 6:
                    break
    chunks = await retrieve(
        session, runtime, {"meeting_ids": [meeting_id], "chunk_types": ["transcript"]}, text
    )
    for c in chunks[:8]:
        if _overlaps_any(c.start_ms, c.end_ms, materials):
            continue
        materials.append(
            {
                "start_ms": c.start_ms,
                "end_ms": c.end_ms,
                "speaker_name": _name(c.speaker_id),
                "text": (c.content or "")[:120],
            }
        )
    materials = materials[:10]
    citations = [
        {
            "start_ms": m["start_ms"],
            "end_ms": m["end_ms"],
            "speaker_name": m["speaker_name"],
            "text": m["text"],
        }
        for m in materials
    ]
    if not materials:
        return {
            "verdict": "unsupported",
            "verdict_label": VERDICT_LABELS["unsupported"],
            "reason": "录音中未检索到相关内容。",
            "citations": [],
        }
    client, _model = _llm_for_role(session, runtime, "qa")
    packed = [
        f"[材料 {i} {m['start_ms']}ms {m['speaker_name']}]\n{m['text']}"
        for i, m in enumerate(materials, start=1)
    ]
    raw = client.complete(
        [
            {
                "role": "system",
                "content": (
                    "判断这条会议报告内容是否被录音转写支持。只依据给出的材料，输出 JSON："
                    '{"verdict":"verbatim|paraphrase|unsupported","reason":"一句话中文理由"}。'
                    "verbatim=与原文基本一致；paraphrase=意思在录音里有但措辞不同；"
                    "unsupported=材料里找不到依据。不要编造。"
                    "若材料支持了主要部分、只是细节没提到，选 paraphrase 并在理由里"
                    "说明哪部分未被材料覆盖；只有主要部分也找不到依据才选 unsupported。"
                ),
            },
            {
                "role": "user",
                "content": f"报告内容：{text}\n\n材料：\n" + "\n\n".join(packed),
            },
        ],
        json_mode=True,
        max_tokens=2048,
    )
    try:
        data = _parse_json_object(raw.get("content") or "")
    except ValueError:
        data = {}
    verdict = str(data.get("verdict") or "unsupported")
    if verdict not in VERDICT_LABELS:
        verdict = "unsupported"
    return {
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "reason": str(data.get("reason") or ""),
        "citations": citations,
    }


def _save(
    session,
    session_id,
    scope,
    question,
    answer,
    citations,
    retrieval,
    model,
    omitted,
    embed_model=None,
    allow_extra: bool = False,
) -> QaMessage:
    model_payload: dict[str, Any] = {
        "provider": model.provider,
        "model": model.model_name,
        "task": "llm",
    }
    if embed_model is not None:
        model_payload["embedding"] = {
            "provider": embed_model.provider,
            "model": embed_model.model_name,
            "task": "embedding",
            "dim": embed_model.embedding_dim,
        }
    rec = QaMessage(
        session_id=session_id,
        scope_json=json.dumps(scope, ensure_ascii=False),
        question=question,
        answer=answer,
        citations_json=json.dumps(citations, ensure_ascii=False),
        retrieval_json=json.dumps(
            {"omitted": omitted, "n": len(retrieval), "allow_extra": allow_extra},
            ensure_ascii=False,
        ),
        model_json=json.dumps(model_payload, ensure_ascii=False),
        usage_json=json.dumps(
            {
                "omitted": omitted,
                "retrieval_n": len(retrieval),
                "prompt_tokens": estimate_tokens(question)
                + sum(estimate_tokens(p) for p in retrieval),
                "completion_tokens": estimate_tokens(answer),
            },
            ensure_ascii=False,
        ),
        created_at=now_iso(),
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    return rec


def _qa_public(rec: QaMessage) -> dict[str, Any]:
    return {
        "id": rec.id,
        "answer": rec.answer,
        "citations": json.loads(rec.citations_json or "[]"),
        "created_at": rec.created_at,
    }
