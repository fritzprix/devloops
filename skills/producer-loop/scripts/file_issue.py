#!/usr/bin/env python3
"""File candidate issues with deduplication, fingerprinting, and rate caps.

Usage:
  python3 file_issue.py --task security --key "hardcoded-secret:auth.py:42" --title "Hardcoded secret in auth.py" --body "..." [--create]
  python3 file_issue.py --task cleanup --findings findings.json [--create]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root and devloops_lib to sys.path
_script_dir = Path(__file__).resolve().parent
for _candidate in (_script_dir.parents[2], _script_dir.parents[3], _script_dir.parents[3] / ".agents"):
    if (_candidate / "devloops_lib").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from devloops_lib.config import add_agent_id_argument, get_state_path, load_config
from devloops_lib.file_issue import file_producer_issues
from devloops_lib.tick_gate import GhError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=str, required=True, help="Task name (e.g. security, cleanup)")
    parser.add_argument("--key", type=str, default="", help="Stable finding key")
    parser.add_argument("--title", type=str, default="", help="Issue title")
    parser.add_argument("--body", type=str, default="", help="Issue body content")
    parser.add_argument("--body-file", type=Path, default=None, help="File containing issue body")
    parser.add_argument("--findings", type=str, default=None, help="Path to JSON file or JSON array of findings")
    parser.add_argument("--create", action="store_true", help="Actually create issues on GitHub (default: dry run)")
    parser.add_argument("--max-new", type=int, default=None, help="Override max new issues per run")
    parser.add_argument("--state-file", type=Path, default=None, help="Path to state.json")
    add_agent_id_argument(parser)
    args = parser.parse_args(argv)

    findings: list[dict] = []
    if args.findings:
        f_path = Path(args.findings)
        if f_path.is_file():
            content = f_path.read_text(encoding="utf-8")
        else:
            content = args.findings
        findings = json.loads(content)
    else:
        body = args.body
        if args.body_file and args.body_file.is_file():
            body = args.body_file.read_text(encoding="utf-8")
        if not args.key or not args.title:
            sys.stderr.write("Error: --key and --title are required when --findings is not used.\n")
            return 1
        findings = [{"task": args.task, "key": args.key, "title": args.title, "body": body}]

    cfg = load_config()
    state_path = args.state_file or get_state_path("producer-loop", config=cfg)

    try:
        result = file_producer_issues(
            findings=findings,
            task=args.task,
            config=cfg,
            state_path=state_path,
            create=args.create,
            max_new=args.max_new,
        )

        for item in result["created"]:
            mode = "CREATED" if args.create else "DRY-RUN"
            print(f"[{mode}] {item['title']} (task: {item['task']}, fp: {item['fp'][:12]})")
            print(f"  Labels: {', '.join(item['labels'])}")

        for item in result["skipped"]:
            print(f"[SKIPPED] {item.get('title', item.get('key'))}: {item['reason']} (fp: {item['fp'][:12]})")

        return 0
    except GhError as e:
        sys.stderr.write(f"GitHub CLI error: {e}\n")
        return 2
    except Exception as e:
        sys.stderr.write(f"Unexpected error: {e}\n")
        return 3


if __name__ == "__main__":
    sys.exit(main())
