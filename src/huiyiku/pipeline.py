# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""Slice 0 offline script: media → wav → FunASR → transcript.md → stats → optional LLM summary."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from huiyiku.asr.client import find_asr_command, transcribe_wav
from huiyiku.config import load_runtime
from huiyiku.domain.stats import METRIC_VERSION
from huiyiku.llm.client import ChatClient
from huiyiku.media.ffmpeg import convert_to_wav, find_ffmpeg
from huiyiku.paths import executable_dir


def run_pipeline(
    src: Path,
    out_dir: Path,
    *,
    with_llm: bool = False,
    model_dir: Path | None = None,
) -> dict:
    runtime = load_runtime()
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg(runtime.app.ffmpeg_path, search_root=executable_dir())
    if not ffmpeg:
        raise SystemExit("FFmpeg not found. Install it on PATH or set ffmpeg_path in app.json.")
    wav = out_dir / (src.stem + ".16k.wav")
    duration_ms = convert_to_wav(ffmpeg, src, wav)
    asr_home = runtime.app.asr_path
    command = find_asr_command(asr_home)
    if command is None:
        raise SystemExit("ASR command not found.")
    md = model_dir or (asr_home / "models")

    progress_path = out_dir / "asr-progress.jsonl"
    with progress_path.open("a", encoding="utf-8") as fh:

        def hb(event: dict) -> None:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()

        segments = transcribe_wav(command, wav, model_dir=md, heartbeat=hb)
    (out_dir / "transcript.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_text = _transcript_markdown(segments, duration_ms)
    (out_dir / "transcript.md").write_text(md_text, encoding="utf-8")
    stats = _stats_from_segments(segments)
    (out_dir / "stats.json").write_text(
        json.dumps(
            {"metric_version": METRIC_VERSION, "speakers": stats}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    (out_dir / "stats.md").write_text(_stats_markdown(stats, duration_ms), encoding="utf-8")
    result = {
        "wav": str(wav),
        "duration_ms": duration_ms,
        "segments": len(segments),
        "speakers": stats,
        "has_speaker_labels": any(s.get("speaker_label") for s in segments),
    }
    if with_llm:
        if not runtime.secrets.llm_api_key:
            raise SystemExit("LLM key missing; omit --llm or configure secrets.json")
        model = _default_llm(runtime)
        summary = _summarize(model, md_text)
        (out_dir / "summary.md").write_text(summary, encoding="utf-8")
        result["summary"] = str(out_dir / "summary.md")
    return result


def _default_llm(runtime) -> ChatClient:
    # Pipeline uses env/secrets only; model name may be in settings later.
    endpoint = os_endpoint()
    model_name = os_model()
    return ChatClient(endpoint, runtime.secrets.llm_api_key, model_name)


def os_endpoint() -> str:
    import os

    return os.environ.get("HUIYIKU_LLM_ENDPOINT") or "https://api.deepseek.com"


def os_model() -> str:
    import os

    return os.environ.get("HUIYIKU_LLM_MODEL") or "deepseek-chat"


def _transcript_markdown(segments: list[dict], duration_ms: int) -> str:
    lines = ["# 转写", "", f"时长: {duration_ms} ms", ""]
    for seg in segments:
        label = seg.get("speaker_label") or "SPEAKER_00"
        start = _fmt_ms(int(seg.get("start_ms") or 0))
        text = seg.get("text") or ""
        lines.append(f"- **{start} {label}** {text}")
    lines.append("")
    return "\n".join(lines)


def _stats_from_segments(segments: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = defaultdict(lambda: {"speech_ms": 0, "segment_count": 0})
    for seg in segments:
        label = seg.get("speaker_label") or "SPEAKER_00"
        start = int(seg.get("start_ms") or 0)
        end = int(seg.get("end_ms") or 0)
        buckets[label]["speech_ms"] += max(0, end - start)
        buckets[label]["segment_count"] += 1
    denom = sum(v["speech_ms"] for v in buckets.values())
    rows = []
    for label, v in buckets.items():
        rows.append(
            {
                "speaker_label": label,
                "speech_ms": v["speech_ms"],
                "segment_count": v["segment_count"],
                "speech_ratio": (v["speech_ms"] / denom) if denom else 0.0,
                "metric_version": METRIC_VERSION,
            }
        )
    return rows


def _stats_markdown(stats: list[dict], duration_ms: int) -> str:
    lines = ["# 发言统计", "", f"metric_version={METRIC_VERSION}", f"duration_ms={duration_ms}", ""]
    for row in stats:
        pct = round(row["speech_ratio"] * 100, 1)
        lines.append(
            f"- {row['speaker_label']}: {row['speech_ms']} ms, {row['segment_count']} 次, {pct}%"
        )
    lines.append("")
    return "\n".join(lines)


def _summarize(client: ChatClient, transcript_md: str) -> str:
    from huiyiku.llm.client import estimate_tokens, map_reduce_groups

    ctx = 32000
    groups = map_reduce_groups([transcript_md], ctx)
    # If still huge, split by lines.
    if len(groups) == 1 and estimate_tokens(transcript_md) > ctx * 0.6:
        lines = transcript_md.splitlines()
        chunk: list[str] = []
        acc = 0
        parts: list[str] = []
        for line in lines:
            n = estimate_tokens(line)
            if chunk and acc + n > ctx * 0.6:
                parts.append("\n".join(chunk))
                chunk, acc = [], 0
            chunk.append(line)
            acc += n
        if chunk:
            parts.append("\n".join(chunk))
        notes = []
        for part in parts:
            notes.append(
                client.complete(
                    [
                        {
                            "role": "system",
                            "content": "Extract key points from this transcript chunk in Chinese JSON "
                            '{"points":["..."]}. Do not invent.',
                        },
                        {"role": "user", "content": part},
                    ],
                    json_mode=True,
                )["content"]
            )
        final = client.complete(
            [
                {
                    "role": "system",
                    "content": "Write a Chinese meeting summary from chunk notes. Cover the whole meeting.",
                },
                {"role": "user", "content": "\n\n".join(notes)},
            ]
        )
        return final["content"]
    return client.complete(
        [
            {"role": "system", "content": "用中文写会议摘要，只根据转写，不要编造。"},
            {"role": "user", "content": transcript_md},
        ]
    )["content"]


def _fmt_ms(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
