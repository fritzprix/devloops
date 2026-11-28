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
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

# Support running directly or as module
try:
    from devloops_lib.config import board_decision_issues, find_config_file, find_project_root, load_config, resolve_board
    from devloops_lib.forge.detect import ForgeDetectionError, detect_forge, get_forge
except ImportError:
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    from devloops_lib.config import board_decision_issues, find_config_file, find_project_root, load_config, resolve_board
    from devloops_lib.forge.detect import ForgeDetectionError, detect_forge, get_forge

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


def check_network(api_url: str = "https://api.github.com", label: str = "GitHub API") -> bool:
    try:
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": "devloops-doctor"},
        )
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            if resp.status == 200:
                print(f"  [OK] Network: {label} reachable ({api_url})")
                return True
            if resp.status in (401, 403, 429):
                print(f"  [WARN] Network: {label} responded HTTP {resp.status} ({api_url})")
                return True
            print(f"  [FAIL] Network: {label} HTTP status {resp.status} ({api_url})")
            return False
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429):
            print(f"  [WARN] Network: {label} responded HTTP {e.code} ({api_url})")
            return True
        print(f"  [FAIL] Network: {label} HTTP error {e.code} ({api_url})")
        return False
    except Exception as e:
        print(f"  [FAIL] Network unreachable ({api_url}): {e}")
        return False


def check_forge(config: dict[str, Any], *, remote_url: str | None = None) -> bool:
    """Detect the forge, verify API reachability of that host, and check auth."""
    try:
        detected = detect_forge(config, remote_url=remote_url)
    except ForgeDetectionError as exc:
        print(f"  [FAIL] Forge: {exc}")
        return False

    forge_type = detected["type"]
    base_url = detected.get("base_url") or ""
    api_url = detected.get("api_url") or base_url
    source = detected.get("source") or "auto"
    print(f"  [OK] Forge: {forge_type} ({base_url}, source={source})")

    ok = True
    if api_url:
        probe_url = f"{api_url.rstrip('/')}/version" if forge_type in ("gitea", "forgejo") else api_url
        ok &= check_network(probe_url, label=f"{forge_type} API")

    if forge_type == "github":
        try:
            forge = get_forge(config, remote_url=remote_url)
            status = forge.auth_check()
            if status.ok:
                who = f" ({status.output})" if status.output else ""
                print(f"  [OK] Forge auth ({forge_type}): authenticated{who}")
            else:
                print(f"  [FAIL] Forge auth ({forge_type}): {status.output}")
                ok = False
        except Exception as exc:
            print(f"  [FAIL] Forge auth ({forge_type}): {exc}")
            ok = False
    else:
        try:
            forge = get_forge(config, remote_url=remote_url)
            status = forge.auth_check()
            if status.ok:
                print(f"  [OK] Forge auth ({forge_type}): authenticated")
            else:
                print(f"  [FAIL] Forge auth ({forge_type}): {status.output}")
                ok = False
        except ForgeDetectionError:
            token_env = str((config.get("forge") or {}).get("token_env") or "").strip()
            if token_env:
                if os.environ.get(token_env):
                    print(f"  [OK] Forge auth ({forge_type}): env var {token_env} is set")
                else:
                    print(f"  [FAIL] Forge auth ({forge_type}): env var {token_env} is not set")
                    ok = False
            else:
                print(f"  [WARN] Forge auth ({forge_type}): forge.token_env is not configured")
    return ok


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


STAMP_FILENAME = ".devloops-version.json"


def get_package_version() -> str:
    try:
        from importlib.metadata import version
        return version("devloops")
    except Exception:
        return "0.0.0"


