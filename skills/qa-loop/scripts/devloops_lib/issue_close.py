"""Find open issues complete under the project definition of done."""
from __future__ import annotations

import re
from typing import Any

POLICY_ORDER = ("skill", "config", "profile", "folklore")

CLOSING_PATTERN = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.I)
BRANCH_ISSUE_PATTERN = re.compile(r"-(\d{3,})\b")

PM_CLOSE_MARKER = "<!-- pm-loop: close-complete -->"

DEFAULT_DONE_SIGNALS: dict[str, list[str]] = {
    "software": ["merged_pr"],
    "writing": ["approved_manuscript"],
    "research": ["experiment_recorded"],
    "mixed": ["board_marked_complete"],
}


def resolve_definition_of_done(
    config: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve active done signals from config then induced profile (skills are applied by the agent)."""
    pm = config.get("pm") or {}
    cfg_raw = pm.get("definition_of_done") or pm.get("definitionOfDone")
    signals: list[str] | None = None

    if isinstance(cfg_raw, str) and cfg_raw.strip():
        signals = [cfg_raw.strip()]
    elif isinstance(cfg_raw, list):
        signals = [str(s) for s in cfg_raw if str(s).strip()]
    elif isinstance(cfg_raw, dict):
        raw_signals = cfg_raw.get("signals")
        if isinstance(raw_signals, list) and raw_signals:
            signals = [str(s) for s in raw_signals]

    source = "config"
    if not signals and profile:
        prof = profile.get("definitionOfDone")
        if isinstance(prof, list) and prof:
            signals = [str(s) for s in prof]
            source = "profile"
        elif isinstance(prof, dict) and prof.get("signals"):
            signals = [str(s) for s in prof["signals"]]
            source = "profile"

    if not signals:
        ptype = (profile or {}).get("projectType", "software")
        signals = list(DEFAULT_DONE_SIGNALS.get(str(ptype), DEFAULT_DONE_SIGNALS["mixed"]))
        source = "profile"

    return {
        "signals": signals,
        "source": source,
        "policyOrder": list(POLICY_ORDER),
    }


def profile_default_definition_of_done(project_type: str) -> list[str]:
    return list(DEFAULT_DONE_SIGNALS.get(project_type, DEFAULT_DONE_SIGNALS["mixed"]))


def issue_numbers_linked(item: dict[str, Any]) -> set[int]:
    explicit = item.get("closesIssues") or item.get("closes_issues") or []
    nums = {int(n) for n in explicit if str(n).isdigit()}
    text = f"{item.get('title') or ''} {item.get('body') or item.get('bodyText') or ''}"
    nums |= {int(n) for n in CLOSING_PATTERN.findall(text)}
    nums |= {int(n) for n in BRANCH_ISSUE_PATTERN.findall(item.get("headRefName") or item.get("branch") or "")}
    return nums


def _signal_satisfied(item: dict[str, Any], signal: str) -> bool:
    done = item.get("doneSignals")
    if isinstance(done, dict) and signal in done:
        return bool(done[signal])

    if signal == "merged_pr":
        return item.get("merged") is True or str(item.get("state", "")).upper() == "MERGED"
    if signal == "approved_manuscript":
        return item.get("approvedManuscript") is True or item.get("manuscriptApproved") is True
    if signal == "experiment_recorded":
        return item.get("experimentRecorded") is True or item.get("experimentComplete") is True
    if signal == "board_marked_complete":
        return item.get("complete") is True or item.get("done") is True
    return item.get(signal) is True


def work_item_meets_definition(item: dict[str, Any], signals: list[str]) -> tuple[bool, list[str]]:
    matched = [s for s in signals if _signal_satisfied(item, s)]
    return bool(matched), matched


def is_open_issue(issue: dict[str, Any]) -> bool:
    state = str(issue.get("state", "OPEN")).upper()
    return state not in {"CLOSED", "DONE"}


def close_comment(issue_number: int, evidence: dict[str, Any]) -> str:
    ref = evidence.get("url") or ""
    label = evidence.get("title") or evidence.get("label") or ""
    lines = [
        PM_CLOSE_MARKER,
        f"pm-loop: Closing issue #{issue_number} under the project definition of done.",
    ]
    if label:
        lines.append(f"Evidence: {label}")
    if ref:
        lines.append(ref)
    return "\n".join(lines)


def closable_issues(
    open_issues: list[dict[str, Any]],
    work_items: list[dict[str, Any]],
    *,
    definition_of_done: dict[str, Any] | None = None,
    signals: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return open issues judged complete under the active definition of done."""
    active = signals or (definition_of_done or {}).get("signals") or DEFAULT_DONE_SIGNALS["software"]
    open_by_number = {
        int(i["number"]): i for i in open_issues if is_open_issue(i) and "number" in i
    }
    seen: set[int] = set()
    actions: list[dict[str, Any]] = []

    for item in work_items:
        meets, matched = work_item_meets_definition(item, active)
        if not meets:
            continue
        for num in issue_numbers_linked(item):
            if num not in open_by_number or num in seen:
                continue
            seen.add(num)
            evidence = {
                "number": item.get("number"),
                "id": item.get("id"),
                "url": item.get("url") or item.get("htmlUrl") or item.get("webUrl"),
                "title": item.get("title"),
                "label": item.get("title") or item.get("id"),
                "matchedSignals": matched,
            }
            actions.append(
                {
                    "issueNumber": num,
                    "evidence": evidence,
                    "matchedSignals": matched,
                    "comment": close_comment(num, evidence),
                    "action": "close_with_comment",
                }
            )
    return sorted(actions, key=lambda a: a["issueNumber"])
