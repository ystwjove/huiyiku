# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

METRIC_VERSION = "v1"


@dataclass
class SpeakerRow:
    speaker_id: int
    status: str
    speech_ms: int = 0
    segment_count: int = 0
    timeline: list[list[int]] | None = None
    speech_ratio: float = 0.0


def compute_speaker_stats(
    segments: list[dict[str, Any]],
    speakers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """metric_version=v1: merged/invalid excluded; ratio denom = sum(speech_ms) of counted speakers."""
    segments = merge_overlap_by_assignment(segments)
    by_id: dict[int, SpeakerRow] = {}
    for sp in speakers:
        sid = int(sp["id"])
        by_id[sid] = SpeakerRow(speaker_id=sid, status=str(sp.get("status") or "pending"))
    for seg in segments:
        sid = seg.get("speaker_id")
        if sid is None:
            continue
        row = by_id.get(int(sid))
        if row is None or row.status in {"merged", "invalid"}:
            continue
        start = int(seg["start_ms"])
        end = int(seg["end_ms"])
        if end < start:
            start, end = end, start
        row.speech_ms += max(0, end - start)
        row.segment_count += 1
        if row.timeline is None:
            row.timeline = []
        row.timeline.append([start, end])
    denom = sum(r.speech_ms for r in by_id.values() if r.status == "confirmed")
    out: list[dict[str, Any]] = []
    for row in by_id.values():
        if row.status != "confirmed":
            continue
        ratio = (row.speech_ms / denom) if denom else 0.0
        out.append(
            {
                "speaker_id": row.speaker_id,
                "speech_ms": row.speech_ms,
                "segment_count": row.segment_count,
                "speech_ratio": ratio,
                "timeline": row.timeline or [],
                "metric_version": METRIC_VERSION,
            }
        )
    return out


def merge_overlap_by_assignment(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clip overlapping ranges of the same assigned speaker so milliseconds are not counted twice.

    Distinct speakers keep their own assigned ranges (see test_overlap_counted_by_assignment_only).
    """
    grouped: dict[Any, list[dict[str, Any]]] = {}
    order: list[Any] = []
    for seg in segments:
        key = seg.get("speaker_id")
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(seg)
    out: list[dict[str, Any]] = []
    for key in order:
        items = sorted(grouped[key], key=lambda s: (int(s["start_ms"]), int(s["end_ms"])))
        last_end: int | None = None
        for seg in items:
            start = int(seg["start_ms"])
            end = int(seg["end_ms"])
            if last_end is not None and start < last_end:
                start = last_end
            if end <= start:
                continue
            clipped = dict(seg)
            clipped["start_ms"] = start
            clipped["end_ms"] = end
            out.append(clipped)
            last_end = end
    return out
