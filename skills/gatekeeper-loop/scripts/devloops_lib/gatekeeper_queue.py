#!/usr/bin/env python3
"""List open PRs by @me with their gatekeeper review status.

This is the canonical implementation (promoted from
``skills/gatekeeper-loop/scripts/review_queue.py`` for issue #71). The gatekeeper
CLI is a thin shim over this module and the PM pipeline counters
(``devloops_lib.pm_pipeline``) reuse its statuses, so "needs review" is decided
the same way everywhere. PRs parked as ``escalate`` or ``needs-human`` are a
human's turn, not another loop's.

Verdicts and worker responses are PR comments authored by @me that carry a
hidden marker (see references/comment-format.md). Markers from any other
author are ignored. Unmarked comments from the owner (or @me) posted after the
last marker are surfaced as `ownerComments` and force a review.

status values:
  review     - needs review: new head/rebase/base change since last verdict, a
               worker response, an owner comment, never reviewed, CI failed
               (also after approve), or CI passed for a clean-marked PR.
  await-ci   - current head has a `verdict=clean` marker (or state entry) and
               only pending CI remains. Not reviewed again. When `ciStalled`
               lists checks queued and not started past the limit, the reason
               says so: report the stall on the PR with the cause; never poll.
  wait-worker- changes requested at the current head without response, or merge
               conflicts. Worker's turn to fix.
  approved   - approve verdict exists for the current head, base unchanged, CI
               not failing, no newer owner comment.
  escalate   - round limit reached or second dispute on the same finding; maintainer direction.

No status skips review (issue #81): every reviewable PR gets a full review at
the first tick that sees it, whatever CI or repo-readiness looks like. Gaps
outside the PR (missing repo CI, missing or ambiguous rubric) are tracked by
issues the gatekeeper files; they never hold back a review verdict.

PRs that change loop skill files get the same statuses as any other PR; they are
reviewed and can be approved. They additionally carry `gatekeeper:needs-human`
(reason `changes loop skill files`) to signal that the merge itself stays with a
human; the label never blocks review or approval.

The list is sorted by priority (worker response / new head / owner comment
first), then oldest PR first. Every entry carries a one-line `reason`.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from devloops_lib.config import GATEKEEPER_APPROVED_LABEL, add_agent_id_argument, get_state_path, load_config
from devloops_lib.gatekeeper_readiness import assess_readiness
from devloops_lib.gh import GhError, gh
from devloops_lib.config import run_main
from devloops_lib import contract

MAX_ROUNDS = 5
# Loop markers are defined once in devloops_lib.contract (issue #65): they must
# open the comment (optional BOM/whitespace aside), cover all three loops, and
# only accepted vocabulary counts.
START_MARKER = contract.MARKER
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", "REQUESTED"}
# Pending states that mean the check has not started yet (issue #81: a check
# that stays queued and never starts is a reportable stall, not silent waiting).
NOT_STARTED = {"QUEUED", "EXPECTED", "REQUESTED", "WAITING"}
DEFAULT_CI_STALL_MINUTES = 45
PROTECTED_SKILL_PREFIXES = (
    "skills/gatekeeper-loop/",
    "skills/worker-loop/",
    ".agents/skills/gatekeeper-loop/",
    ".agents/skills/worker-loop/",
    "skills/qa-loop/",
    ".agents/skills/qa-loop/",
)
OWNER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
# First-pass hint only: a name match lists the path for the gatekeeper to open and
# read. It is never a verdict and never by itself a reason for needs-human.
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
    """Return (gatekeeper markers incl. `clean` and kind=needs-owner, worker
    responses) authored by me, using the shared vocabulary (issue #65).

    Only kind=response worker markers count as responses; `note` and
    `needs-owner` markers never settle a round.
    """
    verdicts = contract.parse_markers(comments, me, loop="gatekeeper-loop")
    responses = [m for m in contract.parse_markers(comments, me, loop="worker-loop")
                 if m.get("kind") == "response"]
    return verdicts, responses


def split_markers(markers: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split gatekeeper markers into (verdicts, clean, needs-owner).

    Only markers with an explicit ``verdict`` key count as verdicts, so
    ``kind=needs-owner`` markers never inflate review rounds.
    """
    verdicts = [m for m in markers if m.get("verdict") and m["verdict"] != "clean"]
    cleans = [m for m in markers if m.get("verdict") == "clean"]
    needs = [m for m in markers if m.get("kind") == contract.NEEDS_OWNER]
    return verdicts, cleans, needs


def owner_comments_after(comments: list[dict], me: str, since: str | None) -> list[dict]:
    """Unmarked comments from the owner/@me newer than the last marker comment."""
    found = []
    for c in comments:
        login = (c.get("author") or {}).get("login") or ""
        if login.endswith("[bot]") or contract.is_marker(c.get("body") or ""):
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


def queued_not_started(checks: list[dict]) -> list[str]:
    """Names of checks still waiting to start (queued/expected/requested/waiting)."""
    return [str(c.get("name") or c.get("context") or "")
            for c in checks if check_state(c) in NOT_STARTED]


def _queued_since_checks(queued_since: dict | None, head: str | None = None) -> dict:
    """Check-name -> first-seen ISO map for `head`, or {} when the head changed.

    Accepts the nested shape ``{"head": sha, "checks": {...}}`` and the legacy
    flat ``{name: iso}`` shape (treated as matching any head).
    """
    raw = queued_since or {}
    if "checks" in raw or "head" in raw:
        if head is not None and str(raw.get("head") or "") != str(head):
            return {}
        checks = raw.get("checks") or {}
        return checks if isinstance(checks, dict) else {}
    return dict(raw)


def merge_queued_since(
    current_queued: list[str],
    queued_since: dict,
    now: str,
    *,
    head: str = "",
) -> dict:
    """First-seen-queued timestamps for the checks queued now on `head`.

    Persisted in state.json per PR as ``{"head": <sha>, "checks": {name: iso}}``
    so a new head cannot inherit stall clocks from an earlier head (issue #81 G1.1).
    """
    prev = _queued_since_checks(queued_since, head=head or None)
    checks = {name: str(prev.get(name) or now) for name in current_queued}
    return {"head": head, "checks": checks}


def stalled_checks(
    current_queued: list[str],
    queued_since: dict,
    now: str,
    limit_minutes: int = DEFAULT_CI_STALL_MINUTES,
    *,
    head: str = "",
) -> list[str]:
    """Checks queued without starting for at least `limit_minutes` (issue #81).

    `queued_since` is the nested map from ``merge_queued_since`` (or legacy flat).
    Times from another head are ignored. Detection only: the tick reports the
    stall on the PR; nothing polls inside a tick.
    """
    try:
        limit = datetime.timedelta(minutes=limit_minutes)
        now_dt = datetime.datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return []
    checks = _queued_since_checks(queued_since, head=head or None)
    stalled = []
    for name in current_queued:
        since = checks.get(name)
        if not since:
            continue
        try:
            since_dt = datetime.datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        except ValueError:
            continue
        if now_dt - since_dt >= limit:
            stalled.append(name)
    return sorted(stalled)


def missing_required_checks(checks: list[dict], config: dict | None = None) -> list[str]:
    """Return required check names from gatekeeper.required_checks that are absent from `checks`."""
    required = ((config or {}).get("gatekeeper") or {}).get("required_checks") or []
    if not required:
        return []
    present = {
        c.get("name") or c.get("context")
        for c in checks
        if (c.get("name") or c.get("context"))
    }
    return [name for name in required if name not in present]


def clear_review_clean_on_head_change(
    forge: Any,
    number: int,
    labels: list[str],
    *,
    reviewed_head: str | None,
    head: str,
) -> bool:
    """Remove gatekeeper:review-clean when the PR head changed before review starts (issue #101 R1)."""
    if not reviewed_head or reviewed_head == head:
        return False
    if contract.GATEKEEPER_REVIEW_CLEAN not in labels:
        return False
    forge.remove_labels(number, [contract.GATEKEEPER_REVIEW_CLEAN])
    return True


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
    repo_has_ci: bool = True,
    stalled: list[str] | None = None,
    missing_required_checks: list[str] | None = None,
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
            return "review", "clean at head but CI failing; diagnose the cause — only a failure this diff caused earns changes-requested", 1
        # G1.2: stall status only where CI applies; stacked/non-dev bases still
        # reach "issue the verdict" instead of waiting on inapplicable checks.
        if stalled and ci_applies:
            return "await-ci", f"clean at head; CI queued and not started ({', '.join(stalled)}) — report the stall on the PR with cause", 1
        if ci_applies and pending_checks:
            return "await-ci", "reviewed clean; waiting for pending CI", 9
        if ci_applies and missing_required_checks:
            return "await-ci", f"reviewed clean; waiting for required checks ({', '.join(missing_required_checks)})", 9
        if ci_applies and total_checks == 0:
            if not repo_has_ci:
                return "await-ci", "reviewed clean; repo has no PR CI — the gatekeeper files the gap issue; never a changes-requested for this cause", 8
            return "await-ci", "reviewed clean; waiting for CI to run on head", 9
        return "review", "reviewed clean; CI done or not applicable, issue the verdict", 1

    if failing_checks and ci_applies:
        return "review", "not reviewed yet; CI failing", 1
    return "review", "not reviewed yet", 1


def determine_status(**kwargs) -> str:
    """Status only; see classify() for the arguments."""
    return classify(**kwargs)[0]


def sensitive_candidates(paths: list[str], protected_paths: tuple[str, ...] = ()) -> list[str]:
    """Changed paths the gatekeeper should open before deciding on stricter review."""
    return sorted(
        p for p in paths
        if SENSITIVE_PATH.search(p) or (protected_paths and p.startswith(protected_paths))
    )


def needs_human_guidance(
    *,
    own_skill: bool,
    labels: list[str],
    needs_comment_present: bool,
) -> dict | None:
    """What the gatekeeper should post/keep for human attention, or None.

    Issue #81: the merge decision is the only gate reserved to a human. Loop-skill PRs
    are reviewed and approved like any other PR; `changes loop skill files`
    signals only that the merge stays with a human. There is no status-based
    reason: ambiguous review criteria is filed as an issue (carrying
    `needs-owner`), never a review skip.

    Idempotency lives here: `commentPresent`/`labelPresent` tell the tick to skip
    work it already did. `staleLabel` marks a `gatekeeper:needs-human` label whose
    reason no longer applies; labels this loop did not add are never removed.
    Path-name matches (`sensitiveCandidates`) are deliberately not a reason: the
    gatekeeper reads those files and applies stricter review itself.
    """
    label_present = "gatekeeper:needs-human" in labels
    reason = "changes loop skill files" if own_skill else None
    if reason is None:
        if not label_present:
            return None
        return {"reason": None, "labelPresent": True,
                "commentPresent": needs_comment_present, "staleLabel": True}
    return {"reason": reason, "labelPresent": label_present,
            "commentPresent": needs_comment_present, "staleLabel": False}


def summarize(
    number: int,
    me: str,
    state: dict | None = None,
    config: dict | None = None,
    readiness: dict | None = None,
) -> dict:
    pr = json.loads(
        gh("pr", "view", str(number), "--json",
           "number,title,url,headRefName,headRefOid,baseRefName,isDraft,mergeable,createdAt,"
           "statusCheckRollup,comments,labels,files")
    )
    cfg = config or load_config()
    readiness = readiness or assess_readiness(config=cfg)
    repo_has_ci = bool((readiness.get("ci") or {}).get("present"))
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

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    queued = queued_not_started(checks)
    seen_queued = pr_state.get("queuedSince") or {}
    queued_since = merge_queued_since(queued, seen_queued, now_iso, head=head)
    try:
        stall_limit = int((cfg.get("gatekeeper") or {}).get("ci_stall_minutes", DEFAULT_CI_STALL_MINUTES))
    except (TypeError, ValueError):
        stall_limit = DEFAULT_CI_STALL_MINUTES
    stalled = stalled_checks(queued, queued_since, now_iso, stall_limit, head=head)

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
    candidates = sensitive_candidates(paths, protected_config_paths)

    missing_req = missing_required_checks(checks, cfg)
    stale_review_clean = bool(
        reviewed_head and reviewed_head != head and contract.GATEKEEPER_REVIEW_CLEAN in labels
    )
    if stale_review_clean:
        try:
            from devloops_lib.forge.detect import get_forge

            if clear_review_clean_on_head_change(
                get_forge(cfg, state=state),
                number,
                labels,
                reviewed_head=reviewed_head,
                head=head,
            ):
                labels = [l for l in labels if l != contract.GATEKEEPER_REVIEW_CLEAN]
                stale_review_clean = False
        except Exception:
            pass

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
        repo_has_ci=repo_has_ci,
        stalled=stalled,
        missing_required_checks=missing_req,
    )
    # Loop-skill PRs keep their normal status; own_skill only drives needsHuman below.
    needs_at_head = next((m for m in reversed(needs) if m.get("head") == head), None)
    needs_human = needs_human_guidance(
        own_skill=own_skill,
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
        "sensitiveCandidates": candidates,
        "labels": labels,
        "staleApprovedLabel": GATEKEEPER_APPROVED_LABEL in labels and status != "approved",
        "needsHuman": needs_human,
        "url": pr["url"],
        "base": base,
        "retargetBeforeMerge": not is_dev_base(base, config=cfg),
        "draft": pr["isDraft"],
        "createdAt": pr["createdAt"],
        "ciPending": pending,
        "ciFailing": failing,
        "ciQueued": queued,
        "ciStalled": stalled,
        "queuedSince": queued_since,
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

        readiness = assess_readiness(config=cfg)
        summaries = [summarize(n, me, state, cfg, readiness) for n in numbers]
        summaries.sort(key=lambda x: (x["priority"], x["createdAt"]))
        print(json.dumps({"readiness": readiness, "prs": summaries}, ensure_ascii=False, indent=2))
    except GhError as e:
        sys.exit(f"gh error: {e}")


if __name__ == "__main__":
    run_main(main)
