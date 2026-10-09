# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from huiyiku.db.migrate import UNGROUPED_NAME
from huiyiku.db.models import (
    ActionItem,
    ActionItemEvent,
    Group,
    Job,
    Meeting,
    MeetingChunk,
    MeetingParticipant,
    MeetingSpeaker,
    ModelRegistry,
    Report,
    ReportVersion,
    SpeakerStat,
    SpeakerTurn,
    TranscriptSegment,
)
from huiyiku.domain.chunks import build_transcript_chunks, tokenize
from huiyiku.domain.jobs import DEFAULT_TIMEOUT, MEETING_STATUS_FOR_JOB, idempotency_key
from huiyiku.domain.stats import METRIC_VERSION, compute_speaker_stats
from huiyiku.timeutil import file_mtime_iso, now_iso


def _fts_delete(session: Session, cid: int) -> None:
    session.execute(text("DELETE FROM meeting_chunks_fts WHERE rowid = :id"), {"id": cid})


def _fts_insert(session: Session, cid: int, tokenized: str | None) -> None:
    session.execute(
        text("INSERT INTO meeting_chunks_fts(rowid, tokenized_content) VALUES (:id, :tok)"),
        {"id": cid, "tok": tokenized or ""},
    )


def get_ungrouped(session: Session) -> Group:
    g = session.scalar(select(Group).where(Group.is_system == 1, Group.name == UNGROUPED_NAME))
    if g is None:
        raise RuntimeError("system group missing")
    return g


def enqueue_job(
    session: Session,
    job_type: str,
    *,
    meeting_id: int | None = None,
    payload: dict[str, Any] | None = None,
    model_version: str = "",
    set_meeting_status: bool = True,
) -> Job:
    payload = payload or {}
    key = idempotency_key(meeting_id, job_type, payload, model_version)
    existing = session.scalar(select(Job).where(Job.idempotency_key == key))
    if existing and existing.status in {"queued", "running", "success"}:
        return existing
    job = Job(
        meeting_id=meeting_id,
        type=job_type,
        status="queued",
        payload_json=json.dumps(payload, ensure_ascii=False),
        idempotency_key=key,
        attempt_count=0,
        timeout_seconds=DEFAULT_TIMEOUT.get(job_type, 1800),
        cancel_requested=0,
        created_at=now_iso(),
    )
    session.add(job)
    if set_meeting_status and meeting_id and job_type in MEETING_STATUS_FOR_JOB:
        meeting = session.get(Meeting, meeting_id)
        if meeting is not None and job_type != "digest":
            meeting.status = MEETING_STATUS_FOR_JOB[job_type]
            meeting.updated_at = now_iso()
    session.flush()
    return job


def create_meeting(
    session: Session,
    *,
    title: str,
    group_id: int | None = None,
    source_type: str = "upload",
    occurred_at: str | None = None,
) -> Meeting:
    if group_id is None:
        group_id = get_ungrouped(session).id
    ts = now_iso()
    meeting = Meeting(
        group_id=group_id,
        title=title or "未命名会议",
        occurred_at=occurred_at or ts,
        source_type=source_type,
        language="zh",
        status="created",
        created_at=ts,
        updated_at=ts,
    )
    session.add(meeting)
    session.flush()
    return meeting


