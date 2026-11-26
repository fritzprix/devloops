"""Pipeline counters for one pm-loop tick: who has work right now.

The PM computes these itself on every tick (issue #71) so a cascade decision
never depends on counters a caller forgot to pass:

  approvedIssues   - issues the worker can start (devloops_lib.issue_candidates
                     candidates that are not waiting on dependencies)
  prsNeedingWorker - my open PRs whose nextAction is in WORKER_PR_ACTIONS
  prsNeedingReview - open PRs the gatekeeper queue classifies as "review"
  prsNeedingQa     - open PRs qa classifies as "review"
  boardFull        - my unapproved open PRs have reached the configured limit

Each counter reuses the classifier the owning loop itself uses, so loops and
PM never disagree about who is next. Parked PRs (``gatekeeper:escalated``,
``needs-owner``, an open blocker, or gatekeeper/qa statuses escalate /
needs-human) are counted nowhere — except a PR whose nextAction is ``rebase``
(#68). Classifier statuses alone are not enough: an escalated PR can still
look like ``fix-ci`` or qa ``review``.
"""
from __future__ import annotations

import json
from typing import Any

from devloops_lib import contract
from devloops_lib import qa as qa_lib
from devloops_lib.config import load_config
from devloops_lib.gatekeeper_queue import get_default_state_file, load_state
from devloops_lib.gatekeeper_queue import summarize as gatekeeper_summarize
from devloops_lib.gatekeeper_readiness import assess_readiness
from devloops_lib.gh import gh
from devloops_lib.issue_candidates import fetch_candidates
from devloops_lib.pr_status import WORKER_PR_ACTIONS, summarize_prs

# Same fields qa-loop's own CLI reads (skills/qa-loop/scripts/qa_queue.py).
QA_PR_FIELDS = "number,title,url,headRefOid,baseRefName,isDraft,mergeable,createdAt,comments,labels,state"

# Issue #71 parked states. open_blockers (#65 follow-up) is consulted when present
# on the entry; label and status checks cover the rest without that helper.
PARKED_LABELS = frozenset({contract.GATEKEEPER_ESCALATED, contract.NEEDS_OWNER})
PARKED_STATUSES = frozenset({"escalate", "needs-human"})
REBASE_ACTION = "rebase"


def _label_names(entry: dict) -> set[str]:
    raw = entry.get("labels") or []
    names: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))
    return names


def pr_is_parked(entry: dict) -> bool:
    """True when a PR must not claim a cascade slot (issue #71 deliverable 2).

    The ``rebase`` nextAction is the #68 exception: a parked-on-blocker PR that
    is ready to rebase still needs a worker slot.
    """
    if entry.get("nextAction") == REBASE_ACTION:
        return False
    if _label_names(entry) & PARKED_LABELS:
        return True
    if entry.get("status") in PARKED_STATUSES:
        return True
    if entry.get("openBlocker") or entry.get("open_blocker"):
        return True
    blockers = entry.get("openBlockers") or entry.get("open_blockers")
    if isinstance(blockers, (list, tuple)) and blockers:
        return True
    return False


def compute_counters(
    *,
    worker_prs: list[dict] | None = None,
    gatekeeper_entries: list[dict] | None = None,
    qa_entries: list[dict] | None = None,
    candidates: dict | None = None,
) -> dict[str, Any]:
    """Pure counter core: feed each loop's own classifier output, get the counters."""
    candidates = candidates or {}
    selectable = [
        c for c in candidates.get("candidates", [])
        if (c.get("dependency") or {}).get("status") not in ("wait", "blocked-on-closure")
    ]
    limit = candidates.get("openPRLimit") or {}
    worker = [pr for pr in (worker_prs or []) if not pr_is_parked(pr)]
    gatekeeper = [e for e in (gatekeeper_entries or []) if not pr_is_parked(e)]
    qa = [e for e in (qa_entries or []) if not pr_is_parked(e)]
    return {
        "approvedIssues": len(selectable),
        "prsNeedingWorker": sum(1 for pr in worker if pr.get("nextAction") in WORKER_PR_ACTIONS),
        "prsNeedingReview": sum(1 for entry in gatekeeper if entry.get("status") == "review"),
        "prsNeedingQa": sum(1 for entry in qa if entry.get("status") == "review"),
        "boardFull": bool(limit.get("reached")),
    }


def _open_pr_numbers(*, author_me: bool) -> list[int]:
    args = ["pr", "list", "--state", "open", "--json", "number,isDraft"]
    if author_me:
        args += ["--author", "@me"]
    listed = json.loads(gh(*args))
    return [pr["number"] for pr in listed if not pr["isDraft"]]


def fetch_pipeline(config: dict | None = None) -> dict[str, Any]:
    """Fetch every counter from GitHub using each loop's own classifier."""
    cfg = config or load_config()
    me = gh("api", "user", "--jq", ".login").strip()

    worker_prs = summarize_prs()  # my open non-draft PRs with nextAction

    gk_numbers = _open_pr_numbers(author_me=True)
    gk_state = load_state(get_default_state_file())
    readiness = assess_readiness(config=cfg)
    gatekeeper_entries = [
        gatekeeper_summarize(number, me, gk_state, cfg, readiness) for number in gk_numbers
    ]

    qa_numbers = _open_pr_numbers(author_me=False)  # qa reviews every open PR, not just mine
    qa_entries = [
        qa_lib.classify_pr(json.loads(gh("pr", "view", str(number), "--json", QA_PR_FIELDS)), me)
        for number in qa_numbers
    ]

    return compute_counters(
        worker_prs=worker_prs,
        gatekeeper_entries=gatekeeper_entries,
        qa_entries=qa_entries,
        candidates=fetch_candidates(),
    )


def main() -> None:
    import sys

    try:
        print(json.dumps(fetch_pipeline(), ensure_ascii=False, indent=2))
    except Exception as e:  # GhError and friends: PM must see the failure, not a silent zero
        sys.exit(f"pm_pipeline error: {e}")


if __name__ == "__main__":
    from devloops_lib.config import run_main

    run_main(main)
