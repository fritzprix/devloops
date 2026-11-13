#!/usr/bin/env python3
"""Persist a one-time user answer for pm-loop budget fields."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.config import get_state_path, load_config, run_main
from devloops_lib.pm_budget import budget_summary, record_user_budget_answer
from devloops_lib.tick_gate import load_state, save_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--field", required=True, choices=["slots", "delegation"])
    parser.add_argument("--value", required=True)
    parser.add_argument("--state-file", type=Path, default=None)
    args = parser.parse_args(argv)

    cfg = load_config()
    state_path = args.state_file or get_state_path("pm-loop", config=cfg)
    state = load_state(state_path)
    try:
        state = record_user_budget_answer(state, args.field, args.value)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    save_state(state_path, state)
    print(json.dumps({"budget": budget_summary(state, cfg)}, indent=2))
    return 0


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]))
