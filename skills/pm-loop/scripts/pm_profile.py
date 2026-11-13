#!/usr/bin/env python3
"""Write or refresh pm-loop projectProfile (induction)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.config import get_state_path, load_config, run_main
from devloops_lib.pm_induction import apply_profile, build_project_profile, induction_required
from devloops_lib.tick_gate import load_state, save_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Build and store projectProfile")
    parser.add_argument("--signals-json", default="{}", help="Optional repo signal JSON")
    parser.add_argument("--confirm", action="store_true", help="Mark profile confirmed after ambiguity review")
    parser.add_argument("--state-file", type=Path, default=None)
    args = parser.parse_args(argv)

    cfg = load_config()
    state_path = args.state_file or get_state_path("pm-loop", config=cfg)
    state = load_state(state_path)

    try:
        signals = json.loads(args.signals_json)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"invalid --signals-json: {exc}\n")
        return 2
    if not isinstance(signals, dict):
        sys.stderr.write("--signals-json must be a JSON object\n")
        return 2

    if args.write:
        profile = build_project_profile(config=cfg, signals=signals)
        state = apply_profile(state, profile, confirm=args.confirm)
        save_state(state_path, state)

    print(
        json.dumps(
            {
                "inductionRequired": induction_required(state),
                "projectProfile": state.get("projectProfile"),
                "profileConfirmed": state.get("profileConfirmed", False),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]))
