#!/usr/bin/env python3
"""List open issues with the signals needed to pick the next one to work on.

Prints JSON sorted with the most likely candidates first. The ordering is a
hint only; read the top candidates before choosing. Excluded issues are listed
separately with the reason.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Add project root and devloops_lib to sys.path
_script_dir = Path(__file__).resolve().parent
for _candidate in (_script_dir.parents[2], _script_dir.parents[3], _script_dir.parents[3] / ".agents"):
    if (_candidate / "devloops_lib").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from devloops_lib.claims import pr_is_approved
from devloops_lib.config import load_config, open_pr_limit as configured_open_pr_limit
from devloops_lib.gh import GhError, gh
from devloops_lib.config import run_main

EXCLUDED_LABELS = {"wontfix", "duplicate", "invalid", "question"}
DEPRIORITIZED_LABELS = {"backlog"}
REF_PATTERN = re.compile(r"(?<![\w/])#(\d+)\b")
EXTERNAL_REF_PATTERN = re.compile(r"\b[\w.-]+/[\w.-]+#\d+\b")
CLOSING_PATTERN = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.I)
BLOCKED_PATTERN = re.compile(r"\b(blocked by|depends on|waiting (?:for|on)|after #\d+ (?:merges|lands))\b", re.I)
UMBRELLA_PATTERN = re.compile(r"\b(umbrella|tracking issue|epic)\b", re.I)
# A "Depends on #1, #3" line. A mention inside a sentence is not a dependency.
DEPENDS_ON_PATTERN = re.compile(r"^[ \t]*(?:[-*][ \t]+)?depends on:?[ \t]+(#\d+(?:(?:,|[ \t]+and)?[ \t]*#\d+)*)", re.I | re.M)
PART_OF_PATTERN = re.compile(r"\bpart of\s+#(\d+)\b", re.I)
HEAD_ISSUE_PATTERN = re.compile(r"-(\d+)$")


# gh helper is imported from devloops_lib.gh


def parse_depends_on(body: str, number: int) -> list[int]:
    """Return the issue or PR numbers in the body's "Depends on" lines."""
    found = set()
    for match in DEPENDS_ON_PATTERN.findall(body):
        found |= {int(n) for n in re.findall(r"#(\d+)", match)}
    return sorted(found - {number})


def _prs_working_on(open_prs: list[dict]) -> dict[int, list[dict]]:
    """Map each issue number to the open PRs that carry its work."""
    result: dict[int, list[dict]] = {}
    for pr in open_prs:
        text = f"{pr['title']} {pr.get('body') or ''}"
        issues = {int(n) for n in CLOSING_PATTERN.findall(text)} | {int(n) for n in PART_OF_PATTERN.findall(text)}
        match = HEAD_ISSUE_PATTERN.search(pr.get("headRefName") or "")
        if match:
            issues.add(int(match.group(1)))
        for issue in issues:
            result.setdefault(issue, []).append(pr)
    return result


def resolve_dependency(
    depends_on: list[int],
    open_issue_numbers: set[int],
    open_prs: list[dict],
    prs_working_on: dict[int, list[dict]],
    base_branch: str,
) -> dict:
    """Decide how an issue can start, given its dependencies. Recomputed on every run.

    Status: none (no dependencies), ready (all closed), stack (one open dependency
    with a PR to branch from), or wait (an open dependency with no such PR, or more
    than one open dependency, because a branch has only one base). A PR that is
    itself stacked is not offered: stacking on it would reach depth 2.
    """
    prs_by_number = {pr["number"]: pr for pr in open_prs}
    unfinished = [d for d in depends_on if d in open_issue_numbers or d in prs_by_number]
    result = {"status": "none" if not depends_on else "ready", "dependsOn": depends_on, "open": unfinished, "stackOn": None}
    if not unfinished:
        return result
    result["status"] = "wait"
    if len(unfinished) > 1:
        return result
    dep = unfinished[0]
    prs = [prs_by_number[dep]] if dep in prs_by_number else prs_working_on.get(dep, [])
    options = [pr for pr in prs if (pr.get("baseRefName") or base_branch) == base_branch]
    if options:
        pr = max(options, key=lambda p: (pr_is_approved(p), p["number"]))
        result["status"] = "stack"
        result["stackOn"] = {"pr": pr["number"], "head": pr.get("headRefName", ""), "approved": pr_is_approved(pr)}
    return result


def open_pr_limit(open_prs: list[dict], me: str, limit: int) -> dict:
    """Count my unapproved open PRs against the limit. Approved PRs wait only for a merge."""
    unapproved = sum(
        1 for pr in open_prs
        if (pr.get("author") or {}).get("login") == me and not pr_is_approved(pr)
    )
    return {"unapproved": unapproved, "limit": limit, "reached": unapproved >= limit}


