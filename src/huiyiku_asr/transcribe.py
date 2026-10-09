# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""FunASR pipeline. Imported only by the ASR process — UI must never import this module."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any


def emit_stderr(payload: dict[str, Any]) -> None:
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def configure_model_cache(model_dir: Path) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    ms = str(model_dir / "modelscope")
    hf = str(model_dir / "hf")
    os.environ["MODELSCOPE_CACHE"] = ms
    os.environ["HF_HOME"] = hf
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(Path(hf) / "hub")
    # Keep FunASR from writing next to the package / _internal.
    os.environ.setdefault("FUNASR_CACHE", str(model_dir / "funasr"))


def _wav_duration_seconds(wav: Path) -> float:
    try:
        import wave

        with wave.open(str(wav), "rb") as w:
            rate = w.getframerate() or 16000
            return w.getnframes() / float(rate)
    except Exception:
        return 0.0


def _heartbeat_loop(stop: threading.Event, state: dict[str, Any]) -> None:
    # 每 5 秒汇报阶段与估算百分比（generate 按 RTF≈0.35 估算，封顶 90%）
    while not stop.wait(5):
        phase = state["phase"]
        pct = 3
        if phase == "generate":
            elapsed = time.monotonic() - state["gen_t0"]
            expected = max(state["duration_s"] * 0.35, 5.0)
            pct = min(90, 10 + int(elapsed / expected * 80))
        emit_stderr({"event": "heartbeat", "phase": phase, "percent": pct})


def run_transcribe(
    wav: Path, model_dir: Path, *, enable_spk: bool = True, hotwords: str | None = None
) -> list[dict[str, Any]]:
    configure_model_cache(model_dir)
    try:
        from funasr import AutoModel  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "FunASR stack import failed. Use the ASR venv or huiyiku-asr.exe. "
            f"Original error: {exc}"
        ) from exc

    kwargs: dict[str, Any] = {
        "model": "paraformer-zh",
        "model_revision": "v2.0.4",
        "vad_model": "fsmn-vad",
        "vad_model_revision": "v2.0.4",
        "punc_model": "ct-punc",
        "punc_model_revision": "v2.0.4",
        "disable_update": True,
        "device": "cpu",
    }
    if enable_spk:
        kwargs["spk_model"] = "cam++"
        kwargs["spk_model_revision"] = "v2.0.2"

    state: dict[str, Any] = {
        "phase": "load_models",
        "gen_t0": 0.0,
        "duration_s": _wav_duration_seconds(wav),
    }
    emit_stderr({"event": "heartbeat", "phase": "load_models", "percent": 3})
    stop = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop, args=(stop, state), daemon=True)
    hb.start()
    try:
        model = AutoModel(**kwargs)
        state["phase"] = "generate"
        state["gen_t0"] = time.monotonic()
        emit_stderr({"event": "heartbeat", "phase": "generate", "percent": 10})
        gen_kwargs: dict[str, Any] = {"input": str(wav), "batch_size_s": 60, "disable_pbar": True}
        if hotwords:
            gen_kwargs["hotword"] = hotwords
        result = model.generate(**gen_kwargs)
        emit_stderr({"event": "heartbeat", "phase": "finalize", "percent": 92})
    finally:
        stop.set()
    return parse_funasr_result(result)


def parse_funasr_result(result: Any) -> list[dict[str, Any]]:
    if not result:
        return []
    first = result[0] if isinstance(result, list) else result
    if not isinstance(first, dict):
        return []
    sentences = first.get("sentence_info")
    if isinstance(sentences, list) and sentences:
        out = []
        for sent in sentences:
            start = _ms(sent.get("start"))
            end = _ms(sent.get("end"))
            text = str(sent.get("text") or sent.get("sentence") or "").strip()
            spk = sent.get("spk")
            label = None
            if spk is not None and str(spk).strip() != "":
                try:
                    label = f"SPEAKER_{int(spk):02d}"
                except (TypeError, ValueError):
                    raw = str(spk)
                    label = raw if raw.upper().startswith("SPEAKER_") else f"SPEAKER_{raw}"
            if not text:
                continue
            out.append(
                {
                    "start_ms": start,
                    "end_ms": end if end >= start else start,
                    "text": text,
                    "speaker_label": label,
                }
            )
        return out
    text = str(first.get("text") or "").strip()
    ts = first.get("timestamp") or []
    start = 0
    end = 0
    if ts and isinstance(ts, list):
        try:
            start = int(ts[0][0])
            end = int(ts[-1][1])
        except (TypeError, ValueError, IndexError):
            start, end = 0, 0
    if not text:
        return []
    return [{"start_ms": start, "end_ms": end, "text": text, "speaker_label": None}]


def _ms(value: Any) -> int:
    if value is None:
        return 0
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0
    # FunASR tutorial uses milliseconds already for sentence start/end.
    return int(n)


def maybe_has_speaker_labels(segments: list[dict[str, Any]]) -> bool:
    return any(s.get("speaker_label") for s in segments)
