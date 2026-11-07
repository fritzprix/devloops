#!/usr/bin/env python3
"""Tests for devloops CLI and install command."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import cli


class CliInstallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)

    def test_install_fresh_project(self):
        target = self.tmp_dir / "my_project"
        ret = cli.main(["install", "--target", str(target)])
        self.assertEqual(ret, 0)

        # Check skills
        for s in ["worker-loop", "gatekeeper-loop", "producer-loop"]:
            self.assertTrue((target / ".agents" / "skills" / s / "SKILL.md").is_file())

        # Check devloops_lib
        self.assertTrue((target / ".agents" / "devloops_lib" / "config.py").is_file())
        self.assertTrue((target / ".agents" / "devloops_lib" / "doctor.py").is_file())

        # Check config
        self.assertTrue((target / "devloops.config.yaml").is_file())

    def test_install_target_with_spaces(self):
        target = self.tmp_dir / "my project with spaces"
        ret = cli.main(["install", "--target", str(target)])
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

        # Rerun install
        ret = cli.main(["install", "--target", str(target)])
        self.assertEqual(ret, 0)

        # Ensure user edits were preserved
        self.assertEqual(config_file.read_text(encoding="utf-8"), custom_content)
        self.assertEqual(skill_file.read_text(encoding="utf-8"), custom_skill)

    def test_missing_assets_fails(self):
        target = self.tmp_dir / "fail_project"
        with patch("devloops_lib.cli.get_asset_paths", side_effect=RuntimeError("Assets missing")):
            ret = cli.main(["install", "--target", str(target)])
            self.assertNotEqual(ret, 0)


if __name__ == "__main__":
    unittest.main()
