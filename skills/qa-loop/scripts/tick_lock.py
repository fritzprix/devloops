#!/usr/bin/env python3
"""Tick lock for qa-loop runs started without `devloops tick`.

Same flags as worker-loop's tick_gate.py, for the qa-loop slot:
`--lock` prints `TOKEN <token>`, `--renew --token T`, `--unlock --token T`.
Under `devloops tick qa`, the wrapper holds the lock and these keep it.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The skill ships its own copy of devloops_lib next to this script.
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.tick_gate import main
from devloops_lib.config import run_main

if __name__ == "__main__":
    run_main(lambda: main(["--loop", "qa-loop", *sys.argv[1:]]))
