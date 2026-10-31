#!/usr/bin/env python3
"""Tests for gatekeeper-loop review_queue status determination."""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_review_queue import DetermineStatusTests, GapTests

if __name__ == "__main__":
    unittest.main()
