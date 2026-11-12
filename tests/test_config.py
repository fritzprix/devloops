#!/usr/bin/env python3
"""Tests for devloops_lib/config.py."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from devloops_lib import config


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
        self.assertIn("browser", config.environment_capabilities(cfg))
        self.assertEqual(cfg["protected_paths"], [])
        self.assertEqual(cfg["cleanup"]["protected_branches"], ["main", "master", "develop", "release"])
        self.assertEqual(cfg["state_dir"], ".devloops")
        self.assertEqual(cfg["labels"]["producer"], "auto:producer")
        self.assertEqual(cfg["labels"]["validated"], "validated")
        self.assertEqual(cfg["labels"]["rejected"], "rejected")
        self.assertEqual(cfg["labels"]["approved"], "approved")
        self.assertEqual(cfg["producer"]["task_periods"]["cleanup"], "7d")
        self.assertEqual(cfg["producer"]["default_period"], "24h")
        self.assertEqual(cfg["scheduler"], "shell")
        self.assertEqual(cfg["worker"]["lock_ttl_seconds"], 1800)

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

    def _with_agent(self, agent_id):
        return unittest.mock.patch.dict(os.environ, {config.AGENT_ID_ENV: agent_id})

    def test_two_agents_get_separate_state_lock_and_worktree_paths(self):
        main = self.tmp_dir / "proj"
        paths = {}
        for agent_id in ("a", "b"):
            with self._with_agent(agent_id):
                paths[agent_id] = (
                    config.get_state_path("worker-loop", start_dir=self.tmp_dir),
                    config.get_lock_path("worker-loop", start_dir=self.tmp_dir),
                    config.get_worktree_path(main),
                )
        base = (self.tmp_dir / ".devloops" / "worker-loop").resolve()
        self.assertEqual(paths["a"][0], base / "agents" / "a" / "state.json")
        self.assertEqual(paths["b"][1], base / "agents" / "b" / "tick.lock")
        self.assertEqual(paths["a"][2], self.tmp_dir / "proj.worktrees" / "worker-a")
        for first, second in zip(paths["a"], paths["b"]):
            self.assertNotEqual(first, second)

    def test_without_agent_id_paths_do_not_change(self):
        main = self.tmp_dir / "proj"
        with unittest.mock.patch.dict(os.environ, {config.AGENT_ID_ENV: ""}):
            state_p = config.get_state_path("worker-loop", start_dir=self.tmp_dir)
            worktree = config.get_worktree_path(main)
        self.assertEqual(state_p, (self.tmp_dir / ".devloops" / "worker-loop" / "state.json").resolve())
        self.assertEqual(worktree, self.tmp_dir / "proj.worktrees" / "worker")

    def test_agent_id_applies_with_loop_state_dir_override(self):
        custom_env = self.tmp_dir / "env_state"
        with unittest.mock.patch.dict(os.environ, {"LOOP_STATE_DIR": str(custom_env), config.AGENT_ID_ENV: "a"}):
            state_p = config.get_state_path("worker-loop", start_dir=self.tmp_dir)
        self.assertEqual(state_p, custom_env / "worker-loop" / "agents" / "a" / "state.json")

    def test_agent_id_must_be_one_safe_path_component(self):
        for bad in ("..", ".", "../x", "a/b", "a\\b", ".a", "a.", "a b", "a:b"):
            with self.subTest(agent_id=bad), self._with_agent(bad), self.assertRaises(ValueError):
                config.get_state_path("worker-loop", start_dir=self.tmp_dir)
        for good in ("a", "A1", "worker-1", "a.b_c"):
            self.assertEqual(config.validate_agent_id(good), good)

    def test_agent_reset_hard_does_not_touch_the_other_worktree(self):
        def git(*args, cwd):
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                cwd=cwd, check=True, capture_output=True, text=True,
            )

        main = self.tmp_dir / "proj"
        main.mkdir()
        git("init", cwd=main)
        (main / "file.txt").write_text("base\n", encoding="utf-8")
        git("add", "file.txt", cwd=main)
        git("commit", "-m", "base", cwd=main)
        worktrees = {}
        for agent_id in ("a", "b"):
            with self._with_agent(agent_id):
                worktrees[agent_id] = config.get_worktree_path(main)
            git("worktree", "add", "--detach", str(worktrees[agent_id]), "HEAD", cwd=main)
            (worktrees[agent_id] / "file.txt").write_text("edited\n", encoding="utf-8")
            (worktrees[agent_id] / "new.txt").write_text("new\n", encoding="utf-8")

        git("reset", "--hard", cwd=worktrees["a"])
        git("clean", "-fd", cwd=worktrees["a"])

        self.assertEqual((worktrees["a"] / "file.txt").read_text(encoding="utf-8").strip(), "base")
        self.assertFalse((worktrees["a"] / "new.txt").exists())
        self.assertEqual((worktrees["b"] / "file.txt").read_text(encoding="utf-8").strip(), "edited")
        self.assertTrue((worktrees["b"] / "new.txt").exists())

    def test_documented_worktree_commands_use_the_agent_suffix(self):
        skills = Path(config.__file__).resolve().parent.parent / "skills"
        worker = (skills / "worker-loop" / "references" / "worktree-setup.md").read_text(encoding="utf-8")
        gatekeeper = (skills / "gatekeeper-loop" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(".worktrees/worker${DEVLOOPS_AGENT_ID:+-$DEVLOOPS_AGENT_ID}", worker)
        self.assertIn('.worktrees/worker$(if ($env:DEVLOOPS_AGENT_ID) { "-$env:DEVLOOPS_AGENT_ID" })', worker)
        self.assertIn(".worktrees/gatekeeper${DEVLOOPS_AGENT_ID:+-$DEVLOOPS_AGENT_ID}", gatekeeper)

    def test_new_config_keys_parsing(self):
        yaml_text = """
