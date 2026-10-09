# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select, update

from huiyiku.config import RuntimeContext, load_runtime
from huiyiku.db.models import Job, Meeting
from huiyiku.db.session import make_engine, session_factory, wal_checkpoint
from huiyiku.domain.jobs import INFERENCE_TYPES, MEDIA_TYPES, recover_status
from huiyiku.logging_setup import setup_logging
from huiyiku.timeutil import now_iso, now_utc_iso
from huiyiku.worker.handlers import handle_job

log = logging.getLogger("huiyiku.worker")


def _env_slots(name: str, default: int, cap: int) -> int:
    raw = os.environ.get(name, "")
    try:
        n = int(raw) if raw else default
    except ValueError:
        n = default
    return max(1, min(cap, n))


MEDIA_SLOTS = 1
INFERENCE_SLOTS = 1


class _Lanes:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.media_slots = _env_slots("HUIYIKU_MEDIA_SLOTS", MEDIA_SLOTS, 1)
        self.infer_slots = _env_slots("HUIYIKU_INFERENCE_SLOTS", INFERENCE_SLOTS, 2)
        self.media = ThreadPoolExecutor(
            max_workers=self.media_slots, thread_name_prefix="huiyiku-media"
        )
        self.infer = ThreadPoolExecutor(
            max_workers=self.infer_slots, thread_name_prefix="huiyiku-infer"
        )
        self.media_n = 0
        self.infer_n = 0
        self.active: set[int] = set()

    def shutdown(self, *, wait: bool = True) -> None:
        self.media.shutdown(wait=wait, cancel_futures=not wait)
        self.infer.shutdown(wait=wait, cancel_futures=not wait)


def run_worker(app_json: Path | None = None, *, once: bool = False) -> int:
    runtime = load_runtime(app_json)
    setup_logging(runtime.layout["logs"])
    engine = make_engine(runtime.layout["db"])
    factory = session_factory(engine)
    _recover(factory)
    try:
        _repair_legacy_report_chunks(factory, runtime)
    except Exception:
        log.warning("legacy report chunk repair failed", exc_info=True)
    lanes = _Lanes()
    log.info("worker started")
    wait_on_exit = True
    last_watchdog = 0.0
    try:
        while True:
            _tick(factory, runtime, lanes)
            if time.monotonic() - last_watchdog > 30:
                last_watchdog = time.monotonic()
                try:
                    with lanes.lock:
                        active = set(lanes.active)
                    _reclaim_stale_running(factory, active)
                except Exception:
                    log.warning("watchdog sweep failed", exc_info=True)
            runtime.layout["logs"].joinpath("worker.heartbeat").write_text(
                now_utc_iso(), encoding="utf-8"
            )
            if once:
                while True:
                    with lanes.lock:
                        busy = lanes.media_n + lanes.infer_n
                    if busy == 0:
                        break
                    time.sleep(0.15)
                return 0
            time.sleep(0.4)
    except KeyboardInterrupt:
        wait_on_exit = False
        return 0
    finally:
        lanes.shutdown(wait=wait_on_exit)
        wal_checkpoint(runtime.layout["db"])


def _repair_legacy_report_chunks(factory, runtime: RuntimeContext) -> None:
    """升级用户的一次性修复：老构建接受的报告，其检索块时间全为 0。

    新构建的块带真实出处（store._add_typed_chunk），但 unsupported 条目的
    块也是零时间，无法靠数据本身区分新老，所以用标记文件只跑一次。
    注意：配置了向量模型时，本修复与 accept 走同一个向量兜底（双路同源），
    每个 accepted 会议会有约两次批量 embedding 网络调用（补齐块向量 + 条目
    向量）——标记文件保证这只在升级后首次启动发生一次。
    """
    marker = runtime.layout["root"] / ".report_chunks_fix_v2"
    if marker.exists():
        return
    from huiyiku.db.models import Meeting, Report, ReportVersion
    from huiyiku.store import rebuild_report_chunks

    fixed = 0
    with factory() as session:
        for report in session.scalars(select(Report)).all():
            ver = (
                session.get(ReportVersion, report.current_version_id)
                if report.current_version_id
                else None
            )
            if ver is None or ver.status != "accepted":
                continue
            meeting = session.get(Meeting, report.meeting_id)
            if meeting is None:
                continue
            rebuild_report_chunks(session, meeting, ver, runtime=runtime)
            fixed += 1
        session.commit()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(now_iso(), encoding="utf-8")
    if fixed:
        log.info("legacy report chunk repair: rebuilt %d meeting(s)", fixed)


