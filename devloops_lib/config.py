"""Configuration loader for devloops.

Loads devloops.config.yaml (or devloops.config.json) from the project root
(walks up from cwd or specified directory), falling back to template defaults.
Python stdlib only.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
from pathlib import Path
from typing import Any

AGENT_ID_ENV = "DEVLOOPS_AGENT_ID"
# One path component on every OS: no separators, no leading or trailing dots.
AGENT_ID_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")

DEFAULT_CONFIG: dict[str, Any] = {
    "project": {
        "repo": "owner/name",
        "default_branch": "main",
        "base_branch": "main",
    },
    "commands": {
        "test": "",
        "lint": "",
        "build": "",
    },
    "protected_paths": [],
    "cleanup": {
        "protected_branches": ["main", "master", "develop", "release"],
    },
    "state_dir": ".devloops",
    "labels": {
        "producer": "auto:producer",
        "validated": "validated",
        "rejected": "rejected",
        "approved": "approved",
    },
    "producer": {
        "default_period": "24h",
        "max_new_issues_per_run": 3,
        "tasks": [
            "security",
            "e2e",
            "uiux",
            "regression",
            "usage-logs",
            "doc-drift",
            "cleanup",
        ],
        "task_skills": {},
        "task_periods": {
            "cleanup": "7d",
        },
        "task_max_new_issues": {},
    },
    "gatekeeper": {
        "max_issue_validations_per_tick": 1,
    },
    "worker": {
        "max_items_per_tick": 1,
    },
    "scheduler": "shell",
}


def _strip_comment(line: str) -> str:
    in_quote = False
    quote_char = ""
    for i, ch in enumerate(line):
        if ch in ('"', "'"):
            if not in_quote:
                in_quote = True
                quote_char = ch
            elif quote_char == ch:
                in_quote = False
        elif ch == "#" and not in_quote:
            return line[:i]
    return line


def _parse_scalar(val: str) -> Any:
    val = val.strip()
    if not val:
        return ""
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    if val == "[]":
        return []
    if val.startswith("[") and val.endswith("]"):
        items = [x.strip() for x in val[1:-1].split(",") if x.strip()]
        return [_parse_scalar(x) for x in items]
    lower = val.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "none", "~"):
        return None
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def parse_yaml_subset(text: str) -> dict[str, Any]:
    """Parse a minimal YAML subset into nested dicts, lists, and scalars."""
    root: dict[str, Any] = {}
    # stack holds (indent, container, key_name)
    stack: list[tuple[int, Any, str | None]] = [(-1, root, None)]

    for raw_line in text.splitlines():
        line = _strip_comment(raw_line)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()

        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent_indent, container, key_name = stack[-1]

        if content.startswith("- "):
            val = _parse_scalar(content[2:])
            if isinstance(container, list):
                container.append(val)
            elif isinstance(container, dict):
                if len(stack) >= 2 and isinstance(container, dict) and len(container) == 0 and key_name:
                    parent = stack[-2][1]
                    new_list = [val]
                    parent[key_name] = new_list
                    stack[-1] = (parent_indent, new_list, key_name)
                elif key_name and key_name in container and isinstance(container[key_name], list):
                    container[key_name].append(val)
        elif ":" in content:
            key, val = content.split(":", 1)
            key = key.strip()
            val = val.strip()
            if isinstance(container, dict):
                if val == "":
                    new_dict: dict[str, Any] = {}
                    container[key] = new_dict
                    stack.append((indent, new_dict, key))
                else:
                    container[key] = _parse_scalar(val)
    return root


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base."""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def find_project_root(start_dir: Path | str | None = None) -> Path:
    """Find project root by walking up from start_dir (or cwd) for config or .git."""
    current = Path(start_dir).resolve() if start_dir else Path.cwd().resolve()
    for directory in [current, *current.parents]:
        if (directory / "devloops.config.yaml").is_file():
            return directory
        if (directory / "devloops.config.json").is_file():
            return directory
        if (directory / ".git").exists():
            return directory
    return current


