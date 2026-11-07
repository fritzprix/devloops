#!/usr/bin/env python3
"""Build one plan for a worker tick, and re-check an item before working on it.

With no check flag, prints JSON: `items` lists everything that needs work now (PRs,
then issues), and `maxItems` is how many to finish this tick. `waiting` lists issues
whose dependency status is `wait`, which the worker reads before deciding.

Each PR item carries a `stamp` of the state the plan was built from: next action,
head SHA, base branch, and the latest gatekeeper verdict (round, verdict, reviewed
head). `--check-pr N --stamp S` reports CHANGED when any of them moved, so a plan
built before a push, a rebase, a retarget, or a new review round is never executed.

Check flags exit 0 if the item is still valid, 1 if it changed, and 3 on error.
"""
from __future__ import annotations

import argparse
import hashlib
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
from devloops_lib.config import run_main

# The pr_status priority order. wait-ci and none need no work now.
PR_ACTIONS = ["resolve-conflict", "restack", "address-gatekeeper", "triage-comments", "fix-ci"]


def max_items_per_tick(config: dict) -> int:
    return max(1, int(config.get("worker", {}).get("max_items_per_tick", 1)))


def _skip_is_current(skipped: dict, candidate: dict) -> bool:
    entry = skipped.get(str(candidate["number"]))
    return bool(entry) and entry.get("updatedAt") == candidate.get("updatedAt")


def _pr_fields(pr: dict) -> dict:
    gatekeeper = pr.get("gatekeeper") or {}
    return {
        "action": pr.get("nextAction"),
        "head": pr.get("headSha"),
        "base": pr.get("base"),
        "gatekeeper": {key: gatekeeper.get(key) for key in ("round", "verdict", "head", "url")},
    }


def pr_stamp(pr: dict) -> str:
    """Hash the PR state a plan item depends on."""
    return hashlib.sha256(json.dumps(_pr_fields(pr), sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _pr_item(pr: dict) -> dict:
    gatekeeper = pr.get("gatekeeper") or {}
    return {
        "kind": "pr", "number": pr["number"], "action": pr["nextAction"], "head": pr.get("headSha"),
        "base": pr.get("base"), "gatekeeperRound": gatekeeper.get("round"), "stamp": pr_stamp(pr),
    }


def build_plan(prs: list[dict], candidates: dict, skipped: dict, max_items: int) -> dict:
    """Return all items that need work now, in order. nextAction is the source of truth for PRs."""
    pr_items = sorted(
        (_pr_item(pr) for pr in prs if pr.get("nextAction") in PR_ACTIONS),
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


def pr_item_is_current(fresh: list[dict], stamp: str) -> bool:
    return bool(fresh) and fresh[0].get("nextAction") in PR_ACTIONS and pr_stamp(fresh[0]) == stamp


def describe_pr(fresh: list[dict]) -> str:
    if not fresh:
        return "PR is closed or not found"
    fields = _pr_fields(fresh[0])
    return (f"action {fields['action']}, head {fields['head']}, base {fields['base']}, "
            f"gatekeeper round {fields['gatekeeper']['round']} {fields['gatekeeper']['verdict']}")


def issue_item_is_current(candidates: dict, number: int, updated_at: str) -> bool:
    for candidate in candidates["candidates"]:
        if candidate["number"] == number:
            return candidate.get("updatedAt") == updated_at and candidate["dependency"]["status"] != "wait"
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check-pr", type=int, metavar="N", help="Re-check a PR item; needs --stamp")
    parser.add_argument("--stamp", help="The stamp recorded in the plan")
    parser.add_argument("--check-issue", type=int, metavar="N", help="Re-check an issue item; needs --updated-at")
    parser.add_argument("--updated-at", help="The updatedAt recorded in the plan")
    parser.add_argument("--state-file", type=Path, default=None, help="Path to state.json")
    args = parser.parse_args(argv)
    if args.check_pr is not None and not args.stamp:
        parser.error("--check-pr needs --stamp")
    if args.check_issue is not None and not args.updated_at:
        parser.error("--check-issue needs --updated-at")

    detail = ""
    try:
        if args.check_pr is not None:
            fresh = summarize_prs(args.check_pr)
            valid = pr_item_is_current(fresh, args.stamp)
            detail = describe_pr(fresh)
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
    print("VALID" if valid else f"CHANGED{': ' + detail if detail else ''}")
    return EXIT_RUN if valid else EXIT_NOOP


if __name__ == "__main__":
    run_main(main)
