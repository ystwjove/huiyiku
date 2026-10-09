# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""导出共用的文档模型与响应工具。

各格式渲染器（markdown/html/docx/csv）消费这里构建的结构化文档，
保证不同格式内容一致：条目、三档出处标签、证据段时间点与人物。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from huiyiku.db.models import Meeting, Person, Report, ReportVersion, TranscriptSegment
from huiyiku.llm.report import attach_report_evidence
from huiyiku.paths import safe_filename

SECTION_ORDER = (
    ("key_points", "重点", "content"),
    ("decisions", "决议", "content"),
    ("action_items", "行动项", "title"),
    ("risks", "风险", "content"),
    ("open_questions", "待跟进", "content"),
)

STATUS_LABELS = {"quoted": "录音原文", "paraphrase": "释义", "unsupported": "AI 补充·录音未找到"}


def ms_to_clock(ms: Any) -> str:
    try:
        total = int(ms or 0) // 1000
    except (TypeError, ValueError):
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _person_names(session: Session) -> dict[int, str]:
    return {p.id: p.name for p in session.scalars(select(Person))}


def build_meeting_doc(
    session: Session,
    meeting: Meeting,
    version: ReportVersion | None,
    *,
    transcript: bool,
    runtime: Any = None,
) -> dict[str, Any]:
    """把一场会议的报告（含出处）整理成格式无关的文档模型。

    runtime 用于向量兜底——导出必须与 GET /report、accept 走同一个溯源，
    否则 UI 标「释义（时间近似）」的条目在导出里仍显示 AI 补充。
    """
    content: dict[str, Any] = {}
    if version is not None:
        try:
            loaded = json.loads(version.content_json)
            if isinstance(loaded, dict):
                content = loaded
        except ValueError:
            content = {}
    if content:
        attach_report_evidence(session, meeting.id, content, runtime=runtime)
    persons = _person_names(session)

    sections: list[dict[str, Any]] = []
    for key, title, field in SECTION_ORDER:
        items_out: list[dict[str, Any]] = []
        for item in content.get(key) or []:
            if not isinstance(item, dict):
                items_out.append({"text": str(item), "status": None, "evidence": []})
                continue
            status = item.get("evidence_status")
            items_out.append(
                {
                    "text": str(item.get(field) or item.get("title") or item.get("content") or ""),
                    "status": status,
                    "status_label": STATUS_LABELS.get(status),
                    "evidence": item.get("evidence") or [],
                    "owner_name": persons.get(item.get("owner_person_id")),
                    "due_at": item.get("due_at"),
                    "priority": item.get("priority"),
                }
            )
        if items_out:
            sections.append({"title": title, "items": items_out})

    doc: dict[str, Any] = {
        "title": meeting.title,
        "occurred_at": meeting.occurred_at,
        "version_status": version.status if version else None,
        "stale_reason": version.stale_reason if version else None,
        "summary": str(content.get("summary") or ""),
        "auto_speakers_unreviewed": bool(content.get("auto_speakers_unreviewed")),
        "evidence_summary": content.get("evidence_summary") or {},
        "sections": sections,
        "participants": content.get("participants") or [],
        "has_report": version is not None,
    }
    if transcript:
        doc["transcript"] = _transcript_lines(session, meeting.id)
    return doc


def _transcript_lines(session: Session, meeting_id: int) -> list[dict[str, Any]]:
    from huiyiku.db.models import MeetingSpeaker

    speakers = {
        s.id: s
        for s in session.scalars(
            select(MeetingSpeaker).where(MeetingSpeaker.meeting_id == meeting_id)
        )
    }
    lines = []
    for seg in session.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.start_ms)
    ):
        sp = speakers.get(seg.speaker_id) if seg.speaker_id else None
        name = (sp.display_name if sp else None) or (sp.speaker_label if sp else "?")
        lines.append(
            {"start_ms": seg.start_ms, "speaker_name": name, "text": seg.text or ""}
        )
    return lines


def current_report_version(session: Session, meeting: Meeting) -> ReportVersion | None:
    report = session.scalar(select(Report).where(Report.meeting_id == meeting.id))
    if report is None or not report.current_version_id:
        return None
    return session.get(ReportVersion, report.current_version_id)


_WINDOWS_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Windows 保留设备名：实测以此为文件名主体会抛 OSError（aux）或行为不可预期
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _windows_safe(name: str) -> str:
    """safe_filename 只挡路径分隔符；Windows 还禁 :*?"<>| 与保留设备名。

    半角冒号尤其隐蔽：写盘不报错但会落到 NTFS 备用数据流，文件名与
    扩展名直接丢失，所以这里统一替换。
    """
    cleaned = _WINDOWS_ILLEGAL.sub("_", name)
    cleaned = cleaned.strip(" ._") or "会议"
    # 保留名看"第一个点之前"的片段（aux.txt 同样非法），所以必须改前缀，
    # 在后缀上追加救不了
    if cleaned.split(".")[0].lower() in _WINDOWS_RESERVED:
        cleaned = f"会议-{cleaned}"
    return cleaned


def export_filename(title: str, occurred_at: str | None, ext: str) -> str:
    date = (occurred_at or "")[:10].replace("-", "") or "无日期"
    try:
        base = safe_filename(title or "会议")
    except ValueError:
        # 标题恰好是 "." / ".." 之类：safe_filename 会抛错，导出不该 500
        base = "会议"
    return f"{_windows_safe(base)}-{date}.{ext}"


def ascii_fallback_name(filename: str) -> str:
    """给 Content-Disposition 的 filename= 造 ASCII 回退名。

    纯中文标题走 filename.encode("ascii","ignore") 会退化成 "-20260924.md"
    （连名字都没了），这里保证回退名可读、且与 UTF-8 的 filename* 区分开。
    """
    stem, dot, ext = filename.rpartition(".")
    ascii_stem = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-")
    if not re.search(r"[A-Za-z]", ascii_stem):
        digits = re.sub(r"[^0-9]", "", ascii_stem)
        ascii_stem = "meeting" + (f"-{digits}" if len(digits) >= 6 else "")
    return f"{ascii_stem}.{ext}" if dot else ascii_stem


def export_response(
    content: bytes, media_type: str, filename: str, exports_dir: Path
) -> Response:
    """落盘到 exports 目录并按附件返回（WebView2 内下载行为不可靠，落盘是保底）。"""
    exports_dir.mkdir(parents=True, exist_ok=True)
    (exports_dir / filename).write_bytes(content)
    disposition = (
        f'attachment; filename="{ascii_fallback_name(filename)}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    # HTTP 头只允许 latin-1：路径里有中文必须 percent-encode
    header_path = quote(str(exports_dir / filename))
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": disposition,
            "X-Export-Path": header_path,
        },
    )
