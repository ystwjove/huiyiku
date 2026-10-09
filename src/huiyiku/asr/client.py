# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

HeartbeatCb = Callable[[dict[str, Any]], None]


class AsrError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _dev_asr_python() -> Path | None:
    root = Path(__file__).resolve().parents[3]
    for candidate in (
        root / ".venv-asr" / "Scripts" / "python.exe",
        root / ".venv-asr" / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate
    return None


def find_asr_command(asr_home: Path) -> list[str] | None:
    exe = asr_home / "huiyiku-asr.exe"
    if exe.is_file():
        return [str(exe)]
    env_py = os.environ.get("HUIYIKU_ASR_PYTHON")
    if env_py and Path(env_py).is_file():
        return [env_py, "-m", "huiyiku_asr"]
    dev = _dev_asr_python()
    if dev is not None:
        return [str(dev), "-m", "huiyiku_asr"]
    return [sys.executable, "-m", "huiyiku_asr"]


def asr_ready(asr_home: Path) -> bool:
    if (asr_home / "huiyiku-asr.exe").is_file():
        return True
    env_py = os.environ.get("HUIYIKU_ASR_PYTHON")
    if env_py and Path(env_py).is_file():
        return True
    return _dev_asr_python() is not None


def asr_exe_installed(asr_home: Path) -> bool:
    """仅判断冻结 ASR 组件（asr\\huiyiku-asr.exe）是否存在；asr_ready 另含 python 途径。"""
    return (asr_home / "huiyiku-asr.exe").is_file()


def asr_python_ready(asr_home: Path) -> bool:
    """仅判断 python 途径（HUIYIKU_ASR_PYTHON 或开发态 .venv-asr）是否可用。

    与 asr_exe_installed 互斥口径：`asr_exe_installed or asr_python_ready` == asr_ready。
    """
    env_py = os.environ.get("HUIYIKU_ASR_PYTHON")
    if env_py and Path(env_py).is_file():
        return True
    return _dev_asr_python() is not None


def transcribe_wav(
    command: list[str],
    wav: Path,
    *,
    model_dir: Path | None = None,
    heartbeat: HeartbeatCb | None = None,
    cancel_check: Callable[[], bool] | None = None,
    timeout_seconds: int = 7200,
    hotwords: str | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> list[dict[str, Any]]:
    """Run the ASR CLI. Drain stdout and stderr concurrently so large JSON cannot deadlock."""
    args = [*command, "transcribe", "--wav", str(wav)]
    if model_dir is not None:
        args.extend(["--model-dir", str(model_dir)])
    if hotwords:
        args.extend(["--hotwords", hotwords])
    env = os.environ.copy()
    # 父进程守望契约只给 serve/worker，不传给 ASR 子进程（不归 bootstrap 管）
    env.pop("HUIYIKU_PARENT_PID", None)
    if model_dir is not None:
        env["HUIYIKU_ASR_MODEL_DIR"] = str(model_dir)
        env.setdefault("HUIYIKU_ASR_HOME", str(Path(model_dir).parent))
    src = Path(__file__).resolve().parents[3] / "src"
    if src.is_dir():
        env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    proc = popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        # ASR 依赖（jieba/模型日志）可能按 GBK 写中文，硬解码遇非法字节会崩掉排空线程
        errors="replace",
        env=env,
        # GUI 主进程拉起控制台子程序时不得弹出 CMD 黑窗
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    stderr_chunks: list[str] = []
    stdout_box: list[str] = []
    started = time.monotonic()
    last_real = time.monotonic()
    lock = threading.Lock()
    assert proc.stderr is not None
    assert proc.stdout is not None

    def _read_stderr() -> None:
        # 必须块读取：modelscope/tqdm 进度条用 \r 且无换行，按行迭代会挂等 \n，
        # 管道 64KB 缓冲写满后 ASR 进程写 stderr 阻塞死锁。
        # \r 同时按行分隔处理，否则进度条会把随后的心跳/错误 JSON 粘成一行无法解析。
        nonlocal last_real
        buf = ""

        def _split_lines(text: str) -> tuple[list[str], str]:
            parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            *complete, tail = parts
            return [c for c in complete if c], tail

        while True:
            chunk = proc.stderr.read(4096)
            if not chunk:
                break
            buf += chunk
            complete, buf = _split_lines(buf)
            for line in complete:
                stderr_chunks.append(line + "\n")
                payload = _parse_json_line(line)
                with lock:
                    last_real = time.monotonic()
                if payload and heartbeat:
                    heartbeat(payload)
        if buf:
            stderr_chunks.append(buf)

    def _read_stdout() -> None:
        stdout_box.append(proc.stdout.read() or "")

    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err.start()
    t_out.start()
    try:
        while proc.poll() is None:
            if cancel_check and cancel_check():
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise AsrError("cancelled", retryable=False)
            now = time.monotonic()
            silent = now - last_real
            wall = now - started
            if silent > timeout_seconds:
                proc.kill()
                raise AsrError("asr timed out", retryable=True)
            if wall > timeout_seconds:
                proc.kill()
                raise AsrError("asr timed out", retryable=True)
            time.sleep(0.2)
        t_out.join(timeout=60)
        t_err.join(timeout=5)
        if t_out.is_alive():
            proc.kill()
            raise AsrError("asr stdout drain timed out", retryable=True)
        stdout = "".join(stdout_box)
        if proc.returncode != 0:
            err_obj = None
            for raw in reversed(stderr_chunks):
                parsed = _parse_json_line(raw)
                if parsed and parsed.get("event") == "error":
                    err_obj = parsed
                    break
            message = (err_obj or {}).get("message") or _summarize_stderr(stderr_chunks)
            if not message:
                # funasr 内部错误（常见为加载模型时 OOM）不带消息
                message = "模型加载失败：可能内存不足（建议关闭大程序后重试）"
            raise AsrError(_friendly_asr_error(message, stderr_chunks), retryable=False)
        try:
            # funasr 导入时会向 stdout 打印版本行；结果是我们最后写的单行 JSON
            nonempty = [line.strip() for line in stdout.splitlines() if line.strip()]
            payload = nonempty[-1] if nonempty else ""
            data = json.loads(payload)
        except (StopIteration, json.JSONDecodeError) as exc:
            raise AsrError("asr stdout is not JSON", retryable=True) from exc
        if not isinstance(data, list):
            raise AsrError("asr stdout is not a JSON array")
        return data
    finally:
        if proc.poll() is None:
            proc.kill()


def _summarize_stderr(chunks: list[str]) -> str:
    """从 stderr 提取干净错误摘要：过滤心跳 JSON / tqdm / modelscope 下载与加载
    日志，只留真实错误；识别不到真错误时给出通用中文提示而非糊日志。"""
    noise_markers = (
        '{"event"',  # 心跳
        "Downloading",  # tqdm/modelscope 下载进度
        "%|",  # tqdm 进度条
        "[INFO]",
        "| INFO",
        "[DEBUG]",
        "[WARNING]",
        "| WARNING",
        "Loading ckpt",
        "Loading pretrained",
        "Loading model",
        "prefix dict",  # jieba 小写开头
        "Dumping model",
        "download models from model hub",
        "Prefix dict",
    )
    lines = []
    for raw in chunks:
        line = raw.strip()
        if not line or any(m in line for m in noise_markers):
            continue
        lines.append(line)
    if not lines:
        return ""
    err_lines = [
        ln for ln in lines if any(k in ln for k in ("Error", "Exception", "Traceback", "error"))
    ]
    text = (err_lines[-1] if err_lines else lines[-1])[:200]
    return text + ("…" if len(text) == 200 else "")


def _friendly_asr_error(message: str, chunks: list[str]) -> str:
    """常见失败翻译为可操作的中文提示；未匹配场景保留原始错误（不得吞真因）。"""
    if "not enough memory" in message or "DefaultCPUAllocator" in message:
        return "内存不足：转写需要约 5GB 空闲内存，请关闭大程序后重试"
    if "database is locked" in message:
        return "数据库忙，请稍后重试"
    # 下载失败归类仅在「无 error 事件、无具体错误」时成立，避免掩盖真因
    if message.startswith("模型") or "not enough memory" in "".join(chunks):
        return message
    joined = "".join(chunks)
    if "Downloading" in joined and '{"event": "error"' not in joined:
        return "模型正在联网下载时失败：请检查网络/代理后重试（首次需约 2GB 权重）"
    return message


def _parse_json_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text.startswith("{"):
        # tqdm 刷新在行首留进度条残迹，心跳/错误 JSON 粘在其后：提取首个 JSON 对象
        idx = text.find('{"event"')
        if idx < 0:
            return None
        text = text[idx:]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
