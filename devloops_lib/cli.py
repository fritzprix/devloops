#!/usr/bin/env python3
"""devloops command line interface."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


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
    excludes = exclude_names or {"__pycache__", "assets", ".git"}

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

    # 1. Install skills
    log_entries.extend(copy_tree_safe(skills_src, skills_dest))

    # 2. Install devloops_lib
    log_entries.extend(copy_tree_safe(lib_src, lib_dest, exclude_names={"__pycache__", "assets"}))

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
    print(f"     python3 {lib_dest / 'doctor.py'}")

    return 0


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

    args = parser.parse_args(argv)

    if args.subcommand == "install":
        return install_command(args.target)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