def _recover(factory) -> None:
    with factory() as session:
        running = session.scalars(select(Job).where(Job.status == "running")).all()
        for job in running:
            # 单 worker 设计：本 worker 启动时，任何 running 的 job 都无主（旧 worker 已死），
            # 立即回收而不等心跳超时——否则 exe 被关后界面会卡在「转写中」最长 2 小时。
            if job.cancel_requested:
                job.status = "cancelled"
                job.finished_at = now_iso()
                _restore_meeting_on_cancel(session, job)
                session.add(job)
                continue
            nxt = recover_status(job.attempt_count)
            job.attempt_count += 1
            job.status = nxt
            if nxt == "failed":
                job.error_json = json.dumps({"message": "worker restarted", "retryable": True})
                job.finished_at = now_iso()
                _fail_meeting(session, job)
            else:
                job.started_at = None
            session.add(job)
        session.commit()


def _fail_meeting(session, job: Job) -> None:
    if not job.meeting_id:
        return
    meeting = session.get(Meeting, job.meeting_id)
    if meeting is None:
        return
    if meeting.status in {"preprocessing", "transcribing", "indexing", "report_draft"}:
        meeting.status = "failed"
        meeting.error_json = job.error_json
        meeting.updated_at = now_iso()


def _tick(factory, runtime: RuntimeContext, lanes: _Lanes) -> bool:
    claimed = False
    while True:
        job_id, lane = _claim_one(factory, lanes)
        if job_id is None or lane is None:
            return claimed
        with lanes.lock:
            if lane == "media":
                lanes.media_n += 1
                pool = lanes.media
            else:
                lanes.infer_n += 1
                pool = lanes.infer
        with lanes.lock:
            lanes.active.add(job_id)
        pool.submit(_run_claimed, factory, runtime, lanes, job_id, lane)
        claimed = True


def _reclaim_stale_running(factory, active: set[int]) -> None:
    """看门狗：running 且心跳超过任务超时的挂死任务（LLM 流中断/ffmpeg 卡死），
    跳过本 worker 正在执行的任务（其心跳容错偶发失败不应误杀），回收重试。"""
    from huiyiku.domain.jobs import is_heartbeat_stale

    with factory() as session:
        running = session.scalars(select(Job).where(Job.status == "running")).all()
        for job in running:
            if job.id in active or not is_heartbeat_stale(job.heartbeat_at, job.timeout_seconds):
                continue
            log.warning("job %s heartbeat stale (watchdog), reclaiming", job.id)
            nxt = recover_status(job.attempt_count)
            job.attempt_count += 1
            job.status = nxt
            if nxt == "failed":
                job.error_json = json.dumps(
                    {"message": "heartbeat timeout (watchdog)", "retryable": True}
                )
                job.finished_at = now_iso()
                _fail_meeting(session, job)
            else:
                job.started_at = None
            session.add(job)
        session.commit()


def _claim_one(factory, lanes: _Lanes) -> tuple[int | None, str | None]:
    with lanes.lock:
        can_media = lanes.media_n < lanes.media_slots
        can_infer = lanes.infer_n < lanes.infer_slots
    with factory() as session:
        job = None
        lane = None
        if can_media:
            job = session.scalar(
                select(Job)
                .where(Job.status == "queued", Job.type.in_(MEDIA_TYPES))
                .order_by(Job.id)
            )
            if job is not None:
                lane = "media"
        if job is None and can_infer:
            job = session.scalar(
                select(Job)
                .where(Job.status == "queued", Job.type.in_(INFERENCE_TYPES))
                .order_by(Job.id)
            )
            if job is not None:
                lane = "infer"
        if job is None or lane is None:
            return None, None
        job_id = job.id
        res = session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "queued")
            .values(status="running", started_at=now_iso(), heartbeat_at=now_utc_iso())
        )
        session.commit()
        if res.rowcount != 1:
            return None, None
        return job_id, lane