def attach_original(
    session: Session,
    meeting: Meeting,
    src: Path,
    dest_dir: Path,
    *,
    filename: str | None = None,
    apply_mtime: bool = False,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    from huiyiku.paths import safe_filename

    name = safe_filename(filename or src.name)
    dest = dest_dir / f"{meeting.id}_{name}"
    shutil.copy2(src, dest)
    digest = hashlib.sha256()
    with dest.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    meeting.original_filename = name
    meeting.original_path = f"media/original/{dest.name}"
    meeting.file_size_bytes = dest.stat().st_size
    meeting.media_checksum = digest.hexdigest()
    if apply_mtime:
        meeting.occurred_at = file_mtime_iso(src)
    meeting.updated_at = now_iso()
    return dest


def persist_transcript(
    session: Session,
    meeting: Meeting,
    segments: list[dict[str, Any]],
    *,
    provider: str,
    model: str,
    version: str,
) -> None:
    # 顺序敏感：SpeakerStat 外键引用 MeetingSpeaker，必须先删统计；
    # 旧转写的派生块/嵌入一并失活，避免重转写后残留可检索的陈旧内容
    session.query(SpeakerStat).filter(SpeakerStat.meeting_id == meeting.id).delete()
    session.query(TranscriptSegment).filter(TranscriptSegment.meeting_id == meeting.id).delete()
    session.query(SpeakerTurn).filter(SpeakerTurn.meeting_id == meeting.id).delete()
    deactivate_chunks(session, meeting.id)
    session.query(MeetingSpeaker).filter(MeetingSpeaker.meeting_id == meeting.id).delete()
    ts = now_iso()
    labels = [s.get("speaker_label") for s in segments]
    has_spk = any(labels)
    duration = meeting.duration_ms or 0
    if segments:
        duration = max(duration, max(int(s.get("end_ms") or 0) for s in segments))
    speaker_rows: dict[str, MeetingSpeaker] = {}
    if not has_spk:
        sp = MeetingSpeaker(
            meeting_id=meeting.id,
            speaker_label="SPEAKER_00",
            display_name="SPEAKER_00",
            status="pending",
            updated_at=ts,
        )
        session.add(sp)
        session.flush()
        speaker_rows["SPEAKER_00"] = sp
        session.add(
            SpeakerTurn(
                meeting_id=meeting.id,
                speaker_label="SPEAKER_00",
                start_ms=0,
                end_ms=duration,
                model=f"{provider}:{model}",
                created_at=ts,
            )
        )
    else:
        for label in dict.fromkeys(str(x) for x in labels if x):
            sp = MeetingSpeaker(
                meeting_id=meeting.id,
                speaker_label=label,
                display_name=label,
                status="pending",
                updated_at=ts,
            )
            session.add(sp)
            session.flush()
            speaker_rows[label] = sp
    for seg in segments:
        label = str(seg.get("speaker_label") or "SPEAKER_00")
        sp = speaker_rows[label]
        start = int(seg.get("start_ms") or 0)
        end = int(seg.get("end_ms") or 0)
        if has_spk:
            turn = SpeakerTurn(
                meeting_id=meeting.id,
                speaker_label=label,
                start_ms=start,
                end_ms=end,
                model=f"{provider}:{model}",
                created_at=ts,
            )
            session.add(turn)
            session.flush()
            evidence = json.dumps([turn.id])
        else:
            evidence = None
        text = str(seg.get("text") or "")
        session.add(
            TranscriptSegment(
                meeting_id=meeting.id,
                speaker_id=sp.id,
                evidence_turn_ids_json=evidence,
                start_ms=start,
                end_ms=end,
                text=text,
                normalized_text=text,
                asr_provider=provider,
                asr_model=model,
                asr_model_version=version,
                review_status="auto",
                updated_at=ts,
            )
        )
    meeting.status = "speaker_review"
    meeting.updated_at = ts
    session.flush()


def next_manual_label(session: Session, meeting_id: int) -> str:
    rows = session.scalars(
        select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == meeting_id)
    ).all()
    n = 1
    existing = {r.speaker_label for r in rows}
    while f"SPEAKER_MANUAL_{n}" in existing:
        n += 1
    return f"SPEAKER_MANUAL_{n}"


def rebuild_stats_and_chunks(session: Session, meeting: Meeting, conn) -> None:
    session.flush()
    speakers = session.scalars(
        select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == meeting.id)
    ).all()
    segs = session.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting.id)
        .order_by(TranscriptSegment.start_ms)
    ).all()
    speaker_dicts = [
        {"id": s.id, "status": s.status, "speaker_label": s.speaker_label} for s in speakers
    ]
    seg_dicts = [
        {
            "id": s.id,
            "speaker_id": s.speaker_id,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "text": s.text,
            "normalized_text": s.normalized_text,
        }
        for s in segs
    ]
    session.query(SpeakerStat).filter(SpeakerStat.meeting_id == meeting.id).delete()
    ts = now_iso()
    for row in compute_speaker_stats(seg_dicts, speaker_dicts):
        session.add(
            SpeakerStat(
                meeting_id=meeting.id,
                speaker_id=row["speaker_id"],
                speech_ms=row["speech_ms"],
                segment_count=row["segment_count"],
                speech_ratio=row["speech_ratio"],
                timeline_json=json.dumps(row["timeline"]),
                metric_version=METRIC_VERSION,
                computed_at=ts,
            )
        )
    old_ids = [
        c.id
        for c in session.scalars(
            select(MeetingChunk).where(
                MeetingChunk.meeting_id == meeting.id,
                MeetingChunk.chunk_type == "transcript",
                MeetingChunk.is_active == 1,
            )
        )
    ]
    for cid in old_ids:
        chunk = session.get(MeetingChunk, cid)
        if chunk:
            chunk.is_active = 0
        _fts_delete(session, cid)
    speaker_map = {s.id: {"id": s.id, "status": s.status} for s in speakers}
    for ch in build_transcript_chunks(seg_dicts, speaker_map):
        row = MeetingChunk(
            meeting_id=meeting.id,
            group_id=meeting.group_id,
            speaker_id=ch["speaker_id"],
            start_ms=ch["start_ms"],
            end_ms=ch["end_ms"],
            chunk_type=ch["chunk_type"],
            content=ch["content"],
            tokenized_content=ch["tokenized_content"],
            is_active=1,
            created_at=ts,
        )
        session.add(row)
        session.flush()
        _fts_insert(session, row.id, row.tokenized_content)
    meeting.updated_at = ts


