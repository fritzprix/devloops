#!/usr/bin/env python3
"""Tests for `devloops forge` CLI and forge-neutral agent guides (issue #47)."""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib.forge.cli import EXIT_OK, EXIT_UNSUPPORTED, forge_main
from tests.forge_contract import USER, FakeForge

DIRECT_GH_PATTERN = re.compile(r"\bgh (?:pr|issue|api|repo|run|auth|label)\b")


class ExtendedFakeForge(FakeForge):
    """FakeForge with optional CLI write/CI helpers enabled."""

    def __init__(self) -> None:
        super().__init__()
        self.created_prs: list[dict] = []
        self.updated_branches: list[int] = []
        self.ensured_labels: list[tuple[str, str, str]] = []
        self.reruns: list[str] = []

    def pr_diff(self, number: int) -> str:
        return f"diff --git a/f.py b/f.py\n# pr {number}\n"

    def create_pr(self, *, base: str, title: str, body: str = "", head: str | None = None) -> str:
        self.created_prs.append({"base": base, "title": title, "body": body, "head": head})
        return "https://example.com/acme/widgets/pull/99"

    def update_branch(self, number: int) -> None:
        self.updated_branches.append(number)

    def ensure_label(self, name: str, *, color: str = "ededed", description: str = "") -> None:
        self.ensured_labels.append((name, color, description))

    def ci_logs_failed(self, target: str) -> str:
        return f"FAILED test_x on run {target}"

    def ci_rerun_failed(self, target: str) -> None:
        self.reruns.append(target)


