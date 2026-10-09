# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""Record ASR process peak working set. Run inside the ASR venv."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def peak_working_set_bytes() -> int | None:
    if os.name != "nt":
        try:
            import resource

            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        except Exception:
            return None
    import subprocess

    out = subprocess.check_output(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-Process -Id {os.getpid()}).PeakWorkingSet64",
        ],
        text=True,
    )
    return int(out.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True)
    parser.add_argument("--model-dir", required=True)
    args = parser.parse_args()
    from huiyiku_asr.transcribe import run_transcribe

    segs = run_transcribe(Path(args.wav), Path(args.model_dir), enable_spk=True)
    peak = peak_working_set_bytes()
    print(
        json.dumps(
            {
                "segments": segs,
                "has_speaker": any(s.get("speaker_label") for s in segs),
                "peak_working_set_bytes": peak,
                "peak_working_set_gb": round((peak or 0) / (1024**3), 3),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
