# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""行动项清单 CSV：Excel 友好（UTF-8 BOM），出处用本地溯源现算。

不读 ActionItem.evidence_json——那是模型自报的 segment_ids，从未校验过。
"""

from __future__ import annotations

import csv
import io
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from huiyiku.db.models import ActionItem, Meeting, MeetingSpeaker, Person, TranscriptSegment
from huiyiku.domain.report_evidence import EvidenceIndex, segment_dicts_from_rows
from huiyiku.export.common import ms_to_clock

CSV_COLUMNS = [
    "会议",
    "会议日期",
    "行动项",
    "负责人",
    "截止",
    "优先级",
    "状态",
    "出处时间点",
    "出处人物",
    "原句",
    "出处备注",
]


def build_action_rows(
    session: Session,
    *,
    person_id: int | None = None,
    group_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    runtime: Any = None,
) -> list[dict[str, Any]]:
    q = select(ActionItem)
    if person_id is not None:
        q = q.where(ActionItem.owner_person_id == person_id)
    if group_id is not None:
        q = q.where(ActionItem.group_id == group_id)
    if status:
        q = q.where(ActionItem.status == status)
    items = list(session.scalars(q.order_by(ActionItem.id.desc())))
    meetings = {
        m.id: m
        for m in session.scalars(
            select(Meeting).where(Meeting.id.in_({a.source_meeting_id for a in items} or {0}))
        )
    }
    persons = {p.id: p.name for p in session.scalars(select(Person))}
    if date_from or date_to:
        items = [
            a
            for a in items
            if _date_ok(meetings.get(a.source_meeting_id).occurred_at if a.source_meeting_id else None, date_from, date_to)
        ]

    # 出处按来源会议分组现算：每场会议只建一次索引
    indexes: dict[int, EvidenceIndex | None] = {}
    for mid in {a.source_meeting_id for a in items}:
        if mid is None:
            continue
        seg_dicts = _seg_dicts_for(session, mid)
        indexes[mid] = EvidenceIndex(seg_dicts) if seg_dicts else None

    rows = []
    pending: list[tuple[ActionItem, str]] = []
    item_ev: dict[int, list] = {}
    for a in items:
        idx = indexes.get(a.source_meeting_id)
        ev: list = []
        if idx is not None:
            ev = idx.resolve(a.title or "").get("evidence") or []
        item_ev[a.id] = ev
        if not ev and (a.title or "").strip():
            pending.append((a, a.title or ""))
    # 词法未命中的行动项走与报告出处同一个向量兜底（第五路同源）
    if runtime is not None and pending:
        from huiyiku.domain.report_evidence import VECTOR_MIN_SCORE
        from huiyiku.llm.report import build_vector_fallback

        by_meeting: dict[int, list[tuple[ActionItem, str]]] = {}
        for a, title in pending:
            by_meeting.setdefault(a.source_meeting_id, []).append((a, title))
        for mid, pairs in by_meeting.items():
            seg_dicts = _seg_dicts_for(session, mid)
            if not seg_dicts:
                continue
            fallback = build_vector_fallback(session, runtime, mid, seg_dicts)
            if fallback is None:
                continue
            try:
                batch = fallback([title for _, title in pairs])
            except Exception:
                continue
            for (a, _title), cands in zip(pairs, batch, strict=False):
                hits = sorted(
                    (c for c in cands or [] if float(c.get("score") or 0) >= VECTOR_MIN_SCORE),
                    key=lambda c: -float(c.get("score") or 0),
                )[:2]
                if hits:
                    item_ev[a.id] = [{**c, "vector_matched": True} for c in hits]

    rows = []
    for a in items:
        meeting = meetings.get(a.source_meeting_id)
        ev = item_ev[a.id]
        note = "时间近似" if any(e.get("vector_matched") for e in ev) else ""
        if ev:
            where = "；".join(ms_to_clock(e.get("start_ms")) for e in ev)
            who = "；".join(str(e.get("speaker_name") or "?") for e in ev)
            quote = " ／ ".join(str(e.get("text") or "") for e in ev)
        else:
            where = who = quote = ""
        rows.append(
            {
                "会议": meeting.title if meeting else "",
                "会议日期": (meeting.occurred_at or "")[:10] if meeting else "",
                "行动项": a.title or "",
                "负责人": persons.get(a.owner_person_id) or "",
                "截止": a.due_at or "",
                "优先级": a.priority or "",
                "状态": a.status or "",
                "出处时间点": where,
                "出处人物": who,
                "原句": quote,
                "出处备注": note,
            }
        )
    return rows


def _seg_dicts_for(session: Session, mid: int) -> list[dict[str, Any]]:
    return segment_dicts_from_rows(
        session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == mid)
            .order_by(TranscriptSegment.start_ms)
        ),
        {
            s.id: s
            for s in session.scalars(
                select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == mid)
            )
        },
    )


def _date_ok(occurred_at: str | None, date_from: str | None, date_to: str | None) -> bool:
    day = (occurred_at or "")[:10]
    if date_from and day and day < date_from:
        return False
    if date_to and day and day > date_to:
        return False
    return True


def _formula_safe(value: Any) -> str:
    """防 CSV 公式注入：以 = + - @ 或控制符开头的单元格加 ' 前缀。

    行动项文本来自 LLM 输出，Excel 打开时可能被当公式执行。
    """
    text = "" if value is None else str(value)
    if text and text[0] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + text
    return text


def render_actions_csv(rows: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    # utf-8-sig：带 BOM，Excel 直接打开中文不乱码
    writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _formula_safe(v) for k, v in row.items()})
    return out.getvalue()