def find_config_file(start_dir: Path | str | None = None) -> Path | None:
    """Find devloops.config.yaml or devloops.config.json by walking up."""
    current = Path(start_dir).resolve() if start_dir else Path.cwd().resolve()
    for directory in [current, *current.parents]:
        yaml_file = directory / "devloops.config.yaml"
        if yaml_file.is_file():
            return yaml_file
        json_file = directory / "devloops.config.json"
        if json_file.is_file():
            return json_file
    return None


def load_config(start_dir: Path | str | None = None) -> dict[str, Any]:
    """Load devloops configuration with defaults."""
    cfg_file = find_config_file(start_dir)
    if cfg_file is None:
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        content = cfg_file.read_text(encoding="utf-8")
        if cfg_file.suffix.lower() == ".json":
            data = json.loads(content)
        else:
            data = parse_yaml_subset(content)
        return deep_merge(DEFAULT_CONFIG, data)
    except Exception:
        return copy.deepcopy(DEFAULT_CONFIG)


def validate_agent_id(agent_id: str) -> str:
    """Return agent_id if it is safe as one path component; raise ValueError if not."""
    if not AGENT_ID_PATTERN.fullmatch(agent_id):
        raise ValueError(f"invalid agent id: {agent_id!r}")
    return agent_id


def get_agent_id() -> str | None:
    """Return the validated agent id from the environment, or None when not set."""
    agent_id = os.environ.get(AGENT_ID_ENV)
    return validate_agent_id(agent_id) if agent_id else None


class _AgentIdAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        # Every path resolves through get_state_path, and child processes inherit this.
        os.environ[AGENT_ID_ENV] = values


def add_agent_id_argument(parser: argparse.ArgumentParser) -> None:
    """Add --agent-id, which scopes state, lock, and worktree paths to one agent."""
    parser.add_argument(
        "--agent-id",
        type=validate_agent_id,
        action=_AgentIdAction,
        default=None,
        help=f"Scope state and lock paths to this agent (default: ${AGENT_ID_ENV}, or no scope)",
    )


def _agent_dir(loop_dir: Path) -> Path:
    agent_id = get_agent_id()
    return loop_dir / "agents" / agent_id if agent_id else loop_dir


def get_state_path(loop_name: str, config: dict[str, Any] | None = None, start_dir: Path | str | None = None) -> Path:
    """Return path to <state_dir>/<loop>/state.json, or .../agents/<id>/state.json with an agent id."""
    env = os.environ.get("LOOP_STATE_DIR")
    if env:
        p = Path(env)
        base = p.parent if p.name == "state.json" else p
        return _agent_dir(base if base.name == loop_name else base / loop_name) / "state.json"

    cfg = config or load_config(start_dir=start_dir)
    state_dir = cfg.get("state_dir", ".devloops")
    root = find_project_root(start_dir=start_dir)
    return (_agent_dir(root / state_dir / loop_name) / "state.json").resolve()


def get_claims_dir(config: dict[str, Any] | None = None, start_dir: Path | str | None = None) -> Path:
    """Return <state_dir>/claims. All agents share it, so it has no agent scope."""
    env = os.environ.get("LOOP_STATE_DIR")
    if env:
        p = Path(env)
        return (p.parent if p.name == "state.json" else p) / "claims"

    cfg = config or load_config(start_dir=start_dir)
    root = find_project_root(start_dir=start_dir)
    return (root / cfg.get("state_dir", ".devloops") / "claims").resolve()


def get_worktree_path(main: Path | str, role: str = "worker") -> Path:
    """Return <main>/../<main name>.worktrees/<role>, or <role>-<id> with an agent id.

    worktree-setup.md and the gatekeeper SKILL document the same form in shell.
    """
    main = Path(main)
    agent_id = get_agent_id()
    return main.parent / f"{main.name}.worktrees" / (f"{role}-{agent_id}" if agent_id else role)


def get_lock_path(loop_name: str, config: dict[str, Any] | None = None, start_dir: Path | str | None = None) -> Path:
    """Return path to <state_dir>/<loop>/tick.lock."""
    state_file = get_state_path(loop_name, config=config, start_dir=start_dir)
    return state_file.parent / "tick.lock"
