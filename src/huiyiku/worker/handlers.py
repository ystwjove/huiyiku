# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from huiyiku.asr.client import AsrError, find_asr_command, transcribe_wav
from huiyiku.config import RuntimeContext
from huiyiku.db.models import Job, Meeting, ModelRegistry
from huiyiku.media.ffmpeg import FFmpegError, convert_to_wav, find_ffmpeg
from huiyiku.paths import executable_dir
from huiyiku.store import enqueue_job, persist_transcript, rebuild_stats_and_chunks
from huiyiku.timeutil import now_iso

CancelCheck = Callable[[], bool]
ProgressCb = Callable[[dict[str, Any]], None]


def handle_job(
    session: Session,
    runtime: RuntimeContext,
    job: Job,
    *,
    cancel_check: CancelCheck,
    progress: ProgressCb,
) -> None:
    if job.type == "preprocess":
        _preprocess(session, runtime, job, cancel_check, progress)
    elif job.type == "transcribe":
        _transcribe(session, runtime, job, cancel_check, progress)
    elif job.type == "stats":
        _stats(session, runtime, job, cancel_check, progress)
    elif job.type == "embed":
        _embed(session, runtime, job, cancel_check, progress)
    elif job.type == "report":
        from huiyiku.llm.report import generate_report

        if cancel_check():
            raise AsrError("cancelled")
        generate_report(session, runtime, job, progress, cancel_check=cancel_check)
    elif job.type == "digest":
        from huiyiku.llm.digest import generate_digest

        if cancel_check():
            raise AsrError("cancelled")
        generate_digest(session, runtime, job, progress, cancel_check=cancel_check)
    else:
        raise RuntimeError(f"unknown job type {job.type}")


def _meeting(session: Session, job: Job) -> Meeting:
    if not job.meeting_id:
        raise RuntimeError("job missing meeting_id")
    meeting = session.get(Meeting, job.meeting_id)
    if meeting is None:
        raise RuntimeError("meeting not found")
    return meeting


def _preprocess(
    session: Session,
    runtime: RuntimeContext,
    job: Job,
    cancel_check: CancelCheck,
    progress: ProgressCb,
) -> None:
    meeting = _meeting(session, job)
    if cancel_check():
        raise AsrError("cancelled")
    ffmpeg = find_ffmpeg(runtime.app.ffmpeg_path, search_root=executable_dir())
    if not ffmpeg:
        raise FFmpegError("FFmpeg not found")
    if not meeting.original_path:
        raise FFmpegError("no original media")
    src = runtime.app.data_path / meeting.original_path
    wav_name = f"{meeting.id}.wav"
    dst = runtime.layout["media_wav"] / wav_name
    progress({"stage": "ffmpeg", "percent": 10})
    duration = convert_to_wav(ffmpeg, src, dst)
    meeting.wav_path = f"media/wav/{wav_name}"
    meeting.duration_ms = duration
    meeting.updated_at = now_iso()
    progress({"stage": "ffmpeg", "percent": 100})
    enqueue_job(session, "transcribe", meeting_id=meeting.id, payload={"from": "preprocess"})


def _transcribe(
    session: Session,
    runtime: RuntimeContext,
    job: Job,
    cancel_check: CancelCheck,
    progress: ProgressCb,
) -> None:
    meeting = _meeting(session, job)
    if not meeting.wav_path:
        raise AsrError("wav missing; run preprocess first")
    wav = runtime.app.data_path / meeting.wav_path
    command = find_asr_command(runtime.app.asr_path)
    if command is None:
        raise AsrError("ASR component not installed")
    model_dir = runtime.app.asr_path / "models"

    def hb(event: dict[str, Any]) -> None:
        progress({"stage": "asr", **event})

    segments = transcribe_wav(
        command,
        wav,
        model_dir=model_dir,
        hotwords=(runtime.settings.hotwords or "").strip() or None,
        heartbeat=hb,
        cancel_check=cancel_check,
        timeout_seconds=job.timeout_seconds,
    )
    persist_transcript(
        session,
        meeting,
        segments,
        provider="funasr",
        model="paraformer-zh",
        version="v2.0.4",
    )
    job.usage_json = json.dumps({"segments": len(segments), "audio_ms": meeting.duration_ms})
    # 转写完立刻建检索块：以前要等确认说话人才建，导致刚转写完的会议在
    # 问答/检索里是空的（报告却出得来，用户会以为知识库是好的）。
    rebuild_stats_and_chunks(session, meeting, session.connection().connection)


def _stats(
    session: Session,
    runtime: RuntimeContext,
    job: Job,
    cancel_check: CancelCheck,
    progress: ProgressCb,
) -> None:
    meeting = _meeting(session, job)
    if cancel_check():
        raise AsrError("cancelled")
    progress({"stage": "stats", "percent": 40})
    conn = session.connection().connection
    rebuild_stats_and_chunks(session, meeting, conn)
    embed = session.scalar(
        select(ModelRegistry).where(
            ModelRegistry.task == "embedding",
            ModelRegistry.is_default == 1,
            ModelRegistry.status == "available",
        )
    )
    if embed is not None and runtime.settings.allow_cloud:
        enqueue_job(
            session,
            "embed",
            meeting_id=meeting.id,
            payload={"model_id": embed.id},
            model_version=f"{embed.provider}:{embed.model_name}",
            set_meeting_status=False,
        )
        meeting.status = "indexing"
    else:
        meeting.status = "indexed"
    meeting.updated_at = now_iso()
    progress({"stage": "stats", "percent": 100})


def _embed(
    session: Session,
    runtime: RuntimeContext,
    job: Job,
    cancel_check: CancelCheck,
    progress: ProgressCb,
) -> None:
    from huiyiku.llm.embed import embed_active_chunks

    meeting = _meeting(session, job)
    if cancel_check():
        raise RuntimeError("cancelled")
    progress({"stage": "embed", "percent": 10})
    embed_active_chunks(session, runtime, meeting, progress, cancel_check=cancel_check)
    meeting.status = "indexed"
    meeting.updated_at = now_iso()
