#!/usr/bin/env python3
"""Unified GitHub CLI helper module."""
from __future__ import annotations

import subprocess


class GhError(RuntimeError):
    """Raised when GitHub CLI execution fails."""


def gh(*args: str) -> str:
    """Execute a GitHub CLI command and return its stdout.

    Raises GhError if gh execution fails or returns non-zero.
    """
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    except Exception as e:
        raise GhError(f"Failed to execute gh: {e}") from e
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed (code {result.returncode}): {result.stderr.strip()}")
    return result.stdout