class ForgeCliTest(unittest.TestCase):
    def _run(self, forge, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = forge_main(list(argv), forge=forge)
        return code, out.getvalue(), err.getvalue()

    def test_no_direct_gh_commands_in_skill_guides(self):
        offenders: list[str] = []
        files = list((REPO_ROOT / "skills").glob("*/SKILL.md"))
        files.extend((REPO_ROOT / "skills").glob("*/references/*"))
        for path in sorted(files):
            if not path.is_file():
                continue
            for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if DIRECT_GH_PATTERN.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{idx}: {line.strip()}")
        self.assertEqual(offenders, [], "Direct `gh` commands found in skill guides:\n" + "\n".join(offenders))

    def test_whoami_and_issue_subcommands_against_fake_forge(self):
        forge = FakeForge()

        code, out, _ = self._run(forge, "whoami")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out), {"login": USER})

        code, out, _ = self._run(forge, "issue", "view", "2")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["title"], "Feature request")

        code, out, _ = self._run(forge, "issue", "list", "--label", "auto:producer")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([i["number"] for i in json.loads(out)], [1])

        with tempfile.TemporaryDirectory() as tmp:
            bf = Path(tmp) / "body.md"
            bf.write_text("from file", encoding="utf-8")
            code, out, _ = self._run(forge, "issue", "create", "--title", "CLI issue", "--body-file", str(bf), "--label", "bug")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("url", json.loads(out))

        code, out, _ = self._run(forge, "issue", "assign-me", "1")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(forge.get_issue(1).assignees, (USER,))

        code, _, _ = self._run(forge, "issue", "label", "add", "1", "extra")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("extra", forge.get_issue(1).labels)

        code, _, _ = self._run(forge, "issue", "label", "remove", "1", "validated")
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("validated", forge.get_issue(1).labels)

    def test_pr_subcommands_against_fake_forge(self):
        forge = FakeForge()

        code, out, _ = self._run(forge, "pr", "view", "10")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["headRefName"], "feat/a")

        code, out, _ = self._run(forge, "pr", "list", "--author", "@me")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual({p["number"] for p in json.loads(out)}, {10, 12})

        code, out, _ = self._run(forge, "pr", "checks", "10")
        self.assertEqual(code, EXIT_OK)
        data = json.loads(out)
        self.assertEqual(data["state"], "failure")
        self.assertEqual(data["failing"], ["lint"])

        with tempfile.TemporaryDirectory() as tmp:
            cf = Path(tmp) / "comment.md"
            cf.write_text("<!-- worker-loop: note -->\nhi", encoding="utf-8")
            code, _, _ = self._run(forge, "pr", "comment", "10", "--body-file", str(cf))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(forge.list_pr_comments(10)[-1].body, "<!-- worker-loop: note -->\nhi")

        code, _, _ = self._run(forge, "issue", "comment", "1", "--body", "issue comment")
        self.assertEqual(code, EXIT_OK)

    def test_issue_comment_targets_issue_not_pr(self):
        forge = ExtendedFakeForge()

        code, _, _ = self._run(forge, "issue", "comment", "1", "--body", "<!-- worker-loop: note -->\non the issue")
        self.assertEqual(code, EXIT_OK)
        comments = forge._issue(1).get("comments", [])
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["body"], "<!-- worker-loop: note -->\non the issue")
        # The PR comment list must be untouched by an issue comment.
        self.assertEqual(len(forge.list_pr_comments(10)), 1)  # seeded PR comment only

    def test_pr_diff_raw_flag(self):
        forge = ExtendedFakeForge()

        code, out, _ = self._run(forge, "pr", "diff", "10", "--raw")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("diff --git a/f.py b/f.py", out)
        self.assertNotIn('"diff":', out)

        code, out, _ = self._run(forge, "pr", "diff", "10")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("diff", json.loads(out))

    def test_no_bare_devloops_forge_in_skill_guides(self):
        """Guides must call the vendored shim, never a bare `devloops forge` command."""
        offenders: list[str] = []
        files = list((REPO_ROOT / "skills").glob("*/SKILL.md"))
        files.extend((REPO_ROOT / "skills").glob("*/references/*"))
        for path in sorted(files):
            if not path.is_file():
                continue
            if not (path.parent / "scripts" / "forge.py").exists():
                continue
            for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                # A bare invocation: `devloops forge` not preceded by the shim call.
                if re.search(r"(?<![\w/$.-])devloops forge\b", line) and "scripts/forge.py" not in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{idx}: {line.strip()}")
        self.assertEqual(offenders, [], "Bare `devloops forge` commands found in skill guides:\n" + "\n".join(offenders))

    def test_verdict_label_swap_keeps_one_verdict_and_preserves_other_labels(self):
        forge = FakeForge()
        # Issue 1 has auto:producer, validated (non-gatekeeper labels)
        forge.add_labels(1, ["gatekeeper:changes-requested", "gatekeeper:escalated"])

        code, out, _ = self._run(forge, "pr", "label", "set-verdict", "1", "approved")
        self.assertEqual(code, EXIT_OK)
        labels = set(forge.get_issue(1).labels)
        self.assertIn("gatekeeper:approved", labels)
        self.assertNotIn("gatekeeper:changes-requested", labels)
        self.assertNotIn("gatekeeper:escalated", labels)
        self.assertTrue({"auto:producer", "validated"} <= labels)

        # Swap to changes-requested
        code, _, _ = self._run(forge, "pr", "label", "set-verdict", "1", "changes-requested")
        self.assertEqual(code, EXIT_OK)
        labels = set(forge.get_issue(1).labels)
        self.assertIn("gatekeeper:changes-requested", labels)
        self.assertNotIn("gatekeeper:approved", labels)
        self.assertTrue({"auto:producer", "validated"} <= labels)

        # Clean removes changes-requested without touching other labels
        code, _, _ = self._run(forge, "pr", "label", "set-verdict", "1", "clean")
        self.assertEqual(code, EXIT_OK)
        labels = set(forge.get_issue(1).labels)
        self.assertNotIn("gatekeeper:changes-requested", labels)
        self.assertTrue({"auto:producer", "validated"} <= labels)

    def test_unsupported_operations_exit_with_distinct_code(self):
        forge = FakeForge()
        for argv in (
            ("pr", "diff", "10"),
            ("pr", "create", "--base", "main", "--title", "t"),
            ("pr", "update-branch", "10"),
            ("ci", "logs", "10", "--failed"),
            ("ci", "rerun", "10", "--failed"),
            ("label", "ensure", "gatekeeper:approved"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self._run(forge, *argv)
                self.assertEqual(code, EXIT_UNSUPPORTED)
                self.assertEqual(json.loads(out)["status"], "unsupported")
                self.assertIn("unsupported", err)

    def test_extended_operations_succeed_when_supported(self):
        forge = ExtendedFakeForge()

        code, out, _ = self._run(forge, "pr", "diff", "10")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("# pr 10", json.loads(out)["diff"])

        code, out, _ = self._run(forge, "pr", "create", "--base", "main", "--title", "New PR", "--body", "Fixes #1")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["url"], "https://example.com/acme/widgets/pull/99")

        code, _, _ = self._run(forge, "pr", "update-branch", "10")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(forge.updated_branches, [10])

        code, out, _ = self._run(forge, "ci", "logs", "42", "--failed")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("42", json.loads(out)["logs"])

        code, _, _ = self._run(forge, "ci", "rerun", "42", "--failed")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(forge.reruns, ["42"])

        code, _, _ = self._run(forge, "label", "ensure", "gatekeeper:approved", "--color", "0e8a16", "--description", "ok")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(forge.ensured_labels, [("gatekeeper:approved", "0e8a16", "ok")])


if __name__ == "__main__":
    unittest.main()
