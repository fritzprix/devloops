#!/usr/bin/env python3
"""Tests for devloops CLI and install command."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import cli, tick_gate


class CliInstallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name).resolve()

    def test_install_fresh_project(self):
        target = self.tmp_dir / "my_project"
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            ret = cli.main(["install", "--target", str(target)])
        self.assertEqual(ret, 0)
        output = stdout_buf.getvalue()

        # Check skills
        for s in ["worker-loop", "gatekeeper-loop", "producer-loop", "pm-loop"]:
            self.assertTrue((target / ".agents" / "skills" / s / "SKILL.md").is_file())

        # Each skill carries its own devloops_lib; there is no shared copy
        for s in ["worker-loop", "gatekeeper-loop", "producer-loop", "pm-loop"]:
            lib = target / ".agents" / "skills" / s / "scripts" / "devloops_lib"
            self.assertTrue((lib / "config.py").is_file())
            self.assertTrue((lib / "cli.py").is_file())
        self.assertFalse((target / ".agents" / "devloops_lib").exists())

        # Check config
        self.assertTrue((target / "devloops.config.yaml").is_file())

        # Check stdout reporting (S1.3)
        self.assertIn("[installed]", output)
        self.assertIn("devloops installed into:", output)
        # Guidance must use a stable command form, not the install-time interpreter
        # (under `uvx --from` that is an ephemeral cache path that breaks later).
        doctor = target.resolve() / ".agents" / "skills" / "worker-loop" / "scripts" / "doctor.py"
        self.assertIn(f'     python3 "{doctor}"', output)
        self.assertNotIn(sys.executable, output)

    def test_install_target_with_spaces(self):
        target = self.tmp_dir / "my project with spaces"
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            ret = cli.main(["install", "--target", str(target)])
        output = stdout_buf.getvalue()
        # G1.1: the doctor next step must be paste-able: quoted full path
        doctor = target.resolve() / ".agents" / "skills" / "worker-loop" / "scripts" / "doctor.py"
        self.assertIn(f'     python3 "{doctor}"', output)
        self.assertEqual(ret, 0)
        self.assertTrue((target / ".agents" / "skills" / "worker-loop" / "SKILL.md").is_file())
        self.assertTrue((target / "devloops.config.yaml").is_file())

    def test_install_rerun_preserves_existing_files(self):
        target = self.tmp_dir / "rerun_project"
        cli.main(["install", "--target", str(target)])

        # Modify config and a skill file
        config_file = target / "devloops.config.yaml"
        custom_content = "# Custom user config"
        config_file.write_text(custom_content, encoding="utf-8")

        skill_file = target / ".agents" / "skills" / "worker-loop" / "SKILL.md"
        custom_skill = "# Custom user skill"
        skill_file.write_text(custom_skill, encoding="utf-8")

        # Rerun install and capture stdout (S1.3)
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            ret = cli.main(["install", "--target", str(target)])
        self.assertEqual(ret, 0)
        output = stdout_buf.getvalue()

        # Ensure user edits were preserved
        self.assertEqual(config_file.read_text(encoding="utf-8"), custom_content)
        self.assertEqual(skill_file.read_text(encoding="utf-8"), custom_skill)

        # Assert skipped status reported in stdout
        self.assertIn("[skipped (already exists)]", output)

    def test_missing_assets_fails(self):
        target = self.tmp_dir / "fail_project"
        with patch("devloops_lib.cli.get_asset_paths", side_effect=RuntimeError("Assets missing")):
            ret = cli.main(["install", "--target", str(target)])
            self.assertNotEqual(ret, 0)

    def test_skill_with_assets_directory_is_preserved(self):
        """G1.1: Ensure skills containing an assets/ directory are not stripped."""
        custom_skills_dir = self.tmp_dir / "custom_skills"
        custom_skills_dir.mkdir()

        # Copy required skills
        for s in ["worker-loop", "gatekeeper-loop", "producer-loop", "pm-loop"]:
            s_dir = custom_skills_dir / s
            s_dir.mkdir()
            (s_dir / "SKILL.md").write_text(f"# {s}", encoding="utf-8")

        # Add a skill with an assets/ directory
        asset_skill_dir = custom_skills_dir / "skill-with-assets"
        assets_subdir = asset_skill_dir / "assets"
        assets_subdir.mkdir(parents=True)
        (asset_skill_dir / "SKILL.md").write_text("# Skill with assets", encoding="utf-8")
        (assets_subdir / "x.txt").write_text("asset file content", encoding="utf-8")

        templates_dir = self.tmp_dir / "custom_templates"
        templates_dir.mkdir()
        (templates_dir / "devloops.config.yaml").write_text("config: true", encoding="utf-8")

        target = self.tmp_dir / "target_with_skill_assets"
        with patch("devloops_lib.cli.get_asset_paths", return_value=(custom_skills_dir, templates_dir)):
            ret = cli.main(["install", "--target", str(target)])
            self.assertEqual(ret, 0)

        installed_asset_file = target / ".agents" / "skills" / "skill-with-assets" / "assets" / "x.txt"
        self.assertTrue(installed_asset_file.is_file(), "Skill assets/x.txt must be installed")
        self.assertEqual(installed_asset_file.read_text(encoding="utf-8"), "asset file content")

    def test_bundled_wheel_package_layout(self):
        """G1.2: Verify bundled-wheel layout asset resolution; the package itself is not installed."""
        fake_pkg = self.tmp_dir / "fake_installed_pkg"
        fake_pkg.mkdir()

        # Create bundled wheel structure: pkg_dir/assets/skills and pkg_dir/assets/templates
        bundled_skills = fake_pkg / "assets" / "skills"
        bundled_templates = fake_pkg / "assets" / "templates"
        for s in ["worker-loop", "gatekeeper-loop", "producer-loop", "pm-loop"]:
            s_dir = bundled_skills / s
            s_dir.mkdir(parents=True)
            (s_dir / "SKILL.md").write_text(f"# {s}", encoding="utf-8")

        bundled_templates.mkdir(parents=True)
        (bundled_templates / "devloops.config.yaml").write_text("# bundled config", encoding="utf-8")

        # Fake files in package root
        fake_cli_py = fake_pkg / "cli.py"
        fake_cli_py.write_text("# fake cli", encoding="utf-8")
        (fake_pkg / "doctor.py").write_text("# fake doctor", encoding="utf-8")

        # Test get_asset_paths resolution with fake pkg
        with patch.object(cli, "__file__", str(fake_cli_py.resolve())):
            resolved_skills, resolved_templates = cli.get_asset_paths()
            self.assertEqual(resolved_skills.resolve(), bundled_skills.resolve())
            self.assertEqual(resolved_templates.resolve(), bundled_templates.resolve())

            target = self.tmp_dir / "target_from_bundled"
            ret = cli.install_command(target)
            self.assertEqual(ret, 0)

            self.assertTrue((target / ".agents" / "skills" / "worker-loop" / "SKILL.md").is_file())
            self.assertFalse((target / ".agents" / "devloops_lib").exists())



    def test_concurrent_tick_runs(self):
        state_file = self.tmp_dir / "worker-loop" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text('{"current": 101}', encoding="utf-8")

        import concurrent.futures
        import time

        sleep_cmd = f'"{sys.executable}" -c "import time; time.sleep(0.4)"'

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(cli.main, ["tick", "worker", "--agent-cmd", sleep_cmd, "--state-file", str(state_file)])
            time.sleep(0.1)
            f2 = executor.submit(cli.main, ["tick", "worker", "--state-file", str(state_file)])
            r1 = f1.result()
            r2 = f2.result()

        # One ran (code 0), one got BUSY (code 2)
        self.assertEqual(r1, 0)
        self.assertEqual(r2, 2)

        # After run ends, next run gets the lock
        r3 = cli.main(["tick", "worker", "--state-file", str(state_file)])
        self.assertEqual(r3, 0)

    def test_tick_flock_does_not_open_the_lock_file(self):
        # Opening tick.lock for flock creates an empty file, which reads as a
        # live owner and makes every POSIX tick report BUSY against itself.
        state_file = self.tmp_dir / "worker-loop" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text('{"current": 101}', encoding="utf-8")

        with patch.object(cli, "acquire_flock", return_value=1) as acquire_flock, \
                patch.object(cli, "release_flock"):
            self.assertEqual(cli.main(["tick", "worker", "--state-file", str(state_file)]), 0)

        self.assertNotEqual(acquire_flock.call_args[0][0], state_file.parent / "tick.lock")

    def test_first_tick_of_a_new_slot_runs(self):
        # The state directory does not exist yet: the flock file must be created, not read as BUSY.
        state_file = self.tmp_dir / "producer-loop" / "agents" / "new" / "state.json"
        self.assertFalse(state_file.parent.exists())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["tick", "producer", "--state-file", str(state_file)]), 0)
        self.assertIn("UNLOCKED", out.getvalue())

    def test_unopenable_flock_file_is_an_error_not_busy(self):
        state_file = self.tmp_dir / "worker-loop" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text('{"current": 101}', encoding="utf-8")
        with patch.object(cli, "acquire_flock", side_effect=PermissionError("denied")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["tick", "worker", "--state-file", str(state_file)]), 3)
        self.assertIn("cannot open the flock file", err.getvalue())
        self.assertFalse((state_file.parent / "tick.lock").exists())

    def test_agent_under_wrapper_can_run_its_gate_commands(self):
        # The agent follows SKILL.md: gate --lock at the start, --unlock at the end.
        # It must not get BUSY from the wrapper's own lock or release that lock.
        state_file = self.tmp_dir / "worker-loop" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text('{"current": 101}', encoding="utf-8")
        lock_file = state_file.parent / "tick.lock"
        results_file = self.tmp_dir / "results.json"
        gate = Path(cli.__file__).resolve().parent.parent / "skills" / "worker-loop" / "scripts" / "tick_gate.py"
        agent = self.tmp_dir / "agent.py"
        agent.write_text(
            "import json, subprocess, sys\n"
            "from pathlib import Path\n"
            "def gate(flag):\n"
            f"    r = subprocess.run([sys.executable, {str(gate)!r}, flag, '--state-file', {str(state_file)!r}],\n"
            "                       capture_output=True, text=True)\n"
            f"    return [r.returncode, r.stdout.strip(), Path({str(lock_file)!r}).exists()]\n"
            f"Path({str(results_file)!r}).write_text(json.dumps([gate('--lock'), gate('--unlock')]))\n",
            encoding="utf-8",
        )

        rc = cli.main(["tick", "worker", "--agent-cmd", f'"{sys.executable}" "{agent}"', "--state-file", str(state_file)])

        self.assertEqual(rc, 0)
        locked, unlocked = json.loads(results_file.read_text())
        # Inside the wrapper, --lock reports the wrapper's own token instead of BUSY.
        self.assertEqual(locked[0], 0)
        self.assertEqual(locked[1].splitlines()[0], "RUN (current work in progress)")
        self.assertRegex(locked[1].splitlines()[1], r"^TOKEN [0-9a-f]{32}$")
        self.assertTrue(locked[2])
        self.assertEqual(unlocked, [0, "LOCK KEPT (held by devloops tick)", True])
        self.assertFalse(lock_file.exists())

    def test_tick_agent_id_scopes_the_lock_and_reaches_the_agent(self):
        base = self.tmp_dir / "state" / "worker-loop" / "agents"
        for agent_id in ("a", "b"):
            (base / agent_id).mkdir(parents=True)
            (base / agent_id / "state.json").write_text('{"current": 101}', encoding="utf-8")
        seen = self.tmp_dir / "seen.txt"
        agent = self.tmp_dir / "agent.py"
        agent.write_text(
            "import os\n"
            f"open({str(seen)!r}, 'w').write(os.environ['DEVLOOPS_AGENT_ID'])\n",
            encoding="utf-8",
        )
        # Agent a holds its lock, so a is busy and b is not.
        tick_gate.acquire_lock(base / "a" / "tick.lock", pid=os.getpid())
        agent_cmd = f'"{sys.executable}" "{agent}"'

        with patch.dict(os.environ, {"LOOP_STATE_DIR": str(self.tmp_dir / "state")}):
            busy = cli.main(["tick", "worker", "--agent-id", "a", "--agent-cmd", agent_cmd])
            self.assertFalse(seen.exists())
            free = cli.main(["tick", "worker", "--agent-id", "b", "--agent-cmd", agent_cmd])

        self.assertEqual((busy, free), (2, 0))
        self.assertEqual(seen.read_text(encoding="utf-8"), "b")
        self.assertTrue((base / "a" / "tick.lock").exists())
        self.assertFalse((self.tmp_dir / "state" / "worker-loop" / "tick.lock").exists())

    def test_tick_rejects_an_unsafe_agent_id(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli.main(["tick", "worker", "--agent-id", "../x"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
