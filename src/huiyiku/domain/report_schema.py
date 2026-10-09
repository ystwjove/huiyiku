# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

REPORT_REQUIRED = (
    "summary",
    "key_points",
    "decisions",
    "action_items",
    "risks",
    "open_questions",
    "participants",
)


def empty_report() -> dict[str, Any]:
    return {
        "summary": "",
        "key_points": [],
        "decisions": [],
        "action_items": [],
        "risks": [],
        "open_questions": [],
        "participants": [],
        "auto_speakers_unreviewed": False,
    }


def validate_report(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("report must be an object")
    missing = [k for k in REPORT_REQUIRED if k not in data]
    if missing:
        raise ValueError(f"report missing keys: {missing}")
    for key in (
        "key_points",
        "decisions",
        "action_items",
        "risks",
        "open_questions",
        "participants",
    ):
        if not isinstance(data[key], list):
            raise ValueError(f"{key} must be a list")
    if not isinstance(data["summary"], str):
        raise ValueError("summary must be a string")
    for key in ("key_points", "decisions", "risks", "open_questions"):
        for i, item in enumerate(data[key]):
            if not isinstance(item, dict):
                raise ValueError(f"{key}[{i}] must be an object")
            ids = item.setdefault("segment_ids", [])
            if not isinstance(ids, list):
                raise ValueError(f"{key}[{i}].segment_ids must be a list")
            if (
                "time_ms" in item
                and item["time_ms"] is not None
                and not isinstance(item["time_ms"], int)
            ):
                raise ValueError(f"{key}[{i}].time_ms must be an int or null")
    for i, item in enumerate(data["action_items"]):
        if not isinstance(item, dict):
            raise ValueError(f"action_items[{i}] must be an object")
        item.setdefault("owner_person_id", None)
        ids = item.setdefault("segment_ids", [])
        if not isinstance(ids, list):
            raise ValueError(f"action_items[{i}].segment_ids must be a list")
    return data


def report_to_markdown(data: dict[str, Any]) -> str:
    lines = ["# 会议报告", ""]
    if data.get("auto_speakers_unreviewed"):
        lines.extend(["> 说话人尚未人工校对，报告仅供参考。", ""])
    lines.extend(["## 摘要", "", data.get("summary") or "", ""])

    def _section(title: str, items: list, field: str = "content") -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not items:
            lines.append("（无）")
            lines.append("")
            return
        for item in items:
            if isinstance(item, dict):
                text = item.get(field) or item.get("title") or json_fallback(item)
                lines.append(f"- {text}")
            else:
                lines.append(f"- {item}")
        lines.append("")

    _section("重点", data.get("key_points") or [])
    _section("决议", data.get("decisions") or [])
    _section("行动项", data.get("action_items") or [], field="title")
    _section("风险", data.get("risks") or [])
    _section("未决问题", data.get("open_questions") or [])
    return "\n".join(lines)


def json_fallback(item: dict) -> str:
    return str(item)
