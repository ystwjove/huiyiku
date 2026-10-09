# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""问答线程导出：md/html，每轮 Q/A（【AI 补充】拆块）+ 引用列表。"""

from __future__ import annotations

import html as html_mod
from typing import Any

from huiyiku.export.common import ms_to_clock
from huiyiku.export.html import _CSS
from huiyiku.llm.qa import EXTRA_MARKER


def _turn_blocks(answer: str) -> tuple[str, str]:
    parts = (answer or "").split(EXTRA_MARKER, 1)
    ground = (parts[0] or "").strip()
    extra = (parts[1] or "").strip() if len(parts) > 1 else ""
    return ground, extra


def _cite_lines(cites: list[dict[str, Any]], indent: str) -> list[str]:
    if not cites:
        return []
    out = [f"{indent}- 引用："]
    for c in cites:
        if c.get("chunk_type") and c.get("chunk_type") != "transcript":
            out.append(f"{indent}  - 报告条目：{c.get('text') or ''}")
            continue
        name = c.get("speaker_name") or "?"
        out.append(
            f"{indent}  - {ms_to_clock(c.get('start_ms'))} · {name}　{c.get('text') or ''}"
        )
    return out


def render_qa_md(title: str, turns: list[dict[str, Any]]) -> str:
    lines = [f"# {title}", ""]
    for i, t in enumerate(turns, start=1):
        ground, extra = _turn_blocks(t.get("answer") or "")
        lines.append(f"## 第 {i} 问")
        lines.append("")
        lines.append(f"**问：** {t.get('question') or ''}")
        lines.append("")
        lines.append(ground)
        lines.append("")
        if extra:
            lines.append(f"> AI 补充（非录音内容，不作为会议事实）：{extra}")
            lines.append("")
        lines.extend(_cite_lines(t.get("citations") or [], ""))
    return "\n".join(lines) + "\n"


def _esc(v: Any) -> str:
    return html_mod.escape(str(v if v is not None else ""))


def _cite_html(cites: list[dict[str, Any]]) -> str:
    if not cites:
        return ""
    out = ['<div class="qa-cites">']
    for c in cites:
        if c.get("chunk_type") and c.get("chunk_type") != "transcript":
            out.append(f'<div class="cite"><span class="tag tag-mid">报告条目</span> {_esc(c.get("text") or "")}</div>')
            continue
        name = c.get("speaker_name") or "?"
        out.append(
            f'<div class="cite"><span class="t">{ms_to_clock(c.get("start_ms"))}</span> '
            f"{_esc(name)}：{_esc(c.get('text') or '')}</div>"
        )
    out.append("</div>")
    return "".join(out)


def render_qa_html(title: str, turns: list[dict[str, Any]]) -> str:
    parts = [f"<h1>{_esc(title)}</h1>"]
    for i, t in enumerate(turns, start=1):
        ground, extra = _turn_blocks(t.get("answer") or "")
        parts.append(f"<h2>第 {i} 问</h2>")
        parts.append(f'<p class="qa-q"><strong>问：</strong>{_esc(t.get("question") or "")}</p>')
        parts.append(f'<p style="white-space: pre-wrap">{_esc(ground)}</p>')
        if extra:
            parts.append(
                '<div class="qa-extra"><p class="muted">AI 补充（非录音内容，不作为会议事实）</p>'
                f'<div style="white-space: pre-wrap">{_esc(extra)}</div></div>'
            )
        parts.append(_cite_html(t.get("citations") or []))
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        f"<title>{_esc(title)}</title><style>{_CSS}</style>"
        "<style>.qa-cites .cite { border-left: 3px solid #4f6ef7; padding-left: 10px; margin: 6px 0; font-size: 13px; }"
        ".qa-cites .t { color: #4f6ef7; font-variant-numeric: tabular-nums; }"
        ".qa-extra { background: #fff7ed; border-radius: 8px; padding: 8px 12px; margin: 8px 0; }"
        ".qa-q { font-size: 15px; }</style>"
        f"<body>{''.join(parts)}</body></html>"
    )
