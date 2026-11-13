#!/usr/bin/env python3
"""Skill wrapper for devloops_lib.pm_invoke."""
from __future__ import annotations

import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.config import run_main
from devloops_lib import pm_invoke

if __name__ == "__main__":
    run_main(lambda: pm_invoke.main(sys.argv[1:]))
