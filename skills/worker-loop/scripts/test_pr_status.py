#!/usr/bin/env python3
"""Tests for pr_status.duplicate_commits."""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_pr_status import DuplicateCommitsTest, MarkerAndGatekeeperStateTest, SelectNextActionTest

if __name__ == "__main__":
    unittest.main()
