#!/usr/bin/env python3
"""Tick lock for gatekeeper-loop runs started without `devloops tick`.

Same flags as worker-loop's tick_gate.py, for the gatekeeper-loop slot:
`--lock` prints `TOKEN <token>`, `--renew --token T`, `--unlock --token T`.
Under `devloops tick gatekeeper`, the wrapper holds the lock and these keep it.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add project root and devloops_lib to sys.path
_script_dir = Path(__file__).resolve().parent
for _candidate in (_script_dir.parents[2], _script_dir.parents[3], _script_dir.parents[3] / ".agents"):
    if (_candidate / "devloops_lib").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from devloops_lib.tick_gate import main

if __name__ == "__main__":
    main(["--loop", "gatekeeper-loop", *sys.argv[1:]])
