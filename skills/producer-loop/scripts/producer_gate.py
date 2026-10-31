#!/usr/bin/env python3
"""Preflight gate and task scheduler for producer-loop.

Usage:
  python3 producer_gate.py --next [--lock] [--pid PID]
  python3 producer_gate.py --done <task> [--unlock]
  python3 producer_gate.py --lock [--pid PID]
  python3 producer_gate.py --unlock
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root and devloops_lib to sys.path
_script_dir = Path(__file__).resolve().parent
for _candidate in (_script_dir.parents[2], _script_dir.parents[3], _script_dir.parents[3] / ".agents"):
    if (_candidate / "devloops_lib").is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from devloops_lib.config import get_state_path, load_config
from devloops_lib.producer import mark_task_done, select_next_task
from devloops_lib.tick_gate import (
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_NOOP,
    EXIT_RUN,
    acquire_lock,
    check_lock,
    load_state,
    release_lock,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--next", action="store_true", help="Print the most overdue task or NOOP")
    parser.add_argument("--task", type=str, default=None, help="Specific task to run")
    parser.add_argument("--done", type=str, default=None, help="Mark specified task as done")
    parser.add_argument("--lock", action="store_true", help="Acquire tick.lock before exiting 0")
    parser.add_argument("--unlock", action="store_true", help="Release tick.lock")
    parser.add_argument("--pid", type=int, default=None, help="Process ID to record in tick.lock")
    parser.add_argument("--state-file", type=Path, default=None, help="Path to state.json")
    args = parser.parse_args(argv)

    cfg = load_config()
    state_path = args.state_file or get_state_path("producer-loop", config=cfg)
    lock_path = state_path.parent / "tick.lock"

    try:
        # 1. Handle --done <task>
        if args.done:
            mark_task_done(args.done, state_path)
            print(f"DONE: {args.done}")
            if args.unlock:
                release_lock(lock_path)
                print("UNLOCKED")
            return EXIT_RUN

        # 2. Handle unlock-only
        if args.unlock and not args.next and not args.task and not args.lock:
            release_lock(lock_path)
            print("UNLOCKED")
            return EXIT_RUN

        # 3. Check active lock before proceeding with --lock, --next, or --task
        if args.lock or args.next or args.task:
            if check_lock(lock_path):
                print("BUSY (tick in progress)")
                return EXIT_BUSY

        # 4. Handle --next
        if args.next:
            state = load_state(state_path)
            task = select_next_task(cfg, state)
            if not task:
                print("NOOP")
                return EXIT_NOOP

            if args.lock:
                acquire_lock(lock_path, pid=args.pid)
            print(task)
            return EXIT_RUN

        # 5. Handle specific task
        if args.task:
            if args.lock:
                acquire_lock(lock_path, pid=args.pid)
            print(args.task)
            return EXIT_RUN

        # 6. Lock-only
        if args.lock:
            acquire_lock(lock_path, pid=args.pid)
            print("LOCKED")
            return EXIT_RUN

        # If --unlock was specified along with other flags
        if args.unlock:
            release_lock(lock_path)
            print("UNLOCKED")
            return EXIT_RUN

        return EXIT_RUN

    except Exception as e:
        sys.stderr.write(f"producer_gate error: {e}\n")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
