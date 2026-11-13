#!/usr/bin/env python3
"""Apply /pm-loop invocation to pm-loop state (parse, budget, scheduler flags)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from devloops_lib.config import find_project_root, get_state_path, load_config, run_main
from devloops_lib.pm_budget import apply_invoke, budget_summary, get_budget
from devloops_lib.pm_command import parse_pm_invoke
from devloops_lib.pm_schedule import clear_pm_scheduler, exclusivity_report, mark_pm_scheduler
from devloops_lib.tick_gate import load_state, save_state

EXIT_OK = 0
EXIT_USAGE = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("invoke", nargs="?", default="/pm-loop", help="Invocation string (default: /pm-loop)")
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    args = parser.parse_args(argv)

    try:
        parsed = parse_pm_invoke(args.invoke)
    except ValueError as exc:
        sys.stderr.write(f"pm_invoke parse error: {exc}\n")
        return EXIT_USAGE

    cfg = load_config()
    state_path = args.state_file or get_state_path("pm-loop", config=cfg)
    state = load_state(state_path)

    if parsed.action == "stop":
        state = clear_pm_scheduler(state)
        save_state(state_path, state)
        out = {"action": "stop", "scheduler": None}
        print(json.dumps(out, indent=2) if args.json else "STOPPED")
        return EXIT_OK

    if parsed.action == "status":
        report = {
            "action": "status",
            "budget": budget_summary(state, cfg),
            "scheduler": state.get("scheduler"),
            "exclusivity": exclusivity_report(state, config=cfg),
            "projectProfile": state.get("projectProfile") is not None,
        }
        print(json.dumps(report, indent=2))
        return EXIT_OK

    state, prompts = apply_invoke(state, parsed, cfg)
    if parsed.interval_seconds is not None:
        state = mark_pm_scheduler(state, kind="shell", intervalSeconds=parsed.interval_seconds)

    save_state(state_path, state)
    payload = {
        "action": "tick",
        "parsed": {
            "intervalSeconds": parsed.interval_seconds,
            "slots": parsed.slots,
            "delegation": parsed.delegation,
        },
        "budget": budget_summary(state, cfg),
        "promptsNeeded": prompts,
        "exclusivity": exclusivity_report(
            state,
            state_root=find_project_root() / str(cfg.get("state_dir", ".devloops")),
            config=cfg,
        ),
    }
    print(json.dumps(payload, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]))
