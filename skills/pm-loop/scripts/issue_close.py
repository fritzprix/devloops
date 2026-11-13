#!/usr/bin/env python3
"""List open issues judged complete under the project definition of done."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.config import load_config, run_main
from devloops_lib.issue_close import closable_issues, resolve_definition_of_done
from devloops_lib.tick_gate import load_state
from devloops_lib.config import get_state_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--board-json",
        required=True,
        help='Board snapshot: {"openIssues": [...], "workItems": [...]}',
    )
    parser.add_argument("--state-file", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        board = json.loads(args.board_json)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"invalid --board-json: {exc}\n")
        return 2
    if not isinstance(board, dict):
        sys.stderr.write("--board-json must be a JSON object\n")
        return 2

    open_issues = board.get("openIssues") or board.get("open_issues") or []
    work_items = board.get("workItems") or board.get("work_items") or board.get("mergedWork") or board.get("merged_work") or []
    if not isinstance(open_issues, list) or not isinstance(work_items, list):
        sys.stderr.write("openIssues and workItems must be arrays\n")
        return 2

    cfg = load_config()
    state = load_state(args.state_file or get_state_path("pm-loop", config=cfg))
    profile = state.get("projectProfile")
    dod = resolve_definition_of_done(cfg, profile if isinstance(profile, dict) else None)
    override = board.get("definitionOfDone") or board.get("definition_of_done")
    if isinstance(override, list) and override:
        dod = {**dod, "signals": [str(s) for s in override], "source": "board"}

    closable = closable_issues(open_issues, work_items, definition_of_done=dod)
    print(
        json.dumps(
            {
                "definitionOfDone": dod,
                "closableIssues": closable,
                "count": len(closable),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]))
