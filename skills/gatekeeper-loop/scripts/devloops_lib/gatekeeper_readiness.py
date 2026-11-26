"""Repo readiness for gatekeeper review: CI, rubrics, and judgment criteria.

Issue #81: the gatekeeper never opens PRs. A gap it discovers (missing CI,
missing or thin rubric) becomes one issue, and each affected PR is
linked/reported against it; a gap never holds back a review verdict.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from devloops_lib.config import find_project_root, load_config

_DEFAULT_RUBRIC_CANDIDATES = (
    "REVIEW_RUBRIC.md",
    "docs/REVIEW_RUBRIC.md",
    "writing/REVIEW_RUBRIC.md",
    ".agents/REVIEW_RUBRIC.md",
    "skills/gatekeeper-loop/references/review-rubric.md",
)

_DEFAULT_ISSUE_RULES_CANDIDATES = (
    "ISSUE_RULES.md",
    "docs/ISSUE_RULES.md",
    "writing/ISSUE_RULES.md",
    ".agents/ISSUE_RULES.md",
)

_REQUIRED_RUBRIC_HINTS = (
    re.compile(r"\b(pass|fail)\b", re.I),
    re.compile(r"criteria|checklist|rubric|acceptance", re.I),
    re.compile(r"procedure|judgment|review", re.I),
)


_WORKFLOW_DIRS = (
    (".github", "workflows"),
    (".forgejo", "workflows"),
    (".gitea", "workflows"),
)


def _workflow_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for parts in _WORKFLOW_DIRS:
        workflows = root.joinpath(*parts)
        if workflows.is_dir():
            found.extend(sorted(workflows.glob("*.yml")))
            found.extend(sorted(workflows.glob("*.yaml")))
    return found


def has_pr_ci_workflow(root: Path) -> bool:
    """True when at least one workflow is likely to run on pull requests."""
    for path in _workflow_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lowered = text.lower()
        if "pull_request" in lowered or "pull-request" in lowered:
            return True
        if "on:" in lowered and "pull" in lowered:
            return True
    return False


def _candidate_paths(root: Path, names: tuple[str, ...], extra: list[str]) -> list[Path]:
    paths: list[Path] = []
    for name in [*extra, *names]:
        p = root / name
        if p.is_file() and p not in paths:
            paths.append(p)
    return paths


def _rubric_quality(text: str) -> str:
    """Return ``present`` or ``insufficient``."""
    stripped = text.strip()
    if len(stripped) < 120:
        return "insufficient"
    hits = sum(1 for pat in _REQUIRED_RUBRIC_HINTS if pat.search(stripped))
    if hits < 2:
        return "insufficient"
    return "present"


def assess_rubric(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    gk = config.get("gatekeeper") or {}
    rubric_paths = _candidate_paths(
        root,
        tuple(gk.get("rubric_paths") or ()),
        list(_DEFAULT_RUBRIC_CANDIDATES),
    )
    issue_paths = _candidate_paths(
        root,
        tuple(gk.get("issue_rules_paths") or ()),
        list(_DEFAULT_ISSUE_RULES_CANDIDATES),
    )
    primary = rubric_paths[0] if rubric_paths else None
    if primary is None and issue_paths:
        primary = issue_paths[0]

    if primary is None:
        return {
            "status": "missing",
            "path": None,
            "paths_found": [],
            "issue_rules": [str(p.relative_to(root)) for p in issue_paths],
            "action": "open_issue",
            "goal": "File one issue: add a REVIEW_RUBRIC.md with checkable criteria and judgment procedure.",
        }

    try:
        body = primary.read_text(encoding="utf-8")
    except OSError:
        return {"status": "missing", "path": str(primary), "action": "open_issue", "goal": "File one issue: restore readable rubric file."}

    quality = _rubric_quality(body)
    rel = str(primary.relative_to(root))
    if quality == "present":
        return {
            "status": "present",
            "path": rel,
            "paths_found": [str(p.relative_to(root)) for p in rubric_paths],
            "issue_rules": [str(p.relative_to(root)) for p in issue_paths],
            "action": None,
        }
    return {
        "status": "insufficient",
        "path": rel,
        "action": "open_issue",
        "goal": "File one issue: expand the rubric with explicit pass/fail criteria and judgment procedure.",
    }


def assess_ci(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    present = has_pr_ci_workflow(root)
    workflows = [str(p.relative_to(root)) for p in _workflow_files(root)]
    if present:
        return {
            "present": True,
            "workflows": workflows,
            "action": None,
            "goal": None,
        }
    commands = config.get("commands") or {}
    hints = [k for k, v in commands.items() if v]
    return {
        "present": False,
        "workflows": workflows,
        "action": "open_issue",
        "goal": "File one issue: add a pull_request workflow "
        f"(lint/test/build: {', '.join(hints) or 'detect from repo'}); affected PRs wait clean, never changes-requested for this cause.",
    }


def assess_readiness(start_dir: Path | str | None = None, config: dict | None = None) -> dict[str, Any]:
    """Return CI/rubric gap assessment for this tick (issue #81).

    Gaps are reported for the gatekeeper to file one issue per gap; they never
    gate review verdicts. The gatekeeper never opens PRs itself.
    """
    cfg = config or load_config(start_dir=start_dir)
    root = find_project_root(start_dir)
    ci = assess_ci(root, cfg)
    rubric = assess_rubric(root, cfg)

    gaps: list[dict[str, Any]] = []
    if not ci["present"]:
        gaps.append({"kind": "ci", "priority": 0, **{k: ci[k] for k in ("action", "goal", "workflows")}})
    if rubric["status"] in ("missing", "insufficient"):
        gaps.append({"kind": "rubric", "priority": 1, "status": rubric["status"],
                     "path": rubric.get("path"), **{k: rubric[k] for k in ("action", "goal")}})

    gaps.sort(key=lambda g: g["priority"])

    return {
        "principle": (
            "Always review: gaps outside the PR (CI, rubrics) are filed as "
            "issues, never verdicts held back. The gatekeeper files issues and "
            "comments; it never opens PRs."
        ),
        "ci": ci,
        "rubric": rubric,
        "gaps": gaps,
    }
