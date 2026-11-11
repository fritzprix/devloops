#!/usr/bin/env python3
"""List open PRs by @me with their gatekeeper review status.

Verdicts and worker responses are PR comments authored by @me that carry a
hidden marker (see references/comment-format.md). Markers from any other
author are ignored. Unmarked comments from the owner (or @me) posted after the
last marker are surfaced as `ownerComments` and force a review.

status values:
  review     - needs review: new head/rebase/base change since last verdict, a
               worker response, an owner comment, never reviewed, CI failed
               (also after approve), or CI passed for a clean-marked PR.
  await-ci   - current head has a `verdict=clean` marker (or state entry) and
               only pending CI remains. Not reviewed again.
  wait-worker- changes requested at the current head without response, or merge
               conflicts. Worker's turn to fix.
  approved   - approve verdict exists for the current head, base unchanged, CI
               not failing, no newer owner comment.
  escalate   - round limit reached or unresolvable dispute; hand to the user.
  needs-human - blocked on a person, not on code: reviewed clean at the current
               head but no CI runs exist when CI is expected. No re-review until
               a new head or genuinely new input arrives.
  human-only - touches the gatekeeper's own skill; never reviewed or approved by
               the loop, but surfaced to the human via `gatekeeper:needs-human`.

The list is sorted by priority (worker response / new head / owner comment
first), then oldest PR first. Every entry carries a one-line `reason`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# The skill ships its own copy of devloops_lib next to this script.
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.config import GATEKEEPER_APPROVED_LABEL, add_agent_id_argument, get_state_path, load_config
from devloops_lib.gh import GhError, gh
from devloops_lib.config import run_main

MAX_ROUNDS = 5
# Loop markers must appear at the very start of the comment (ignoring optional UTF-8 BOM and leading whitespace).
# Quoted markers or mentions later in the comment are ignored.
START_MARKER = re.compile(r"^(?:\ufeff)?\s*<!--\s*(gatekeeper-loop|worker-loop):\s*(.*?)\s*-->")
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", "REQUESTED"}
PROTECTED_SKILL_PREFIXES = (
    "skills/gatekeeper-loop/",
    "skills/worker-loop/",
    ".agents/skills/gatekeeper-loop/",
    ".agents/skills/worker-loop/",
)
OWNER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
SENSITIVE_PATH = re.compile(
    r"(shell[_-]?policy|tool[_-]?approval|permission|secret|credential|keychain|sandbox)", re.I
)


def get_default_state_file() -> Path:
    return get_state_path("gatekeeper-loop")


# gh helper is imported from devloops_lib.gh


def check_state(check: dict) -> str:
    return (check.get("conclusion") or check.get("state") or check.get("status") or "").upper()


def is_dev_base(base: str, config: dict | None = None) -> bool:
    """True if CI runs on PRs targeting this base branch."""
    if config:
        proj = config.get("project", {})
        base_branch = proj.get("base_branch")
        if base_branch and base == base_branch:
            return True
        default_branch = proj.get("default_branch")
        if default_branch and base == default_branch:
            return True
    return base in ("main", "master") or base.startswith("dev/") or base.startswith("release/")


def parse_markers(comments: list[dict], me: str) -> tuple[list[dict], list[dict]]:
    """Return (gatekeeper markers incl. `clean`, worker responses) authored by me."""
    verdicts, responses = [], []
    for c in comments:
        if (c.get("author") or {}).get("login") != me:
            continue
        match = START_MARKER.match(c.get("body") or "")
        if not match:
            continue
        fields = dict(kv.split("=", 1) for kv in match.group(2).split() if "=" in kv)
        entry = {**fields, "createdAt": c["createdAt"], "url": c.get("url")}
        (verdicts if match.group(1) == "gatekeeper-loop" else responses).append(entry)
    return verdicts, responses


def split_markers(markers: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split gatekeeper markers into (verdicts, clean, needs-human).

    Only markers with an explicit ``verdict`` key count as verdicts, so
    ``kind=needs-human`` markers never inflate review rounds.
    """
    verdicts = [m for m in markers if m.get("verdict") and m["verdict"] != "clean"]
    cleans = [m for m in markers if m.get("verdict") == "clean"]
    needs = [m for m in markers if m.get("kind") == "needs-human"]
    return verdicts, cleans, needs


