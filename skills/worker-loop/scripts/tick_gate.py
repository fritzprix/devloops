#!/usr/bin/env python3
"""Preflight gate for worker-loop."""
from __future__ import annotations

import sys
from pathlib import Path

# The skill ships its own copy of devloops_lib next to this script.
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.tick_gate import *
from devloops_lib.tick_gate import main
from devloops_lib.config import run_main

if __name__ == "__main__":
    run_main(main)
