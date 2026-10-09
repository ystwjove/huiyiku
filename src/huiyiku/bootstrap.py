# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import httpx

from huiyiku.config import load_runtime
from huiyiku.db.session import wal_checkpoint
from huiyiku.logging_setup import setup_logging
from huiyiku.paths import is_frozen

LISTEN = "http://127.0.0.1:8787"


def _child_cmd(flag: str) -> list[str]:
    if is_frozen():
        return [sys.executable, flag]
    return [sys.executable, "-m", "huiyiku", flag]


def _spawn_child(flag: str) -> subprocess.Popen:
    """启动子进程并传 PID 供其守望：父进程消亡（含崩溃/强杀）时子进程自退。"""
    env = {**os.environ, "HUIYIKU_PARENT_PID": str(os.getpid())}
    return subprocess.Popen(_child_cmd(flag), env=env)


def _health() -> dict | None:
    try:
        r = httpx.get(f"{LISTEN}/api/health", timeout=0.6)
        if r.status_code == 200:
            return r.json()
    except httpx.HTTPError:
        return None
    return None


def _occupied_by_other() -> bool:
    try:
        r = httpx.get(f"{LISTEN}/api/health", timeout=0.6)
    except httpx.HTTPError:
        return False
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return data.get("app") != "huiyiku"


def _webview2_available() -> bool:
    # 缺 WebView2/.NET 时 pywebview 会静默降到 IE 内核（mshtml），本界面不能用。
    if sys.platform != "win32":
        return False
    try:
        from webview.platforms import winforms

        return winforms.renderer == "edgechromium"
    except Exception:
        logging.getLogger("huiyiku.bootstrap").exception("webview backend unusable")
        return False


def _clamped_window_size(sw: float, sh: float) -> tuple[int, int, int, int]:
    """给定逻辑屏尺寸，返回 (width, height, min_w, min_h)。

    常规屏钳制到屏 90%/85%，下限 960×540；极小屏（可用区低于下限时）
    放弃下限、以屏内为准，min_size 同步缩小，保证任何屏都不超边界。
    """
    width, height = 1280, 820
    min_w, min_h = 960, 540
    if sw <= 0 or sh <= 0:
        return width, height, min_w, min_h
    avail_w, avail_h = int(sw * 0.9), int(sh * 0.85)
    width = min(width, avail_w) if avail_w >= min_w else avail_w
    height = min(height, avail_h) if avail_h >= min_h else avail_h
    min_w, min_h = min(min_w, width), min(min_h, height)
    return width, height, min_w, min_h


def _window_size() -> tuple[int, int, int, int]:
    """窗口与最小尺寸（逻辑像素，pywebview 内部再乘 DPI 缩放）。"""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        scale = user32.GetDpiForSystem() / 96.0 or 1.0  # 物理→逻辑（100% 缩放时为 1）
        sw = user32.GetSystemMetrics(0) / scale
        sh = user32.GetSystemMetrics(1) / scale
        return _clamped_window_size(sw, sh)
    except Exception:
        return _clamped_window_size(0, 0)


def _native_window(storage_dir: Path | None = None) -> int:
    import webview

    width, height, min_w, min_h = _window_size()
    storage = None
    if storage_dir is not None:
        # 持久化 WebView2 用户数据（Cookie/localStorage），否则访问令牌每次启动都要重输
        storage_dir.mkdir(parents=True, exist_ok=True)
        storage = str(storage_dir)
    webview.create_window(
        "会议库",
        LISTEN,
        width=width,
        height=height,
        min_size=(min_w, min_h),
        text_select=True,
    )
    webview.start(gui="edgechromium", private_mode=storage is None, storage_path=storage)
    return 0


