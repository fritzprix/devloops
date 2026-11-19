"""Forge adapter seam (issue #44).

`Forge` is the protocol every forge backend implements; `GitHubForge` is the
behavior-preserving adapter over the `gh` CLI. Normalized models carry only the
fields devloops reads, so a second forge (see #46) can plug in without touching
loop logic. Stdlib-only, Python 3.8 floor.
"""
from __future__ import annotations

from devloops_lib.forge.base import Forge, ForgeError
from devloops_lib.forge.github import GitHubForge
from devloops_lib.forge.models import (
    CI_FAILURE,
    CI_NONE,
    CI_PENDING,
    CI_SUCCESS,
    AuthStatus,
    CiSummary,
    GateSnapshot,
    Issue,
    PrComment,
    PullRequest,
    RepoRef,
    ReviewThread,
)

__all__ = [
    "Forge",
    "ForgeError",
    "GitHubForge",
    "AuthStatus",
    "CiSummary",
    "GateSnapshot",
    "Issue",
    "PrComment",
    "PullRequest",
    "RepoRef",
    "ReviewThread",
    "CI_SUCCESS",
    "CI_FAILURE",
    "CI_PENDING",
    "CI_NONE",
]