def owner_comments_after(comments: list[dict], me: str, since: str | None) -> list[dict]:
    """Unmarked comments from the owner/@me newer than the last marker comment."""
    found = []
    for c in comments:
        login = (c.get("author") or {}).get("login") or ""
        if login.endswith("[bot]") or START_MARKER.match(c.get("body") or ""):
            continue
        if login != me and c.get("authorAssociation") not in OWNER_ASSOCIATIONS:
            continue
        if since is None or c["createdAt"] > since:
            found.append({"author": login, "createdAt": c["createdAt"], "url": c.get("url"),
                          "body": (c.get("body") or "")[:500]})
    return found


def is_rebased(reviewed_head: str | None, head: str, is_ancestor) -> bool:
    """True if the reviewed head is not an ancestor of head (rebase/force-push or unknown object)."""
    if not reviewed_head or reviewed_head == head:
        return False
    return not is_ancestor(reviewed_head, head)


def git_is_ancestor(old: str, new: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", old, new],
                          capture_output=True).returncode == 0


def load_state(path: Path | None = None) -> dict:
    state_path = path or get_default_state_file()
    if state_path.is_file():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def classify(
    *,
    mergeable: str | None,
    head: str,
    base: str,
    last_verdict: dict | None,
    worker_response: dict | None,
    total_verdicts: int,
    state_reviewed_head: str | None,
    state_pending_ci: bool,
    has_open_findings: bool,
    pending_checks: list[str],
    failing_checks: list[str],
    clean_head: str | None = None,
    reviewed_base: str | None = None,
    rebased: bool = False,
    owner_comment: bool = False,
    total_checks: int | None = None,
    config: dict | None = None,
) -> tuple[str, str, int]:
    """Return (status, one-line reason, priority). Lower priority number is handled first."""
    ci_applies = is_dev_base(base, config=config)
    ci_failed = ci_applies and bool(failing_checks)

    if mergeable == "CONFLICTING":
        return "wait-worker", "merge conflict; worker must rebase", 9

    if owner_comment:
        return "review", "owner comment after last verdict; treat as required items", 0

    if total_verdicts >= MAX_ROUNDS:
        return "escalate", f"round limit ({MAX_ROUNDS}) reached", 9

    at_head = bool(last_verdict) and last_verdict.get("head") == head
    base_changed = bool(reviewed_base) and reviewed_base != base

    if at_head and last_verdict.get("verdict") == "approve":
        if base_changed:
            return "review", f"base changed after approve ({reviewed_base} -> {base})", 0
        if ci_failed:
            return "review", "CI failing after approve", 0
        return "approved", "approved at current head", 9

    if at_head and last_verdict.get("verdict") == "changes-requested":
        if worker_response is None:
            return "wait-worker", "changes requested; waiting for worker response", 9
        return "review", "worker responded to the latest verdict", 0

    if rebased:
        return "review", "head was rebased/force-pushed since last verdict; full review or range-diff", 0

    # A clean marker on GitHub (or, as a fallback, state.json) means the code
    # review for *this head* is already done. It outranks older verdicts: a
    # current-head clean must not lose to a verdict issued at an earlier head.
    reviewed_clean = clean_head == head or (
        state_reviewed_head == head and state_pending_ci and not has_open_findings
    )
    if last_verdict and not reviewed_clean:
        return "review", "new head since last verdict", 0

    if reviewed_clean:
        if failing_checks and ci_applies:
            return "review", "clean at head but CI failing; diagnose and request changes", 1
        if ci_applies and pending_checks:
            return "await-ci", "reviewed clean; waiting for pending CI", 9
        if ci_applies and total_checks == 0:
            return "needs-human", "reviewed clean but no CI runs on the head (no CI on head)", 9
        return "review", "reviewed clean; CI done or not applicable, issue the verdict", 1

    if failing_checks and ci_applies:
        return "review", "not reviewed yet; CI failing", 1
    return "review", "not reviewed yet", 1


