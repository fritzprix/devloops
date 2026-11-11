#!/usr/bin/env python3
"""Copy devloops_lib and the config template into every skill.

Skill installers such as `npx skills add` copy one skill directory and nothing
outside it, so each skill ships its own copy of the shared code:

  devloops_lib/*.py                -> skills/<skill>/scripts/devloops_lib/
  templates/devloops.config.yaml   -> skills/<skill>/assets/devloops.config.yaml

The root `devloops_lib/` and `templates/` are the only sources to edit. Run this
script after changing them; `--check` exits 1 when a copy differs (CI runs it
through tests/test_skill_lib_sync.py).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SRC = REPO_ROOT / "devloops_lib"
TEMPLATE_SRC = REPO_ROOT / "templates" / "devloops.config.yaml"
SKILLS_DIR = REPO_ROOT / "skills"


def skill_dirs() -> list[Path]:
    return sorted(p.parent for p in SKILLS_DIR.glob("*/SKILL.md"))


def expected_files(skill_dir: Path) -> dict[Path, Path]:
    """Map each vendored destination path to its source path."""
    files = {skill_dir / "scripts" / "devloops_lib" / src.name: src for src in sorted(LIB_SRC.glob("*.py"))}
    files[skill_dir / "assets" / "devloops.config.yaml"] = TEMPLATE_SRC
    return files


def stale_files(skill_dir: Path) -> list[Path]:
    """Return vendored files that are missing, differ from the source, or have no source."""
    expected = expected_files(skill_dir)
    stale = [dest for dest, src in expected.items() if not dest.is_file() or dest.read_bytes() != src.read_bytes()]
    lib_dest = skill_dir / "scripts" / "devloops_lib"
    if lib_dest.is_dir():
        stale.extend(p for p in sorted(lib_dest.glob("*.py")) if p not in expected)
    return stale


def sync(skill_dir: Path) -> list[Path]:
    """Bring one skill's vendored files in line with the sources; return the changed paths."""
    expected = expected_files(skill_dir)
    changed = stale_files(skill_dir)
    for dest in changed:
        src = expected.get(dest)
        if src is None:
            dest.unlink()
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
    return changed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Report stale copies and exit 1 instead of writing")
    args = parser.parse_args(argv)

    stale = [p for skill in skill_dirs() for p in (stale_files(skill) if args.check else sync(skill))]
    for path in stale:
        print(f"{'stale' if args.check else 'synced'}: {path.relative_to(REPO_ROOT).as_posix()}")
    if args.check and stale:
        print("Run `python3 scripts/sync_skill_lib.py` and commit the result.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