def deactivate_chunks(
    session: Session,
    meeting_id: int,
    *,
    types: tuple[str, ...] | None = None,
) -> None:
    q = select(MeetingChunk).where(
        MeetingChunk.meeting_id == meeting_id, MeetingChunk.is_active == 1
    )
    if types:
        q = q.where(MeetingChunk.chunk_type.in_(types))
    for chunk in session.scalars(q):
        chunk.is_active = 0
        _fts_delete(session, chunk.id)


def enqueue_embed_if_configured(
    session: Session, meeting: Meeting, *, allow_cloud: bool
) -> Job | None:
    if not allow_cloud:
        return None
    embed = session.scalar(
        select(ModelRegistry).where(
            ModelRegistry.task == "embedding",
            ModelRegistry.is_default == 1,
            ModelRegistry.status == "available",
        )
    )
    if embed is None:
        return None
    return enqueue_job(
        session,
        "embed",
        meeting_id=meeting.id,
        payload={"reason": "rebuild", "t": now_iso()},
        model_version=f"{embed.provider}:{embed.model_name}",
        set_meeting_status=False,
    )


def mark_reports_stale(session: Session, meeting_id: int, reason: str) -> None:
    report = session.scalar(select(Report).where(Report.meeting_id == meeting_id))
    if report is None or report.current_version_id is None:
        return
    ver = session.get(ReportVersion, report.current_version_id)
    if ver is not None:
        ver.stale_reason = reason


def delete_meeting_contents(
    session: Session,
    meeting: Meeting,
    data_dir: Path,
    conn,
) -> dict[str, Any]:
    mid = meeting.id
    chunk_ids = [
        c.id for c in session.scalars(select(MeetingChunk).where(MeetingChunk.meeting_id == mid))
    ]
    for cid in chunk_ids:
        _fts_delete(session, cid)
    session.query(MeetingChunk).filter(MeetingChunk.meeting_id == mid).delete()
    session.query(SpeakerStat).filter(SpeakerStat.meeting_id == mid).delete()
    session.query(TranscriptSegment).filter(TranscriptSegment.meeting_id == mid).delete()
    session.query(SpeakerTurn).filter(SpeakerTurn.meeting_id == mid).delete()
    session.query(MeetingSpeaker).filter(MeetingSpeaker.meeting_id == mid).delete()
    session.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == mid).delete()
    session.query(Job).filter(Job.meeting_id == mid).delete()
    reports = session.scalars(select(Report).where(Report.meeting_id == mid)).all()
    for report in reports:
        session.query(ReportVersion).filter(ReportVersion.report_id == report.id).delete()
        session.delete(report)
    extracted = session.scalars(
        select(ActionItem).where(
            ActionItem.source_meeting_id == mid,
            ActionItem.origin == "report_extract",
            ActionItem.merged_into_id.is_(None),
        )
    ).all()
    for item in extracted:
        events = session.scalars(
            select(ActionItemEvent).where(ActionItemEvent.action_item_id == item.id)
        ).all()
        other = [e for e in events if e.meeting_id and e.meeting_id != mid]
        if other:
            item.source_meeting_id = None
        else:
            session.query(ActionItemEvent).filter(
                ActionItemEvent.action_item_id == item.id
            ).delete()
            session.delete(item)
    leftovers = session.scalars(select(ActionItem).where(ActionItem.source_meeting_id == mid)).all()
    for item in leftovers:
        item.source_meeting_id = None
    files: list[Path] = []
    for rel in (
        meeting.original_path,
        meeting.wav_path,
        meeting.waveform_path,
        meeting.upload_audio_path,
    ):
        if not rel:
            continue
        path = (data_dir / rel).resolve()
        try:
            path.relative_to(data_dir.resolve())
        except ValueError:
            continue
        files.append(path)
    session.delete(meeting)
    return {"meeting_id": mid, "chunks_removed": len(chunk_ids), "files": files}


