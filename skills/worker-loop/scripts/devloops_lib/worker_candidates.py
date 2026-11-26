"""Shared worker-candidate filter for the worker and the tick gate (issues #66/#74).

One function decides whether an open issue is a worker candidate. Callers may
also exclude issues that an open PR already closes, and the gate additionally
honours the worker ``skipped`` list when ``updatedAt`` matches.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from devloops_lib import contract

# Defaults for the candidate policy. The filled project scaffolding policy
# (epic #102) overrides these; the loops keep no project policy of their own.
# Soft-exclude labels (plus ``rejected``, which is always excluded by name).
EXCLUDED_LABELS = frozenset({"wontfix", "duplicate", "invalid", "question"})
EPIC_LABEL = "epic"
DEFAULT_CANDIDATE_POLICY: Mapping[str, Any] = {
    # Issues need ``approved`` to be worker candidates (project-policy rule).
    "require_approved": True,
    # Label meaning epic/parent, on top of the forge sub-issue check.
    "epic_label": EPIC_LABEL,
    "excluded_labels": EXCLUDED_LABELS,
    # Title patterns meaning epic/umbrella. There is NO built-in default: a
    # title guess is a project policy, set via scaffolding (issue #95).
    "epic_title_patterns": (),
}


def parent_or_epic_reason(
    title: str,
    labels: Iterable[str],
    sub_issue_numbers: Sequence[int] = (),
    *,
    epic_label: str = EPIC_LABEL,
    epic_title_patterns: Sequence[str] = (),
) -> Optional[str]:
    """Exclude parent issues (have sub-issues) and epic/umbrella issues (issue #74).

    Title matching is policy-driven only (``epic_title_patterns``): there is no
    built-in title regex. An invalid regex is matched as a case-insensitive
    substring.
    """
    children = sorted({int(n) for n in sub_issue_numbers})
    if children:
        return f"parent of #{', #'.join(str(n) for n in children)}"
    label_set = {str(x) for x in labels if x}
    if epic_label and epic_label in label_set:
        return f"label: {epic_label}"
    for pattern in epic_title_patterns or ():
        pattern = str(pattern)
        try:
            matched = re.search(pattern, title or "", re.I)
        except re.error:
            matched = pattern.lower() in (title or "").lower()
        if matched:
            return f"title matches epic pattern {pattern!r}"
    return None


def exclusion_reason(
    labels: Iterable[str],
    assignees: Iterable[str],
    me: str,
    *,
    open_blocker_numbers: Sequence[int] = (),
    sub_issue_numbers: Sequence[int] = (),
    title: str = "",
    approved_label: str = contract.APPROVED,
    rejected_label: str = contract.REJECTED,
    excluded_labels: Optional[frozenset[str]] = None,
    policy: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Return a human-readable exclusion reason, or None when the issue is a candidate.

    The scaffolding candidate policy (``policy``, epic #102) overrides the built-in
    defaults: ``require_approved``, ``epic_label``, ``excluded_labels`` and
    ``epic_title_patterns``. An explicit ``excluded_labels`` argument wins over the
    policy. Defaults: ``approved_label`` present; ``rejected_label`` and
    ``excluded_labels`` absent; no assignee other than ``me``; no open forge
    blockers; not a parent or epic (issue #74).
    """
    resolved: dict[str, Any] = dict(DEFAULT_CANDIDATE_POLICY)
    if policy:
        resolved.update({k: v for k, v in policy.items() if k in resolved and v is not None})
    if excluded_labels is not None:
        resolved["excluded_labels"] = frozenset(excluded_labels)
    label_set = {str(x) for x in labels if x}
    if resolved["require_approved"] and approved_label not in label_set:
        return f"missing {approved_label}"
    if rejected_label in label_set:
        return f"label: {rejected_label}"
    hit = label_set & set(resolved["excluded_labels"])
    if hit:
        return f"label: {', '.join(sorted(hit))}"
    others = sorted({str(a) for a in assignees if a and a != me})
    if others:
        return f"assigned to {', '.join(others)}"
    blockers = sorted({int(n) for n in open_blocker_numbers})
    if blockers:
        return f"open blocker #{', #'.join(str(n) for n in blockers)}"
    return parent_or_epic_reason(
        title,
        label_set,
        sub_issue_numbers,
        epic_label=resolved["epic_label"],
        epic_title_patterns=resolved["epic_title_patterns"],
    )


def list_sub_issue_numbers(
    forge: Any,
    number: int,
    *,
    list_fn: Callable[[Any, int], Sequence[int]] | None = None,
) -> list[int]:
    """Child issue numbers via ``Forge.list_sub_issues`` (issue #74)."""
    if list_fn is not None:
        return [int(n) for n in list_fn(forge, number)]
    return [int(n) for n in forge.list_sub_issues(number)]


def is_skipped(
    number: int,
    updated_at: str,
    skipped: Mapping[str, Any] | None,
) -> bool:
    """True when ``skipped`` holds this issue with the same ``updatedAt``."""
    if not skipped:
        return False
    entry = skipped.get(str(number))
    if not isinstance(entry, dict):
        return False
    return str(entry.get("updatedAt") or "") == str(updated_at or "")


def open_blocker_numbers(
    forge: Any,
    number: int,
    *,
    open_blockers_fn: Callable[[Any, int], Sequence[int]] | None = None,
) -> list[int]:
    """Open blocker issue/PR numbers via ``contract.open_blockers`` (no direct forge tool)."""
    fn = open_blockers_fn or contract.open_blockers
    return [int(n) for n in fn(forge, number)]
