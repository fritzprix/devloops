#!/usr/bin/env python3
"""Diagnostic check for agent loop prerequisites.

Checks:
  - Network connectivity to GitHub
  - gh CLI installed and authenticated
  - git installed and remote access working
  - python3 version >= 3.8
  - Configured project commands (if specified in config)
  - Push permission to remote repository

  - Every installed devloops skill ships the same devloops_lib

`--init` creates devloops.config.yaml in the project root from the skill's
bundled template and runs no other check.

Exit codes:
  0 - All checks passed
  1 - One or more critical checks failed
"""
from __future__ import annotations

import argparse
import hashlib
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence

# Support running directly or as module
try:
    from devloops_lib.config import find_config_file, find_project_root, load_config
except ImportError:
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    from devloops_lib.config import find_config_file, find_project_root, load_config

NETWORK_TIMEOUT_SECONDS = 5
CONFIG_NAME = "devloops.config.yaml"


def check(name: str, cmd: list[str]) -> bool:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            print(f"  [OK] {name}")
            return True
        else:
            print(f"  [FAIL] {name}: {res.stderr.strip() or res.stdout.strip()}")
            return False
    except FileNotFoundError:
        print(f"  [FAIL] {name}: command not found ({cmd[0]})")
        return False
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        return False


def check_network() -> bool:
    try:
        req = urllib.request.Request(
            "https://api.github.com",
            headers={"User-Agent": "devloops-doctor"}
        )
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            if resp.status == 200:
                print("  [OK] Network: GitHub API reachable")
                return True
            if resp.status in (403, 429):
                print(f"  [WARN] Network: GitHub API rate limited (HTTP {resp.status})")
                return True
            print(f"  [FAIL] Network: HTTP status {resp.status}")
            return False
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            print(f"  [WARN] Network: GitHub API rate limited (HTTP {e.code})")
            return True
        print(f"  [FAIL] Network: HTTP error {e.code}")
        return False
    except Exception as e:
        print(f"  [FAIL] Network unreachable: {e}")
        return False


def check_push_permission() -> bool:
    try:
        res = subprocess.run(
            ["git", "push", "--dry-run", "origin", "HEAD"],
            capture_output=True, text=True, check=False
        )
        if res.returncode == 0:
            print("  [OK] Push permission (dry-run passed)")
            return True
        err = res.stderr.strip()
        # If rejected solely because the current branch is not fast-forward, push permission is valid
        if "non-fast-forward" in err or ("[rejected]" in err and "fetch first" in err):
            print("  [OK] Push permission (remote accessible, non-fast-forward rejected)")
            return True
        print(f"  [FAIL] Push permission failed: {err}")
        return False
    except Exception as e:
        print(f"  [FAIL] Push permission check exception: {e}")
        return False


def config_template(skill_dir: Path | None) -> Path:
    """Return the config template: the skill's bundled copy, else the source tree's."""
    if skill_dir is not None:
        return skill_dir / "assets" / CONFIG_NAME
    return Path(__file__).resolve().parent.parent / "templates" / CONFIG_NAME


def init_config(template: Path, start_dir: Path | None = None) -> int:
    """Create devloops.config.yaml in the project root unless a config already exists."""
    existing = find_config_file(start_dir)
    if existing is not None:
        print(f"Config already exists, not changed: {existing}")
        return 0
    if not template.is_file():
        print(f"Config template not found: {template}", file=sys.stderr)
        return 1
    dest = find_project_root(start_dir) / CONFIG_NAME
    shutil.copyfile(template, dest)
    print(f"Created {dest}")
    print("Edit project.repo, the branches, and commands before the first tick.")
    return 0


def _lib_digest(lib_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(lib_dir.glob("*.py")):
        digest.update(path.name.encode("utf-8") + b"\0")
        # Line endings can change on checkout; they do not change the code.
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def check_skill_libs(skills_dir: Path) -> bool:
    """Check that every devloops skill in skills_dir ships the same devloops_lib.

    The loops share state and lock files, so skills from different releases must
    not run side by side. That happens when only some skills are updated.
    """
    by_digest: dict[str, list[str]] = {}
    for lib_dir in sorted(skills_dir.glob("*/scripts/devloops_lib")):
        by_digest.setdefault(_lib_digest(lib_dir), []).append(lib_dir.parent.parent.name)
    if len(by_digest) <= 1:
        count = sum(len(names) for names in by_digest.values())
        print(f"  [OK] Skill libraries: {count} devloops skill(s) share one devloops_lib")
        return True
    groups = "; ".join(", ".join(names) for names in by_digest.values())
    print(f"  [FAIL] Skill libraries differ between skills ({groups}): update all devloops skills together")
    return False


def main(argv: Sequence[str] = (), skill_dir: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="doctor.py", description="Check devloops prerequisites.")
    parser.add_argument("--init", action="store_true", help=f"Create {CONFIG_NAME} from the template and exit")
    args = parser.parse_args(list(argv))
    if args.init:
        return init_config(config_template(skill_dir))

    print("Checking agent loop prerequisites:")
    all_ok = True

    if find_config_file() is None:
        print(f"  [WARN] No {CONFIG_NAME} found; defaults apply. Create one with `doctor.py --init`.")
    if skill_dir is not None:
        all_ok &= check_skill_libs(skill_dir.parent)

    # 1. Python version
    py_ok = sys.version_info >= (3, 8)
    if py_ok:
        print(f"  [OK] Python version: {sys.version.split()[0]}")
    else:
        print(f"  [FAIL] Python >= 3.8 required, found {sys.version.split()[0]}")
        all_ok = False

    # 2. Network connectivity
    all_ok &= check_network()

    # 3. Git
    all_ok &= check("git", ["git", "--version"])

    # 4. GitHub CLI authentication
    all_ok &= check("gh auth", ["gh", "auth", "status"])

    # 5. Configured project commands (if any)
    try:
        cfg = load_config()
        cmds = cfg.get("commands", {})
        for cmd_name, cmd_str in cmds.items():
            if cmd_str and isinstance(cmd_str, str) and cmd_str.strip():
                parts = shlex.split(cmd_str.strip())
                if parts:
                    all_ok &= check(f"command.{cmd_name} ({parts[0]})", [parts[0], "--version"])
    except Exception:
        pass

    # 6. Remote push permission test (dry-run)
    all_ok &= check_push_permission()

    print("\nSummary: " + ("All checks passed." if all_ok else "Some checks failed."))
    return 0 if all_ok else 1


if __name__ == "__main__":
    from devloops_lib.config import run_main

    run_main(lambda: main(sys.argv[1:]))