def determine_status(**kwargs) -> str:
    """Status only; see classify() for the arguments."""
    return classify(**kwargs)[0]


def needs_human_guidance(
    *,
    status: str,
    own_skill: bool,
    sensitive: list[str],
    labels: list[str],
    needs_comment_present: bool,
) -> dict | None:
    """What the gatekeeper should post/keep for human attention, or None.

    Idempotency lives here: `commentPresent`/`labelPresent` tell the tick to skip
    work it already did. `staleLabel` marks a `gatekeeper:needs-human` label whose
    reason no longer applies; labels this loop did not add are never removed.
    """
    label_present = "gatekeeper:needs-human" in labels
    if status == "human-only":
        reason = "changes loop skill files"
    elif status == "needs-human":
        reason = "no CI on head"
    elif sensitive:
        reason = "security-sensitive paths"
    else:
        reason = None
    if reason is None:
        if not label_present:
            return None
        return {"reason": None, "labelPresent": True,
                "commentPresent": needs_comment_present, "staleLabel": True}
    return {"reason": reason, "labelPresent": label_present,
            "commentPresent": needs_comment_present, "staleLabel": False}


def summarize(number: int, me: str, state: dict | None = None, config: dict | None = None) -> dict:
    pr = json.loads(
        gh("pr", "view", str(number), "--json",
           "number,title,url,headRefName,headRefOid,baseRefName,isDraft,mergeable,createdAt,"
           "statusCheckRollup,comments,labels,files")
    )
    cfg = config or load_config()
    base = pr["baseRefName"]
    checks = pr.get("statusCheckRollup") or []
    failing = [c.get("name") or c.get("context") for c in checks if check_state(c) in FAILED]
    pending = [c.get("name") or c.get("context") for c in checks if check_state(c) in PENDING]
    comments = pr.get("comments") or []
    markers, responses = parse_markers(comments, me)
    verdicts, cleans, needs = split_markers(markers)

    head = pr["headRefOid"]
    last = verdicts[-1] if verdicts else None
    response = None
    if last:
        response = next(
            (r for r in reversed(responses)
             if r.get("round") == last.get("round") and r["createdAt"] > last["createdAt"]),
            None,
        )
    clean = next((c for c in reversed(cleans)
                  if last is None or c["createdAt"] > last["createdAt"]), None)

    pr_state = (state or {}).get("prs", {}).get(str(number), {})
    state_reviewed_head = pr_state.get("reviewedHead")
    state_pending_ci = pr_state.get("pendingApproveWaitCi", False)
    findings = pr_state.get("findings", {})
    has_open_findings = any(v == "open" for v in findings.values()) if isinstance(findings, dict) else bool(findings)

    reviewed_head = (clean or last or {}).get("head") or state_reviewed_head
    if reviewed_head and reviewed_head != head:
        subprocess.run(["git", "fetch", "origin", f"refs/pull/{number}/head", "--quiet"],
                       capture_output=True)
    rebased = is_rebased(reviewed_head, head, git_is_ancestor)

    last_marker_at = max((m["createdAt"] for m in [*markers, *responses]), default=None)
    owner = owner_comments_after(comments, me, last_marker_at)

    labels = sorted(label["name"] for label in pr.get("labels") or [])
    paths = [f["path"] for f in pr.get("files") or []]
    own_skill = any(p.startswith(PROTECTED_SKILL_PREFIXES) for p in paths)
    protected_config_paths = tuple(cfg.get("protected_paths", []))
    sensitive = sorted(
        p for p in paths
        if SENSITIVE_PATH.search(p) or (protected_config_paths and any(p.startswith(pref) for pref in protected_config_paths))
    )

    status, reason, priority = classify(
        mergeable=pr.get("mergeable"),
        head=head,
        base=base,
        last_verdict=last,
        worker_response=response,
        total_verdicts=len(verdicts),
        state_reviewed_head=state_reviewed_head,
        state_pending_ci=state_pending_ci,
        has_open_findings=has_open_findings,
        pending_checks=pending,
        failing_checks=failing,
        clean_head=clean.get("head") if clean else None,
        reviewed_base=(last or {}).get("base"),
        rebased=rebased,
        owner_comment=bool(owner),
        total_checks=len(checks),
        config=cfg,
    )
    if own_skill:
        status, reason, priority = "human-only", "changes loop skill files; left to the human", 9

    needs_at_head = next((m for m in reversed(needs) if m.get("head") == head), None)
    needs_human = needs_human_guidance(
        status=status, own_skill=own_skill, sensitive=sensitive,
        labels=labels, needs_comment_present=bool(needs_at_head),
    )

    if status != "review" or not reviewed_head or reviewed_head == head:
        review_mode = "full" if status == "review" and not reviewed_head else "incremental"
    else:
        review_mode = "range-diff" if rebased else "incremental"

    # Place review state first so the model and operator see review status before CI details.
    return {
        "number": pr["number"],
        "status": status,
        "reason": reason,
        "priority": priority,
        "reviewMode": review_mode,
        "round": len(verdicts) + (1 if status in ("review", "await-ci") else 0),
        "title": pr["title"],
        "head": pr["headRefName"],
        "headSha": head,
        "reviewedHead": reviewed_head,
        "rebased": rebased,
        "lastVerdict": last,
        "cleanMarker": clean,
        "workerResponse": response,
        "ownerComments": owner,
        "securitySensitive": sensitive,
        "labels": labels,
        "staleApprovedLabel": GATEKEEPER_APPROVED_LABEL in labels and status != "approved",
        "needsHuman": needs_human,
        "url": pr["url"],
        "base": base,
        "draft": pr["isDraft"],
        "createdAt": pr["createdAt"],
        "ciPending": pending,
        "ciFailing": failing,
    }


