#!/usr/bin/env python3
"""Run one pm-loop tick: gate check, lock, agent command, unlock."""
from __future__ import annotations

import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.cli import tick_main

if __name__ == "__main__":
    sys.exit(tick_main("pm", sys.argv[1:]))
