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
# gatekeeper:review-clean marks a PR whose code review is clean while CI runs
# (issue #101). Separate from the exactly-one verdict labels (like needs-human).
GATEKEEPER_REVIEW_CLEAN = "gatekeeper:review-clean"
# gatekeeper:needs-human means one thing: a human merges this PR (it changes
# loop skill files, or the review criteria are ambiguous). It never blocks
# review or approval. There is no marker kind "needs-human"; markers that ask a
# human for attention use kind=needs-owner.
GATEKEEPER_NEEDS_HUMAN = "gatekeeper:needs-human"
QA_ACCEPTED = "qa:accepted"
QA_CHANGES_REQUESTED = "qa:changes-requested"

GATEKEEPER_VERDICT_LABELS = (
    GATEKEEPER_APPROVED,
    GATEKEEPER_CHANGES_REQUESTED,
    GATEKEEPER_ESCALATED,
)
QA_VERDICT_LABELS = (QA_ACCEPTED, QA_CHANGES_REQUESTED)


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
    """Sorted numbers of the still-open blockers of issue `number`.

    `forge` is any object with the `Forge.blocked_by` operation.
    """
    return sorted(int(b.number) for b in forge.blocked_by(number) if str(b.state).lower() == "open")


def blocker_numbers(forge: Any, number: int) -> List[int]:
    """Sorted numbers of every blocker link on issue `number` (open or closed)."""
    return sorted(int(b.number) for b in forge.blocked_by(number))


CLOSES_ISSUE_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.I)


def closed_issue_numbers(text: str) -> List[int]:
    """Return sorted unique issue numbers referenced by closing keywords in a PR title/body."""
    return sorted({int(m.group(1)) for m in CLOSES_ISSUE_RE.finditer(text or "")})


def pr_blocker_numbers(forge: Any, pr_text: str) -> List[int]:
    """Sorted blocker numbers (open or closed) across all issues closed by a PR.

    Dependencies exist only between issues (owner decision G1.3 / #68); never
    query PR-level dependencies.
    """
    seen: set[int] = set()
    for issue_num in closed_issue_numbers(pr_text):
        seen.update(blocker_numbers(forge, issue_num))
    return sorted(seen)


def pr_open_blockers(forge: Any, pr_text: str) -> List[int]:
    """Sorted still-open blocker numbers across all issues closed by a PR."""
    seen: set[int] = set()
    for issue_num in closed_issue_numbers(pr_text):
        seen.update(open_blockers(forge, issue_num))
    return sorted(seen)


def needs_blocker_rebase(
    *,
    blocker_links: Iterable[Any],
    open_blocker_numbers: Iterable[int],
    head: str,
    clean_head: Optional[str],
) -> bool:
    """True when the worker should rebase a parked PR after its blockers closed (issue #68).

    Requires at least one blocker link, no open blockers, and a gatekeeper ``clean``
    marker whose ``head`` still matches the PR head.
    """
    if not list(blocker_links):
        return False
    if list(open_blocker_numbers):
        return False
    return bool(clean_head) and clean_head == head


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
QA_VERDICTS = ("accepted", "changes-requested")


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
        if verdict == "clean" and "blocked-by" in fields:
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


# --- needs-owner protocol (issue #67) ------------------------------------------

# Association values that mark a comment author as acting for the repository.
OWNER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

# Informational kind=needs-owner markers: merge stays human; they never park.
# Normalized forms of "changes loop skill files", "changes-loop-skill-files",
# and qa’s short reason "loop-skill".
_NEEDS_OWNER_NOTICE_REASONS = frozenset({
    "changes-loop-skill-files",
    "loop-skill",
})


def _normalize_reason(reason: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (reason or "").lower()).strip("-")


def is_needs_owner_ask(fields: dict) -> bool:
    """True when marker fields are a real needs-owner ask (not a notice)."""
    if (fields or {}).get("kind") != NEEDS_OWNER:
        return False
    return _normalize_reason(str(fields.get("reason") or "")) not in _NEEDS_OWNER_NOTICE_REASONS


def author_login(comment: dict) -> str:
    author = comment.get("author") or {}
    if isinstance(author, dict):
        return str(author.get("login") or "")
    return str(author or "")


def author_association(comment: dict) -> str:
    """Author association of a comment, upper-cased, or "" when the forge does not report it."""
    value = comment.get("authorAssociation") or comment.get("author_association") or ""
    return str(value).upper()


def first_owner_reply(comments: list[dict], me: str, since: str) -> Optional[dict]:
    """First owner answer after `since` (ISO time), or None.

    An owner answer is the first comment posted after `since` that carries no
    loop marker and was written by a repository owner, member, or collaborator,
    or by the shared account `me` itself.
    """
    for c in sorted(comments or [], key=lambda c: c.get("createdAt", "")):
        if is_marker(c.get("body") or ""):
            continue
        if c.get("createdAt", "") <= since:
            continue
        login = author_login(c)
        if login == me or author_association(c) in OWNER_ASSOCIATIONS:
            return {
                "author": login,
                "createdAt": c.get("createdAt", ""),
                "url": c.get("url"),
            }
    return None


def latest_needs_owner_ask_at(comments: list[dict]) -> Optional[str]:
    """CreatedAt of the latest real needs-owner ask (any loop), or None.

    Loop-skill notices are ignored so they cannot re-park after an answer.
    """
    asks: list[str] = []
    for c in comments or []:
        parsed = parse_marker(c.get("body") or "")
        if parsed is not None and is_needs_owner_ask(parsed[1]):
            asks.append(c.get("createdAt", ""))
    return max(asks) if asks else None


def needs_owner_parked(comments: list[dict], me: str, *, labeled: bool = True) -> bool:
    """True while a needs-owner ask is unresolved, so no loop may act.

    The ask is the latest real ``kind=needs-owner`` marker (any loop; notices
    excluded). It is resolved by ``first_owner_reply`` after that ask.

    When there is no findable ask: return ``labeled`` (default True so callers
    that already checked the ``needs-owner`` label keep fail-closed behaviour).
    Pass ``labeled=False`` when only markers are in play (e.g. qa classify).
    """
    ask_at = latest_needs_owner_ask_at(comments)
    if ask_at is None:
        return bool(labeled)
    return first_owner_reply(comments or [], me, ask_at) is None
