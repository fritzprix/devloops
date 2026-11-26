"""Shared worker-candidate filter for the worker and the tick gate (issue #66).

One function decides whether an open issue is a worker candidate. Callers may
also exclude issues that an open PR already closes, and the gate additionally
honours the worker ``skipped`` list when ``updatedAt`` matches.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from devloops_lib import contract

# Soft-exclude labels (plus ``rejected``, which is always excluded by name).
EXCLUDED_LABELS = frozenset({"wontfix", "duplicate", "invalid", "question"})


def exclusion_reason(
    labels: Iterable[str],
    assignees: Iterable[str],
    me: str,
    *,
    open_blocker_numbers: Sequence[int] = (),
    approved_label: str = contract.APPROVED,
    rejected_label: str = contract.REJECTED,
    excluded_labels: frozenset[str] = EXCLUDED_LABELS,
) -> Optional[str]:
    """Return a human-readable exclusion reason, or None when the issue is a candidate.

    Required: ``approved_label`` present; ``rejected_label`` and ``excluded_labels``
    absent; no assignee other than ``me``; no open forge blockers.
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
    return None


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
