# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from typing import Any

import jieba

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?;；])")


def tokenize(text: str) -> str:
    parts = [p.strip() for p in jieba.cut(text or "") if p.strip()]
    return " ".join(parts)


def build_transcript_chunks(
    segments: list[dict[str, Any]],
    speakers: dict[int, dict[str, Any]],
    *,
    target_min_s: int = 30,
    target_max_s: int = 120,
    max_chars: int = 300,
) -> list[dict[str, Any]]:
    """Non-overlapping chunks. Unconfirmed/invalid speakers omit speaker_id and use time windows."""
    confirmed = {int(s["id"]) for s in speakers.values() if s.get("status") == "confirmed"}
    buckets: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_speaker: int | None = None
    current_confirmed = False

    def flush() -> None:
        nonlocal current
        if current:
            buckets.append(current)
            current = []

    for seg in sorted(segments, key=lambda s: int(s["start_ms"])):
        sid = seg.get("speaker_id")
        is_conf = sid in confirmed if sid is not None else False
        if current:
            dur = int(seg["end_ms"]) - int(current[0]["start_ms"])
            chars = sum(len(s.get("normalized_text") or s.get("text") or "") for s in current)
            speaker_changed = is_conf and current_confirmed and sid != current_speaker
            if speaker_changed or dur > target_max_s * 1000 or chars >= max_chars:
                flush()
                current_speaker = sid if is_conf else None
                current_confirmed = is_conf
        elif not current:
            current_speaker = sid if is_conf else None
            current_confirmed = is_conf
        current.append(seg)
        dur = int(current[-1]["end_ms"]) - int(current[0]["start_ms"])
        chars = sum(len(s.get("normalized_text") or s.get("text") or "") for s in current)
        if dur >= target_min_s * 1000 and (not current_confirmed or chars >= 80):
            # keep aggregating until max or speaker change
            if chars >= max_chars:
                extra = _split_oversize(current, max_chars)
                current = []
                buckets.extend(extra)
    flush()

    chunks: list[dict[str, Any]] = []
    for group in buckets:
        if not group:
            continue
        texts = [(g.get("normalized_text") or g.get("text") or "").strip() for g in group]
        content = "".join(texts)
        sids = {g.get("speaker_id") for g in group}
        speaker_id = next(iter(sids)) if len(sids) == 1 and next(iter(sids)) in confirmed else None
        start_ms = int(group[0]["start_ms"])
        end_ms = int(group[-1]["end_ms"])
        pieces = split_text_sentences(content, max_chars) if len(content) > max_chars else [content]
        span = max(end_ms - start_ms, 1)
        n = len(pieces)
        for i, piece in enumerate(pieces):
            a = start_ms + span * i // n
            b = start_ms + span * (i + 1) // n
            chunks.append(
                {
                    "speaker_id": speaker_id,
                    "start_ms": a,
                    "end_ms": b,
                    "chunk_type": "transcript",
                    "content": piece,
                    "tokenized_content": tokenize(piece),
                }
            )
    return chunks


def split_text_sentences(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text] if text else []
    parts = [p for p in _SENTENCE_SPLIT.split(text) if p]
    if not parts:
        return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
    out: list[str] = []
    buf = ""
    for part in parts:
        if buf and len(buf) + len(part) > max_chars:
            out.append(buf)
            buf = part
        else:
            buf += part
    if buf:
        out.append(buf)
    extra: list[str] = []
    for item in out:
        if len(item) <= max_chars:
            extra.append(item)
        else:
            extra.extend(item[i : i + max_chars] for i in range(0, len(item), max_chars))
    return extra or [text]


def _split_oversize(group: list[dict[str, Any]], max_chars: int) -> list[list[dict[str, Any]]]:
    text = "".join((g.get("normalized_text") or g.get("text") or "") for g in group)
    if len(text) <= max_chars:
        return [group]
    # Fall back to keeping original segment boundaries (already non-overlapping).
    out: list[list[dict[str, Any]]] = []
    buf: list[dict[str, Any]] = []
    n = 0
    for g in group:
        piece = g.get("normalized_text") or g.get("text") or ""
        if buf and n + len(piece) > max_chars:
            out.append(buf)
            buf = []
            n = 0
        buf.append(g)
        n += len(piece)
    if buf:
        out.append(buf)
    return out


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        raise ValueError("embedding dimension mismatch")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def rrf_fuse(
    fts_ids: list[int],
    vec_ids: list[int],
    *,
    k: int = 60,
    limit: int = 20,
) -> list[int]:
    scores: dict[int, float] = {}
    for rank, cid in enumerate(fts_ids, start=1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    for rank, cid in enumerate(vec_ids, start=1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return [cid for cid, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]]
