#!/usr/bin/env python3
"""Run the forge contract suite (issue #44) against the in-memory FakeForge."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.forge_contract import FakeForge, ForgeContract  # noqa: E402


class TestFakeForgeContract(ForgeContract):
    def make_forge(self):
        return FakeForge()

    def auth_failure_forge(self):
        return FakeForge(auth_ok=False)
