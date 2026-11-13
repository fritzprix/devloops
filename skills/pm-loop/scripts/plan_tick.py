#!/usr/bin/env python3
"""JSON orchestration plan for one pm-loop tick."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.config import find_project_root, get_state_path, load_config, run_main
from devloops_lib.pm_budget import budget_summary, get_budget, prompts_needed
from devloops_lib.pm_induction import induction_required
from devloops_lib.pm_schedule import cascade_plan, exclusivity_report
from devloops_lib.tick_gate import load_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pipeline-json",
        default="{}",
        help="Optional pipeline counters: approvedIssues, prsNeedingReview, producerDue, boardFull",
    )
    parser.add_argument("--state-file", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        pipeline = json.loads(args.pipeline_json)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"invalid --pipeline-json: {exc}\n")
        return 2
    if not isinstance(pipeline, dict):
        sys.stderr.write("--pipeline-json must be a JSON object\n")
        return 2

    cfg = load_config()
    state_path = args.state_file or get_state_path("pm-loop", config=cfg)
    state = load_state(state_path)
    root = find_project_root()
    state_root = root / str(cfg.get("state_dir", ".devloops"))

    budget = budget_summary(state, cfg)
    slots = budget.get("slots") or 1

    plan = {
        "budget": budget,
        "promptsNeeded": prompts_needed(get_budget(state)),
        "exclusivity": exclusivity_report(state, state_root=state_root, config=cfg),
        "inductionRequired": induction_required(state),
        "projectProfile": state.get("projectProfile"),
        "cascade": cascade_plan(slots=int(slots), pipeline=pipeline),
        "constraints": {
            "neverApprove": True,
            "neverMerge": True,
            "neverAutoMerge": True,
            "producerLabelOnly": (cfg.get("labels") or {}).get("producer", "auto:producer"),
            "noProducerWhenBoardFull": True,
        },
        "roleSkills": {
            "worker-loop": "worker-loop/SKILL.md",
            "gatekeeper-loop": "gatekeeper-loop/SKILL.md",
            "producer-loop": "producer-loop/SKILL.md",
        },
    }
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]))
