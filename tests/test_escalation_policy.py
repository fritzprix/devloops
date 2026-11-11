#!/usr/bin/env python3
"""Regression tests for shared autonomy / escalation policy (docs)."""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

POLICY = REPO_ROOT / "skills/worker-loop/references/escalation-policy.md"
WORKER_SKILL = REPO_ROOT / "skills/worker-loop/SKILL.md"
TEMPLATE = REPO_ROOT / "templates/devloops.config.yaml"


class EscalationPolicyTest(unittest.TestCase):
    def test_policy_file_exists(self):
        self.assertTrue(POLICY.is_file())

    def test_policy_forbids_vague_external_services_wording(self):
        text = WORKER_SKILL.read_text(encoding="utf-8")
        self.assertNotIn("external services", text.lower())

    def test_open_pr_limit_is_not_needs_user(self):
        text = WORKER_SKILL.read_text(encoding="utf-8")
        self.assertNotIn('Record `needsUser` ("Waiting for open PRs', text)
        self.assertIn("no** `needsUser` entry", text)

    def test_policy_escalates_ambiguity(self):
        body = POLICY.read_text(encoding="utf-8")
        self.assertIn("(a) Ambiguity", body)
        self.assertNotIn("**Default: proceed.**", body)
        self.assertIn("environment.capabilities", body)

    def test_template_declares_environment_capabilities(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("environment:", text)
        self.assertIn("screen_capture", text)


if __name__ == "__main__":
    unittest.main()