def _run_claimed(factory, runtime, lanes: _Lanes, job_id: int, lane: str) -> None:
    try:
        _execute(factory, runtime, job_id)
    finally:
        with lanes.lock:
            lanes.active.discard(job_id)
            if lane == "media":
                lanes.media_n = max(0, lanes.media_n - 1)
            else:
                lanes.infer_n = max(0, lanes.infer_n - 1)


def _execute(factory, runtime: RuntimeContext, job_id: int) -> None:
    def cancel_check() -> bool:
        # 偶发 database is locked 不应炸掉正在执行的任务
        try:
            with factory() as s:
                current = s.get(Job, job_id)
                return bool(current and current.cancel_requested)
        except Exception:
            return False

    def progress(payload: dict) -> None:
        # 进度/心跳更新失败只记日志：与主事务的写锁冲突不应把任务打成 failed
        try:
            with factory() as s:
                current = s.get(Job, job_id)
                if current is None:
                    return
                current.progress_json = json.dumps(payload, ensure_ascii=False)
                current.heartbeat_at = now_utc_iso()
                s.commit()
        except Exception:
            log.warning("job %s progress update skipped (db busy)", job_id, exc_info=True)

    try:
        # 跨进程设置热词/allow_cloud 后，serve 只写 settings.json；
        # worker 必须每个任务前重读，否则配置到 worker 重启才生效
        from huiyiku.config import load_settings

        runtime.settings = load_settings(runtime.layout["settings"])
        with factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            handle_job(session, runtime, job, cancel_check=cancel_check, progress=progress)
            session.refresh(job)  # cancel 端点可能在 handler 期间置 cancel_requested
            if job.cancel_requested:
                job.status = "cancelled"
                job.finished_at = now_iso()
                _restore_meeting_on_cancel(session, job)
            else:
                job.status = "success"
                job.finished_at = now_iso()
            session.commit()
    except Exception as exc:
        retryable = (
            bool(getattr(exc, "retryable", False)) or "database is locked" in str(exc).lower()
        )
        status_code = getattr(exc, "status_code", None)
        # 失败处理器自身也可能遇锁：包裹后至少留下日志，任务不至于无痕卡在 running
        try:
            with factory() as session:
                job = session.get(Job, job_id)
                if job is None:
                    return
                if job.cancel_requested:
                    job.status = "cancelled"
                    _restore_meeting_on_cancel(session, job)
                else:
                    job.status = "failed"
                    job.error_json = json.dumps(
                        {
                            "message": str(exc),
                            "retryable": retryable,
                            "http_status": status_code,
                        },
                        ensure_ascii=False,
                    )
                    _fail_meeting(session, job)
                job.finished_at = now_iso()
                session.commit()
        except Exception:
            log.exception(
                "job %s failed AND failure-handler errored (job may need manual retry)", job_id
            )
            return
        log.exception("job %s failed", job_id)


def _restore_meeting_on_cancel(session, job: Job) -> None:
    from huiyiku.db.models import TranscriptSegment

    if not job.meeting_id:
        return
    meeting = session.get(Meeting, job.meeting_id)
    if meeting is None:
        return
    has_tr = session.scalar(
        select(TranscriptSegment.id).where(TranscriptSegment.meeting_id == meeting.id)
    )
    if has_tr:
        meeting.status = (
            "speaker_review" if meeting.status in {"transcribing", "indexing"} else "indexed"
        )
    else:
        meeting.status = "created"
    meeting.updated_at = now_iso()