def filter_and_rank_issues(
    issues: list[dict],
    open_prs: list[dict],
    me: str,
    config: dict | None = None,
) -> dict:
    cfg = config or load_config()
    producer_label = cfg.get("labels", {}).get("producer", "auto:producer")
    approved_label = cfg.get("labels", {}).get("approved", "approved")

    open_issue_numbers = {i["number"] for i in issues}
    open_pr_numbers = {p["number"] for p in open_prs}
    base_branch = cfg.get("project", {}).get("base_branch", "main")
    prs_working_on = _prs_working_on(open_prs)

    closing_pr: dict[int, list[int]] = {}
    mentioned_by_pr: dict[int, list[int]] = {}
    for pr in open_prs:
        text = f"{pr['title']} {pr.get('body') or ''}"
        closes = {int(n) for n in CLOSING_PATTERN.findall(text)}
        closes |= {int(n) for n in re.findall(r"-(\d{3,})\b", pr.get("headRefName") or "")}
        for ref in closes:
            closing_pr.setdefault(ref, []).append(pr["number"])
        for ref in {int(n) for n in REF_PATTERN.findall(text)} - closes:
            mentioned_by_pr.setdefault(ref, []).append(pr["number"])

    candidates, excluded = [], []
    for issue in issues:
        number = issue["number"]
        body = issue.get("body") or ""
        labels = {label["name"] if isinstance(label, dict) else label for label in issue.get("labels") or []}
        assignees = [
            a["login"] if isinstance(a, dict) else a
            for a in issue.get("assignees") or []
        ]
        refs = sorted({int(n) for n in REF_PATTERN.findall(body)} - {number})
        checkboxes = re.findall(r"^\s*[-*] \[( |x|X)\]", body, re.M)

        reason = None
        if labels & EXCLUDED_LABELS:
            reason = f"label: {', '.join(sorted(labels & EXCLUDED_LABELS))}"
        elif producer_label in labels and approved_label not in labels:
            reason = f"producer issue not approved ({producer_label})"
        elif assignees and me not in assignees:
            reason = f"assigned to {', '.join(assignees)}"
        elif number in closing_pr:
            reason = f"closed by open PR #{', #'.join(map(str, closing_pr[number]))}"

        entry = {
            "number": number,
            "title": issue["title"],
            "url": issue.get("url", ""),
            "labels": sorted(labels),
            "assignedToMe": me in assignees,
            "checkboxes": {"total": len(checkboxes), "done": sum(c.lower() == "x" for c in checkboxes)},
            "bodyChars": len(body),
            "refsOpenIssues": [r for r in refs if r in open_issue_numbers],
            "refsOpenPRs": [r for r in refs if r in open_pr_numbers],
            "mentionedByOpenPRs": mentioned_by_pr.get(number, []),
            "externalRefs": sorted(set(EXTERNAL_REF_PATTERN.findall(body))),
            "dependency": resolve_dependency(
                parse_depends_on(body, number), open_issue_numbers, open_prs, prs_working_on, base_branch
            ),
            "blockedHint": bool(BLOCKED_PATTERN.search(body)),
            "umbrellaHint": bool(UMBRELLA_PATTERN.search(issue["title"])),
            "updatedAt": issue.get("updatedAt", ""),
        }
        if reason:
            excluded.append({"number": number, "title": issue["title"], "reason": reason})
            continue
        candidates.append(entry)

    def dependency_rank(dependency: dict) -> int:
        if dependency["status"] == "wait":
            return 2
        stack_on = dependency["stackOn"]
        return 1 if stack_on and not stack_on["approved"] else 0

    def rank(entry: dict) -> tuple:
        return (
            dependency_rank(entry["dependency"]),
            not entry["assignedToMe"],
            entry["blockedHint"],
            entry["umbrellaHint"],
            bool(entry["refsOpenPRs"] or entry["externalRefs"]),
            bool(set(entry["labels"]) & DEPRIORITIZED_LABELS),
            entry["checkboxes"]["total"] == 0,
            "bug" not in entry["labels"],
            entry["bodyChars"],
        )

    candidates.sort(key=rank)
    return {
        "openPRLimit": open_pr_limit(open_prs, me, configured_open_pr_limit(cfg)),
        "candidates": candidates,
        "excluded": excluded,
    }


def fetch_candidates(limit: int = 100) -> dict:
    me = gh("api", "user", "--jq", ".login").strip()
    issues = json.loads(
        gh("issue", "list", "--state", "open", "--limit", str(limit), "--json",
           "number,title,labels,assignees,body,createdAt,updatedAt,url")
    )
    open_prs = json.loads(
        gh("pr", "list", "--state", "open", "--limit", "100", "--json",
           "number,title,body,headRefName,baseRefName,reviewDecision,labels,author")
    )
    return filter_and_rank_issues(issues, open_prs, me)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, help="Candidate issues limit (default: 100)")
    args = parser.parse_args()

    try:
        print(json.dumps(fetch_candidates(args.limit), ensure_ascii=False, indent=2))
    except GhError as e:
        sys.exit(f"gh error: {e}")


if __name__ == "__main__":
    run_main(main)
