# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""自包含 HTML 渲染：内联 CSS、无 JS，浏览器打印即可存成 PDF。"""

from __future__ import annotations

import html as html_mod
from typing import Any

from huiyiku.export.common import ms_to_clock

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; margin: 32px auto;
       max-width: 860px; color: #1f2430; line-height: 1.7; background: #fff; }
h1 { font-size: 24px; } h2 { font-size: 19px; margin-top: 28px; }
.meta { color: #5b6472; font-size: 13px; }
.sec { margin: 18px 0; }
.item { border: 1px solid #e6e9ef; border-radius: 8px; padding: 10px 14px; margin: 10px 0; }
.item-text { white-space: pre-wrap; }
.ev { color: #4f6ef7; font-size: 13px; margin-top: 6px; }
.ev .none { color: #d97706; }
.quote { color: #5b6472; font-size: 13px; border-left: 3px solid #4f6ef7;
         padding-left: 10px; margin-top: 6px; }
.tag { display: inline-block; font-size: 12px; border-radius: 999px; padding: 1px 10px;
       border: 1px solid #e6e9ef; color: #5b6472; margin-left: 8px; }
.tag-ok { color: #16a34a; border-color: #9fdcb6; }
.tag-warn { color: #d97706; border-color: #f0c98a; }
.tr { font-size: 13px; color: #333; }
.tr .t { color: #4f6ef7; font-variant-numeric: tabular-nums; }
.note { background: #fff7ed; border-radius: 8px; padding: 8px 12px; color: #92600a; font-size: 13px; }
@media print { body { margin: 0; } .item { break-inside: avoid; } }
"""


def _esc(text: Any) -> str:
    return html_mod.escape(str(text if text is not None else ""))


def _tag(status: str | None) -> str:
    if not status:
        return ""
    cls = {"quoted": "tag tag-ok", "paraphrase": "tag", "unsupported": "tag tag-warn"}.get(
        status, "tag"
    )
    labels = {"quoted": "录音原文", "paraphrase": "释义", "unsupported": "AI 补充·录音未找到"}
    return f'<span class="{cls}">{_esc(labels.get(status, status))}</span>'


def _item_html(item: dict[str, Any]) -> str:
    ev = item.get("evidence") or []
    extra = ""
    if item.get("owner_name"):
        extra = f'<span class="meta">负责人：{_esc(item["owner_name"])}'
        if item.get("due_at"):
            extra += f"，截止：{_esc(item['due_at'])}"
        extra += "</span>"
    if ev:
        ev_html = (
            '<div class="ev">出处：'
            + "；".join(
                f"{ms_to_clock(e.get('start_ms'))}"
                + ("（时间近似）" if e.get("vector_matched") else "")
                + f" · {_esc(e.get('speaker_name') or '?')}"
                for e in ev
            )
            + "</div>"
        )
        quotes = "".join(
            f'<div class="quote">{ms_to_clock(e.get("start_ms"))} '
            f"{_esc(e.get('speaker_name') or '?')}：{_esc(e.get('text') or '')}</div>"
            for e in ev
        )
    else:
        ev_html = '<div class="ev none">出处：未在录音中定位到原文（AI 补充）</div>'
        quotes = ""
    return (
        f'<div class="item"><div class="item-text">{_esc(item.get("text"))}{_tag(item.get("status"))}</div>'
        f"{extra}{ev_html}{quotes}</div>"
    )


def _body(doc: dict[str, Any]) -> str:
    status = "已接受" if doc.get("version_status") == "accepted" else "草稿"
    parts = [
        f"<h1>{_esc(doc['title'])}</h1>",
        f'<p class="meta">时间：{_esc(doc.get("occurred_at"))}　报告状态：{status}</p>',
    ]
    if doc.get("stale_reason"):
        parts.append(f'<p class="note">{_esc(doc["stale_reason"])}</p>')
    if doc.get("auto_speakers_unreviewed"):
        parts.append('<p class="note">说话人尚未人工校对，出处里的人物名为机器标注。</p>')
    if doc.get("summary"):
        parts.append("<h2>摘要</h2>")
        parts.append(f'<div class="sec">{_esc(doc["summary"])}</div>')
    for sec in doc.get("sections") or []:
        parts.append(f"<h2>{_esc(sec['title'])}</h2>")
        parts.extend(_item_html(item) for item in sec["items"])
    if doc.get("participants"):
        names = "、".join(
            _esc(p.get("name") or "?")
            + (f"（发言 {p['segment_count']} 段）" if p.get("segment_count") else "")
            for p in doc["participants"]
            if isinstance(p, dict)
        )
        parts.append(f"<h2>参会人</h2><p>{names}</p>")
    transcript = doc.get("transcript")
    if transcript:
        parts.append("<h2>转写</h2>")
        rows = "".join(
            f'<p class="tr"><span class="t">{ms_to_clock(t["start_ms"])}</span> '
            f"{_esc(t['speaker_name'])}：{_esc(t['text'])}</p>"
            for t in transcript
        )
        parts.append(rows)
    if not doc.get("has_report"):
        parts.append('<p class="meta">（本会议还没有生成报告）</p>')
    return "\n".join(parts)


def render_meeting_html(doc: dict[str, Any]) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        f"<title>{_esc(doc['title'])}</title><style>{_CSS}</style>"
        f"<body>{_body(doc)}</body></html>"
    )


def render_group_html(group_name: str, digest_md: str, meeting_docs: list[dict[str, Any]]) -> str:
    parts = [
        f"<h1>会议组汇总：{_esc(group_name)}</h1>",
        f'<p class="meta">共 {len(meeting_docs)} 场会议</p>',
    ]
    if digest_md:
        parts.append("<h2>项目进展</h2>")
        parts.extend(f'<p>{_esc(line)}</p>' for line in digest_md.splitlines() if line.strip())
    for doc in meeting_docs:
        status = "已接受" if doc.get("version_status") == "accepted" else "草稿"
        parts.append(f'<p class="meta">{_esc(doc.get("occurred_at"))}　{status}</p>')
        parts.append(_body(doc))
    return (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        f"<title>汇总：{_esc(group_name)}</title><style>{_CSS}</style>"
        f"<body>{'<hr>'.join(parts)}</body></html>"
    )