def head_is_current(pr: dict, head: str, base: str | None) -> bool:
    """Return True if a verdict for this head and base still applies to the open PR."""
    if pr.get("state") != "OPEN" or pr.get("headRefOid") != head:
        return False
    return base is None or pr.get("baseRefName") == base


def check_head(number: int, head: str, base: str | None) -> int:
    """Exit 0 if the reviewed head is still current, 1 if it changed, 3 on error."""
    try:
        pr = json.loads(gh("pr", "view", str(number), "--json", "state,headRefOid,baseRefName"))
    except GhError as e:
        sys.stderr.write(f"gh error: {e}\n")
        return 3
    if head_is_current(pr, head, base):
        print("VALID")
        return 0
    print(f"CHANGED: head {pr.get('headRefOid')}, base {pr.get('baseRefName')}, state {pr.get('state')}")
    return 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, help="Only this PR number")
    parser.add_argument("--state-file", type=Path, default=None, help="Path to state.json")
    add_agent_id_argument(parser)
    parser.add_argument("--check-head", type=int, metavar="N", help="Re-check PR N before posting a verdict; needs --head")
    parser.add_argument("--head", help="The full head SHA that was reviewed")
    parser.add_argument("--base", help="The base branch that was reviewed")
    args = parser.parse_args(argv)

    if args.check_head is not None:
        if not args.head:
            parser.error("--check-head needs --head")
        sys.exit(check_head(args.check_head, args.head, args.base))

    try:
        state = load_state(args.state_file)
        cfg = load_config()
        me = gh("api", "user", "--jq", ".login").strip()
        if args.pr:
            numbers = [args.pr]
        else:
            listed = json.loads(gh("pr", "list", "--author", "@me", "--state", "open",
                                    "--json", "number,isDraft"))
            numbers = [p["number"] for p in listed if not p["isDraft"]]

        summaries = [summarize(n, me, state, cfg) for n in numbers]
        summaries.sort(key=lambda x: (x["priority"], x["createdAt"]))
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
    except GhError as e:
        sys.exit(f"gh error: {e}")


if __name__ == "__main__":
    run_main(main)
