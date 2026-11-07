#!/usr/bin/env python3
"""Build one plan for a worker tick, and re-check an item before working on it.

With no check flag, prints JSON: `items` lists everything that needs work now (PRs,
then issues), and `maxItems` is how many to finish this tick. `waiting` lists issues
whose dependency status is `wait`, which the worker reads before deciding.

Check flags exit 0 if the item is still valid, 1 if it changed, and 3 on error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root and devloops_lib to sys.path
_script_dir = Path(__file__).resolve().parent
for _candidate in (_script_dir, _script_dir.parents[2], _script_dir.parents[3], _script_dir.parents[3] / ".agents"):
    if (_candidate / "devloops_lib").is_dir() or _candidate == _script_dir:
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))

from issue_candidates import fetch_candidates

from devloops_lib.config import get_state_path, load_config
from devloops_lib.gh import GhError
from devloops_lib.pr_status import summarize_prs
from devloops_lib.tick_gate import EXIT_ERROR, EXIT_NOOP, EXIT_RUN, load_state

# The pr_status priority order. wait-ci and none need no work now.
PR_ACTIONS = ["resolve-conflict", "restack", "address-gatekeeper", "triage-comments", "fix-ci"]


def max_items_per_tick(config: dict) -> int:
    return max(1, int(config.get("worker", {}).get("max_items_per_tick", 1)))


def _skip_is_current(skipped: dict, candidate: dict) -> bool:
    entry = skipped.get(str(candidate["number"]))
    return bool(entry) and entry.get("updatedAt") == candidate.get("updatedAt")


def build_plan(prs: list[dict], candidates: dict, skipped: dict, max_items: int) -> dict:
    """Return all items that need work now, in order. nextAction is the source of truth for PRs."""
    pr_items = sorted(
        ({"kind": "pr", "number": pr["number"], "action": pr["nextAction"]}
         for pr in prs if pr.get("nextAction") in PR_ACTIONS),
        key=lambda item: (PR_ACTIONS.index(item["action"]), item["number"]),
    )
    open_issues = [c for c in candidates["candidates"] if not _skip_is_current(skipped, c)]
    waiting = [c["number"] for c in open_issues if c["dependency"]["status"] == "wait"]
    issue_items = [
        {"kind": "issue", "number": c["number"], "title": c["title"],
         "updatedAt": c.get("updatedAt"), "dependency": c["dependency"]}
        for c in open_issues if c["dependency"]["status"] != "wait"
    ]
    limit = candidates["openPRLimit"]
    return {
        "maxItems": max_items,
        "openPRLimit": limit,
        "items": pr_items + ([] if limit["reached"] else issue_items),
        "waiting": waiting,
    }


def pr_item_is_current(fresh: list[dict], action: str) -> bool:
    return bool(fresh) and fresh[0].get("nextAction") == action


def issue_item_is_current(candidates: dict, number: int, updated_at: str) -> bool:
    for candidate in candidates["candidates"]:
        if candidate["number"] == number:
            return candidate.get("updatedAt") == updated_at and candidate["dependency"]["status"] != "wait"
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check-pr", type=int, metavar="N", help="Re-check a PR item; needs --action")
    parser.add_argument("--action", help="The nextAction recorded in the plan")
    parser.add_argument("--check-issue", type=int, metavar="N", help="Re-check an issue item; needs --updated-at")
    parser.add_argument("--updated-at", help="The updatedAt recorded in the plan")
    parser.add_argument("--state-file", type=Path, default=None, help="Path to state.json")
    args = parser.parse_args(argv)
    if args.check_pr is not None and not args.action:
        parser.error("--check-pr needs --action")
    if args.check_issue is not None and not args.updated_at:
        parser.error("--check-issue needs --updated-at")

    try:
        if args.check_pr is not None:
            valid = pr_item_is_current(summarize_prs(args.check_pr), args.action)
        elif args.check_issue is not None:
            valid = issue_item_is_current(fetch_candidates(), args.check_issue, args.updated_at)
        else:
            state = load_state(args.state_file or get_state_path("worker-loop"))
            plan = build_plan(
                summarize_prs(), fetch_candidates(), state.get("skipped") or {}, max_items_per_tick(load_config())
            )
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return EXIT_RUN
    except GhError as e:
        sys.stderr.write(f"plan_tick error: {e}\n")
        return EXIT_ERROR
    print("VALID" if valid else "CHANGED")
    return EXIT_RUN if valid else EXIT_NOOP


if __name__ == "__main__":
    sys.exit(main())
