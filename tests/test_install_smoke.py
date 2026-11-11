#!/usr/bin/env python3
"""Automated relocated-install smoke test (issue #8 acceptance criteria).

Installer under test:
- By default the installer runs from the SOURCE TREE (subprocess with
  PYTHONPATH=<repo root>), in a separate process, unrelated cwd, explicit --target.
- With DEVLOOPS_SMOKE_INSTALLER set to a JSON argv list (for example
  ["uvx", "--from", "/abs/devloops-0.1.0-py3-none-any.whl", "devloops"]), the
  same checks run against that installer instead, with no PYTHONPATH, so the
  assets come from the built wheel. scripts/ci_smoke.py does this in CI.
- All post-install checks run with PYTHONPATH removed, from unrelated
  directories, against the installed layout only.

Checks:
- every `$SKILL_DIR/<rel>.py` script reference (and any legacy `skills/...`
  form) in installed skill docs resolves under the installed layout, counted
  per skill so a future path-form change fails loudly instead of passing empty;
- every relative markdown link in installed skill docs (SKILL.md and
  references/*.md, including sibling-skill links) resolves against the doc's
  own directory;
- the quoted doctor path printed in the next-steps guidance is exactly the
  installed skill-wrapper entry point;
- the documented library-import one-liner from worktree-setup.md runs verbatim
  from the installed project root with no PYTHONPATH;
- installed skill scripts run `--help` (exit 0) from an unrelated cwd,
  bootstrapping their own imports from the skill's own devloops_lib;
- doctor entry points compile, and `doctor.py --init` writes the bundled template;
- re-install from a second unrelated cwd keeps edited skill files and the
  edited config, and reports them as skipped;
- no dev-only test wrappers are installed.

Cross-platform: plain subprocess calls, no POSIX-only commands.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_SKILLS = ["worker-loop", "gatekeeper-loop", "producer-loop"]

# `python3 "$SKILL_DIR/<rel>.py"` commands in skill docs; resolved against the
# skill's own installed directory.
SKILL_DIR_REF = re.compile(r"\$SKILL_DIR/([A-Za-z0-9_./-]+\.py)")
# Legacy `skills/<skill>/.../<name>.py` form; resolved under the installed `.agents/`.
LEGACY_SCRIPT_REF = re.compile(r"skills/[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+\.py")
# Relative markdown links, e.g. (references/x.md) or (../worker-loop/references/y.md).
MD_LINK = re.compile(r"\]\((?!https?://|mailto:)([^)#\s]+\.md)(?:#[^)\s]*)?\)")
# The documented base-branch library-import one-liner (bash form) in worktree-setup.md.
DOC_ONELINER = re.compile(r'BASE=\$\(python3 -c "(.+?)" "\$SKILL_DIR/scripts"\)')


def _run(cmd: list[str], cwd: Path, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=env, timeout=120)


INSTALLER_ENV = "DEVLOOPS_SMOKE_INSTALLER"


def _installer() -> list[str] | None:
    """Return the installer argv from $DEVLOOPS_SMOKE_INSTALLER, or None for the source tree."""
    raw = os.environ.get(INSTALLER_ENV)
    if not raw:
        return None
    argv = json.loads(raw)
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError(f"{INSTALLER_ENV} must be a non-empty JSON list of strings")
    return argv


def _install(cwd_dir: Path, target: Path) -> str:
    """Run the CLI install from an unrelated cwd into the target; return stdout."""
    installer = _installer()
    if installer:
        proc = _run([*installer, "install", "--target", str(target)], cwd=cwd_dir)
    else:
        code = (
            "import sys; from devloops_lib.cli import main; "
            f"sys.exit(main(['install', '--target', {str(target)!r}]))"
        )
        proc = _run([sys.executable, "-c", code], cwd=cwd_dir,
                    env_extra={"PYTHONPATH": str(REPO_ROOT)})
    if proc.returncode != 0:
        raise RuntimeError(f"install failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def _skill_docs(skills_dest: Path, skill: str) -> list[Path]:
    skill_dir = skills_dest / skill
    return [skill_dir / "SKILL.md"] + sorted((skill_dir / "references").glob("*.md"))


class RelocatedInstallSmokeTest(unittest.TestCase):
    """Read-only checks against one class-level install (S1.2)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp_dir = Path(cls._tmp.name).resolve()
        # Fresh, empty, unrelated current directory (never the repo root).
        cls.cwd_dir = cls.tmp_dir / "elsewhere"
        cls.cwd_dir.mkdir()
        # Target path contains spaces.
        cls.target = cls.tmp_dir / "my project with spaces" / "my project"
        cls.skills_dest = cls.target / ".agents" / "skills"
        cls.install_output = _install(cls.cwd_dir, cls.target)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_install_lays_out_all_skills_lib_and_config(self):
        for skill in REQUIRED_SKILLS:
            self.assertTrue((self.skills_dest / skill / "SKILL.md").is_file(),
                            f"{skill}/SKILL.md missing after install")
            self.assertTrue((self.skills_dest / skill / "scripts" / "devloops_lib" / "config.py").is_file(),
                            f"{skill} must carry its own devloops_lib")
        self.assertTrue((self.target / "devloops.config.yaml").is_file())

    def test_skill_script_references_resolve_and_are_counted_per_skill(self):
        total = 0
        for skill in REQUIRED_SKILLS:
            skill_dir = self.skills_dest / skill
            resolved = set()
            for doc in _skill_docs(self.skills_dest, skill):
                if not doc.is_file():
                    continue
                text = doc.read_text(encoding="utf-8", errors="replace")
                for rel in SKILL_DIR_REF.findall(text):
                    path = skill_dir / rel
                    self.assertTrue(path.is_file(),
                                    f"{skill}/{doc.name}: $SKILL_DIR/{rel} missing in installed layout")
                    resolved.add(path)
                for ref in LEGACY_SCRIPT_REF.findall(text):
                    path = self.target / ".agents" / ref
                    self.assertTrue(path.is_file(),
                                    f"{skill}/{doc.name}: {ref} missing in installed layout")
                    resolved.add(path)
            self.assertGreaterEqual(
                len(resolved), 1,
                f"{skill}: no resolvable script reference — documented command path form likely changed",
            )
            total += len(resolved)
        self.assertGreaterEqual(total, 5,
                                "expected the documented script set (>=5 distinct installed scripts)")

    def test_relative_markdown_links_resolve_against_doc_dir(self):
        # Covers references/*.md links AND sibling-skill links such as
        # producer-loop SKILL.md -> ../worker-loop/references/scheduling.md (#8 task 5).
        producer_skill_md = self.skills_dest / "producer-loop" / "SKILL.md"
        self.assertIn("](../worker-loop/references/scheduling.md)",
                      producer_skill_md.read_text(encoding="utf-8"),
                      "sibling-skill scheduling.md link must remain documented")
        checked = 0
        for skill in REQUIRED_SKILLS:
            for doc in _skill_docs(self.skills_dest, skill):
                if not doc.is_file():
                    continue
                text = doc.read_text(encoding="utf-8", errors="replace")
                for link in sorted(set(MD_LINK.findall(text))):
                    resolved = (doc.parent / link).resolve()
                    self.assertTrue(resolved.is_file(),
                                    f"{skill}/{doc.name}: markdown link ({link}) -> {resolved} missing")
                    checked += 1
        self.assertGreaterEqual(checked, 1, "no markdown links found to check")

    def test_next_steps_doctor_guidance_is_exact_installed_path(self):
        # Post-#20 the guidance prints exactly the quoted skill-wrapper path.
        doctor = self.skills_dest / "worker-loop" / "scripts" / "doctor.py"
        self.assertTrue(doctor.is_file(), f"installed doctor wrapper missing: {doctor}")
        self.assertIn(f'python3 "{doctor}"', self.install_output,
                      "next-steps guidance must print the exact quoted installed doctor path")

    def test_documented_library_import_oneliner_runs_from_target(self):
        # The one-liner from the installed worktree-setup.md, run verbatim from
        # the installed project root with no PYTHONPATH (it previously failed
        # with ModuleNotFoundError in a relocated install).
        doc = self.skills_dest / "worker-loop" / "references" / "worktree-setup.md"
        self.assertTrue(doc.is_file(), f"installed doc missing: {doc}")
        match = DOC_ONELINER.search(doc.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "documented base-branch one-liner missing from worktree-setup.md")
        code = match.group(1)
        self.assertIn("sys.path.insert(0, sys.argv[1])", code)
        self.assertIn("base_branch", code)
        skill_scripts = self.skills_dest / "worker-loop" / "scripts"
        proc = _run([sys.executable, "-c", code, str(skill_scripts)], cwd=self.target)
        self.assertEqual(proc.returncode, 0,
                         f"documented one-liner failed in relocated install:\n{proc.stderr}")
        self.assertEqual(proc.stdout.strip(), "main")

    def test_installed_skill_scripts_run_help_from_unrelated_cwd(self):
        scripts = sorted(self.skills_dest.glob("*/scripts/*.py"))
        self.assertGreaterEqual(len(scripts), 3, "expected installed skill scripts to smoke-test")
        for script in scripts:
            proc = _run([sys.executable, str(script), "--help"], cwd=self.cwd_dir)
            self.assertEqual(proc.returncode, 0,
                             f"{script.relative_to(self.target)} --help failed:\n{proc.stderr}")

    def test_installed_doctor_scripts_compile(self):
        doctors = sorted(self.skills_dest.glob("*/scripts/doctor.py"))
        self.assertTrue(doctors, "worker-loop doctor.py wrapper must be installed")
        for doctor in doctors:
            proc = _run([sys.executable, "-m", "py_compile", str(doctor)], cwd=self.cwd_dir)
            self.assertEqual(proc.returncode, 0,
                             f"{doctor.relative_to(self.target)} failed to compile:\n{proc.stderr}")

    def test_installed_lib_imports_from_relocated_layout(self):
        # Skill installers copy one skill directory, so each skill's own copy must import.
        for skill in REQUIRED_SKILLS:
            proc = _run(
                [sys.executable, "-c", "import devloops_lib.config, devloops_lib.tick_gate, devloops_lib.gh, "
                                       "devloops_lib.claims, devloops_lib.guard, devloops_lib.cli"],
                cwd=self.cwd_dir,
                env_extra={"PYTHONPATH": str(self.skills_dest / skill / "scripts")},
            )
            self.assertEqual(proc.returncode, 0, f"{skill} devloops_lib import failed:\n{proc.stderr}")

    def test_skill_tick_wrapper_runs_agent_under_lock(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name).resolve() / "tick project"
        (project / ".git").mkdir(parents=True)
        tick = self.skills_dest / "gatekeeper-loop" / "scripts" / "tick.py"
        agent = subprocess.list2cmdline([sys.executable, "-c", "print('agent ran')"])
        proc = _run([sys.executable, str(tick), "--agent-cmd", agent], cwd=project)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LOCKED (gatekeeper-loop)", proc.stdout)
        self.assertIn("agent ran", proc.stdout)
        self.assertIn("UNLOCKED", proc.stdout)
        self.assertFalse((project / ".devloops" / "gatekeeper-loop" / "tick.lock").exists())

    def test_doctor_init_creates_config_from_skill_template(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name).resolve() / "fresh project"
        (project / ".git").mkdir(parents=True)
        doctor = self.skills_dest / "worker-loop" / "scripts" / "doctor.py"
        proc = _run([sys.executable, str(doctor), "--init"], cwd=project)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((project / "devloops.config.yaml").read_text(encoding="utf-8"),
                         (REPO_ROOT / "templates" / "devloops.config.yaml").read_text(encoding="utf-8"))

    def test_no_dev_only_test_wrappers_are_installed(self):
        self.assertEqual(sorted(self.skills_dest.glob("*/scripts/test_*.py")), [])

    def test_rerun_from_another_unrelated_cwd_keeps_edits(self):
        # Own target (mutates state), separate from the class-level install.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        target = base / "rerun target"
        first_cwd = base / "cwd one"
        first_cwd.mkdir()
        second_cwd = base / "cwd two"
        second_cwd.mkdir()
        _install(first_cwd, target)
        skill_md = target / ".agents" / "skills" / "worker-loop" / "SKILL.md"
        config = target / "devloops.config.yaml"
        skill_md.write_text("edited skill\n", encoding="utf-8")
        config.write_text("base_branch: dev\n", encoding="utf-8")
        second = _install(second_cwd, target)
        self.assertEqual(skill_md.read_text(encoding="utf-8"), "edited skill\n")
        self.assertEqual(config.read_text(encoding="utf-8"), "base_branch: dev\n")
        for path in (Path(".agents") / "skills" / "worker-loop" / "SKILL.md", Path("devloops.config.yaml")):
            self.assertIn(f"[skipped (already exists)] {path}", second)
        self.assertNotIn("[installed]", second)


if __name__ == "__main__":
    unittest.main()
