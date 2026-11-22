"""Gatekeeper issue queue and validation verdict management.

Python stdlib only.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from devloops_lib.config import get_state_path, load_config
from devloops_lib.gh import GhError, gh
from devloops_lib.tick_gate import load_state, save_state

VALID_VERDICTS = {"validated", "rejected"}
TASK_LABEL_PATTERN = re.compile(r"^auto:(?!producer$)(.+)$")


def extract_labels(issue: dict[str, Any]) -> set[str]:
    """Extract set of label name strings from issue dict."""
    labels = issue.get("labels") or []
    extracted: set[str] = set()
    for lbl in labels:
        if isinstance(lbl, dict):
            name = lbl.get("name")
            if name:
                extracted.add(name)
        elif isinstance(lbl, str):
            extracted.add(lbl)
    return extracted


def extract_task_from_labels(labels: set[str]) -> str:
    """Infer task name from auto:<task> label."""
    for lbl in labels:
        m = TASK_LABEL_PATTERN.match(lbl)
        if m:
            return m.group(1)
    return "general"


def filter_unjudged_producer_issues(
    issues: list[dict[str, Any]],
    producer_label: str = "auto:producer",
    validated_label: str = "validated",
    rejected_label: str = "rejected",
) -> list[dict[str, Any]]:
    """Return open issues that have producer_label and neither validated nor rejected."""
    unjudged: list[dict[str, Any]] = []
    for issue in issues:
        lbls = extract_labels(issue)
        if producer_label not in lbls:
            continue
        if validated_label in lbls or rejected_label in lbls:
            continue
        task = extract_task_from_labels(lbls)
        item = dict(issue)
        item["task"] = task
        item["label_names"] = list(lbls)
        unjudged.append(item)

    # Sort oldest first by number
    unjudged.sort(key=lambda x: int(x.get("number", 0)))
    return unjudged


def list_unjudged_issues(
    config: dict[str, Any] | None = None,
    repo: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch open issues via gh and filter for unjudged producer issues."""
    cfg = config or load_config()
    cmd = [
        "issue",
        "list",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title,labels,body,createdAt",
    ]
    target_repo = repo or cfg.get("project", {}).get("repo")
    if target_repo and target_repo != "owner/name":
        cmd.extend(["--repo", target_repo])

    res_stdout = gh(*cmd)
    try:
        issues = json.loads(res_stdout) if res_stdout.strip() else []
    except json.JSONDecodeError as e:
        raise GhError(f"Failed to parse gh issue list output: {e}") from e

    producer_lbl = cfg.get("labels", {}).get("producer", "auto:producer")
    validated_lbl = cfg.get("labels", {}).get("validated", "validated")
    rejected_lbl = cfg.get("labels", {}).get("rejected", "rejected")

    return filter_unjudged_producer_issues(
        issues,
        producer_label=producer_lbl,
        validated_label=validated_lbl,
        rejected_label=rejected_lbl,
    )


def apply_verdict(
    issue_number: int,
    verdict: str,
    reason: str,
    evidence: str,
    task: str | None = None,
    config: dict[str, Any] | None = None,
    state_path: Path | None = None,
    apply: bool = False,
    existing_issue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate or reject a producer issue with file:line evidence.

    Never adds labels.approved.
    Never closes issues.
    Never modifies code.
    Records verdict counts per producer task in gatekeeper-loop state.
    """
    v_norm = verdict.strip().lower()
    if v_norm not in VALID_VERDICTS:
        raise ValueError(f"Invalid verdict '{verdict}'. Allowed: {VALID_VERDICTS}")

    cfg = config or load_config()
    approved_label = cfg.get("labels", {}).get("approved", "approved")
    target_label = cfg.get("labels", {}).get(v_norm, v_norm)

    if target_label == approved_label or v_norm == "approved":
        raise ValueError("Gatekeeper issue validation must never add approved label.")

    st_path = state_path or get_state_path("gatekeeper-loop", config=cfg)
    target_repo = cfg.get("project", {}).get("repo")
    repo_arg = target_repo if target_repo and target_repo != "owner/name" else None

    # Fetch issue if not supplied
    if existing_issue is None:
        cmd = ["gh", "issue", "view", str(issue_number), "--json", "number,title,labels,body"]
        if repo_arg:
            cmd.extend(["--repo", repo_arg])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        except Exception as e:
            raise GhError(f"Failed to execute gh issue view: {e}") from e
        if res.returncode != 0:
            raise GhError(f"gh issue view failed (code {res.returncode}): {res.stderr.strip()}")
        issue_data = json.loads(res.stdout)
    else:
        issue_data = existing_issue

    inferred_task = task
    if not inferred_task:
        lbls = extract_labels(issue_data)
        inferred_task = extract_task_from_labels(lbls)

    current_body = issue_data.get("body") or ""
    validation_section = (
        f"\n\n## Gatekeeper Validation\n"
        f"**Verdict**: {v_norm}\n"
        f"**Reason**: {reason}\n"
        f"**Evidence**: {evidence}\n"
    )
    new_body = current_body + validation_section

    result = {
        "issue": issue_number,
        "verdict": v_norm,
        "label": target_label,
        "task": inferred_task,
        "reason": reason,
        "evidence": evidence,
        "dry_run": not apply,
    }

    if apply:
        edit_cmd = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--body",
            new_body,
            "--add-label",
            target_label,
        ]
        if repo_arg:
            edit_cmd.extend(["--repo", repo_arg])
        try:
            edit_res = subprocess.run(edit_cmd, capture_output=True, text=True, encoding="utf-8")
        except Exception as e:
            raise GhError(f"Failed to execute gh issue edit: {e}") from e
        if edit_res.returncode != 0:
            raise GhError(f"gh issue edit failed (code {edit_res.returncode}): {edit_res.stderr.strip()}")

        # Record validated/rejected counts per producer task in gatekeeper state
        state = load_state(st_path)
        stats = state.setdefault("producerStats", {})
        task_stats = stats.setdefault(inferred_task, {"validated": 0, "rejected": 0})
        task_stats[v_norm] = task_stats.get(v_norm, 0) + 1
        save_state(st_path, state)
        result["state_updated"] = True

    return result
