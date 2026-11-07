#!/usr/bin/env python3
"""Gatekeeper issue queue and validation verdict application.

Lists unjudged candidate issues filed by producer-loop and applies validation
verdicts (validated or rejected) with file:line evidence.

Usage:
  python3 issue_queue.py                     # list unjudged producer issues
  python3 issue_queue.py --next              # show the next unjudged issue to validate
  python3 issue_queue.py --issue 42 --verdict validated --reason "Confirmed leak" --evidence "auth.py:42" [--apply]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root and devloops_lib to sys.path
_script_dir = Path(__file__).resolve().parent
for _candidate in (_script_dir.parents[2], _script_dir.parents[3], _script_dir.parents[3] / ".agents"):
    if (_candidate / "devloops_lib").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from devloops_lib.config import add_agent_id_argument, get_state_path, load_config
from devloops_lib.issue_queue import apply_verdict, list_unjudged_issues
from devloops_lib.tick_gate import GhError
from devloops_lib.config import run_main


def validation_batch(issues: list[dict], config: dict) -> list[dict]:
    """Return the issues to validate this tick, up to the configured cap (at least 1)."""
    cap = int(config.get("gatekeeper", {}).get("max_issue_validations_per_tick", 1))
    return issues[:max(1, cap)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List unjudged producer issues (default)")
    parser.add_argument("--next", action="store_true", help="Print the next unjudged issue JSON or NOOP")
    parser.add_argument("--batch", action="store_true", help="Print this tick's issues as a JSON list, up to the configured cap, or NOOP")
    parser.add_argument("--issue", type=int, default=None, help="Issue number to judge")
    parser.add_argument("--verdict", type=str, choices=["validated", "rejected"], default=None, help="Validation verdict")
    parser.add_argument("--reason", type=str, default="", help="Validation reason")
    parser.add_argument("--evidence", type=str, default="", help="file:line evidence")
    parser.add_argument("--task", type=str, default=None, help="Producer task override (default: inferred from label)")
    parser.add_argument("--apply", action="store_true", help="Apply changes via gh and update state (default: dry run)")
    parser.add_argument("--state-file", type=Path, default=None, help="Path to gatekeeper state.json")
    add_agent_id_argument(parser)
    args = parser.parse_args(argv)

    cfg = load_config()
    state_path = args.state_file or get_state_path("gatekeeper-loop", config=cfg)

    try:
        # If judging an issue
        if args.issue and args.verdict:
            res = apply_verdict(
                issue_number=args.issue,
                verdict=args.verdict,
                reason=args.reason,
                evidence=args.evidence,
                task=args.task,
                config=cfg,
                state_path=state_path,
                apply=args.apply,
            )
            mode = "APPLIED" if args.apply else "DRY-RUN"
            print(f"[{mode}] Issue #{res['issue']} marked as {res['verdict']} (task: {res['task']})")
            print(f"  Reason: {res['reason']}")
            print(f"  Evidence: {res['evidence']}")
            return 0

        if args.batch:
            issues = list_unjudged_issues(config=cfg)
            if not issues:
                print("NOOP")
                return 1
            print(json.dumps(validation_batch(issues, cfg), indent=2))
            return 0

        # If --next requested
        if args.next:
            issues = list_unjudged_issues(config=cfg)
            if not issues:
                print("NOOP")
                return 1
            print(json.dumps(issues[0], indent=2))
            return 0

        # Default: list unjudged issues
        issues = list_unjudged_issues(config=cfg)
        if not issues:
            print("No unjudged producer issues in queue.")
            return 0

        print(f"Found {len(issues)} unjudged producer issue(s):")
        for iss in issues:
            print(f"- #{iss['number']}: {iss['title']} (task: {iss['task']})")
        return 0

    except GhError as e:
        sys.stderr.write(f"GitHub CLI error: {e}\n")
        return 2
    except Exception as e:
        sys.stderr.write(f"issue_queue error: {e}\n")
        return 3


if __name__ == "__main__":
    run_main(main)
