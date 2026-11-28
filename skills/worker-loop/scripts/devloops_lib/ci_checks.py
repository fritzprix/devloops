"""Shared CI check deduplication across worker and gatekeeper loops (issue #116).

Judges CI from the latest run of each workflow only:
- Check runs are grouped by workflow (or check name when workflowName is absent).
- Within a workflow, checks are grouped into runs by Actions run id (from
  detailsUrl/targetUrl) or checkSuite; start time decides first, run id second.
- A run where every job is SKIPPED is ignored (not a CI result).
- A newer queued/in-progress run supersedes an older green run.
- A cancelled run with no later run remains a failure.
- Status contexts keep their latest result per context name.
"""
from __future__ import annotations

import re
from typing import Any

FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", "REQUESTED"}

_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)\b")


def check_state(check: dict[str, Any]) -> str:
    return (check.get("conclusion") or check.get("state") or check.get("status") or "").upper()


def check_run_ts(check: dict[str, Any]) -> str:
    for field in ("startedAt", "createdAt", "completedAt"):
        val = str(check.get(field) or "")
        if val and not val.startswith("0001-01-01"):
            return val
    return ""


def _extract_run_id(check: dict[str, Any]) -> int | None:
    for field in ("detailsUrl", "targetUrl", "url"):
        val = str(check.get(field) or "")
        match = _RUN_ID_RE.search(val)
        if match:
            return int(match.group(1))
    return None


def _is_status_context(check: dict[str, Any]) -> bool:
    if check.get("__typename") == "StatusContext":
        return True
    return "context" in check and "name" not in check


def effective_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the effective checks from the latest non-skipped run of each workflow."""
    if not checks:
        return []

    status_latest: dict[str, tuple[str, int, dict[str, Any]]] = {}
    # workflow_key -> run_key -> list of (idx, check, ts, run_id)
    workflows: dict[str, dict[Any, list[tuple[int, dict[str, Any], str, int]]]] = {}

    for idx, check in enumerate(checks):
        ts = check_run_ts(check)
        if _is_status_context(check):
            ctx = str(check.get("context") or check.get("name") or f"__ctx_{idx}")
            prev = status_latest.get(ctx)
            if prev is None or (ts, idx) >= (prev[0], prev[1]):
                status_latest[ctx] = (ts, idx, check)
            continue

        workflow = str(check.get("workflowName") or "")
        name = str(check.get("name") or check.get("context") or f"__chk_{idx}")
        wf_key = f"wf:{workflow}" if workflow else f"name:{name}"

        run_id = _extract_run_id(check)
        if run_id is not None:
            run_key: Any = ("run", run_id)
            run_id_num = run_id
        elif check.get("checkSuite") is not None:
            suite = check["checkSuite"]
            suite_id = suite.get("id") if isinstance(suite, dict) else suite
            run_key = ("suite", str(suite_id))
            run_id_num = 0
        else:
            run_key = ("single", idx)
            run_id_num = 0

        workflows.setdefault(wf_key, {}).setdefault(run_key, []).append((idx, check, ts, run_id_num))

    kept: list[tuple[int, dict[str, Any]]] = [
        (idx, check) for (_, idx, check) in status_latest.values()
    ]

    for runs in workflows.values():
        best_rank: tuple[str, int, int] | None = None
        best_items: list[tuple[int, dict[str, Any], str]] = []
        for items in runs.values():
            if all(check_state(c) == "SKIPPED" for _, c, _, _ in items):
                continue
            max_ts = max(ts for _, _, ts, _ in items)
            max_run_id = max(rid for _, _, _, rid in items)
            max_idx = max(i for i, _, _, _ in items)
            rank = (max_ts, max_run_id, max_idx)
            if best_rank is None or _run_newer(rank, best_rank):
                best_rank = rank
                best_items = [(i, c, ts) for i, c, ts, _ in items]
        # S1.1: within the chosen run, keep the latest entry per job name (re-run attempts).
        latest_in_run: dict[str, tuple[str, int, dict[str, Any]]] = {}
        for i, c, ts in best_items:
            name = str(c.get("name") or c.get("context") or f"__chk_{i}")
            prev = latest_in_run.get(name)
            if prev is None or (ts, i) >= (prev[0], prev[1]):
                latest_in_run[name] = (ts, i, c)
        kept.extend((i, c) for (_, i, c) in latest_in_run.values())

    kept.sort(key=lambda pair: pair[0])
    return [c for _, c in kept]


def _run_newer(cand: tuple[str, int, int], best: tuple[str, int, int]) -> bool:
    """Compare two runs: start time first when both started, else run id, else index."""
    c_ts, c_rid, c_idx = cand
    b_ts, b_rid, b_idx = best
    if c_ts and b_ts:
        return (c_ts, c_rid, c_idx) > (b_ts, b_rid, b_idx)
    if c_rid and b_rid:
        return (c_rid, c_idx) > (b_rid, b_idx)
    return c_idx > b_idx


def summarize_checks(checks: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Return (failing_names, pending_names) from effective_checks(checks)."""
    eff = effective_checks(checks)
    failing = [c.get("name") or c.get("context") for c in eff if check_state(c) in FAILED]
    pending = [c.get("name") or c.get("context") for c in eff if check_state(c) in PENDING]
    return failing, pending
