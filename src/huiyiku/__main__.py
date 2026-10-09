# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""CLI entry: ``python -m huiyiku`` / frozen ``Huiyiku.exe --serve|--worker``."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from huiyiku import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="huiyiku", description="会议库：本机会议录音知识库")
    parser.add_argument("--version", action="version", version=f"huiyiku {__version__}")
    parser.add_argument("--serve", action="store_true", help="Start the API process.")
    parser.add_argument("--worker", action="store_true", help="Start the background worker.")
    sub = parser.add_subparsers(dest="cmd")
    pipe = sub.add_parser(
        "pipeline", help="Slice 0 offline: media → transcript.md → stats → optional LLM"
    )
    pipe.add_argument("--input", required=True, help="Audio or video file")
    pipe.add_argument("--out-dir", default="pipeline-out")
    pipe.add_argument("--llm", action="store_true", help="Also call remote LLM for summary.md")
    pipe.add_argument("--model-dir", default=None)
    return parser


def _watch_parent() -> None:
    """父进程退出（含崩溃/被强杀）时子进程自退，避免孤儿占着 8787。"""
    raw = os.environ.get("HUIYIKU_PARENT_PID", "")
    if not raw.isdigit() or sys.platform != "win32":
        return
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=False)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        synchronize = 0x00100000
        infinite = 0xFFFFFFFF
        handle = kernel32.OpenProcess(synchronize, False, int(raw))
        if not handle:
            os._exit(1)
        threading.Thread(
            target=lambda: (kernel32.WaitForSingleObject(handle, infinite), os._exit(1)),
            daemon=True,
        ).start()
    except Exception:
        os._exit(1)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.serve and args.worker:
        parser.error("use either --serve or --worker, not both")
    if args.serve:
        _watch_parent()
        return run_serve()
    if args.worker:
        _watch_parent()
        from huiyiku.worker.loop import run_worker

        return run_worker()
    if args.cmd == "pipeline":
        from huiyiku.pipeline import run_pipeline

        result = run_pipeline(
            Path(args.input),
            Path(args.out_dir),
            with_llm=args.llm,
            model_dir=Path(args.model_dir) if args.model_dir else None,
        )
        print(result)
        return 0
    from huiyiku.bootstrap import install_excepthook, run_bootstrap
    from huiyiku.config import load_runtime

    runtime = load_runtime()
    install_excepthook(runtime.layout["logs"])
    return run_bootstrap()


def run_serve() -> int:
    import uvicorn

    from huiyiku.api.app import create_app
    from huiyiku.config import load_runtime
    from huiyiku.logging_setup import setup_logging

    runtime = load_runtime()
    setup_logging(runtime.layout["logs"])
    app = create_app(runtime)
    # 无控制台（frozen）下 sys.stdout 为 None，uvicorn 默认 formatter 会调
    # isatty() 崩溃；仅该场景禁用彩色，开发终端不受影响。
    # 固定监听全部 IPv4 接口。开或关内网共享只改设置，不必重启
    # （重启会关掉桌面窗口）。关闭时中间件拒绝非本机请求。
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8787,
        workers=1,
        log_level="info",
        use_colors=None if sys.stdout is not None else False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