cleanup:
  protected_branches: [main, staging]

producer:
  task_skills:
    security: code-audit-expert
  task_periods:
    cleanup: 7d
    security: 12h
  task_max_new_issues:
    cleanup: 1
    security: 5
"""
        parsed = config.parse_yaml_subset(yaml_text)
        merged = config.deep_merge(config.DEFAULT_CONFIG, parsed)
        self.assertEqual(merged["cleanup"]["protected_branches"], ["main", "staging"])
        self.assertEqual(merged["producer"]["task_skills"]["security"], "code-audit-expert")
        self.assertEqual(merged["producer"]["task_periods"]["cleanup"], "7d")
        self.assertEqual(merged["producer"]["task_periods"]["security"], "12h")
        self.assertEqual(merged["producer"]["task_max_new_issues"]["cleanup"], 1)
        self.assertEqual(merged["producer"]["task_max_new_issues"]["security"], 5)

    def test_worktree_resolves_to_main_checkout(self):
        def git(*args, cwd):
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                cwd=cwd, check=True, capture_output=True, text=True,
            )

        main = self.tmp_dir / "proj"
        main.mkdir()
        git("init", cwd=main)
        (main / "devloops.config.yaml").write_text("state_dir: .custom_loops\n", encoding="utf-8")
        git("add", "devloops.config.yaml", cwd=main)
        git("commit", "-m", "init", cwd=main)

        wt = config.get_worktree_path(main, "worker")
        git("worktree", "add", "--detach", str(wt), "HEAD", cwd=main)

        # Worktree root and nested directory should both resolve to main
        self.assertEqual(config.resolve_worktree_main_root(wt), main.resolve())
        nested = wt / "subdir" / "deep"
        nested.mkdir(parents=True)
        self.assertEqual(config.resolve_worktree_main_root(nested), main.resolve())

        # find_project_root and find_config_file from wt resolve to main
        self.assertEqual(config.find_project_root(wt), main.resolve())
        self.assertEqual(config.find_project_root(nested), main.resolve())
        self.assertEqual(config.find_config_file(wt), (main / "devloops.config.yaml").resolve())

        # State and lock paths resolve to main's state_dir
        expected_state = (main / ".custom_loops" / "worker-loop" / "state.json").resolve()
        expected_lock = (main / ".custom_loops" / "worker-loop" / "tick.lock").resolve()
        expected_claims = (main / ".custom_loops" / "claims").resolve()
        self.assertEqual(config.get_state_path("worker-loop", start_dir=wt), expected_state)
        self.assertEqual(config.get_lock_path("worker-loop", start_dir=wt), expected_lock)
        self.assertEqual(config.get_claims_dir(start_dir=wt), expected_claims)

        # get_worktree_path called with wt as input does not double-nest .worktrees
        self.assertEqual(config.get_worktree_path(wt, "worker"), wt.resolve())


class ConfigErrorTest(unittest.TestCase):
    def test_unreadable_config_raises_instead_of_using_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "devloops.config.json").write_text("{", encoding="utf-8")
            with self.assertRaises(config.ConfigError):
                config.load_config(start_dir=tmp)
            (Path(tmp) / "devloops.config.json").write_text("[1]", encoding="utf-8")
            with self.assertRaises(config.ConfigError):
                config.load_config(start_dir=tmp)

    def test_worker_limits_are_read_and_validated(self):
        caps = {"environment": {"capabilities": ["cli", "network"]}}
        self.assertEqual(config.environment_capabilities(caps), ("cli", "network"))
        self.assertEqual(config.open_pr_limit({}), 5)
        self.assertEqual(config.issue_claim_ttl_seconds({}), 7200)
        parsed = config.parse_yaml_subset("worker:\n  open_pr_limit: 2\n  issue_claim_ttl_minutes: 1.5\n")
        self.assertEqual((config.open_pr_limit(parsed), config.issue_claim_ttl_seconds(parsed)), (2, 90))
        for bad in (0, -1, "5", True):
            with self.subTest(value=bad), self.assertRaises(config.ConfigError):
                config.open_pr_limit({"worker": {"open_pr_limit": bad}})

    def test_entry_points_exit_3_not_1_on_a_broken_config(self):
        # Exit 1 means NOOP to the gates, so a broken config must not look like "nothing to do".
        root = Path(__file__).resolve().parent.parent
        scripts = [
            [root / "skills" / "worker-loop" / "scripts" / "tick_gate.py", "--lock"],
            [root / "skills" / "worker-loop" / "scripts" / "claim.py", "pr", "1", "--token", "t"],
            [root / "skills" / "worker-loop" / "scripts" / "guard.py", "check", "--token", "t"],
            [root / "skills" / "worker-loop" / "scripts" / "plan_tick.py"],
            [root / "skills" / "producer-loop" / "scripts" / "producer_gate.py", "--next", "--lock"],
            [root / "skills" / "gatekeeper-loop" / "scripts" / "tick_lock.py", "--lock"],
            ["-m", "devloops_lib.cli", "tick", "worker", "--agent-cmd", "exit 0"],
        ]
        env = {k: v for k, v in os.environ.items() if k not in ("LOOP_STATE_DIR", "DEVLOOPS_TICK_TOKEN")}
        env["PYTHONPATH"] = str(root)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            (Path(tmp) / "devloops.config.json").write_text("{", encoding="utf-8")
            for argv in scripts:
                with self.subTest(script=str(argv[0]) if argv[0] != "-m" else "devloops tick"):
                    result = subprocess.run([sys.executable, *map(str, argv)], cwd=tmp, env=env,
                                            capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                    self.assertIn("devloops config error", result.stderr)


if __name__ == "__main__":
    unittest.main()
