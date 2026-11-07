#!/usr/bin/env python3
"""Tests for the release checklist (issue #8, task 8).

These keep ``RELEASE.md`` truthful against ``pyproject.toml`` and the repo state.
They are deterministic: no network, no build, no publishing.
"""
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RELEASE_DOC = REPO_ROOT / "RELEASE.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _pyproject_text():
    return PYPROJECT.read_text(encoding="utf-8")


def _project_field(name):
    """Top-level ``[project]`` value, matched at line start so nested
    tables (for example the ``name`` inside ``authors``) do not match."""
    match = re.search(r'^%s = "([^"]*)"' % re.escape(name), _pyproject_text(), re.M)
    return match.group(1) if match else None


def _console_script():
    section = _pyproject_text().split("[project.scripts]", 1)
    if len(section) != 2:
        return None
    body = section[1].split("[", 1)[0]
    match = re.search(r'^\s*devloops\s*=\s*"([^"]*)"', body, re.M)
    return match.group(1) if match else None


def _doc_text():
    return RELEASE_DOC.read_text(encoding="utf-8")


def _tracked_license_files():
    return [p for p in REPO_ROOT.iterdir() if p.is_file() and p.stem.upper() == "LICENSE"]


class ReleaseChecklistDocumentTest(unittest.TestCase):
    def test_document_exists(self):
        self.assertTrue(RELEASE_DOC.is_file(), "RELEASE.md must exist at the repo root")

    def test_declares_it_does_not_publish(self):
        doc = _doc_text()
        self.assertRegex(doc, r"never publishes anything")
        self.assertRegex(doc, r"[Dd]o not add PyPI credentials")

    def test_sections_cover_required_topics(self):
        doc = _doc_text()
        for heading in (
            "Package name",
            "Version and metadata",
            "License metadata",
            "Build both artifacts",
            "Verify the wheel",
            "Verify the sdist",
            "Release trigger",
            "Relocated-install smoke test",
            "Pre-release run sheet",
        ):
            self.assertRegex(doc, r"(?im)^#{1,3} .*\b%s\b" % re.escape(heading))

    def test_package_name_matches_pyproject(self):
        name = _project_field("name")
        self.assertIsNotNone(name)
        self.assertIn('`%s`' % name, _doc_text())

    def test_version_matches_pyproject(self):
        version = _project_field("version")
        self.assertIsNotNone(version)
        self.assertRegex(_doc_text(), r"currently `%s`" % re.escape(version))

    def test_license_expression_matches_pyproject(self):
        license_id = _project_field("license")
        self.assertIsNotNone(license_id)
        self.assertRegex(_doc_text(), r"`license = \"%s\"`" % re.escape(license_id))

    def test_console_entry_point_matches_pyproject(self):
        entry_point = _console_script()
        self.assertEqual(entry_point, "devloops_lib.cli:main")
        self.assertIn(entry_point, _doc_text())

    def test_requires_python_floor_is_documented(self):
        floor = _project_field("requires-python")
        self.assertIsNotNone(floor)
        self.assertRegex(_doc_text(), r"`requires-python = \"%s\"`" % re.escape(floor))

    def test_license_shipment_claim_matches_repo_state(self):
        """If a LICENSE file is added, the doc must stop calling it a blocker."""
        doc = _doc_text()
        if _tracked_license_files():
            self.assertNotIn(
                "Open blocker",
                doc,
                "RELEASE.md still says no LICENSE file is tracked, but one exists",
            )
        else:
            self.assertRegex(
                doc,
                r"no `LICENSE` file is tracked",
                "RELEASE.md must flag the missing LICENSE file while none is tracked",
            )

    def test_wheel_asset_paths_are_documented(self):
        doc = _doc_text()
        for skill in ("worker-loop", "gatekeeper-loop", "producer-loop"):
            self.assertIn(
                "devloops_lib/assets/skills/%s/SKILL.md" % skill,
                doc,
            )
        self.assertIn("devloops_lib/assets/templates/devloops.config.yaml", doc)


if __name__ == "__main__":
    unittest.main()
