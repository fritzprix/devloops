#!/usr/bin/env python3
"""Tests for doctor.py diagnostic checks."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import doctor


class DoctorTest(unittest.TestCase):
    @patch("devloops_lib.doctor.subprocess.run")
    def test_check_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="v1.0.0", stderr="")
        self.assertTrue(doctor.check("test-cmd", ["dummy"]))

    @patch("devloops_lib.doctor.subprocess.run")
    def test_check_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error occurred")
        self.assertFalse(doctor.check("test-cmd", ["dummy"]))

    @patch("devloops_lib.doctor.subprocess.run")
    def test_check_file_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        self.assertFalse(doctor.check("nonexistent", ["nonexistent"]))

    @patch("devloops_lib.doctor.urllib.request.urlopen")
    def test_check_network_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp
        self.assertTrue(doctor.check_network())
        self.assertEqual(mock_urlopen.call_args.kwargs.get("timeout"), doctor.NETWORK_TIMEOUT_SECONDS)

    @patch("devloops_lib.doctor.urllib.request.urlopen")
    def test_check_network_failure(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("network unreachable")
        self.assertFalse(doctor.check_network())

    @patch("devloops_lib.doctor.urllib.request.urlopen")
    def test_check_network_rate_limit_403(self, mock_urlopen):
        mock_urlopen.side_effect = doctor.urllib.error.HTTPError("https://api.github.com", 403, "rate limit", {}, None)
        self.assertTrue(doctor.check_network())

    @patch("devloops_lib.doctor.urllib.request.urlopen")
    def test_check_network_rate_limit_429(self, mock_urlopen):
        mock_urlopen.side_effect = doctor.urllib.error.HTTPError("https://api.github.com", 429, "too many requests", {}, None)
        self.assertTrue(doctor.check_network())

    @patch("devloops_lib.doctor.urllib.request.urlopen")
    def test_check_network_http_500_failure(self, mock_urlopen):
        mock_urlopen.side_effect = doctor.urllib.error.HTTPError("https://api.github.com", 500, "internal server error", {}, None)
        self.assertFalse(doctor.check_network())

    @patch("devloops_lib.doctor.subprocess.run")
    def test_check_push_permission_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        self.assertTrue(doctor.check_push_permission())

    @patch("devloops_lib.doctor.subprocess.run")
    def test_check_push_permission_non_fast_forward(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="[rejected] (non-fast-forward)")
        self.assertTrue(doctor.check_push_permission())

    @patch("devloops_lib.doctor.subprocess.run")
    def test_check_push_permission_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="fatal: remote error: access denied")
        self.assertFalse(doctor.check_push_permission())


class DoctorSkillInstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = Path(tmp.name).resolve()

    def _project(self) -> Path:
        project = self.tmp_dir / "project"
        (project / ".git").mkdir(parents=True)
        return project

    def _skill_lib(self, skills_dir: Path, skill: str, content: str) -> None:
        lib = skills_dir / skill / "scripts" / "devloops_lib"
        lib.mkdir(parents=True)
        (lib / "config.py").write_text(content, encoding="utf-8")

    def test_init_creates_config_in_project_root(self):
        project = self._project()
        template = self.tmp_dir / "template.yaml"
        template.write_text("project:\n  repo: owner/name\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = doctor.init_config(template, start_dir=project)
        self.assertEqual(code, 0)
        self.assertEqual((project / doctor.CONFIG_NAME).read_text(encoding="utf-8"), template.read_text(encoding="utf-8"))

    def test_init_keeps_existing_config(self):
        project = self._project()
        config = project / doctor.CONFIG_NAME
        config.write_text("# mine\n", encoding="utf-8")
        template = self.tmp_dir / "template.yaml"
        template.write_text("# template\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = doctor.init_config(template, start_dir=project)
        self.assertEqual(code, 0)
        self.assertEqual(config.read_text(encoding="utf-8"), "# mine\n")

    def test_init_without_template_fails(self):
        project = self._project()
        with contextlib.redirect_stderr(io.StringIO()):
            code = doctor.init_config(self.tmp_dir / "missing.yaml", start_dir=project)
        self.assertEqual(code, 1)
        self.assertFalse((project / doctor.CONFIG_NAME).exists())

    def test_skill_libs_match(self):
        skills_dir = self.tmp_dir / "skills"
        self._skill_lib(skills_dir, "worker-loop", "x = 1\n")
        self._skill_lib(skills_dir, "gatekeeper-loop", "x = 1\r\n")
        (skills_dir / "unrelated-skill").mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(doctor.check_skill_libs(skills_dir))

    def test_skill_libs_differ_after_partial_update(self):
        skills_dir = self.tmp_dir / "skills"
        self._skill_lib(skills_dir, "worker-loop", "x = 2\n")
        self._skill_lib(skills_dir, "gatekeeper-loop", "x = 1\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(doctor.check_skill_libs(skills_dir))
        self.assertIn("gatekeeper-loop", out.getvalue())
        self.assertIn("worker-loop", out.getvalue())

    def test_skill_template_path(self):
        skill_dir = self.tmp_dir / "worker-loop"
        self.assertEqual(doctor.config_template(skill_dir), skill_dir / "assets" / doctor.CONFIG_NAME)


if __name__ == "__main__":
    unittest.main()
