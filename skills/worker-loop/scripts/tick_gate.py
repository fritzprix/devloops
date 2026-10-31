#!/usr/bin/env python3
"""Preflight gate for worker-loop."""
from __future__ import annotations

import sys
from pathlib import Path

# Add project root and devloops_lib to sys.path
_script_dir = Path(__file__).resolve().parent
for _candidate in (_script_dir.parents[2], _script_dir.parents[3], _script_dir.parents[3] / ".agents"):
    if (_candidate / "devloops_lib").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from devloops_lib.tick_gate import *
from devloops_lib.tick_gate import main

if __name__ == "__main__":
    main()
