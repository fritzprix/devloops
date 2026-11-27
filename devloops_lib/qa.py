"""qa-loop helpers: testability verdicts, two-party sign-off, and merged-PR tracking.

qa-loop is the second reviewer next to gatekeeper-loop. The gatekeeper asks
"is this change correct?"; qa asks "can this change be tested with the tools
that qa-loop has?". A PR is ready for a human merge only when both agree at the
same head. After a merge, qa-loop builds and tests the integrated base branch
and files issues for failures.

Python stdlib only. All GitHub access goes through devloops_lib.gh.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from devloops_lib import contract
from devloops_lib.config import GATEKEEPER_APPROVED_LABEL
from devloops_lib.gh import gh

# Label names and the marker vocabulary live in devloops_lib.contract (issue #65).
QA_ACCEPTED_LABEL = contract.QA_ACCEPTED
QA_CHANGES_LABEL = contract.QA_CHANGES_REQUESTED
QA_NEEDS_HUMAN_LABEL = contract.QA_NEEDS_HUMAN
QA_LABELS = contract.QA_VERDICT_LABELS
QA_VERDICTS = contract.QA_VERDICTS
VERDICT_LABEL = {
    "accepted": QA_ACCEPTED_LABEL,
    "changes-requested": QA_CHANGES_LABEL,
    "needs-human": QA_NEEDS_HUMAN_LABEL,
}

OWNER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def _author(comment: dict) -> str:
    author = comment.get("author") or {}
    return author.get("login", "") if isinstance(author, dict) else str(author)


def parse_markers(comments: list[dict], me: str, loop: str = "qa-loop") -> list[dict]:
    """Valid markers by `me` for one loop, oldest first, with `createdAt`.

    Quoted markers and markers outside the shared vocabulary (issue #65) never
    appear.
    """
    return contract.parse_markers(comments, me, loop=loop)


def last_qa_verdict(comments: list[dict], me: str) -> Optional[dict]:
    """Latest qa verdict marker (kind=verdict) by `me`, or None."""
    verdicts = [m for m in parse_markers(comments, me) if m.get("kind") == "verdict"]
    return verdicts[-1] if verdicts else None


def last_gatekeeper_verdict(comments: list[dict], me: str) -> Optional[dict]:
    """Latest gatekeeper round verdict marker by `me` (clean and needs-human markers excluded)."""
    verdicts = [m for m in parse_markers(comments, me, loop="gatekeeper-loop")
                if "round" in m and m.get("verdict")]
    return verdicts[-1] if verdicts else None


def owner_comments_after(comments: list[dict], me: str, since: Optional[str]) -> list[dict]:
    """Unmarked owner (or `me`) comments posted after `since`."""
    out = []
    for c in comments:
        if contract.is_marker(c.get("body") or ""):
            continue
        if since and c.get("createdAt", "") <= since:
            continue
        if _author(c) == me or c.get("authorAssociation") in OWNER_ASSOCIATIONS:
            out.append({"author": _author(c), "createdAt": c.get("createdAt"), "url": c.get("url")})
    return out


def classify_pr(pr: dict, me: str) -> dict:
    """Return qa status for one open PR.

    status:
      review       - no qa verdict at the current head, or an owner comment after it.
      wait-worker  - qa requested changes at the current head; the worker must answer.
      needs-human  - qa cannot decide at the current head; a human must answer.
      accepted     - qa accepted the current head.
    """
    head = pr["headRefOid"]
    comments = pr.get("comments") or []
    labels = sorted(label["name"] for label in pr.get("labels") or [])
    last = last_qa_verdict(comments, me)
    owner = owner_comments_after(comments, me, (last or {}).get("createdAt"))
    worker_reply = bool(last) and any(
        m["createdAt"] > last["createdAt"]
        for m in parse_markers(comments, me, loop="worker-loop")
        if m.get("kind") == "qa-response"
    )

    if last is None:
        status, reason = "review", "never reviewed by qa"
    elif last.get("head") != head:
        status, reason = "review", "new head since the last qa verdict"
    elif owner:
        status, reason = "review", "owner comment after the last qa verdict"
    elif last["verdict"] == "changes-requested" and worker_reply:
        status, reason = "review", "worker response after qa changes-requested"
    elif last["verdict"] == "changes-requested":
        status, reason = "wait-worker", "qa requested changes at this head"
    elif last["verdict"] == "needs-human":
        status, reason = "needs-human", "qa asked a human to decide at this head"
    else:
        status, reason = "accepted", "qa accepted this head"

    want = VERDICT_LABEL.get(last["verdict"]) if last and last.get("head") == head else None
    stale = [lbl for lbl in labels if lbl in QA_LABELS and lbl != want]
    return {
        "number": pr["number"],
        "status": status,
        "reason": reason,
        "headSha": head,
        "lastVerdict": last,
        "ownerComments": owner,
        "labels": labels,
        "staleLabels": stale,
        "missingLabel": want if want and want not in labels else None,
        "url": pr.get("url"),
        "title": pr.get("title"),
        "createdAt": pr.get("createdAt"),
    }


def signoff(pr: dict, me: str, config: dict | None = None) -> dict:
    """Two-party sign-off: gatekeeper approve AND qa accepted, both at the current head.

    Labels alone are not enough: a label can survive a push. Each party must
    also have a marker comment for the current head. When `config` lists
    `gatekeeper.required_checks`, every required check must also be green at
    the current head (issue #111).
    """
    head = pr["headRefOid"]
    comments = pr.get("comments") or []
    labels = {label["name"] for label in pr.get("labels") or []}
    blockers = []

    gk = last_gatekeeper_verdict(comments, me)
    if GATEKEEPER_APPROVED_LABEL not in labels:
        blockers.append(f"gatekeeper: label {GATEKEEPER_APPROVED_LABEL} missing")
    if not gk or gk.get("verdict") != "approve":
        blockers.append("gatekeeper: latest verdict is not approve")
    elif gk.get("head") != head:
        blockers.append("gatekeeper: approve is for an older head")

    qa = last_qa_verdict(comments, me)
    if QA_ACCEPTED_LABEL not in labels:
        blockers.append(f"qa: label {QA_ACCEPTED_LABEL} missing")
    if not qa or qa.get("verdict") != "accepted":
        blockers.append("qa: latest verdict is not accepted")
    elif qa.get("head") != head:
        blockers.append("qa: accepted is for an older head")

    if config:
        from devloops_lib import gatekeeper_queue

        checks = pr.get("statusCheckRollup") or []
        missing_req = gatekeeper_queue.missing_required_checks(checks, config, head=head)
        if missing_req:
            blockers.append(f"ci: required checks not green at current head ({', '.join(missing_req)})")

    if pr.get("isDraft"):
        blockers.append("PR is a draft")
    if pr.get("mergeable") == "CONFLICTING":
        blockers.append("PR has merge conflicts")
    return {"number": pr["number"], "headSha": head, "mergeReady": not blockers, "blockers": blockers}


# --- merged-PR tracking -----------------------------------------------------

def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def advance_merged(state: dict, merged: list[dict], now: Callable[[], str] = _now) -> dict:
    """Add PRs merged after the cursor to `pendingTests`; move the cursor.

    First run (no cursor): set the cursor to the newest merge (or now) and queue
    nothing, so a new install does not test the whole history. Use `--since` to
    backfill on purpose.
    """
    cursor = state.get("mergedCursor")
    pending = state.setdefault("pendingTests", {})
    tested = state.setdefault("tested", {})
    newest = max((p.get("mergedAt") or "" for p in merged), default="")
    if cursor is None:
        state["mergedCursor"] = newest or now()
        return state
    for p in sorted(merged, key=lambda x: x.get("mergedAt") or ""):
        key = str(p["number"])
        if (p.get("mergedAt") or "") <= cursor or key in tested or key in pending:
            continue
        pending[key] = {
            "number": p["number"],
            "title": p.get("title"),
            "url": p.get("url"),
            "mergedAt": p.get("mergedAt"),
            "mergeCommit": (p.get("mergeCommit") or {}).get("oid"),
        }
    if newest > cursor:
        state["mergedCursor"] = newest
    return state


def record_test(state: dict, number: int, result: str, build: str, issues: list[int], now: Callable[[], str] = _now) -> dict:
    """Move one PR from pendingTests to tested. `blocked` keeps it pending for a later tick."""
    if result not in ("pass", "fail", "blocked"):
        raise ValueError(f"unknown result: {result}")
    key = str(number)
    pending = state.setdefault("pendingTests", {})
    entry = dict(pending.get(key) or {"number": number})
    entry.update({"result": result, "build": build, "issues": issues, "at": now()})
    if result == "blocked":
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        pending[key] = entry
    else:
        pending.pop(key, None)
        state.setdefault("tested", {})[key] = entry
    return state


def fetch_merged(base: str, since: Optional[str], limit: int = 50) -> list[dict]:
    args = ["pr", "list", "--state", "merged", "--base", base, "--limit", str(limit),
            "--json", "number,title,url,mergedAt,mergeCommit"]
    if since:
        args += ["--search", f"merged:>={since[:10]}"]
    return json.loads(gh(*args))


# --- issue dependencies (blocked-by) ---------------------------------------
# Routed through the Forge protocol (issue #65): no loop script talks to a
# forge dependency API directly.

def link_blocked_by(forge: Any, issue: int, blocker: int) -> list[int]:
    """Link `issue` as blocked by `blocker` via the forge, then re-query the
    forge and return the verified sorted list of open blocker numbers."""
    forge.add_blocker(issue, blocker)
    return contract.open_blockers(forge, issue)


def qa_config(cfg: dict) -> dict[str, Any]:
    """Return the qa section with safe types (a YAML `{}` may parse as a string)."""
    qa = dict(cfg.get("qa") or {})
    for key in ("tools", "blocked_tools", "board_fields", "post_merge"):
        if not isinstance(qa.get(key), dict):
            qa[key] = {}
    return qa