def accept_report_actions(
    session: Session,
    meeting: Meeting,
    version: ReportVersion,
    *,
    runtime: Any = None,
) -> None:
    data = json.loads(version.content_json)
    items = data.get("action_items") or []
    ts = now_iso()
    existing = session.scalars(
        select(ActionItem).where(ActionItem.source_meeting_id == meeting.id)
    ).all()
    for raw in items:
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        owner = raw.get("owner_person_id")
        dup = next(
            (
                a
                for a in existing
                if a.title == title and a.owner_person_id == owner and a.status != "cancelled"
            ),
            None,
        )
        if dup:
            session.add(
                ActionItemEvent(
                    action_item_id=dup.id,
                    from_status=dup.status,
                    to_status=dup.status,
                    reason="新版本再次提到",
                    meeting_id=meeting.id,
                    created_at=ts,
                )
            )
            continue
        item = ActionItem(
            group_id=meeting.group_id,
            source_meeting_id=meeting.id,
            source_report_version_id=version.id,
            title=title,
            owner_person_id=owner,
            status="open",
            priority=raw.get("priority") or "normal",
            due_at=raw.get("due_at"),
            evidence_json=json.dumps(raw.get("segment_ids") or []),
            origin="report_extract",
            user_edited=0,
            updated_at=ts,
        )
        session.add(item)
        session.flush()
        session.add(
            ActionItemEvent(
                action_item_id=item.id,
                from_status=None,
                to_status="open",
                reason="report_extract",
                meeting_id=meeting.id,
                created_at=ts,
            )
        )
        existing.append(item)
    deactivate_chunks(
        session,
        meeting.id,
        types=("summary", "decision", "action", "risk", "question"),
    )
    _index_report_chunks(
        session, meeting, version, data, runtime=runtime
    )


def rebuild_report_chunks(
    session: Session,
    meeting: Meeting,
    version: ReportVersion,
    *,
    runtime: Any = None,
) -> None:
    """按给定报告版本重建五类报告检索块（幂等：先失活再重建）。

    供 worker 开机的一次性老数据修复与后续重算使用。
    """
    data = json.loads(version.content_json)
    deactivate_chunks(
        session,
        meeting.id,
        types=("summary", "decision", "action", "risk", "question"),
    )
    _index_report_chunks(
        session, meeting, version, data, runtime=runtime
    )


def _index_report_chunks(
    session: Session,
    meeting: Meeting,
    version: ReportVersion,
    data: dict,
    *,
    runtime: Any = None,
) -> None:
    mapping = {
        "summary": "summary",
        "key_points": "summary",
        "decisions": "decision",
        "action_items": "action",
        "risks": "risk",
        "open_questions": "question",
    }
    ts = now_iso()
    conn = session.connection().connection
    from huiyiku.domain.report_evidence import EvidenceIndex, segment_dicts_from_rows

    seg_dicts = segment_dicts_from_rows(
        session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == meeting.id)
            .order_by(TranscriptSegment.start_ms)
        ),
        {
            s.id: s
            for s in session.scalars(
                select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == meeting.id)
            )
        },
    )
    index = EvidenceIndex(seg_dicts) if seg_dicts else None
    fallback = None
    if index is not None and runtime is not None:
        # 双路同源：accept / 开机修复路径与 GET /report 走同一个 resolve
        # （含向量兜底），保证出处标签与检索块时间一致
        from huiyiku.llm.report import build_vector_fallback

        try:
            fallback = build_vector_fallback(session, runtime, meeting.id, seg_dicts)
        except Exception:
            fallback = None
    if index is not None:
        from huiyiku.domain.report_evidence import resolve_report_evidence

        resolve_report_evidence(data, index, vector_fallback=fallback)
    if data.get("summary"):
        # 摘要是全场概括，没有单一出处，不带时间/人物
        _add_typed_chunk(session, conn, meeting, version, "summary", data["summary"], ts)
    for key, ctype in mapping.items():
        if key == "summary":
            continue
        for item in data.get(key) or []:
            text = item.get("content") or item.get("title") or ""
            if not text:
                continue
            evidence = item.get("evidence") or [] if isinstance(item, dict) else []
            _add_typed_chunk(
                session, conn, meeting, version, ctype, text, ts, evidence=evidence
            )


def _add_typed_chunk(
    session,
    conn,
    meeting,
    version,
    ctype,
    text,
    ts,
    evidence: list | None = None,
) -> None:
    # 报告条目的出处写进检索块：时间取证据段的区间，人物仅在唯一说话人时写。
    # 以前全写 0/None，导致按"决议/行动项"提问时引用全部跳到 00:00:00。
    ev = evidence or []
    starts = [int(e["start_ms"]) for e in ev if e.get("start_ms") is not None]
    ends = [int(e["end_ms"]) for e in ev if e.get("end_ms") is not None]
    sids = {e.get("speaker_id") for e in ev if e.get("speaker_id") is not None}
    row = MeetingChunk(
        meeting_id=meeting.id,
        group_id=meeting.group_id,
        speaker_id=next(iter(sids)) if len(sids) == 1 else None,
        start_ms=min(starts) if starts else 0,
        end_ms=max(ends) if ends else 0,
        chunk_type=ctype,
        content=text,
        tokenized_content=tokenize(text),
        source_report_version_id=version.id,
        is_active=1,
        created_at=ts,
    )
    session.add(row)
    session.flush()
    _fts_insert(session, row.id, row.tokenized_content)
