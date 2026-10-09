# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from wave import open as wave_open

ALLOWED_SUFFIXES = {".wav", ".mp3", ".m4a", ".mp4"}


class FFmpegError(RuntimeError):
    pass


def find_ffmpeg(
    ffmpeg_path: str = "",
    *,
    search_root: Path | None = None,
    path_env: str | None = None,
) -> str | None:
    """Lookup order: app.json ffmpeg_path → <root>/ffmpeg/ffmpeg.exe → PATH."""
    if ffmpeg_path:
        candidate = Path(ffmpeg_path)
        if candidate.is_file():
            return str(candidate)
    if search_root is not None:
        sibling = search_root / "ffmpeg" / "ffmpeg.exe"
        if sibling.is_file():
            return str(sibling)
        posix = search_root / "ffmpeg" / "ffmpeg"
        if posix.is_file():
            return str(posix)
    env = os.environ["PATH"] if path_env is None else path_env
    found = shutil.which("ffmpeg", path=env)
    return found


def _ffprobe_bin(ffmpeg_bin: str) -> str:
    p = Path(ffmpeg_bin)
    probe = p.with_name("ffprobe.exe" if p.suffix.lower() == ".exe" else "ffprobe")
    if probe.is_file():
        return str(probe)
    return shutil.which("ffprobe") or "ffprobe"


def probe_duration_ms(ffmpeg_bin: str, media: Path) -> int | None:
    probe = _ffprobe_bin(ffmpeg_bin)
    try:
        out = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media),
            ],
            check=True,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.CalledProcessError):
        return _wav_duration_ms(media)
    text = (out.stdout or "").strip()
    try:
        return int(float(text) * 1000)
    except ValueError:
        return _wav_duration_ms(media)


def _wav_duration_ms(path: Path) -> int | None:
    if path.suffix.lower() != ".wav":
        return None
    with wave_open(str(path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        if rate <= 0:
            return None
        return int(frames * 1000 / rate)


def convert_to_wav(ffmpeg_bin: str, src: Path, dst: Path) -> int:
    """16 kHz mono 16-bit PCM WAV. Returns duration_ms."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(src),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        str(dst),
    ]
    _flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, creationflags=_flags)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc))[-2000:]
        raise FFmpegError(f"ffmpeg failed: {err}") from exc
    duration = probe_duration_ms(ffmpeg_bin, dst) or _wav_duration_ms(dst)
    if duration is None:
        raise FFmpegError("could not determine WAV duration")
    return duration
