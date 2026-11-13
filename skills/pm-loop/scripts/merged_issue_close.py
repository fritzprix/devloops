#!/usr/bin/env python3
"""List open issues the PM should close because their work has merged."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.config import run_main
from devloops_lib.pm_issue_close import closable_merged_issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--board-json",
        required=True,
        help='Board snapshot: {"openIssues": [...], "mergedWork": [...]}',
    )
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
    merged_work = board.get("mergedWork") or board.get("merged_work") or []
    if not isinstance(open_issues, list) or not isinstance(merged_work, list):
        sys.stderr.write("openIssues and mergedWork must be arrays\n")
        return 2

    closable = closable_merged_issues(open_issues, merged_work)
    print(json.dumps({"closable": closable, "count": len(closable)}, indent=2))
    return 0


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]))
