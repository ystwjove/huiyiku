# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from repo_gates import _run, check_notices  # noqa: E402


def main() -> int:
    return _run("notices", check_notices)


if __name__ == "__main__":
    raise SystemExit(main())