def _skill_rel_files(skill_dir: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(skill_dir)
        parts = rel.parts
        if any(p in ("__pycache__", ".git") or p.endswith(".egg-info") for p in parts):
            continue
        if path.name == STAMP_FILENAME or path.suffix in (".pyc", ".pyo"):
            continue
        out.append(rel.as_posix())
    return out


def compute_skill_digest(skill_dir: Path, only_files: Sequence[str] | None = None) -> str:
    """SHA-256 over non-cache files in a skill directory (or a specified relative file list)."""
    digest = hashlib.sha256()
    rel_files = sorted(only_files) if only_files is not None else _skill_rel_files(skill_dir)
    for rel_posix in rel_files:
        path = skill_dir / rel_posix
        digest.update(rel_posix.encode("utf-8") + b"\0")
        if path.is_file():
            digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
        else:
            digest.update(b"\0MISSING\0")
    return digest.hexdigest()


def read_skill_stamp(skill_dir: Path) -> dict | None:
    stamp_path = skill_dir / STAMP_FILENAME
    if not stamp_path.is_file():
        return None
    try:
        data = json.loads(stamp_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_skill_stamp(
    skill_dir: Path,
    version: str | None = None,
    source_skill_dir: Path | None = None,
) -> Path:
    stamp_path = skill_dir / STAMP_FILENAME
    files_dir = source_skill_dir if source_skill_dir is not None else skill_dir
    rel_files = _skill_rel_files(files_dir)
    payload = {
        "version": version if version is not None else get_package_version(),
        "digest": compute_skill_digest(skill_dir, only_files=rel_files),
        "files": rel_files,
    }
    stamp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return stamp_path


def find_skill_stamp_problems(skills_dir: Path, source_skills_dir: Path | None = None) -> list[str]:
    """Return human-readable drift/mismatch errors for installed skills in skills_dir (#130)."""
    problems: list[str] = []
    skill_dirs = sorted(p.parent.parent for p in skills_dir.glob("*/scripts/devloops_lib"))
    stamps_by_version: dict[str, list[str]] = {}
    unstamped: list[str] = []
    for sdir in skill_dirs:
        stamp = read_skill_stamp(sdir)
        if stamp is not None:
            ver = str(stamp.get("version") or "")
            stamps_by_version.setdefault(ver, []).append(sdir.name)
            stamp_files = stamp.get("files") if isinstance(stamp.get("files"), list) else None
            actual_digest = compute_skill_digest(sdir, only_files=stamp_files)
            if stamp.get("digest") and actual_digest != stamp["digest"]:
                problems.append(
                    f"skill '{sdir.name}' files differ from its installed stamp ({STAMP_FILENAME})"
                )
        else:
            unstamped.append(sdir.name)
        if source_skills_dir is not None:
            src_sdir = source_skills_dir / sdir.name
            if src_sdir.is_dir():
                src_files = _skill_rel_files(src_sdir)
                if compute_skill_digest(sdir, only_files=src_files) != compute_skill_digest(src_sdir, only_files=src_files):
                    problems.append(f"skill '{sdir.name}' differs from source release")
    if len(stamps_by_version) > 1:
        groups = "; ".join(f"{v or 'unknown'}: {', '.join(names)}" for v, names in sorted(stamps_by_version.items()))
        problems.append(f"installed skills have mixed version stamps ({groups})")
    if stamps_by_version and unstamped:
        problems.append(
            f"installed skills mix stamped and unstamped installs (unstamped: {', '.join(unstamped)})"
        )
    return problems


def _lib_digest(lib_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(lib_dir.glob("*.py")):
        digest.update(path.name.encode("utf-8") + b"\0")
        # Line endings can change on checkout; they do not change the code.
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def check_skill_libs(skills_dir: Path, source_skills_dir: Path | None = None) -> bool:
    """Check that every devloops skill in skills_dir ships the same devloops_lib and matches source/stamps.

    The loops share state and lock files, so skills from different releases must
    not run side by side. That happens when only some skills are updated.
    """
    ok = True
    by_digest: dict[str, list[str]] = {}
    for lib_dir in sorted(skills_dir.glob("*/scripts/devloops_lib")):
        by_digest.setdefault(_lib_digest(lib_dir), []).append(lib_dir.parent.parent.name)
    if len(by_digest) <= 1:
        count = sum(len(names) for names in by_digest.values())
        print(f"  [OK] Skill libraries: {count} devloops skill(s) share one devloops_lib")
    else:
        groups = "; ".join(", ".join(names) for names in by_digest.values())
        print(f"  [FAIL] Skill libraries differ between skills ({groups}): run `devloops install --update`")
        ok = False

    if source_skills_dir is None:
        in_source_repo = False
        has_stamps = any((p.parent.parent / STAMP_FILENAME).is_file() for p in skills_dir.glob("*/scripts/devloops_lib"))
        if has_stamps:
            try:
                from devloops_lib.cli import get_asset_paths
                src_skills, _ = get_asset_paths()
                if src_skills.resolve() != skills_dir.resolve():
                    source_skills_dir = src_skills
                else:
                    in_source_repo = True
            except Exception:
                source_skills_dir = None
        if source_skills_dir is None and not in_source_repo:
            print(
                "  [WARN] Skill source comparison not verified (no source release found; "
                "run `devloops doctor` or pass `--source-skills`)"
            )

    problems = find_skill_stamp_problems(skills_dir, source_skills_dir=source_skills_dir)
    if problems:
        for msg in problems:
            print(f"  [FAIL] Skill drift: {msg}. Run `devloops install --update` to update.")
        ok = False

    if source_skills_dir is not None:
        for lib_dir in sorted(skills_dir.glob("*/scripts/devloops_lib")):
            sdir = lib_dir.parent.parent
            src_sdir = source_skills_dir / sdir.name
            if src_sdir.is_dir():
                extras = sorted(set(_skill_rel_files(sdir)) - set(_skill_rel_files(src_sdir)))
                if extras:
                    print(
                        f"  [WARN] Skill '{sdir.name}' has extra file(s) not in release: "
                        f"{', '.join(extras)}"
                    )
    return ok


def main(argv: Sequence[str] = (), skill_dir: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="doctor.py", description="Check devloops prerequisites.")
    parser.add_argument("--init", action="store_true", help=f"Create {CONFIG_NAME} from the template and exit")
    parser.add_argument("--source-skills", type=Path, default=None, help="Source skills directory to compare against")
    args = parser.parse_args(list(argv))
    if args.init:
        return init_config(config_template(skill_dir))

    print("Checking agent loop prerequisites:")
    all_ok = True

    if find_config_file() is None:
        print(f"  [WARN] No {CONFIG_NAME} found; defaults apply. Create one with `doctor.py --init`.")
    if skill_dir is not None:
        all_ok &= check_skill_libs(skill_dir.parent, source_skills_dir=args.source_skills)

    # 1. Python version
    py_ok = sys.version_info >= (3, 8)
    if py_ok:
        print(f"  [OK] Python version: {sys.version.split()[0]}")
    else:
        print(f"  [FAIL] Python >= 3.8 required, found {sys.version.split()[0]}")
        all_ok = False

    # 2. Git
    all_ok &= check("git", ["git", "--version"])

    # 3. Forge detection, API reachability, and auth (replaces hard-coded GitHub checks)
    cfg: dict[str, Any] = {}
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    all_ok &= check_forge(cfg)

    # 4. Configured project commands & board decision (if any)
    try:
        cmds = cfg.get("commands", {})
        for cmd_name, cmd_str in cmds.items():
            if cmd_str and isinstance(cmd_str, str) and cmd_str.strip():
                parts = shlex.split(cmd_str.strip())
                if parts:
                    all_ok &= check(f"command.{cmd_name} ({parts[0]})", [parts[0], "--version"])
        # Board decision (issue #49): a user decision, never a silent default.
        for level, message in board_decision_issues(cfg):
            print(f"  [{'FAIL' if level == 'fail' else 'WARN'}] Board: {message}")
            if level == "fail":
                all_ok = False
        decided = resolve_board(cfg)
        if decided["provider"] and decided["confirmed"]:
            print(f"  [OK] Board: provider={decided['provider']} (confirmed)")
    except Exception:
        pass

    # 6. Remote push permission test (dry-run)
    all_ok &= check_push_permission()

    print("\nSummary: " + ("All checks passed." if all_ok else "Some checks failed."))
    return 0 if all_ok else 1


if __name__ == "__main__":
    from devloops_lib.config import run_main

    run_main(lambda: main(sys.argv[1:]))
