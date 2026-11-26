"""Forge protocol: the seam every forge adapter implements (issue #44).

Rules every implementation must honor:
- Label operations are additive/subtractive; never replace the full label set.
- Forge-specific fields stay Optional on the models; callers handle ``None``.
- The protocol carries only operations devloops reads today; new forges must
  pass the contract suite in ``tests/forge_contract.py``.
"""
from __future__ import annotations

from typing import List, Optional, Protocol, Sequence, Tuple

from devloops_lib.forge.models import (
    AuthStatus,
    Blocker,
    CiSummary,
    GateSnapshot,
    Issue,
    PrComment,
    PullRequest,
    RepoRef,
    ReviewThread,
)
from devloops_lib.gh import GhError

# One error type across forges. The GitHub binding reuses GhError so migrated
# callers keep catching the same exception class.
ForgeError = GhError

__all__ = [
    "Forge",
    "ForgeError",
    "AuthStatus",
    "Blocker",
    "CiSummary",
    "GateSnapshot",
    "Issue",
    "PrComment",
    "PullRequest",
    "RepoRef",
    "ReviewThread",
]


class Forge(Protocol):
    """Operations devloops performs against a git forge."""

    # identity / repository
    def whoami(self) -> str: ...

    def repo_ref(self) -> RepoRef: ...

    def auth_check(self) -> AuthStatus: ...

    # issues
    def list_issues(
        self,
        state: str = "open",
        labels: Optional[Sequence[str]] = None,
        limit: int = 100,
    ) -> List[Issue]: ...

    def get_issue(self, number: int) -> Issue: ...

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> str: ...

    def update_issue_body(self, number: int, body: str) -> None: ...

    def add_labels(self, number: int, names: Sequence[str]) -> None: ...

    def remove_labels(self, number: int, names: Sequence[str]) -> None: ...

    def add_assignee(self, number: int, login: str) -> None: ...

    # pull requests
    def list_prs(
        self,
        state: str = "open",
        author: Optional[str] = None,
        limit: int = 100,
    ) -> List[PullRequest]: ...

    def get_pr(self, number: int) -> PullRequest: ...

    def ci_status(self, pr: PullRequest) -> CiSummary: ...

    def unresolved_review_threads(self, pr: PullRequest) -> List[ReviewThread]: ...

    def list_pr_comments(self, number: int) -> List[PrComment]: ...

    def post_pr_comment(self, number: int, body: str) -> None: ...

    def post_issue_comment(self, number: int, body: str) -> None: ...

    # issue dependencies (issue #65). Gitea/Forgejo accept issue and PR numbers.
    # GitHub's API is issue-only: add_blocker raises ForgeError when either
    # number is a pull request (owner decision on #91). Unknown numbers raise.
    def blocked_by(self, number: int) -> List[Blocker]: ...

    def add_blocker(self, number: int, blocker_number: int) -> None: ...

    # gate / refs
    def gate_snapshot(self, pr_limit: int = 100, issue_limit: int = 100) -> GateSnapshot: ...

    def pr_head_ref(self, number: int) -> str: ...
