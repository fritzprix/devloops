"""Issue creation, deduplication, and fingerprinting for producer loop.

Python stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from devloops_lib.config import get_state_path, load_config
from devloops_lib.producer import (
    DEFAULT_MAX_NEW_ISSUES_PER_RUN,
    FP_MARKER_PREFIX,
    FP_MARKER_REGEX,
    compute_fingerprint,
)
from devloops_lib.gh import GhError, gh
from devloops_lib.tick_gate import load_state, save_state


def get_existing_issue_fingerprints(repo: str | None = None) -> set[str]:
    """Query gh for open and closed issues and extract producer fingerprint markers."""
    cmd = ["issue", "list", "--state", "all", "--limit", "1000", "--json", "number,body"]
    if repo:
        cmd.extend(["--repo", repo])
    res_stdout = gh(*cmd)
    try:
        issues = json.loads(res_stdout) if res_stdout.strip() else []
    except json.JSONDecodeError as e:
        raise GhError(f"Failed to parse gh issue list output: {e}") from e

    found: set[str] = set()
    for issue in issues:
        body = issue.get("body") or ""
        for match in FP_MARKER_REGEX.finditer(body):
            found.add(match.group(1).lower())
    return found


def get_max_new_issues(
    task: str,
    config: dict[str, Any],
    cli_override: int | None = None,
) -> int:
    """Determine maximum new issues to file in a single run."""
    if cli_override is not None:
        return cli_override

    producer_cfg = config.get("producer", {})
    # Check per-task overrides
    task_max = producer_cfg.get("task_max_new_issues") or producer_cfg.get("max_new_issues_per_task") or {}
    if task in task_max:
        return int(task_max[task])
    if task in producer_cfg and isinstance(producer_cfg[task], dict) and "max_new_issues_per_run" in producer_cfg[task]:
        return int(producer_cfg[task]["max_new_issues_per_run"])

    return int(producer_cfg.get("max_new_issues_per_run", DEFAULT_MAX_NEW_ISSUES_PER_RUN))


def create_github_issue(
    title: str,
    body: str,
    labels: list[str],
    repo: str | None = None,
) -> str:
    """Invoke gh issue create and return stdout."""
    cmd = ["issue", "create", "--title", title, "--body", body]
    for lbl in labels:
        cmd.extend(["--label", lbl])
    if repo:
        cmd.extend(["--repo", repo])

    return gh(*cmd).strip()


def file_producer_issues(
    findings: list[dict[str, Any]],
    task: str,
    config: dict[str, Any] | None = None,
    state_path: Path | None = None,
    create: bool = False,
    max_new: int | None = None,
    existing_fps: set[str] | None = None,
) -> dict[str, Any]:
    """Process findings: dedup against state and issue markers, cap, and optionally create."""
    cfg = config or load_config()
    st_path = state_path or get_state_path("producer-loop", config=cfg)
    state = load_state(st_path)

    # State fingerprints: support both dict and list in state
    raw_fps = state.get("fingerprints", {})
    if isinstance(raw_fps, dict):
        state_fps = {k.lower() for k in raw_fps.keys()}
    elif isinstance(raw_fps, list):
        state_fps = {str(k).lower() for k in raw_fps}
    else:
        state_fps = set()

    # Query issue markers from GitHub if not passed in
    if existing_fps is None:
        repo = cfg.get("project", {}).get("repo")
        # Only query if not owner/name placeholder
        if repo and repo != "owner/name":
            existing_fps = get_existing_issue_fingerprints(repo=repo)
        else:
            existing_fps = get_existing_issue_fingerprints()
    else:
        existing_fps = {fp.lower() for fp in existing_fps}

    limit = get_max_new_issues(task, cfg, cli_override=max_new)
    producer_label = cfg.get("labels", {}).get("producer", "auto:producer")
    task_label = f"auto:{task}"
    labels = [producer_label, task_label]

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    created_count = 0

    for finding in findings:
        f_task = finding.get("task", task)
        key = finding.get("key", "")
        title = finding.get("title", "Automated finding")
        body = finding.get("body", "")

        fp = compute_fingerprint(f_task, key).lower()
        marker = f"<!-- producer:fp={fp} -->"

        if fp in state_fps:
            skipped.append({"fp": fp, "key": key, "title": title, "reason": "fingerprint in state"})
            continue

        if fp in existing_fps:
            skipped.append({"fp": fp, "key": key, "title": title, "reason": "fingerprint in existing issue"})
            continue

        if created_count >= limit:
            skipped.append({"fp": fp, "key": key, "title": title, "reason": f"cap reached (max {limit})"})
            continue

        # Format body with fingerprint marker if missing
        final_body = body
        if marker not in final_body:
            final_body = f"{final_body.rstrip()}\n\n{marker}\n"

        finding_labels = finding.get("labels") or labels

        item = {
            "task": f_task,
            "key": key,
            "fp": fp,
            "title": title,
            "body": final_body,
            "labels": finding_labels,
            "dry_run": not create,
        }

        if create:
            repo = cfg.get("project", {}).get("repo")
            repo_arg = repo if repo and repo != "owner/name" else None
            out = create_github_issue(title, final_body, finding_labels, repo=repo_arg)
            item["output"] = out

            # Update state fingerprints
            if not isinstance(state.get("fingerprints"), dict):
                state["fingerprints"] = {}
            state["fingerprints"][fp] = {
                "task": f_task,
                "key": key,
                "created_at": int(time.time()),
            }
            state_fps.add(fp)

        created_count += 1
        created.append(item)

    if create and created:
        save_state(st_path, state)

    return {
        "task": task,
        "created": created,
        "skipped": skipped,
        "dry_run": not create,
        "cap": limit,
    }
