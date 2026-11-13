"""Persist PM tick interval, delegate slot cap, and delegation backend."""
from __future__ import annotations

from typing import Any

from devloops_lib.pm_command import ParsedInvoke, VALID_DELEGATIONS
from devloops_lib.producer import parse_period


def _default_interval_seconds(config: dict[str, Any]) -> int:
    pm = config.get("pm") or {}
    return parse_period(pm.get("default_interval", "10m"))


def _default_slots(config: dict[str, Any]) -> int:
    pm = config.get("pm") or {}
    value = pm.get("default_slots", 1)
    try:
        slots = int(value)
    except (TypeError, ValueError):
        slots = 1
    return max(1, slots)


def _default_delegation(config: dict[str, Any]) -> str:
    pm = config.get("pm") or {}
    raw = str(pm.get("default_delegation", "cursor")).strip().lower()
    return raw if raw in VALID_DELEGATIONS else "cursor"


def get_budget(state: dict[str, Any]) -> dict[str, Any]:
    """Return the budget object from pm-loop state (may be partial)."""
    budget = state.get("budget")
    return dict(budget) if isinstance(budget, dict) else {}


def budget_is_complete(budget: dict[str, Any]) -> bool:
    return (
        isinstance(budget.get("slots"), int)
        and budget.get("slots", 0) >= 1
        and isinstance(budget.get("delegation"), str)
        and budget.get("delegation") in VALID_DELEGATIONS
    )


def prompts_needed(budget: dict[str, Any]) -> list[str]:
    """Fields the agent must ask for once when missing."""
    missing: list[str] = []
    if "slots" not in budget:
        missing.append("slots")
    if "delegation" not in budget:
        missing.append("delegation")
    return missing


def apply_invoke(
    state: dict[str, Any],
    parsed: ParsedInvoke,
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Merge parsed invocation into state; return updated state and prompts still needed."""
    state = dict(state)
    budget = get_budget(state)

    if parsed.interval_seconds is not None:
        budget["intervalSeconds"] = parsed.interval_seconds
    if parsed.slots is not None:
        if parsed.slots < 1:
            raise ValueError("slots must be at least 1")
        budget["slots"] = parsed.slots
    if parsed.delegation is not None:
        if parsed.delegation not in VALID_DELEGATIONS:
            raise ValueError(f"unknown delegation backend: {parsed.delegation!r}")
        budget["delegation"] = parsed.delegation

    if "intervalSeconds" not in budget:
        budget["intervalSeconds"] = _default_interval_seconds(config)
    if parsed.action == "tick" and prompts_needed(budget):
        state["budget"] = budget
        return state, prompts_needed(budget)

    state["budget"] = budget
    return state, prompts_needed(budget)


def record_user_budget_answer(state: dict[str, Any], field: str, value: str | int) -> dict[str, Any]:
    """Persist a one-time user answer for slots or delegation."""
    state = dict(state)
    budget = get_budget(state)
    if field == "slots":
        slots = int(value)
        if slots < 1:
            raise ValueError("slots must be at least 1")
        budget["slots"] = slots
    elif field == "delegation":
        delegation = str(value).strip().lower()
        if delegation not in VALID_DELEGATIONS:
            raise ValueError(f"unknown delegation backend: {delegation!r}")
        budget["delegation"] = delegation
    else:
        raise ValueError(f"unknown budget field: {field!r}")
    state["budget"] = budget
    return state


def budget_summary(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Resolved budget for planners and status output."""
    budget = get_budget(state)
    return {
        "intervalSeconds": budget.get("intervalSeconds", _default_interval_seconds(config)),
        "slots": budget.get("slots"),
        "delegation": budget.get("delegation"),
        "complete": budget_is_complete(budget),
        "promptsNeeded": prompts_needed(budget),
    }
