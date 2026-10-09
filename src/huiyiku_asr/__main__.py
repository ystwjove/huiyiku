# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""CLI contract: ``huiyiku-asr.exe transcribe --wav <16kHz_mono.wav>``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from huiyiku_asr import __version__

# Windows 管道下 Python 默认按控制台代码页（cp936）写中文，worker 按 UTF-8 读会乱码入库。
# 强制本进程 stdio 一律 UTF-8，与 worker 的解码约定一致。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="huiyiku-asr",
        description="会议库本机 ASR 组件（FunASR Paraformer）",
    )
    parser.add_argument("--version", action="version", version=f"huiyiku-asr {__version__}")
    sub = parser.add_subparsers(dest="command")
    transcribe = sub.add_parser("transcribe", help="Transcribe a 16 kHz mono WAV.")
    transcribe.add_argument("--wav", required=True, help="Path to 16 kHz mono WAV.")
    transcribe.add_argument(
        "--hotwords",
        default=None,
        help="Space-separated hotwords to bias recognition (paraformer).",
    )
    transcribe.add_argument(
        "--model-dir",
        default=None,
        help="Override model dir. Else HUIYIKU_ASR_MODEL_DIR, else asr_home.",
    )
    transcribe.add_argument(
        "--no-spk",
        action="store_true",
        help="Disable CAM++ speaker embeddings.",
    )
    return parser


def resolve_model_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("HUIYIKU_ASR_MODEL_DIR")
    if env:
        return Path(env)
    home = os.environ.get("HUIYIKU_ASR_HOME")
    if home:
        return Path(home) / "models"
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "models"
    here = Path(__file__).resolve()
    # src/huiyiku_asr/__main__.py → repository root / asr/models
    if here.parent.name == "huiyiku_asr":
        return here.parents[2] / "asr" / "models"
    return Path.cwd() / "asr" / "models"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "transcribe":
        parser.print_help()
        return 0
    wav = Path(args.wav)
    if not wav.is_file():
        _error(f"wav not found: {wav}")
        return 2
    model_dir = resolve_model_dir(args.model_dir)
    try:
        from huiyiku_asr.transcribe import emit_stderr, run_transcribe
    except Exception as exc:  # pragma: no cover - import surface
        _error(str(exc))
        return 2
    try:
        emit_stderr({"event": "heartbeat"})
        segments = run_transcribe(
            wav, model_dir, enable_spk=not args.no_spk, hotwords=args.hotwords
        )
    except Exception as exc:
        import traceback

        _error(str(exc) or traceback.format_exc())
        return 2
    sys.stdout.write(json.dumps(segments, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def _error(message: str) -> None:
    sys.stderr.write(json.dumps({"event": "error", "message": message}, ensure_ascii=False) + "\n")
    sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
