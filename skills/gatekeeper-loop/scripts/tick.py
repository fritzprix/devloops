#!/usr/bin/env python3
"""Run one gatekeeper-loop tick: gate check, lock, agent command, unlock.

The same wrapper as `devloops tick gatekeeper`, for installs without the devloops command.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The skill ships its own copy of devloops_lib next to this script.
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.cli import tick_main

if __name__ == "__main__":
    sys.exit(tick_main("gatekeeper", sys.argv[1:]))
