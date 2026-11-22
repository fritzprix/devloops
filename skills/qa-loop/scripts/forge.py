#!/usr/bin/env python3
"""Forge-neutral PR, issue, CI, and label CLI shim for qa-loop.

The same command as `devloops forge`, for installs without `devloops` on PATH.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The skill ships its own copy of devloops_lib next to this script.
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.forge.cli import forge_main

if __name__ == "__main__":
    sys.exit(forge_main(sys.argv[1:]))
