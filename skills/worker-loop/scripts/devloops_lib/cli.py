#!/usr/bin/env python3
"""devloops command line interface."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from devloops_lib.claims import release_tick_claims
from devloops_lib.config import EXIT_CONFIG_ERROR, ConfigError, add_agent_id_argument, get_claims_dir, get_state_path
from devloops_lib.proc_util import popen_agent, terminate_process_tree
from devloops_lib.tick_gate import (
    TOKEN_ENV,
    WRAPPER_PID_ENV,
    acquire_lock_token,
    check_lock,
    describe_lock,
    load_state,
    lock_ttl,
    owner_fields,
    preflight_decision,
    release_lock,
    renew_lock,
)


def get_asset_paths() -> tuple[Path, Path]:
    """Locate (skills_dir, templates_dir).

    Raises RuntimeError if any required assets are missing.
    """
    pkg_dir = Path(__file__).resolve().parent

    # 1. Bundled assets inside wheel/installed package
    bundled_skills = pkg_dir / "assets" / "skills"
    bundled_templates = pkg_dir / "assets" / "templates"
    if bundled_skills.is_dir() and bundled_templates.is_dir():
        return bundled_skills, bundled_templates

    # 2. Source tree layout
    repo_root = pkg_dir.parent
    src_skills = repo_root / "skills"
    src_templates = repo_root / "templates"
    if src_skills.is_dir() and src_templates.is_dir():
        return src_skills, src_templates

    raise RuntimeError(
        "Missing required devloops assets (skills and templates). "
        "Installation cannot proceed from an incomplete package or corrupted tree."
    )


def copy_tree_safe(src: Path, dest: Path, exclude_names: set[str] | None = None) -> list[tuple[str, Path]]:
    """Copy files from src to dest preserving existing files.

    Returns list of (status, dest_path) where status is 'installed' or 'skipped (already exists)'.
    """
    results: list[tuple[str, Path]] = []
    excludes = exclude_names if exclude_names is not None else {"__pycache__", ".git"}

    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in excludes and not d.endswith(".egg-info")]
        rel_root = Path(root).relative_to(src)
        target_dir = dest / rel_root
        target_dir.mkdir(parents=True, exist_ok=True)

        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            src_file = Path(root) / f
            dest_file = target_dir / f

            if dest_file.exists():
                results.append(("skipped (already exists)", dest_file))
            else:
                shutil.copy2(src_file, dest_file)
                results.append(("installed", dest_file))

    return results


def install_command(target_dir: Path) -> int:
    """Install devloops skills (each with its own devloops_lib) and config into target_dir."""
    try:
        skills_src, templates_src = get_asset_paths()
    except RuntimeError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1

    # Verify required skill directories exist
    required_skills = ["worker-loop", "gatekeeper-loop", "producer-loop"]
    for s in required_skills:
        if not (skills_src / s).is_dir():
            sys.stderr.write(f"Error: Required skill directory '{s}' not found in {skills_src}.\n")
            return 1

    config_template = templates_src / "devloops.config.yaml"
    if not config_template.is_file():
        sys.stderr.write(f"Error: Required config template not found at {config_template}.\n")
        return 1

    target_dir = target_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    skills_dest = target_dir / ".agents" / "skills"
    config_dest = target_dir / "devloops.config.yaml"

    log_entries: list[tuple[str, Path]] = []

    # 1. Install skills (skills can contain their own assets/ dir; exclude only cache/git)
    log_entries.extend(copy_tree_safe(skills_src, skills_dest, exclude_names={"__pycache__", ".git"}))

    # 2. Install config (root config copied only if absent)
    if config_dest.exists():
        log_entries.append(("skipped (already exists)", config_dest))
    else:
        config_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config_template, config_dest)
        log_entries.append(("installed", config_dest))

    # Print summary
    print(f"devloops installed into: {target_dir}")
    print("\nSummary of actions:")
    for status, path in log_entries:
        try:
            rel = path.relative_to(target_dir)
            print(f"  [{status}] {rel}")
        except ValueError:
            print(f"  [{status}] {path}")

    print("\nNext steps:")
    print("  1. Agent setup: Configure your coding agent to load skills from:")
    print(f"     {skills_dest}")
    print("  2. Project config: Review and customize:")
    print(f"     {config_dest}")
    print("  3. Diagnostics: Verify prerequisites and installation by running")
    print("     (any Python 3.8+ on PATH; use `python` on Windows if `python3` is absent):")
    doctor_path = skills_dest / "worker-loop" / "scripts" / "doctor.py"
    print(f'     python3 "{doctor_path}"')

    return 0



def acquire_flock(lock_file: Path) -> int | None:
    """Acquire flock on POSIX systems. Returns the open fd, or None if another process holds it.

    Raises OSError when the flock file cannot be opened: that is an error, not BUSY.
    """
    if sys.platform == "win32":
        return None
    import fcntl
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        raise
    return fd


def release_flock(fd: int | None) -> None:
    """Release flock on POSIX systems."""
    if fd is not None and sys.platform != "win32":
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


def acquire_wrapper_lock(lock_file: Path, pid: int, loop: str = "worker-loop") -> str | None:
    """Acquire the wrapper lock atomically; return the ownership token.

    Delegates to tick_gate.acquire_lock_token so publication is atomic and
    stale reclamation is ownership-safe (issue #22). The returned token makes
    the matching release ownership-safe across stale replacement.
    """
    return acquire_lock_token(lock_file, pid=pid, extra=owner_fields(loop))


class LockHeartbeat:
    """Renew the tick lock every TTL/3 while the agent runs, so a long tick keeps it."""

    def __init__(self, lock_file: Path, token: str) -> None:
        self.lock_file = lock_file
        self.token = token
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="devloops-lock-heartbeat", daemon=True)

    def _run(self) -> None:
        # Three renewals per TTL, so one late renewal never lets the lock expire.
        interval = lock_ttl() / 3
        while not self._stop.wait(interval):
            if not renew_lock(self.lock_file, self.token):
                self.lost = True
                sys.stderr.write(
                    f"devloops tick: lost the tick lock ({describe_lock(self.lock_file)}); "
                    "the agent's guard commands now refuse to push\n"
                )
                return

    def __enter__(self) -> "LockHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


def tick_command(
    loop: str,
    agent_cmd: str | None = None,
    state_file: Path | None = None,
) -> int:
    """Run tick wrapper: gate check, acquire lock, execute agent command, and unlock."""
    norm_loop = loop.strip().lower()
    if not norm_loop.endswith("-loop"):
        norm_loop = f"{norm_loop}-loop"

    state_path = state_file or get_state_path(norm_loop)
    lock_path = state_path.parent / "tick.lock"

    # Preflight check lock. BUSY means a tick of this slot holds the lock, which can
    # also be this slot's own earlier tick, so the holder is described, not guessed.
    if check_lock(lock_path):
        print(f"BUSY ({describe_lock(lock_path)})")
        return 2

    state = load_state(state_path)

    # Preflight gate for worker-loop
    if norm_loop == "worker-loop":
        if not state.get("current"):
            try:
                fp, backlog = preflight_decision()
                last_fp = state.get("lastFingerprint")
                if last_fp and last_fp == fp and not backlog:
                    print("NOOP (no changes)")
                    return 1
            except Exception as e:
                # If network fails or repo cannot be reached, do not exit NOOP
                sys.stderr.write(f"Preflight error: {e}\n")

    # Hold flock on POSIX, then publish the portable lock. The flock needs its own
    # file: opening tick.lock would create the empty file that publishing must not
    # find, and a fresh empty lock counts as a live owner.
    try:
        flock_fd = acquire_flock(lock_path.with_name(f"{lock_path.name}.flock"))
    except OSError as e:
        sys.stderr.write(f"devloops tick: cannot open the flock file: {e}\n")
        return 3
    if sys.platform != "win32" and flock_fd is None:
        print(f"BUSY (another devloops tick of this slot holds the flock; {describe_lock(lock_path)})")
        return 2

    # Acquire atomic lock (the token makes the release ownership-safe)
    lock_token = acquire_wrapper_lock(lock_path, pid=os.getpid(), loop=norm_loop)
    if lock_token is None:
        release_flock(flock_fd)
        print(f"BUSY ({describe_lock(lock_path)})")
        return 2

    print(f"LOCKED ({norm_loop})")
    agent_proc: subprocess.Popen[bytes] | None = None
    try:
        with LockHeartbeat(lock_path, lock_token):
            cmd_to_run = agent_cmd or os.environ.get("DEVLOOPS_AGENT_CMD") or os.environ.get("AGENT_CMD")
            if cmd_to_run:
                print(f"Executing agent command: {cmd_to_run}")
                # Tell the agent's gate, claim, and guard commands which lock this tick owns.
                agent_env = {**os.environ, WRAPPER_PID_ENV: str(os.getpid()), TOKEN_ENV: lock_token}
                agent_proc = popen_agent(cmd_to_run, agent_env)
                return agent_proc.wait()
            return 0
    finally:
        if agent_proc is not None and agent_proc.poll() is None:
            terminate_process_tree(agent_proc.pid)
            try:
                agent_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                terminate_process_tree(agent_proc.pid, wait_seconds=5)
        released = release_tick_claims(get_claims_dir(), lock_token)
        if released:
            print(f"RELEASED CLAIMS: {', '.join(released)}")
        release_lock(lock_path, token=lock_token)
        release_flock(flock_fd)
        print("UNLOCKED")


def add_tick_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent-cmd", type=str, default=None, help="Agent command to execute while holding lock")
    parser.add_argument("--state-file", type=Path, default=None, help="Path to state.json")
    add_agent_id_argument(parser)


def run_tick(loop: str, args: argparse.Namespace) -> int:
    try:
        return tick_command(loop, agent_cmd=args.agent_cmd, state_file=args.state_file)
    except ConfigError as e:
        # Exit 1 would read as NOOP to a scheduler.
        sys.stderr.write(f"devloops config error: {e}\n")
        return EXIT_CONFIG_ERROR


def tick_main(loop: str, argv: list[str]) -> int:
    """Entry point of a skill's scripts/tick.py, which fixes the loop."""
    parser = argparse.ArgumentParser(
        prog="tick.py",
        description=f"Run one {loop}-loop tick: preflight gate, lock, agent command, unlock.",
    )
    add_tick_options(parser)
    return run_tick(loop, parser.parse_args(argv))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="devloops",
        description="Reusable agent-loop skills for coding agents."
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    install_parser = subparsers.add_parser("install", help="Install devloops skills and configuration into a project")
    install_parser.add_argument(
        "--target",
        type=Path,
        default=Path("."),
        help="Target project directory (default: current directory)"
    )

    tick_parser = subparsers.add_parser("tick", help="Run preflight gate, lock, execute agent command, and unlock safely")
    tick_parser.add_argument("loop", choices=["worker", "worker-loop", "producer", "producer-loop", "gatekeeper", "gatekeeper-loop"], help="Loop name to tick")
    add_tick_options(tick_parser)

    args = parser.parse_args(argv)

    if args.subcommand == "install":
        return install_command(args.target)
    if args.subcommand == "tick":
        return run_tick(args.loop, args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
