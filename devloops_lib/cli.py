#!/usr/bin/env python3
"""devloops command line interface."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from devloops_lib.config import get_state_path, load_config
from devloops_lib.tick_gate import (
    acquire_lock,
    check_lock,
    load_state,
    preflight_decision,
    release_lock,
    save_state,
)


def get_asset_paths() -> tuple[Path, Path, Path]:
    """Locate (skills_dir, devloops_lib_dir, templates_dir).

    Raises RuntimeError if any required assets are missing.
    """
    pkg_dir = Path(__file__).resolve().parent

    # 1. Bundled assets inside wheel/installed package
    bundled_skills = pkg_dir / "assets" / "skills"
    bundled_templates = pkg_dir / "assets" / "templates"
    if bundled_skills.is_dir() and bundled_templates.is_dir():
        return bundled_skills, pkg_dir, bundled_templates

    # 2. Source tree layout
    repo_root = pkg_dir.parent
    src_skills = repo_root / "skills"
    src_templates = repo_root / "templates"
    if src_skills.is_dir() and src_templates.is_dir():
        return src_skills, pkg_dir, src_templates

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
    """Install devloops skills, devloops_lib, and config into target_dir."""
    try:
        skills_src, lib_src, templates_src = get_asset_paths()
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
    lib_dest = target_dir / ".agents" / "devloops_lib"
    config_dest = target_dir / "devloops.config.yaml"

    log_entries: list[tuple[str, Path]] = []

    # 1. Install skills (skills can contain their own assets/ dir; exclude only cache/git)
    log_entries.extend(copy_tree_safe(skills_src, skills_dest, exclude_names={"__pycache__", ".git"}))

    # 2. Install devloops_lib (exclude internal packaging assets and cache)
    log_entries.extend(copy_tree_safe(lib_src, lib_dest, exclude_names={"__pycache__", "assets", ".git"}))

    # 3. Install config (root config copied only if absent)
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
    print("  3. Diagnostics: Verify prerequisites and installation by running:")
    py_cmd = sys.executable or "python3"
    print(f"     {py_cmd} {lib_dest / 'doctor.py'}")

    return 0



def acquire_flock(lock_file: Path) -> int | None:
    """Acquire flock on POSIX systems. Returns open fd or None if blocked/unsupported."""
    if sys.platform == "win32":
        return None
    try:
        import fcntl
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (BlockingIOError, OSError):
        return None


def release_flock(fd: int | None) -> None:
    """Release flock on POSIX systems."""
    if fd is not None and sys.platform != "win32":
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


def acquire_wrapper_lock(lock_file: Path, pid: int) -> bool:
    """Acquire lock atomically with O_CREAT|O_EXCL and record wrapper PID."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"time": int(time.time()), "pid": pid}).encode("utf-8")
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        return True
    except FileExistsError:
        if not check_lock(lock_file):
            release_lock(lock_file)
            try:
                fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "wb") as f:
                    f.write(payload)
                return True
            except FileExistsError:
                return False
        return False


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

    # Preflight check lock
    if check_lock(lock_path):
        print("BUSY (tick in progress)")
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

    # Acquire lock (with flock on POSIX and atomic O_EXCL + PID on Windows/portable)
    flock_fd = acquire_flock(lock_path)
    if sys.platform != "win32" and flock_fd is None:
        print("BUSY (tick in progress)")
        return 2

    # Acquire atomic lock
    if not acquire_wrapper_lock(lock_path, pid=os.getpid()):
        release_flock(flock_fd)
        print("BUSY (tick in progress)")
        return 2

    print(f"LOCKED ({norm_loop})")
    try:
        cmd_to_run = agent_cmd or os.environ.get("DEVLOOPS_AGENT_CMD") or os.environ.get("AGENT_CMD")
        if cmd_to_run:
            print(f"Executing agent command: {cmd_to_run}")
            res = subprocess.run(cmd_to_run, shell=True)
            return res.returncode
        return 0
    finally:
        release_lock(lock_path)
        release_flock(flock_fd)
        print("UNLOCKED")


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
    tick_parser.add_argument("--agent-cmd", type=str, default=None, help="Agent command to execute while holding lock")
    tick_parser.add_argument("--state-file", type=Path, default=None, help="Path to state.json")

    args = parser.parse_args(argv)

    if args.subcommand == "install":
        return install_command(args.target)
    if args.subcommand == "tick":
        return tick_command(args.loop, agent_cmd=args.agent_cmd, state_file=args.state_file)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
