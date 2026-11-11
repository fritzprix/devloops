#!/usr/bin/env python3
"""Each skill's vendored devloops_lib and config template match the root sources."""
import contextlib
import io
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import sync_skill_lib


class SkillLibSyncTest(unittest.TestCase):
    def test_every_skill_is_found(self):
        names = [p.name for p in sync_skill_lib.skill_dirs()]
        self.assertEqual(names, ["gatekeeper-loop", "producer-loop", "worker-loop"])

    def test_vendored_copies_match_sources(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = sync_skill_lib.main(["--check"])
        self.assertEqual(code, 0, f"stale vendored files; run scripts/sync_skill_lib.py:\n{out.getvalue()}")


if __name__ == "__main__":
    unittest.main()
