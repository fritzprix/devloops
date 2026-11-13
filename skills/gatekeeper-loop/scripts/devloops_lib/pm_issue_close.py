"""Find open issues whose work has already merged and format PM close actions."""
from __future__ import annotations

import re
from typing import Any

# Same heuristics as issue_candidates.py and tick_gate.py.
CLOSING_PATTERN = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.I)
BRANCH_ISSUE_PATTERN = re.compile(r"-(\d{3,})\b")

PM_CLOSE_MARKER = "<!-- pm-loop: close-merged -->"


def issue_numbers_closed_by_item(item: dict[str, Any]) -> set[int]:
    """Return issue numbers this merged work item closes (GitHub PR text/branch or explicit list)."""
    explicit = item.get("closesIssues") or item.get("closes_issues") or []
    nums = {int(n) for n in explicit if str(n).isdigit()}
    text = f"{item.get('title') or ''} {item.get('body') or item.get('bodyText') or ''}"
    nums |= {int(n) for n in CLOSING_PATTERN.findall(text)}
    nums |= {int(n) for n in BRANCH_ISSUE_PATTERN.findall(item.get("headRefName") or item.get("branch") or "")}
    return nums


def is_merged_item(item: dict[str, Any]) -> bool:
    if item.get("merged") is True:
        return True
    state = str(item.get("state", "")).upper()
    return state in {"MERGED", "DONE", "CLOSED"}


def is_open_issue(issue: dict[str, Any]) -> bool:
    state = str(issue.get("state", "OPEN")).upper()
    return state not in {"CLOSED", "DONE"}


def close_comment(issue_number: int, merged_item: dict[str, Any]) -> str:
    """Comment body the PM posts when closing after a verified merge."""
    ref = merged_item.get("url") or merged_item.get("htmlUrl") or merged_item.get("webUrl") or ""
    label = merged_item.get("title") or f"change #{merged_item.get('number', merged_item.get('id', ''))}"
    lines = [
        PM_CLOSE_MARKER,
        f"pm-loop: Closing issue #{issue_number} because the work has merged.",
        f"Merged change: {label}",
    ]
    if ref:
        lines.append(ref)
    return "\n".join(lines)


def closable_merged_issues(
    open_issues: list[dict[str, Any]],
    merged_work: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return open issues that have merged work ready to close (any board provider)."""
    open_by_number = {
        int(i["number"]): i for i in open_issues if is_open_issue(i) and "number" in i
    }
    seen: set[int] = set()
    actions: list[dict[str, Any]] = []

    for item in merged_work:
        if not is_merged_item(item):
            continue
        for num in issue_numbers_closed_by_item(item):
            if num not in open_by_number or num in seen:
                continue
            seen.add(num)
            actions.append(
                {
                    "issueNumber": num,
                    "mergedWork": {
                        "number": item.get("number"),
                        "id": item.get("id"),
                        "url": item.get("url") or item.get("htmlUrl") or item.get("webUrl"),
                        "title": item.get("title"),
                    },
                    "comment": close_comment(num, item),
                    "action": "close_with_comment",
                }
            )
    return sorted(actions, key=lambda a: a["issueNumber"])
