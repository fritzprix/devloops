#!/usr/bin/env python3
"""Automated relocated-install smoke test (issue #8 acceptance criteria).

Simulates the relocated install that `uvx --from <wheel> devloops install`
performs: the installer runs as a separate process from an empty, unrelated
current directory, into a fresh target path containing spaces. Afterwards:

- every script path referenced by an installed SKILL.md or its references/
  resolves under the installed `.agents/skills` layout;
- every relative markdown link in an installed SKILL.md exists;
- the doctor path printed in the next-steps guidance exists on disk;
- every installed skill script runs with `--help` (exit 0) from the unrelated
  current directory, bootstrapping its own imports;
- the installed `devloops_lib` imports cleanly with only `.agents` on the path.

Cross-platform: plain subprocess calls, no POSIX-only commands.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_SKILLS = ["worker-loop", "gatekeeper-loop", "producer-loop"]

# `skills/<skill>/.../<name>.py` references inside skill docs and commands.
SCRIPT_REF = re.compile(r"skills/[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+\.py")
# Relative markdown links inside installed SKILL.md files, e.g. (references/x.md).
MD_LINK = re.compile(r"\]\((references/[^)#\s]+)\)")


def _run(cmd: list[str], cwd: Path, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=env, timeout=120)


class RelocatedInstallSmokeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name).resolve()
        # Fresh, empty, unrelated current directory (never the repo root).
        self.cwd_dir = self.tmp_dir / "elsewhere"
        self.cwd_dir.mkdir()
        # Target path contains spaces.
        self.target = self.tmp_dir / "my project with spaces" / "my project"
        self.skills_dest = self.target / ".agents" / "skills"

    def _install(self) -> str:
        """Run the CLI install from the unrelated cwd into the spaced target.

        Mirrors the uvx invocation without requiring network/wheel builds:
        a separate Python process, unrelated cwd, target passed as an argument.
        """
        code = (
            "import sys; from devloops_lib.cli import main; "
            f"sys.exit(main(['install', '--target', {str(self.target)!r}]))"
        )
        proc = _run([sys.executable, "-c", code], cwd=self.cwd_dir,
                    env_extra={"PYTHONPATH": str(REPO_ROOT)})
        self.assertEqual(proc.returncode, 0, f"install failed:\n{proc.stdout}\n{proc.stderr}")
        return proc.stdout

    def test_install_lays_out_all_skills_lib_and_config(self):
        self._install()
        for skill in REQUIRED_SKILLS:
            self.assertTrue((self.skills_dest / skill / "SKILL.md").is_file(),
                            f"{skill}/SKILL.md missing after install")
        self.assertTrue((self.target / ".agents" / "devloops_lib" / "config.py").is_file())
        self.assertTrue((self.target / "devloops.config.yaml").is_file())

    def test_script_and_doc_paths_referenced_by_skills_resolve(self):
        self._install()
        checked = 0
        for skill in REQUIRED_SKILLS:
            skill_dir = self.skills_dest / skill
            docs = [skill_dir / "SKILL.md"] + sorted((skill_dir / "references").glob("*.md"))
            for doc in docs:
                if not doc.is_file():
                    continue
                text = doc.read_text(encoding="utf-8", errors="replace")
                for ref in sorted(set(SCRIPT_REF.findall(text))):
                    self.assertTrue((self.target / ".agents" / ref).is_file(),
                                    f"{doc.name} references {ref}, missing in installed layout")
                    checked += 1
                if doc.name == "SKILL.md":
                    for link in sorted(set(MD_LINK.findall(text))):
                        self.assertTrue((skill_dir / link).is_file(),
                                        f"{doc.name} links to {link}, missing after install")
                        checked += 1
        self.assertGreaterEqual(checked, len(REQUIRED_SKILLS),
                                "expected at least one resolvable reference per skill")

    def test_next_steps_doctor_path_exists(self):
        output = self._install()
        # Tolerant of either guidance form (bare or quoted path, lib or skill wrapper):
        # the named doctor entry point must be one of the installed candidates and exist.
        candidates = [
            self.target / ".agents" / "devloops_lib" / "doctor.py",
            self.target / ".agents" / "skills" / "worker-loop" / "scripts" / "doctor.py",
        ]
        hits = [c for c in candidates if str(c) in output]
        self.assertTrue(hits, "install next-steps guidance must name an installed doctor.py path")
        for candidate in hits:
            self.assertTrue(candidate.is_file(),
                            f"guidance doctor path missing: {candidate}")

    def test_installed_skill_scripts_run_help_from_unrelated_cwd(self):
        self._install()
        # doctor.py ignores argv (it runs full environment diagnostics and its exit
        # code reflects the machine), so it gets a compile check instead of --help.
        scripts = [s for s in sorted(self.skills_dest.glob("*/scripts/*.py"))
                   if not s.name.startswith("test_") and s.name != "doctor.py"]
        self.assertGreaterEqual(len(scripts), 3, "expected installed skill scripts to smoke-test")
        for script in scripts:
            proc = _run([sys.executable, str(script), "--help"], cwd=self.cwd_dir)
            self.assertEqual(proc.returncode, 0,
                             f"{script.relative_to(self.target)} --help failed:\n{proc.stderr}")

    def test_installed_doctor_scripts_compile(self):
        self._install()
        doctors = sorted(self.skills_dest.glob("*/scripts/doctor.py"))
        self.assertTrue(doctors, "worker-loop doctor.py wrapper must be installed")
        for doctor in doctors:
            proc = _run([sys.executable, "-m", "py_compile", str(doctor)], cwd=self.cwd_dir)
            self.assertEqual(proc.returncode, 0,
                             f"{doctor.relative_to(self.target)} failed to compile:\n{proc.stderr}")

    def test_installed_lib_imports_from_relocated_layout(self):
        self._install()
        proc = _run(
            [sys.executable, "-c", "import devloops_lib.config, devloops_lib.tick_gate, devloops_lib.gh"],
            cwd=self.cwd_dir,
            env_extra={"PYTHONPATH": str(self.target / ".agents")},
        )
        self.assertEqual(proc.returncode, 0, f"installed devloops_lib import failed:\n{proc.stderr}")

    def test_rerun_from_another_unrelated_cwd_is_idempotent(self):
        self._install()
        second_cwd = self.tmp_dir / "other place"
        second_cwd.mkdir()
        code = (
            "import sys; from devloops_lib.cli import main; "
            f"sys.exit(main(['install', '--target', {str(self.target)!r}]))"
        )
        proc = _run([sys.executable, "-c", code], cwd=second_cwd,
                    env_extra={"PYTHONPATH": str(REPO_ROOT)})
        self.assertEqual(proc.returncode, 0, f"re-run failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("[skipped (already exists)]", proc.stdout)


if __name__ == "__main__":
    unittest.main()
