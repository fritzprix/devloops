"""Parse /pm-loop invocation strings.

Examples:
  /pm-loop
  /pm-loop 10m
  /pm-loop 10m slots: 3 delegation: claude
  /pm-loop status
  /pm-loop stop
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from devloops_lib.producer import parse_period

VALID_DELEGATIONS = frozenset({"cursor", "claude", "harness"})

_SLOTS_RE = re.compile(r"\bslots:\s*(\d+)\b", re.I)
_DELEGATION_RE = re.compile(r"\bdelegation:\s*([A-Za-z0-9_-]+)\b", re.I)
_PREFIX_RE = re.compile(r"^/?pm-loop\b", re.I)
_INTERVAL_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?[smhdw]$", re.I)


@dataclass(frozen=True)
class ParsedInvoke:
    action: str  # tick | status | stop
    interval_seconds: int | None = None
    slots: int | None = None
    delegation: str | None = None
    raw: str = ""

    @property
    def delegation_valid(self) -> bool:
        if self.delegation is None:
            return True
        return self.delegation.lower() in VALID_DELEGATIONS


def _strip_kv_tokens(text: str) -> tuple[str, int | None, str | None]:
    slots: int | None = None
    delegation: str | None = None
    m = _SLOTS_RE.search(text)
    if m:
        slots = int(m.group(1))
        if slots < 1:
            raise ValueError("slots must be at least 1")
    m = _DELEGATION_RE.search(text)
    if m:
        delegation = m.group(1).lower()
    stripped = _SLOTS_RE.sub("", text)
    stripped = _DELEGATION_RE.sub("", stripped)
    return stripped.strip(), slots, delegation


def _validate_delegation(delegation: str | None) -> None:
    if delegation and delegation not in VALID_DELEGATIONS:
        raise ValueError(f"unknown delegation backend: {delegation!r}")


def parse_pm_invoke(text: str) -> ParsedInvoke:
    """Parse a slash command or plain argument string for pm-loop."""
    raw = (text or "").strip()
    body = _PREFIX_RE.sub("", raw).strip()
    body, slots, delegation = _strip_kv_tokens(body)
    _validate_delegation(delegation)

    if not body:
        return ParsedInvoke(action="tick", slots=slots, delegation=delegation, raw=raw)

    lower = body.lower()
    if lower == "status":
        return ParsedInvoke(action="status", slots=slots, delegation=delegation, raw=raw)
    if lower == "stop":
        return ParsedInvoke(action="stop", slots=slots, delegation=delegation, raw=raw)

    interval_seconds: int | None = None
    tokens = body.split()
    if tokens and _INTERVAL_TOKEN_RE.match(tokens[0]):
        interval_seconds = parse_period(tokens[0])
        body = " ".join(tokens[1:]).strip()
        if body:
            raise ValueError(f"unexpected tokens after interval: {body!r}")

    return ParsedInvoke(
        action="tick",
        interval_seconds=interval_seconds,
        slots=slots,
        delegation=delegation,
        raw=raw,
    )
