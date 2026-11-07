#!/usr/bin/env python3
"""Relocated-install smoke test of the built wheel and sdist (issue #8, CI task 13).

Run after `uv build` from the repository root:

    python scripts/ci_smoke.py --dist dist

1. Inspects the wheel and sdist: required skills, library, config template, entry
   point, and no dev-only test wrappers.
2. Runs tests/test_install_smoke.py with the installer `uvx --from <wheel> devloops`,
   so every check (asset set, script --help from an unrelated cwd, library imports,
   documented one-liner, markdown links, re-run keeping edits) runs against the
   wheel, in temp directories whose paths contain spaces, with no PYTHONPATH.
3. Installs the wheel into a fresh venv, removes a required bundled asset, and
   checks that `devloops install` exits non-zero with a clear message and writes
   nothing.

Exits non-zero on the first failure. Never publishes anything.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS = ("worker-loop", "gatekeeper-loop", "producer-loop")
WHEEL_REQUIRED = [f"devloops_lib/assets/skills/{skill}/SKILL.md" for skill in SKILLS] + [
    "devloops_lib/assets/templates/devloops.config.yaml",
    "devloops_lib/cli.py",
    "devloops_lib/claims.py",
    "devloops_lib/guard.py",
    "devloops_lib/tick_gate.py",
]
SDIST_REQUIRED = [f"skills/{skill}/SKILL.md" for skill in SKILLS] + [
    "templates/devloops.config.yaml",
    "devloops_lib/cli.py",
    "pyproject.toml",
    "PKG-INFO",
]


class SmokeError(RuntimeError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def clean_env() -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def run(cmd: list, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(str(c) for c in cmd)}  (cwd: {cwd})", flush=True)
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd), env=env or clean_env(),
                          capture_output=True, text=True, timeout=600)


def only(dist: Path, pattern: str) -> Path:
    found = sorted(dist.glob(pattern))
    check(len(found) == 1, f"expected exactly one {pattern} in {dist}, found {[p.name for p in found]}")
    return found[0].resolve()


def inspect_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
        entry_points = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
        check(len(entry_points) == 1, "wheel has no entry_points.txt")
        check("devloops = devloops_lib.cli:main" in z.read(entry_points[0]).decode("utf-8"),
              "wheel entry point `devloops = devloops_lib.cli:main` missing")
    missing = [n for n in WHEEL_REQUIRED if n not in names]
    check(not missing, f"wheel is missing {missing}")
    test_wrappers = sorted(n for n in names if "/scripts/test_" in n)
    check(not test_wrappers, f"wheel ships dev-only test wrappers: {test_wrappers}")
    print(f"wheel OK: {wheel.name} ({len(names)} entries)")


def inspect_sdist(sdist: Path) -> None:
    with tarfile.open(sdist) as tar:
        names = {name.split("/", 1)[1] for name in tar.getnames() if "/" in name}
    missing = [n for n in SDIST_REQUIRED if n not in names]
    check(not missing, f"sdist is missing {missing}")
    print(f"sdist OK: {sdist.name} ({len(names)} entries)")


def run_install_smoke_tests(wheel: Path) -> None:
    uvx = shutil.which("uvx")
    check(uvx is not None, "uvx not found on PATH (install uv)")
    env = clean_env()
    env["DEVLOOPS_SMOKE_INSTALLER"] = json.dumps([uvx, "--isolated", "--from", str(wheel), "devloops"])
    result = run([sys.executable, "-m", "unittest", "-v", "tests.test_install_smoke"], REPO_ROOT, env)
    sys.stdout.write(result.stdout + result.stderr)
    check(result.returncode == 0, "relocated-install smoke tests failed against the wheel")


def venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def venv_python(venv: Path) -> Path:
    return venv_bin(venv) / ("python.exe" if os.name == "nt" else "python")


def check_missing_assets(wheel: Path, work: Path) -> None:
    venv = work / "venv with spaces"
    result = run([sys.executable, "-m", "venv", venv], work)
    check(result.returncode == 0, f"venv creation failed: {result.stderr}")
    python = venv_python(venv)
    result = run([python, "-m", "pip", "install", "--no-index", "--quiet", wheel], work)
    check(result.returncode == 0, f"pip install of the wheel failed: {result.stdout}{result.stderr}")
    result = run([python, "-c", "import devloops_lib, pathlib; print(pathlib.Path(devloops_lib.__file__).parent)"], work)
    check(result.returncode == 0, f"installed devloops_lib does not import: {result.stderr}")
    package = Path(result.stdout.strip())
    check(venv.resolve() in package.resolve().parents, f"devloops_lib imported from outside the venv: {package}")
    devloops = venv_bin(venv) / ("devloops.exe" if os.name == "nt" else "devloops")
    check(devloops.is_file(), f"console script missing: {devloops}")

    cases = [
        (package / "assets" / "templates" / "devloops.config.yaml", "config template"),
        (package / "assets" / "skills" / "producer-loop", "producer-loop"),
    ]
    for removed, message in cases:
        if removed.is_dir():
            shutil.rmtree(removed)
        else:
            removed.unlink()
        target = work / f"target without {removed.name}"
        result = run([devloops, "install", "--target", target], work)
        check(result.returncode != 0, f"install succeeded without {removed}:\n{result.stdout}")
        check(message in result.stderr, f"unclear error without {removed}: {result.stderr!r}")
        check(not (target / ".agents").exists() and not (target / "devloops.config.yaml").exists(),
              f"install without {removed} wrote files into {target}")
        print(f"missing {removed.name}: exit {result.returncode}, {result.stderr.strip()}")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dist", type=Path, default=REPO_ROOT / "dist", help="Directory with the built wheel and sdist")
    args = parser.parse_args(argv)
    try:
        wheel = only(args.dist, "*.whl")
        sdist = only(args.dist, "*.tar.gz")
        inspect_wheel(wheel)
        inspect_sdist(sdist)
        run_install_smoke_tests(wheel)
        with tempfile.TemporaryDirectory(prefix="devloops smoke ") as tmp:
            check_missing_assets(wheel, Path(tmp))
    except SmokeError as e:
        sys.stderr.write(f"SMOKE FAILED: {e}\n")
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
