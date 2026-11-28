#!/usr/bin/env python3
"""qa-loop queue: open-PR testability status, two-party sign-off, merged-PR tests.

Subcommands (all print JSON unless noted):
  prs [--pr N]                      open PRs by @me with qa status, oldest first
  check-head N --head SHA           exit 0 VALID, 1 CHANGED, 3 error (run before posting)
  signoff N                         gatekeeper approve AND qa accepted at the current head?
                                    exit 0 merge-ready, 1 blocked
  merged [--since YYYY-MM-DD]       find PRs merged into the base branch after the cursor
                                    and add them to pendingTests (first run: set cursor only)
  record N --result pass|fail|blocked --build SHA [--issue M ...]
                                    record one post-merge test result
  link-blocked-by --issue N --blocked-by M
                                    add a forge blocked-by dependency, re-query, print the
                                    verified open-blocker list; exit 1 if M is not in it
  status                            print the qa-loop state

Markers and labels: references/comment-format.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The skill ships its own copy of devloops_lib next to this script.
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.config import add_agent_id_argument, get_state_path, load_config, run_main
from devloops_lib.forge.detect import get_forge
from devloops_lib.gh import GhError, gh
from devloops_lib import qa

PR_FIELDS = "number,title,url,headRefOid,baseRefName,isDraft,mergeable,createdAt,comments,labels,state,statusCheckRollup"


def _view(number: int) -> dict:
    return json.loads(gh("pr", "view", str(number), "--json", PR_FIELDS))


def _me() -> str:
    return gh("api", "user", "--jq", ".login").strip()


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-file", type=Path, default=None, help="Path to qa-loop state.json")
    add_agent_id_argument(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prs"); p.add_argument("--pr", type=int)
    p = sub.add_parser("check-head"); p.add_argument("number", type=int); p.add_argument("--head", required=True)
    p = sub.add_parser("signoff"); p.add_argument("number", type=int)
    p = sub.add_parser("merged"); p.add_argument("--since", help="Backfill: queue PRs merged on or after this date")
    p = sub.add_parser("record")
    p.add_argument("number", type=int)
    p.add_argument("--result", required=True, choices=["pass", "fail", "blocked"])
    p.add_argument("--build", required=True, help="Commit SHA that was built and tested")
    p.add_argument("--issue", type=int, action="append", default=[], help="Issue filed for a failure")
    p = sub.add_parser("link-blocked-by")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--blocked-by", type=int, required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)

    cfg = load_config()
    state_path = args.state_file or get_state_path("qa-loop", cfg)
    try:
        if args.cmd == "prs":
            me = _me()
            if args.pr:
                numbers = [args.pr]
            else:
                listed = json.loads(gh("pr", "list", "--state", "open", "--json", "number,isDraft"))
                numbers = [x["number"] for x in listed if not x["isDraft"]]
            rows = [qa.classify_pr(_view(n), me) for n in numbers]
            order = {"review": 0, "wait-worker": 1, "needs-human": 2, "accepted": 3}
            rows.sort(key=lambda r: (order.get(r["status"], 9), r.get("createdAt") or ""))
            _out({"prs": rows})
        elif args.cmd == "check-head":
            pr = _view(args.number)
            if pr.get("state") == "OPEN" and pr.get("headRefOid") == args.head:
                print("VALID"); sys.exit(0)
            print(f"CHANGED: head {pr.get('headRefOid')}, state {pr.get('state')}"); sys.exit(1)
        elif args.cmd == "signoff":
            from devloops_lib.gatekeeper_readiness import assess_readiness
            repo_has_ci = bool((assess_readiness(config=cfg).get("ci") or {}).get("present"))
            result = qa.signoff(_view(args.number), _me(), config=cfg, repo_has_ci=repo_has_ci)
            _out(result)
            sys.exit(0 if result["mergeReady"] else 1)
        elif args.cmd == "merged":
            base = cfg.get("project", {}).get("base_branch", "main")
            state = qa.load_state(state_path)
            if args.since:
                state["mergedCursor"] = args.since
            merged = qa.fetch_merged(base, state.get("mergedCursor"))
            state = qa.advance_merged(state, merged)
            qa.save_state(state_path, state)
            pending = list(state.get("pendingTests", {}).values())
            _out({"base": base, "mergedCursor": state["mergedCursor"], "pendingTests": pending})
            if not pending:
                print("NOOP", file=sys.stderr)
        elif args.cmd == "record":
            state = qa.record_test(qa.load_state(state_path), args.number, args.result, args.build, args.issue)
            qa.save_state(state_path, state)
            _out({"pendingTests": list(state.get("pendingTests", {}).values())})
        elif args.cmd == "link-blocked-by":
            verified = qa.link_blocked_by(get_forge(cfg), args.issue, args.blocked_by)
            _out({"issue": args.issue, "blockedBy": verified})
            sys.exit(0 if args.blocked_by in verified else 1)
        elif args.cmd == "status":
            _out(qa.load_state(state_path))
    except GhError as e:
        sys.stderr.write(f"gh error: {e}\n")
        sys.exit(3)


if __name__ == "__main__":
    run_main(main)
