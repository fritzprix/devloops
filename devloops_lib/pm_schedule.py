"""PM schedule exclusivity: only the PM arms a cron; peers tick when cascaded."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from devloops_lib.config import load_config, peer_loops as configured_peer_loops
from devloops_lib.tick_gate import load_state, save_state

# Default peer loops; the filled scaffolding (pm.peer_loops) is the source of
# truth (issue #71 deliverable 4, epic #102). Kept for back-compat imports.
PEER_LOOPS = ("worker-loop", "gatekeeper-loop", "qa-loop", "producer-loop")
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
    cfg = config if config is not None else load_config()
    root = state_root
    if root is None:
        from devloops_lib.config import find_project_root

        root = find_project_root() / str(cfg.get("state_dir", ".devloops"))

    pm_active = pm_scheduler_active(pm_state)
    peer_status: dict[str, Any] = {}
    conflicts: list[str] = []

    for loop in configured_peer_loops(cfg):
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
            "the PM cascades ticks to worker, gatekeeper, qa, and producer."
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


def write_exclusivity_flags(
    state_root: Path,
    pm_active: bool,
    *,
    config: dict[str, Any] | None = None,
) -> None:
    """Write peer-loop state flags so external schedulers know not to run."""
    flag = {"pmCascadeActive": pm_active, "ownedBy": _PM_LOOP}
    for loop in configured_peer_loops(config if config is not None else load_config()):
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


# Fixed base order for cascade allocation; round-robin rotates through this
# tuple when the slot budget is smaller than the number of roles with work (#118).
CASCADE_ROLES = ("gatekeeper-loop", "worker-loop", "qa-loop", "producer-loop")


def cascade_plan(
    *,
    slots: int,
    pipeline: dict[str, int] | None = None,
    state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Allocate delegate slots across roles inside the user cap (generic heuristic).

    Counters come from devloops_lib.pm_pipeline (issue #71). A worker slot is
    armed by either an approved issue backlog or open PRs that need worker
    action, so PR-service keeps the loop alive even with an empty issue board.
    Parked work (escalate, needs-human, wait-ci) is counted nowhere and never
    cascades.

    When the slot budget is smaller than the number of roles with work, roles
    are chosen in round-robin order starting after ``state["lastCascadeRole"]``
    (starting at ``gatekeeper-loop`` when absent) so no role starves (#118).
    When ``state`` is provided and at least one role is planned,
    ``state["lastCascadeRole"]`` is updated in place to the last planned role.
    """
    pipeline = pipeline or {}
    approved_backlog = max(0, int(pipeline.get("approvedIssues", 0)))
    pr_work = max(0, int(pipeline.get("prsNeedingWorker", 0)))
    review_queue = max(0, int(pipeline.get("prsNeedingReview", 0)))
    qa_queue = max(0, int(pipeline.get("prsNeedingQa", 0)))
    producer_due = max(0, int(pipeline.get("producerDue", 0)))

    worker_backlog = approved_backlog + pr_work
    worker_reason = (
        "approved backlog"
        if not pr_work
        else ("PR work" if not approved_backlog else "approved backlog + PR work")
    )

    active: dict[str, dict[str, Any]] = {}
    if review_queue:
        active["gatekeeper-loop"] = {"role": "gatekeeper-loop", "slots": 1, "reason": "review bottleneck"}
    if worker_backlog:
        active["worker-loop"] = {"role": "worker-loop", "slots": 1, "reason": worker_reason}
    if qa_queue:
        active["qa-loop"] = {"role": "qa-loop", "slots": 1, "reason": "qa review queue"}
    if producer_due and not pipeline.get("boardFull"):
        active["producer-loop"] = {"role": "producer-loop", "slots": 1, "reason": "producer task due"}

    if not active:
        return []

    budget = max(1, slots)
    if budget >= len(active):
        plan = [dict(active[role]) for role in CASCADE_ROLES if role in active]
        extra = budget - len(plan)
        if extra > 0 and "worker-loop" in active and worker_backlog > 1:
            for item in plan:
                if item["role"] == "worker-loop":
                    item["slots"] += min(extra, worker_backlog - 1)
                    break
    else:
        last_role = state.get("lastCascadeRole") if isinstance(state, dict) else None
        if last_role in CASCADE_ROLES:
            start = (CASCADE_ROLES.index(last_role) + 1) % len(CASCADE_ROLES)
        else:
            start = 0
        rotated = CASCADE_ROLES[start:] + CASCADE_ROLES[:start]
        plan = [dict(active[role]) for role in rotated if role in active][:budget]

    if isinstance(state, dict) and plan:
        state["lastCascadeRole"] = plan[-1]["role"]
    return plan
