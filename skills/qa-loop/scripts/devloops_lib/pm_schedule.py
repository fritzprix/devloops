"""PM schedule exclusivity: only the PM arms a cron; peers tick when cascaded."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from devloops_lib.config import load_config
from devloops_lib.tick_gate import load_state, save_state

PEER_LOOPS = ("worker-loop", "gatekeeper-loop", "producer-loop")
_PM_LOOP = "pm-loop"


def _scheduler_active(scheduler: Any) -> bool:
    if not isinstance(scheduler, dict):
        return False
    kind = scheduler.get("kind")
    if kind == "process":
        return bool(scheduler.get("id"))
    if kind == "cron":
        return bool(scheduler.get("armed"))
    if kind == "shell":
        return bool(scheduler.get("intervalSeconds") or scheduler.get("armed"))
    return False


def read_loop_scheduler(state_path: Path) -> dict[str, Any] | None:
    state = load_state(state_path)
    sched = state.get("scheduler")
    return sched if _scheduler_active(sched) else None


def pm_scheduler_active(state: dict[str, Any]) -> bool:
    return _scheduler_active(state.get("scheduler"))


def exclusivity_report(
    pm_state: dict[str, Any],
    *,
    state_root: Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return whether peer-loop crons conflict with an armed PM scheduler."""
    cfg = config or load_config()
    root = state_root
    if root is None:
        from devloops_lib.config import find_project_root

        root = find_project_root() / str(cfg.get("state_dir", ".devloops"))

    pm_active = pm_scheduler_active(pm_state)
    peer_status: dict[str, Any] = {}
    conflicts: list[str] = []

    for loop in PEER_LOOPS:
        sched = read_loop_scheduler(root / loop / "state.json")
        peer_status[loop] = {"schedulerActive": sched is not None, "scheduler": sched}
        if pm_active and sched is not None:
            conflicts.append(loop)

    return {
        "pmSchedulerActive": pm_active,
        "peerLoops": peer_status,
        "peerConflicts": conflicts,
        "exclusiveOk": not conflicts,
        "instruction": (
            "Disarm peer-loop shell/cron schedulers while pm-loop is armed; "
            "the PM cascades ticks to worker, gatekeeper, and producer."
            if pm_active
            else "Peer loops may use their own schedulers only when pm-loop is not armed."
        ),
    }


def mark_pm_scheduler(state: dict[str, Any], *, kind: str, **fields: Any) -> dict[str, Any]:
    """Record PM scheduler metadata when the user arms /pm-loop <interval>."""
    state = dict(state)
    scheduler = {"kind": kind, **fields}
    state["scheduler"] = scheduler
    state["peerLoopsDisarmed"] = True
    return state


def clear_pm_scheduler(state: dict[str, Any]) -> dict[str, Any]:
    state = dict(state)
    state.pop("scheduler", None)
    state["peerLoopsDisarmed"] = False
    return state


def write_exclusivity_flags(state_root: Path, pm_active: bool) -> None:
    """Write peer-loop state flags so external schedulers know not to run."""
    flag = {"pmCascadeActive": pm_active, "ownedBy": _PM_LOOP}
    for loop in PEER_LOOPS:
        path = state_root / loop / "state.json"
        state = load_state(path)
        schedule = state.get("schedule") or {}
        schedule["pm"] = flag
        state["schedule"] = schedule
        save_state(path, state)


def peer_tick_allowed(peer_state: dict[str, Any]) -> bool:
    """Return False when a peer loop's own cron must not fire (PM is ticking)."""
    sched = (peer_state.get("schedule") or {}).get("pm") or {}
    if sched.get("pmCascadeActive"):
        return False
    return True


def cascade_plan(
    *,
    slots: int,
    pipeline: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Allocate delegate slots across roles inside the user cap (generic heuristic)."""
    pipeline = pipeline or {}
    approved_backlog = max(0, int(pipeline.get("approvedIssues", 0)))
    review_queue = max(0, int(pipeline.get("prsNeedingReview", 0)))
    producer_due = max(0, int(pipeline.get("producerDue", 0)))

    plan: list[dict[str, Any]] = []
    remaining = max(1, slots)

    if review_queue and remaining:
        plan.append({"role": "gatekeeper-loop", "slots": 1, "reason": "review bottleneck"})
        remaining -= 1
    if approved_backlog and remaining:
        plan.append({"role": "worker-loop", "slots": min(remaining, approved_backlog), "reason": "approved backlog"})
        remaining -= min(remaining, approved_backlog)
    if producer_due and remaining and not pipeline.get("boardFull"):
        plan.append({"role": "producer-loop", "slots": 1, "reason": "producer task due"})
        remaining -= 1
    return plan
