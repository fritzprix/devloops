#!/usr/bin/env python3
"""Tick lock for pm-loop runs started without `devloops tick`."""
from __future__ import annotations

import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.config import run_main
from devloops_lib.tick_gate import main

if __name__ == "__main__":
    run_main(lambda: main(["--loop", "pm-loop", *sys.argv[1:]]))