def run_bootstrap() -> int:
    runtime = load_runtime()
    setup_logging(runtime.layout["logs"])
    existing = _health()
    if existing and existing.get("app") == "huiyiku":
        # 单窗口原则：能开原生窗口就开（唯一入口）；开不出来才退系统浏览器
        try:
            if _webview2_available():
                return _native_window(runtime.layout["root"] / "webview")
        except Exception:
            logging.getLogger("huiyiku.bootstrap").exception(
                "native window failed; falling back to browser"
            )
        webbrowser.open(LISTEN)
        return 0
    if _occupied_by_other():
        _fatal(runtime.layout["logs"], "端口 8787 已被其它程序占用，会议库不会改绑其它端口。")
        return 1
    serve = _spawn_child("--serve")
    worker = _spawn_child("--worker")
    ready = False
    for _ in range(80):
        if _health():
            ready = True
            break
        if serve.poll() is not None:
            break
        time.sleep(0.25)
    if not ready:
        _stop(serve, worker)
        _fatal(runtime.layout["logs"], "API 未能在 8787 上启动。")
        return 1
    restarted_worker = False
    stopping = threading.Event()
    guard = threading.Lock()

    def watch() -> None:
        nonlocal worker, restarted_worker
        while True:
            if stopping.wait(timeout=2):
                return
            if serve.poll() is not None:
                return
            with guard:
                # 持锁复查：shutdown 期间不重启，避免 _stop 之后留下孤儿 worker
                if stopping.is_set():
                    return
                if worker.poll() is not None and worker.returncode != 0 and not restarted_worker:
                    restarted_worker = True
                    worker = _spawn_child("--worker")

    threading.Thread(target=watch, daemon=True).start()

    def _shutdown_children() -> None:
        stopping.set()
        with guard:
            _stop(serve, worker)

    if _webview2_available():
        try:
            code = _native_window(runtime.layout["root"] / "webview")
        except Exception:
            logging.getLogger("huiyiku.bootstrap").exception(
                "native window failed; falling back to browser"
            )
        else:
            _shutdown_children()
            try:
                wal_checkpoint(runtime.layout["db"])
            except Exception:
                pass
            return code
    webbrowser.open(LISTEN)
    code = _status_window(runtime, serve, lambda: worker)
    _shutdown_children()
    try:
        wal_checkpoint(runtime.layout["db"])
    except Exception:
        pass
    return code


def _stop(serve: subprocess.Popen, worker: subprocess.Popen) -> None:
    for proc in (worker, serve):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()


def _fatal(log_dir: Path, message: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "bootstrap-error.log").write_text(message + "\n", encoding="utf-8")
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("会议库", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def _status_window(runtime, serve: subprocess.Popen, worker_fn) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except Exception:
        serve.wait()
        return 0

    root = tk.Tk()
    root.title("会议库")
    root.geometry("420x220")
    tk.Label(root, text=LISTEN, font=("Segoe UI", 12)).pack(pady=12)

    def add_file() -> None:
        path = filedialog.askopenfilename(
            title="添加本地文件（新建会议）",
            filetypes=[("Media", "*.wav *.mp3 *.m4a *.mp4"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            r = httpx.post(f"{LISTEN}/api/meetings/import-local", json={"path": path}, timeout=30)
            r.raise_for_status()
        except Exception as exc:
            messagebox.showerror("会议库", str(exc))

    def on_quit() -> None:
        root.destroy()

    tk.Button(root, text="添加本地文件", command=add_file).pack(fill="x", padx=24, pady=4)
    tk.Button(root, text="退出", command=on_quit).pack(fill="x", padx=24, pady=4)
    status = tk.Label(root, text="运行中", fg="#2563eb")
    status.pack(pady=8)

    def poll() -> None:
        if serve.poll() is not None:
            status.config(text="API 已退出", fg="#b91c1c")
            return
        w = worker_fn()
        if w.poll() is not None and w.returncode != 0:
            status.config(text="Worker 异常（已尝试重启一次）", fg="#b45309")
        root.after(1000, poll)

    root.after(1000, poll)
    root.protocol("WM_DELETE_WINDOW", on_quit)
    root.mainloop()
    return 0


def install_excepthook(log_dir: Path) -> None:
    def _hook(exc_type, exc, tb) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        (log_dir / "unhandled.log").write_text(text, encoding="utf-8")
        print(text, file=sys.stderr)

    sys.excepthook = _hook
