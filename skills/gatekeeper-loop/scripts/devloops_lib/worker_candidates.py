"""Shared worker-candidate filter for the worker and the tick gate (issues #66/#74).

One function decides whether an open issue is a worker candidate. Callers may
also exclude issues that an open PR already closes, and the gate additionally
honours the worker ``skipped`` list when ``updatedAt`` matches.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from devloops_lib import contract

# Soft-exclude labels (plus ``rejected``, which is always excluded by name).
EXCLUDED_LABELS = frozenset({"wontfix", "duplicate", "invalid", "question"})
EPIC_LABEL = "epic"
# Title-only epic/umbrella when there are no sub-issues yet (issue #74 / #43).
UMBRELLA_TITLE = re.compile(r"\b(umbrella|tracking issue|epic)\b", re.I)


def parent_or_epic_reason(
    title: str,
    labels: Iterable[str],
    sub_issue_numbers: Sequence[int] = (),
    *,
    epic_label: str = EPIC_LABEL,
) -> Optional[str]:
    """Exclude parent issues (have sub-issues) and epic/umbrella issues (issue #74)."""
    children = sorted({int(n) for n in sub_issue_numbers})
    if children:
        return f"parent of #{', #'.join(str(n) for n in children)}"
    label_set = {str(x) for x in labels if x}
    if epic_label in label_set:
        return f"label: {epic_label}"
    if UMBRELLA_TITLE.search(title or ""):
        return "title names epic/umbrella"
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
    excluded_labels: frozenset[str] = EXCLUDED_LABELS,
) -> Optional[str]:
    """Return a human-readable exclusion reason, or None when the issue is a candidate.

    Required: ``approved_label`` present; ``rejected_label`` and ``excluded_labels``
    absent; no assignee other than ``me``; no open forge blockers; not a parent
    or epic/umbrella issue (issue #74).
    """
    label_set = {str(x) for x in labels if x}
    if approved_label not in label_set:
        return f"missing {approved_label}"
    if rejected_label in label_set:
        return f"label: {rejected_label}"
    hit = label_set & set(excluded_labels)
    if hit:
        return f"label: {', '.join(sorted(hit))}"
    others = sorted({str(a) for a in assignees if a and a != me})
    if others:
        return f"assigned to {', '.join(others)}"
    blockers = sorted({int(n) for n in open_blocker_numbers})
    if blockers:
        return f"open blocker #{', #'.join(str(n) for n in blockers)}"
    return parent_or_epic_reason(title, label_set, sub_issue_numbers)


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
