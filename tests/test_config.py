#!/usr/bin/env python3
"""Tests for lib/config.py."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from lib import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)

    def test_default_config_values(self):
        cfg = config.DEFAULT_CONFIG
        self.assertEqual(cfg["project"]["repo"], "owner/name")
        self.assertEqual(cfg["project"]["default_branch"], "main")
        self.assertEqual(cfg["project"]["base_branch"], "main")
        self.assertEqual(cfg["commands"]["test"], "")
        self.assertEqual(cfg["commands"]["lint"], "")
        self.assertEqual(cfg["commands"]["build"], "")
        self.assertEqual(cfg["protected_paths"], [])
        self.assertEqual(cfg["state_dir"], ".devloops")
        self.assertEqual(cfg["labels"]["producer"], "auto:producer")
        self.assertEqual(cfg["labels"]["validated"], "validated")
        self.assertEqual(cfg["labels"]["rejected"], "rejected")
        self.assertEqual(cfg["labels"]["approved"], "approved")
        self.assertEqual(cfg["scheduler"], "shell")

    def test_parse_yaml_subset(self):
        yaml_text = """
# Project settings
project:
  repo: my-org/my-project
  base_branch: develop

commands:
  test: pytest
  lint: ruff check

protected_paths:
  - sensitive/
  - config/

state_dir: custom_state

labels:
  producer: bot:producer
  approved: human:approved

producer:
  max_new_issues_per_run: 5
  tasks: [security, cleanup]

scheduler: cron
"""
        parsed = config.parse_yaml_subset(yaml_text)
        self.assertEqual(parsed["project"]["repo"], "my-org/my-project")
        self.assertEqual(parsed["project"]["base_branch"], "develop")
        self.assertEqual(parsed["commands"]["test"], "pytest")
        self.assertEqual(parsed["commands"]["lint"], "ruff check")
        self.assertEqual(parsed["protected_paths"], ["sensitive/", "config/"])
        self.assertEqual(parsed["state_dir"], "custom_state")
        self.assertEqual(parsed["labels"]["producer"], "bot:producer")
        self.assertEqual(parsed["labels"]["approved"], "human:approved")
        self.assertEqual(parsed["producer"]["max_new_issues_per_run"], 5)
        self.assertEqual(parsed["producer"]["tasks"], ["security", "cleanup"])
        self.assertEqual(parsed["scheduler"], "cron")

    def test_load_config_with_yaml_file(self):
        sub_dir = self.tmp_dir / "a" / "b"
        sub_dir.mkdir(parents=True)
        config_file = self.tmp_dir / "devloops.config.yaml"
        config_file.write_text(
            """
project:
  repo: custom/repo
commands:
  test: npm test
""",
            encoding="utf-8",
        )
        # Load from nested sub_dir (walk up)
        cfg = config.load_config(start_dir=sub_dir)
        self.assertEqual(cfg["project"]["repo"], "custom/repo")
        # Unspecified fields in YAML should retain defaults
        self.assertEqual(cfg["project"]["base_branch"], "main")
        self.assertEqual(cfg["commands"]["test"], "npm test")
        self.assertEqual(cfg["state_dir"], ".devloops")

    def test_load_config_with_json_fallback(self):
        sub_dir = self.tmp_dir / "deep" / "dir"
        sub_dir.mkdir(parents=True)
        config_file = self.tmp_dir / "devloops.config.json"
        config_file.write_text(
            json.dumps({
                "project": {"repo": "json/repo"},
                "state_dir": ".json_state"
            }),
            encoding="utf-8",
        )
        cfg = config.load_config(start_dir=sub_dir)
        self.assertEqual(cfg["project"]["repo"], "json/repo")
        self.assertEqual(cfg["state_dir"], ".json_state")

    def test_load_config_defaults_when_missing(self):
        # Empty temp dir with no config
        cfg = config.load_config(start_dir=self.tmp_dir)
        self.assertEqual(cfg, config.DEFAULT_CONFIG)

    def test_get_state_and_lock_paths(self):
        config_file = self.tmp_dir / "devloops.config.yaml"
        config_file.write_text("state_dir: .custom_loops\n", encoding="utf-8")

        state_p = config.get_state_path("worker-loop", start_dir=self.tmp_dir)
        self.assertEqual(state_p, (self.tmp_dir / ".custom_loops" / "worker-loop" / "state.json").resolve())

        lock_p = config.get_lock_path("worker-loop", start_dir=self.tmp_dir)
        self.assertEqual(lock_p, (self.tmp_dir / ".custom_loops" / "worker-loop" / "tick.lock").resolve())

    def test_get_state_path_env_override(self):
        custom_env = self.tmp_dir / "env_state"
        with unittest.mock.patch.dict(os.environ, {"LOOP_STATE_DIR": str(custom_env)}):
            state_p = config.get_state_path("worker-loop", start_dir=self.tmp_dir)
            self.assertEqual(state_p, custom_env / "worker-loop" / "state.json")


if __name__ == "__main__":
    unittest.main()
