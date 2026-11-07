#!/usr/bin/env python3
"""Check tick ownership before pushes and destructive git commands."""
from __future__ import annotations

import sys
from pathlib import Path

# Add project root and devloops_lib to sys.path
_script_dir = Path(__file__).resolve().parent
for _candidate in (_script_dir.parents[2], _script_dir.parents[3], _script_dir.parents[3] / ".agents"):
    if (_candidate / "devloops_lib").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from devloops_lib.guard import main
from devloops_lib.config import run_main

if __name__ == "__main__":
    run_main(main)
