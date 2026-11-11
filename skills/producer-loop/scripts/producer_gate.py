#!/usr/bin/env python3
"""Preflight gate and task scheduler for producer-loop.

Usage:
  python3 producer_gate.py --next [--lock] [--pid PID]
  python3 producer_gate.py --done <task> [--unlock --token TOKEN]
  python3 producer_gate.py --lock [--pid PID]
  python3 producer_gate.py --unlock --token TOKEN

`--lock` prints `TOKEN <token>` after the task; `--unlock` releases only the lock
that token owns (under `devloops tick`, the wrapper's token comes from the
environment and the wrapper keeps the lock). `--force-unlock` is for operators.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# The skill ships its own copy of devloops_lib next to this script.
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from devloops_lib.config import add_agent_id_argument, get_state_path, load_config, run_main
from devloops_lib.producer import mark_task_done, select_next_task
from devloops_lib.tick_gate import (
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_LOST,
    EXIT_NOOP,
    EXIT_RUN,
    TOKEN_ENV,
    check_lock,
    describe_lock,
    held_by_wrapper,
    load_state,
    release_lock,
    renew_lock,
    take_tick_lock,
    token_holds_lock,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--next", action="store_true", help="Print the most overdue task or NOOP")
    parser.add_argument("--task", type=str, default=None, help="Specific task to run")
    parser.add_argument("--done", type=str, default=None, help="Mark specified task as done")
    parser.add_argument("--lock", action="store_true", help="Acquire tick.lock before exiting 0")
    parser.add_argument("--unlock", action="store_true", help="Release tick.lock owned by --token")
    parser.add_argument("--token", default=None, help=f"Token printed by --lock (default: ${TOKEN_ENV})")
    parser.add_argument("--force-unlock", action="store_true", help="Operator only: remove tick.lock whoever owns it")
    parser.add_argument("--pid", type=int, default=None, help="Process ID to record in tick.lock")
    parser.add_argument("--state-file", type=Path, default=None, help="Path to state.json")
    add_agent_id_argument(parser)
    args = parser.parse_args(argv)

    cfg = load_config()
    state_path = args.state_file or get_state_path("producer-loop", config=cfg)
    lock_path = state_path.parent / "tick.lock"
    token = args.token or os.environ.get(TOKEN_ENV)
    # Under the wrapper, --lock and --unlock leave its lock in place.
    wrapper_held = held_by_wrapper(lock_path)

    tick_token: str | None = None

    def take_lock() -> bool:
        nonlocal tick_token
        tick_token = take_tick_lock(lock_path, "producer-loop", pid=args.pid)
        return tick_token is not None

    def busy() -> int:
        print(f"BUSY ({describe_lock(lock_path)})")
        return EXIT_BUSY

    def drop_lock() -> int:
        if wrapper_held:
            print("LOCK KEPT (held by devloops tick)")
            return EXIT_RUN
        if not token:
            sys.stderr.write(f"--unlock needs the token printed by --lock (--token or ${TOKEN_ENV})\n")
            return EXIT_ERROR
        if not token_holds_lock(lock_path, token):
            print(f"LOST ({describe_lock(lock_path)})")
            return EXIT_LOST
        release_lock(lock_path, token=token)
        print("UNLOCKED")
        return EXIT_RUN

    def print_token() -> None:
        if tick_token:
            print(f"TOKEN {tick_token}")

    try:
        if args.force_unlock:
            release_lock(lock_path)
            print("UNLOCKED (forced by operator)")
            return EXIT_RUN

        # 1. Handle --done <task>
        if args.done:
            # Renewing keeps the lock live until drop_lock, so nobody reclaims it in between.
            if args.unlock and not wrapper_held and not renew_lock(lock_path, token or ""):
                # A tick that lost its lock must not record the task as done either.
                return drop_lock()
            mark_task_done(args.done, state_path)
            print(f"DONE: {args.done}")
            return drop_lock() if args.unlock else EXIT_RUN

        # 2. Handle unlock-only
        if args.unlock and not args.next and not args.task and not args.lock:
            return drop_lock()

        # 3. Check active lock before proceeding with --lock, --next, or --task
        if args.lock or args.next or args.task:
            if check_lock(lock_path) and not wrapper_held:
                return busy()

        # 4. Handle --next
        if args.next:
            state = load_state(state_path)
            task = select_next_task(cfg, state)
            if not task:
                print("NOOP")
                return EXIT_NOOP

            if args.lock and not take_lock():
                return busy()
            print(task)
            print_token()
            return EXIT_RUN

        # 5. Handle specific task
        if args.task:
            if args.lock and not take_lock():
                return busy()
            print(args.task)
            print_token()
            return EXIT_RUN

        # 6. Lock-only
        if args.lock:
            if not take_lock():
                return busy()
            print("LOCKED")
            print_token()
            return EXIT_RUN

        # If --unlock was specified along with other flags
        if args.unlock:
            return drop_lock()

        return EXIT_RUN

    except Exception as e:
        sys.stderr.write(f"producer_gate error: {e}\n")
        return EXIT_ERROR


if __name__ == "__main__":
    run_main(main)
