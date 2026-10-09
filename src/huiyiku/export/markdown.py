# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""Markdown 渲染：条目带〔时间点 · 人物〕出处与原句引用。"""

from __future__ import annotations

from typing import Any

from huiyiku.export.common import ms_to_clock


def _provenance_lines(item: dict[str, Any], indent: str) -> list[str]:
    ev = item.get("evidence") or []
    if not ev:
        return [f"{indent}- 出处：未在录音中定位到原文（AI 补充）"]
    out = []
    for e in ev:
        name = e.get("speaker_name") or "?"
        near = "（时间近似）" if e.get("vector_matched") else ""
        out.append(
            f"{indent}- 出处：{ms_to_clock(e.get('start_ms'))}{near} · {name}　原句：{e.get('text') or ''}"
        )
    return out


def render_meeting_md(doc: dict[str, Any]) -> str:
    lines: list[str] = [f"# {doc['title']}", ""]
    status = "已接受" if doc.get("version_status") == "accepted" else "草稿"
    lines.append(f"时间: {doc.get('occurred_at') or ''}　报告状态: {status}")
    if doc.get("stale_reason"):
        lines.append(f"注意: {doc['stale_reason']}")
    if doc.get("auto_speakers_unreviewed"):
        lines.append("说话人尚未人工校对，出处里的人物名为机器标注。")
    lines.append("")

    if doc.get("summary"):
        lines.extend(["## 摘要", "", doc["summary"], ""])
    for sec in doc.get("sections") or []:
        lines.extend([f"## {sec['title']}", ""])
        for item in sec["items"]:
            extra = ""
            if item.get("owner_name"):
                extra += f"（负责人：{item['owner_name']}"
                if item.get("due_at"):
                    extra += f"，截止：{item['due_at']}"
                extra += "）"
            lines.append(f"- {item['text']}{extra}")
            lines.extend(_provenance_lines(item, "  "))
        lines.append("")
    if doc.get("participants"):
        parts = [
            f"{p.get('name') or '?'}"
            + (f"（发言 {p['segment_count']} 段）" if p.get("segment_count") else "")
            for p in doc["participants"]
            if isinstance(p, dict)
        ]
        if parts:
            lines.extend(["## 参会人", "", "、".join(parts), ""])

    transcript = doc.get("transcript")
    if transcript:
        lines.extend(["## 转写", ""])
        for t in transcript:
            lines.append(f"- {ms_to_clock(t['start_ms'])} {t['speaker_name']}: {t['text']}")
        lines.append("")
    if not doc.get("has_report"):
        lines.extend(["（本会议还没有生成报告）", ""])
    return "\n".join(lines) + "\n"


def render_group_md(group_name: str, digest_md: str, meeting_docs: list[dict[str, Any]]) -> str:
    lines: list[str] = [f"# 会议组汇总：{group_name}", "", f"共 {len(meeting_docs)} 场会议。", ""]
    if digest_md:
        lines.extend(["## 项目进展", "", digest_md, ""])
    for doc in meeting_docs:
        status = "已接受" if doc.get("version_status") == "accepted" else "草稿"
        lines.extend([f"## {doc['title']}（{doc.get('occurred_at') or ''}，{status}）", ""])
        if doc.get("summary"):
            lines.extend([f"{doc['summary']}", ""])
        for sec in doc.get("sections") or []:
            lines.append(f"### {sec['title']}")
            for item in sec["items"]:
                lines.append(f"- {item['text']}")
                lines.extend(_provenance_lines(item, "  "))
            lines.append("")
    return "\n".join(lines) + "\n"
