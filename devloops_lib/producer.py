"""Producer loop task selection, scheduling periods, and state management.

Shared reusable logic for skills/producer-loop.
Python stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from devloops_lib.config import get_state_path, load_config
from devloops_lib.tick_gate import (
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_NOOP,
    EXIT_RUN,
    LOCK_TTL_SECONDS,
    acquire_lock,
    check_lock,
    load_state,
    release_lock,
    save_state,
)

# Named constants
DEFAULT_TASK_PERIOD = "24h"
DEFAULT_TASK_PERIOD_SECONDS = 86400  # 1 day
DEFAULT_TASKS = [
    "security",
    "e2e",
    "uiux",
    "regression",
    "usage-logs",
    "doc-drift",
    "cleanup",
]
DEFAULT_MAX_NEW_ISSUES_PER_RUN = 3
FP_MARKER_PREFIX = "<!-- producer:fp="
FP_MARKER_REGEX = re.compile(r"<!--\s*producer:fp=([a-fA-F0-9]+)\s*-->")


def parse_period(val: str | int | float | None) -> int:
    """Parse human readable period (e.g. 24h, 10m, 7d, 30s) or seconds into integer seconds."""
    if val is None:
        return DEFAULT_TASK_PERIOD_SECONDS
    if isinstance(val, (int, float)):
        return int(val)
    val_str = str(val).strip().lower()
    if not val_str:
        return DEFAULT_TASK_PERIOD_SECONDS
    if val_str.isdigit():
        return int(val_str)

    units = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }
    unit = val_str[-1]
    number_part = val_str[:-1]
    if unit in units:
        try:
            return int(float(number_part) * units[unit])
        except ValueError:
            pass
    return DEFAULT_TASK_PERIOD_SECONDS


def get_task_period(task: str, config: dict[str, Any], state: dict[str, Any]) -> int:
    """Determine the period for a task respecting state, per-task config, and default period."""
    # 1. State overrides (taskPeriods or task_periods)
    state_periods = state.get("taskPeriods") or state.get("task_periods") or {}
    if task in state_periods:
        return parse_period(state_periods[task])

    # 2. Config per-task overrides
    producer_cfg = config.get("producer", {})
    task_periods = producer_cfg.get("task_periods") or producer_cfg.get("periods") or {}
    if task in task_periods:
        return parse_period(task_periods[task])

    # 3. Config default period
    if "default_period" in producer_cfg:
        return parse_period(producer_cfg["default_period"])

    return DEFAULT_TASK_PERIOD_SECONDS


def compute_fingerprint(task: str, stable_key: str) -> str:
    """Compute sha256(task + stable finding key)."""
    payload = (task + stable_key).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def get_due_tasks(
    config: dict[str, Any],
    state: dict[str, Any],
    now: float | None = None,
) -> list[tuple[str, float]]:
    """Return all due tasks sorted by overdue duration descending (most overdue first)."""
    current_time = time.time() if now is None else now
    tasks = config.get("producer", {}).get("tasks", DEFAULT_TASKS)
    last_runs = state.get("lastRun") or state.get("last_run") or {}

    due_tasks: list[tuple[str, float]] = []
    for task in tasks:
        period = get_task_period(task, config, state)
        last_run = last_runs.get(task, 0)
        due_at = last_run + period
        if current_time >= due_at:
            overdue_duration = current_time - due_at
            due_tasks.append((task, overdue_duration))

    # Sort descending by overdue duration; tie-break by original task order
    task_order = {t: i for i, t in enumerate(tasks)}
    due_tasks.sort(key=lambda item: (-item[1], task_order.get(item[0], 999)))
    return due_tasks


def select_next_task(
    config: dict[str, Any],
    state: dict[str, Any],
    now: float | None = None,
) -> str | None:
    """Select the most overdue task, or None if no task is due."""
    due = get_due_tasks(config, state, now=now)
    if not due:
        return None
    return due[0][0]


def mark_task_done(
    task: str,
    state_path: Path,
    now: float | None = None,
) -> dict[str, Any]:
    """Record completion timestamp for task in state.json."""
    current_time = int(time.time() if now is None else now)
    state = load_state(state_path)
    if "lastRun" not in state:
        state["lastRun"] = {}
    state["lastRun"][task] = current_time
    save_state(state_path, state)
    return state
