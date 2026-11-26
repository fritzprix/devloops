#!/usr/bin/env python3
"""Summarize actionable state for the current user's open PRs.

Prints JSON: one entry per open PR authored by @me with merge state, failing
and pending checks, unresolved review threads, and PR comments from others
that arrived after the latest commit, plus the latest gatekeeper-loop verdict
(own marker comments only). `duplicateCommits` lists head commits whose patch-id is
already in the base (needs a prior `git fetch origin`); they yield `restack`.
Bodies are truncated; fetch the full text only for items you act on.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from devloops_lib.gh import GhError, gh
from pathlib import Path

# Support running directly or as module
try:
    from devloops_lib.config import load_config
except ImportError:
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    from devloops_lib.config import load_config

from devloops_lib import contract
# Quoted markers or mentions later in the comment are ignored.
START_MARKER = contract.MARKER

THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id isResolved isOutdated path line
          comments(first: 1) { nodes { author { login } body url } }
        }
      }
    }
  }
}
"""

FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", "REQUESTED"}
CLOSING_PATTERN = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.I)
HEAD_ISSUE_PATTERN = re.compile(r"-(\d+)$")


# gh helper is imported from devloops_lib.gh


def short(text: str | None, limit: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def check_state(check: dict) -> str:
    return (check.get("conclusion") or check.get("state") or check.get("status") or "").upper()


def duplicate_commits(base: str, head: str, cwd: str | None = None) -> list[str]:
    """Head commits whose patch-id also exists in base (`git cherry` marks them `-`).

    These appear when the base was rebased or the commits were cherry-picked into it:
    the PR still carries the old copies and needs a restack. Returns [] when either
    ref is missing locally.
    """
    result = subprocess.run(
        ["git", "cherry", f"origin/{base}", f"origin/{head}"],
        capture_output=True, text=True, encoding="utf-8", cwd=cwd,
    )
    if result.returncode != 0:
        return []
    return [line[2:] for line in result.stdout.splitlines() if line.startswith("- ")]


def gatekeeper_state(comments: list[dict], me: str, head_sha: str) -> dict | None:
    """Latest gatekeeper verdict (own markers only) and whether it awaits the worker.

    Only markers that carry a verdict replace the latest verdict, so a
    kind=needs-owner marker posted afterwards cannot hide a changes-requested
    from the worker. Only kind=response worker markers count as responses.
    """
    verdict, responded_rounds = None, set()
    for m in contract.parse_markers(comments, me):
        if m["loop"] == "gatekeeper-loop":
            if m.get("verdict"):
                verdict = {k: v for k, v in m.items() if k != "loop"}
        elif m["loop"] == "worker-loop" and m.get("kind") == "response":
            if verdict and m["createdAt"] > verdict["createdAt"]:
                responded_rounds.add(m.get("round"))
    if verdict is None:
        return None
    verdict["awaitingWorker"] = (
        verdict.get("verdict") == "changes-requested"
        and verdict.get("head") == head_sha
        and verdict.get("round") not in responded_rounds
    )
    return verdict


def qa_state(comments: list[dict], me: str, head_sha: str) -> dict | None:
    """Latest qa verdict (own verdict markers only) and whether it awaits the worker.

    Only kind=verdict markers that carry a verdict replace the latest verdict, so
    a kind=needs-owner marker posted afterwards cannot hide a changes-requested
    from the worker. Only kind=qa-response worker markers count as answers to a
    qa round; the plain `response` kind answers the gatekeeper only (issue #69).
    """
    verdict, responded_rounds = None, set()
    for m in contract.parse_markers(comments, me):
        if m["loop"] == "qa-loop" and m.get("kind") == "verdict":
            if m.get("verdict"):
                verdict = {k: v for k, v in m.items() if k != "loop"}
        elif m["loop"] == "worker-loop" and m.get("kind") == "qa-response":
            if verdict and m["createdAt"] > verdict["createdAt"]:
                responded_rounds.add(m.get("round"))
    if verdict is None:
        return None
    verdict["awaitingWorker"] = (
        verdict.get("verdict") == "changes-requested"
        and verdict.get("head") == head_sha
        and verdict.get("round") not in responded_rounds
    )
    return verdict


def parent_pr_merged(
    base: str,
    default_base: str,
    merged_prs: list[dict] | None,
    open_head_refs: set[str] | frozenset[str] | None = None,
) -> dict | None:
    """Return the merged parent PR if `base` is a stacked branch whose PR merged into `default_base`.

    Stacks are capped at depth 1, so only parents merged into `default_base` trigger retargeting.
    If an open PR still uses ``base`` as its head (branch name reused after an
    earlier merge), that open PR is the live parent — do not retarget yet.
    """
    if not base or base == default_base or not merged_prs:
        return None
    if open_head_refs and base in open_head_refs:
        return None
    for pr in merged_prs:
        if pr.get("headRefName") == base and (pr.get("baseRefName") or default_base) == default_base:
            return {
                "number": pr["number"],
                "base": pr.get("baseRefName", ""),
                "url": pr.get("url", ""),
            }
    return None


def _linked_issue_numbers(pr: dict) -> set[int]:
    text = f"{pr.get('title') or ''} {pr.get('body') or ''}"
    nums = {int(n) for n in CLOSING_PATTERN.findall(text)}
    if not nums:
        match = HEAD_ISSUE_PATTERN.search(pr.get("headRefName") or "")
        if match:
            nums.add(int(match.group(1)))
    return nums


def _ancestor_merged_into_default(
    pr: dict,
    merged_by_head: dict[str, dict],
    default_base: str,
) -> dict | None:
    """If `pr` merged into a non-default branch that later merged into `default_base`, return the landing PR."""
    base = pr.get("baseRefName") or ""
    if not base or base == default_base:
        return None
    visited: set[str] = {pr.get("headRefName") or ""}
    current_base = base
    while current_base and current_base not in visited:
        visited.add(current_base)
        parent = merged_by_head.get(current_base)
        if parent is None:
            return None
        parent_base = parent.get("baseRefName") or ""
        if parent_base == default_base:
            return parent
        current_base = parent_base
    return None


def find_merged_via_parent_issues(
    open_issues: list[dict],
    merged_prs: list[dict],
    default_base: str = "main",
) -> list[dict]:
    """Find open issues whose PR merged into a parent branch that then merged into `default_base`."""
    if not open_issues or not merged_prs:
        return []
    open_numbers = {i["number"] for i in open_issues}
    merged_by_head = {
        pr["headRefName"]: pr for pr in merged_prs if pr.get("headRefName")
    }
    found: dict[int, dict] = {}
    for child in merged_prs:
        parent = _ancestor_merged_into_default(child, merged_by_head, default_base)
        if parent is None:
            continue
        for issue_num in sorted(_linked_issue_numbers(child)):
            if issue_num not in open_numbers or issue_num in found:
                continue
            child_base = child.get("baseRefName") or ""
            found[issue_num] = {
                "issue": issue_num,
                "childPr": child["number"],
                "childUrl": child.get("url", ""),
                "childBase": child_base,
                "parentPr": parent["number"],
                "parentUrl": parent.get("url", ""),
                "reason": (
                    f"merged in PR #{child['number']} into {child_base}, "
                    f"landed on {default_base} via PR #{parent['number']}; close #{issue_num}"
                ),
            }
    return [found[n] for n in sorted(found)]


# The pr_status priority order. wait-ci and none need no work now.
# Canonical list shared by the worker plan (skills/worker-loop/scripts/plan_tick.py)
# and the PM pipeline counter prsNeedingWorker (devloops_lib.pm_pipeline, issue #71).
WORKER_PR_ACTIONS = ["retarget", "resolve-conflict", "restack", "address-gatekeeper", "address-qa", "triage-comments", "fix-ci"]


def select_next_action(
    *,
    retarget: bool = False,
    conflicting: bool,
    duplicates: list[str],
    gatekeeper: dict | None,
    qa: dict | None,
    has_comments: bool,
    failing: list[str],
    pending: list[str],
    escalated: bool = False,
) -> str:
    # Issue #70: a PR parked with gatekeeper:escalated is the owner's turn.
    # The worker must not rebase, fix CI, triage, or answer reviews on it.
    if escalated:
        return "none"
    if retarget:
        return "retarget"
    if conflicting:
        return "resolve-conflict"
    if duplicates:
        return "restack"
    if gatekeeper and gatekeeper["awaitingWorker"]:
        return "address-gatekeeper"
    if qa and qa["awaitingWorker"]:
        return "address-qa"
    if has_comments:
        return "triage-comments"
    if failing:
        return "fix-ci"
    if pending:
        return "wait-ci"
    return "none"


def summarize(
    number: int,
    owner: str,
    name: str,
    me: str,
    *,
    default_base: str | None = None,
    merged_prs: list[dict] | None = None,
    open_head_refs: set[str] | frozenset[str] | None = None,
) -> dict:
    pr = json.loads(
        gh(
            "pr", "view", str(number), "--json",
            "number,title,url,headRefName,headRefOid,baseRefName,isDraft,mergeable,"
            "mergeStateStatus,reviewDecision,statusCheckRollup,commits,comments,labels",
        )
    )
    gatekeeper = gatekeeper_state(pr.get("comments") or [], me, pr["headRefOid"])
    qa = qa_state(pr.get("comments") or [], me, pr["headRefOid"])
    checks = pr.get("statusCheckRollup") or []
    failing = [c.get("name") or c.get("context") for c in checks if check_state(c) in FAILED]
    pending = [c.get("name") or c.get("context") for c in checks if check_state(c) in PENDING]
    labels: list[str] = []
    for item in pr.get("labels") or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            labels.append(str(name))
    labels = sorted(labels)
    escalated = contract.GATEKEEPER_ESCALATED in labels

    threads = []
    cursor = None
    while True:
        args = ["api", "graphql", "-f", f"query={THREADS_QUERY}", "-F", f"owner={owner}",
                "-F", f"name={name}", "-F", f"number={number}"]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        threads_raw = json.loads(gh(*args))
        review_threads = (
            threads_raw.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads") or {}
        )
        nodes = review_threads.get("nodes") or []
        for node in nodes:
            if node["isResolved"]:
                continue
            first = (node["comments"]["nodes"] or [{}])[0]
            threads.append({
                "threadId": node["id"],
                "outdated": node["isOutdated"],
                "path": node["path"],
                "line": node["line"],
                "author": (first.get("author") or {}).get("login"),
                "body": short(first.get("body")),
                "url": first.get("url"),
            })
        page_info = review_threads.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    commits = pr.get("commits") or []
    last_commit_at = commits[-1]["committedDate"] if commits else ""
    # Filter comments: keep non-bot comments posted after the latest commit that do NOT
    # carry loop markers. This includes human owner comments even when the owner shares login @me.
    new_comments = []
    for c in pr.get("comments") or []:
        author_login = (c.get("author") or {}).get("login") or ""
        body = c.get("body") or ""
        if author_login.endswith("[bot]") or contract.is_marker(body):
            continue
        if c.get("createdAt", "") > last_commit_at:
            new_comments.append({
                "author": author_login,
                "createdAt": c["createdAt"],
                "body": short(body),
                "url": c.get("url"),
            })

    duplicates = duplicate_commits(pr["baseRefName"], pr["headRefName"])
    resolved_default = default_base or load_config().get("project", {}).get("base_branch", "main")
    merged_parent = parent_pr_merged(
        pr["baseRefName"],
        resolved_default,
        merged_prs,
        open_head_refs=open_head_refs,
    )
    retarget_base = resolved_default if merged_parent else None

    next_action = select_next_action(
        retarget=bool(merged_parent),
        conflicting=pr.get("mergeable") == "CONFLICTING",
        duplicates=duplicates,
        gatekeeper=gatekeeper,
        qa=qa,
        has_comments=bool(threads or new_comments),
        failing=failing,
        pending=pending,
        escalated=escalated,
    )

    return {
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "head": pr["headRefName"],
        "headSha": pr["headRefOid"],
        "base": pr["baseRefName"],
        "draft": pr["isDraft"],
        "mergeable": pr.get("mergeable"),
        "mergeState": pr.get("mergeStateStatus"),
        "reviewDecision": pr.get("reviewDecision"),
        "failingChecks": failing,
        "pendingChecks": pending,
        "unresolvedThreads": threads,
        "newComments": new_comments,
        "gatekeeper": gatekeeper,
        "qa": qa,
        "labels": labels,
        "duplicateCommits": duplicates,
        "mergedParent": merged_parent,
        "retargetBase": retarget_base,
        "nextAction": next_action,
    }


def _fetch_merged_prs() -> list[dict]:
    return json.loads(
        gh("pr", "list", "--state", "merged", "--limit", "100", "--json",
           "number,title,body,headRefName,baseRefName,url")
    )


def summarize_prs(number: int | None = None) -> list[dict]:
    """Summarize one PR, or all non-draft open PRs by @me."""
    repo = json.loads(gh("repo", "view", "--json", "owner,name"))
    owner, name = repo["owner"]["login"], repo["name"]
    me = gh("api", "user", "--jq", ".login").strip()
    default_base = load_config().get("project", {}).get("base_branch", "main")
    merged_prs: list[dict] | None = None
    # All open heads (any author): a reused base branch with a live PR must not
    # look like a merged parent just because an older PR of the same name merged.
    open_listed = json.loads(
        gh("pr", "list", "--state", "open", "--limit", "100", "--json", "number,isDraft,headRefName,baseRefName,author")
    )
    open_head_refs = {
        p["headRefName"]
        for p in open_listed
        if p.get("headRefName") and not p.get("isDraft")
    }
    if number:
        numbers = [number]
        merged_prs = _fetch_merged_prs()
    else:
        open_ready = [
            p for p in open_listed
            if not p.get("isDraft") and (p.get("author") or {}).get("login") == me
        ]
        numbers = [p["number"] for p in open_ready]
        if any((p.get("baseRefName") or default_base) != default_base for p in open_ready):
            merged_prs = _fetch_merged_prs()
    return [
        summarize(
            n,
            owner,
            name,
            me,
            default_base=default_base,
            merged_prs=merged_prs,
            open_head_refs=open_head_refs,
        )
        for n in numbers
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, help="Only this PR number")
    args = parser.parse_args()

    try:
        print(json.dumps(summarize_prs(args.pr), ensure_ascii=False, indent=2))
    except GhError as e:
        sys.exit(f"gh error: {e}")


if __name__ == "__main__":
    from devloops_lib.config import run_main

    run_main(main)
