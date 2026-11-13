"""Project induction for pm-loop: policy priority without baking repo rules into PM."""
from __future__ import annotations

import json
from typing import Any

POLICY_PRIORITY = ("skill", "config", "profile", "folklore")


def resolve_policy_source(conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the winning policy source when multiple disagree (skill wins over folklore)."""
    if not conflicts:
        return {"winner": None, "ordered": list(POLICY_PRIORITY)}
    ranked = sorted(conflicts, key=lambda c: POLICY_PRIORITY.index(c["source"]))
    winner = ranked[0]
    return {
        "winner": winner,
        "ordered": list(POLICY_PRIORITY),
        "losers": [c for c in ranked[1:]],
    }


def infer_project_type(signals: dict[str, Any]) -> str:
    """Classify project as software, research, writing, or mixed (default heuristic only)."""
    scores = {"software": 0, "research": 0, "writing": 0}
    if signals.get("hasCi"):
        scores["software"] += 2
    if signals.get("issueCount", 0) > 5:
        scores["software"] += 1
    if signals.get("markdownRatio", 0) > 0.5:
        scores["writing"] += 2
    if signals.get("notebookCount", 0) > 0:
        scores["research"] += 2
    top = max(scores, key=scores.get)
    if sum(1 for v in scores.values() if v == scores[top]) > 1:
        return "mixed"
    return top


def build_project_profile(
    *,
    config: dict[str, Any],
    signals: dict[str, Any] | None = None,
    skill_refs: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a durable profile from config + repo signals; never infer budget or merge authority."""
    signals = signals or {}
    skill_refs = skill_refs or {
        "worker": "worker-loop/SKILL.md",
        "gatekeeper": "gatekeeper-loop/SKILL.md",
        "producer": "producer-loop/SKILL.md",
    }
    project = config.get("project") or {}
    return {
        "projectType": infer_project_type(signals),
        "repo": project.get("repo"),
        "baseBranch": project.get("base_branch", project.get("default_branch")),
        "labels": dict(config.get("labels") or {}),
        "roleSkills": skill_refs,
        "policyOrder": list(POLICY_PRIORITY),
        "humanOnly": ["approved", "merge", "auto-merge"],
        "note": (
            "Role skills and devloops.config.yaml override informal comment folklore. "
            "PM never sets approved, never merges, and never infers slot budget or delegation."
        ),
        "signals": signals,
    }


def induction_required(state: dict[str, Any], *, ambiguous: bool = False) -> bool:
    if "projectProfile" not in state:
        return True
    if ambiguous and not state.get("profileConfirmed"):
        return True
    return False


def apply_profile(state: dict[str, Any], profile: dict[str, Any], *, confirm: bool = False) -> dict[str, Any]:
    state = dict(state)
    state["projectProfile"] = profile
    if confirm:
        state["profileConfirmed"] = True
    return state


def profile_for_display(profile: dict[str, Any]) -> str:
    return json.dumps(profile, indent=2, ensure_ascii=False)
