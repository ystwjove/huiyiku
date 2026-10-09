# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from huiyiku.config import RuntimeContext
from huiyiku.db.models import GroupDigest, Job, Meeting, Report, ReportVersion
from huiyiku.llm.client import ChatError, estimate_tokens, map_reduce_groups
from huiyiku.llm.report import _llm_for_role
from huiyiku.timeutil import now_iso, pulse_progress


def generate_digest(
    session: Session, runtime: RuntimeContext, job: Job, progress, cancel_check=None
) -> None:
    payload = json.loads(job.payload_json or "{}")
    group_id = payload.get("group_id")
    if not group_id:
        raise ChatError("digest missing group_id")
    period_start = payload.get("period_start")
    period_end = payload.get("period_end")
    q = select(Meeting).where(Meeting.group_id == group_id, Meeting.status == "ready")
    meetings = list(session.scalars(q))
    accepted_docs: list[tuple[int, str]] = []
    for meeting in meetings:
        report = session.scalar(select(Report).where(Report.meeting_id == meeting.id))
        if report is None or report.current_version_id is None:
            continue
        ver = session.get(ReportVersion, report.current_version_id)
        if ver is None or ver.status != "accepted":
            continue
        if period_start and meeting.occurred_at < period_start:
            continue
        if period_end and meeting.occurred_at > period_end:
            continue
        accepted_docs.append((meeting.id, ver.content_md or ver.content_json))
    if not accepted_docs:
        raise ChatError("no accepted reports in range")
    if cancel_check and cancel_check():
        raise ChatError("cancelled")
    client, model = _llm_for_role(session, runtime, "digest")
    ctx = getattr(client, "context_length", 32000)
    texts = [f"会议 {mid}:\n{doc}" for mid, doc in accepted_docs]
    progress({"stage": "digest", "percent": 20})
    if estimate_tokens("\n".join(texts)) > ctx * 0.6:
        notes = []
        for i, group in enumerate(map_reduce_groups(texts, ctx), start=1):
            if cancel_check and cancel_check():
                raise ChatError("cancelled")
            progress({"stage": "digest-map", "i": i})
            with pulse_progress(progress, "digest-map-llm"):
                notes.append(
                    client.complete(
                        [
                            {
                                "role": "system",
                                "content": "抽取项目进展 JSON：progress/decisions/risks/action_items/next_steps，每条含 meeting_id。",
                            },
                            {"role": "user", "content": "\n\n".join(group)},
                        ],
                        json_mode=True,
                    )["content"]
                )
        user_content = "\n\n".join(notes)
    else:
        user_content = "\n\n".join(texts)
    if cancel_check and cancel_check():
        raise ChatError("cancelled")
    with pulse_progress(progress, "digest-llm"):
        raw = client.complete(
            [
                {
                    "role": "system",
                    "content": "生成会议组进展 JSON："
                    '{"period":"...","progress":[],"decisions":[],"risks":[],"action_items":[],'
                    '"next_steps":[],"source_meetings":[]}。每条必须能回到会议 ID。中文。不要编造。',
                },
                {"role": "user", "content": user_content},
            ],
            json_mode=True,
        )
    data = json.loads(raw["content"])
    data["source_meetings"] = [mid for mid, _ in accepted_docs]
    ts = now_iso()
    digest = GroupDigest(
        group_id=group_id,
        period_start=period_start,
        period_end=period_end,
        content_json=json.dumps(data, ensure_ascii=False),
        content_md=_digest_md(data),
        source_meeting_ids_json=json.dumps(data["source_meetings"]),
        model=f"{model.provider}:{model.model_name}",
        prompt_version="digest-v1",
        created_at=ts,
    )
    session.add(digest)
    job.usage_json = json.dumps(raw.get("usage") or {})
    progress({"stage": "digest", "percent": 100})


def _digest_md(data: dict) -> str:
    lines = ["# 项目进展", "", data.get("period") or "", ""]
    for key in ("progress", "decisions", "risks", "action_items", "next_steps"):
        lines.append(f"## {key}")
        for item in data.get(key) or []:
            if isinstance(item, dict):
                lines.append(f"- {item.get('content') or item.get('title') or item}")
            else:
                lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines)
