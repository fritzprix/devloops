"""Single source of truth for the state shared by the devloops loops (issue #65).

Holds the label names, the overlap order for PR states, and one marker pattern
plus one parser for the gatekeeper-, worker-, and qa-loops. Every consumer
imports from this module; label names and marker patterns must not be defined
again anywhere else in the repository.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional

# --- Labels -------------------------------------------------------------------

# Config-renameable issue labels: the value is the default, the config key is
# labels.<key> in devloops.config.yaml.
APPROVED = "approved"
REJECTED = "rejected"
VALIDATED = "validated"
PRODUCER = "auto:producer"

# Parked PR states (epic #64). The `blocked:base` label is gone (issue #65):
# base blocking is derived from open forge-level blockers instead.
NEEDS_OWNER = "needs-owner"
OPEN_BLOCKER = "open-blocker"

# Review verdict labels. Part of the comment protocol; never renamed by config.
GATEKEEPER_APPROVED = "gatekeeper:approved"
GATEKEEPER_CHANGES_REQUESTED = "gatekeeper:changes-requested"
GATEKEEPER_ESCALATED = "gatekeeper:escalated"
# gatekeeper:needs-human means one thing: a human merges this PR (it changes
# loop skill files, or the review criteria are ambiguous). It never blocks
# review or approval. There is no marker kind "needs-human"; markers that ask a
# human for attention use kind=needs-owner.
GATEKEEPER_NEEDS_HUMAN = "gatekeeper:needs-human"
QA_ACCEPTED = "qa:accepted"
QA_CHANGES_REQUESTED = "qa:changes-requested"
# Transitional until issue #67 replaces the qa needs-human verdict with
# needs-owner; until then it stays defined here and nowhere else.
QA_NEEDS_HUMAN = "qa:needs-human"

GATEKEEPER_VERDICT_LABELS = (
    GATEKEEPER_APPROVED,
    GATEKEEPER_CHANGES_REQUESTED,
    GATEKEEPER_ESCALATED,
)
QA_VERDICT_LABELS = (QA_ACCEPTED, QA_CHANGES_REQUESTED, QA_NEEDS_HUMAN)


def issue_label(config: dict, key: str) -> str:
    """Effective name of a config-renameable issue label.

    `key` is the config key: approved, rejected, validated, or producer.
    """
    defaults = {"approved": APPROVED, "rejected": REJECTED, "validated": VALIDATED, "producer": PRODUCER}
    return (config or {}).get("labels", {}).get(key, defaults[key])


# --- PR state overlap ----------------------------------------------------------

# When several states apply to one PR, the first listed state wins.
STATE_ORDER = (NEEDS_OWNER, GATEKEEPER_ESCALATED, OPEN_BLOCKER, "merge-conflict", "verdict")

VERDICT_LABELS = GATEKEEPER_VERDICT_LABELS + QA_VERDICT_LABELS


def has_open_blocker(blockers: Optional[Iterable[Any]]) -> bool:
    """True when any entry of `blockers` (forge `Blocker` objects) is still open."""
    return any(str(getattr(b, "state", "open")).lower() == "open" for b in blockers or ())


def first_state(
    labels: Iterable[str],
    mergeable: Optional[str] = None,
    blockers: Optional[Iterable[Any]] = None,
) -> Optional[str]:
    """Return the first state in `STATE_ORDER` that applies to a PR, or None.

    `labels` are the PR's label names; `mergeable` is the forge's mergeability
    value (`"CONFLICTING"` marks a merge conflict); `blockers` are forge
    `Blocker` objects for the PR — any open blocker yields the derived
    `open-blocker` state (issue #65; no `blocked:base` label involved).
    """
    present = set(labels or ())
    if NEEDS_OWNER in present:
        return NEEDS_OWNER
    if GATEKEEPER_ESCALATED in present:
        return GATEKEEPER_ESCALATED
    if has_open_blocker(blockers):
        return OPEN_BLOCKER
    if mergeable == "CONFLICTING":
        return "merge-conflict"
    if present & set(VERDICT_LABELS):
        return "verdict"
    return None


def open_blockers(forge: Any, number: int) -> List[int]:
    """Sorted numbers of the still-open blockers of issue/PR `number`.

    `forge` is any object with the `Forge.blocked_by` operation.
    """
    return sorted(int(b.number) for b in forge.blocked_by(number) if str(b.state).lower() == "open")


# --- Marker grammar --------------------------------------------------------------

# A marker must open the comment (ignoring an optional UTF-8 BOM and leading
# whitespace). Quoted markers or mentions later in the comment never count.
MARKER = re.compile(r"^(?:\ufeff)?\s*<!--\s*(gatekeeper-loop|worker-loop|qa-loop):\s*(.*?)\s*-->")

# The accepted vocabulary. A marker outside it is ignored: not a verdict, not a
# response, and not an owner comment. kind "needs-human" does not exist; the
# human-attention marker kind is needs-owner.
GATEKEEPER_VERDICTS = ("changes-requested", "approve", "clean", "escalate")
GATEKEEPER_KINDS = ("needs-owner",)
WORKER_KINDS = ("response", "qa-response", "note", "needs-owner")
QA_KINDS = ("verdict", "post-merge", "needs-owner")
# qa verdict values. "needs-human" is transitional until issue #67.
QA_VERDICTS = ("accepted", "changes-requested", "needs-human")
# `clean` accepts one optional field: blocked-by=#N.
BLOCKED_BY = re.compile(r"#\d+\Z")


def is_marker(body: str) -> bool:
    """True if the comment opens with a loop marker, valid vocabulary or not.

    A marker comment is never an owner comment for any loop.
    """
    return MARKER.match(body or "") is not None


def parse_marker(body: str) -> Optional[tuple[str, dict]]:
    """Parse one comment body. Return (loop, fields), or None if there is no
    valid marker at the start of the comment.

    A bare first token counts as the kind (``worker-loop: response round=2``).
    """
    match = MARKER.match(body or "")
    if not match:
        return None
    loop = match.group(1)
    fields: dict[str, str] = {}
    bare_kind: Optional[str] = None
    for token in match.group(2).split():
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
        elif bare_kind is None:
            bare_kind = token
    if bare_kind is not None:
        fields.setdefault("kind", bare_kind)
    kind = fields.get("kind")
    verdict = fields.get("verdict")
    if loop == "gatekeeper-loop":
        if kind is not None:
            if kind not in GATEKEEPER_KINDS or verdict is not None:
                return None
        elif verdict not in GATEKEEPER_VERDICTS:
            return None
        if verdict == "clean" and not BLOCKED_BY.match(fields.get("blocked-by", "#0")):
            return None
    elif loop == "worker-loop":
        if kind not in WORKER_KINDS:
            return None
    else:  # qa-loop
        if kind is None:
            kind = fields["kind"] = "verdict"
        if kind not in QA_KINDS:
            return None
        if kind == "verdict" and verdict not in QA_VERDICTS:
            return None
    return loop, fields


def parse_markers(comments: list[dict], me: str, loop: Optional[str] = None) -> list[dict]:
    """Valid markers authored by `me`, oldest first, each with loop/createdAt/url.

    Markers by other authors, quoted markers, and markers outside the accepted
    vocabulary never appear.
    """
    found: list[dict] = []
    for c in comments:
        if (c.get("author") or {}).get("login") != me:
            continue
        parsed = parse_marker(c.get("body") or "")
        if parsed is None:
            continue
        name, fields = parsed
        if loop is not None and name != loop:
            continue
        found.append({**fields, "loop": name, "createdAt": c.get("createdAt", ""), "url": c.get("url")})
    found.sort(key=lambda f: f["createdAt"])
    return found
