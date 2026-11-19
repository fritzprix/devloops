"""Normalized forge data models (issue #44).

Fields carry only what devloops reads. GitHub-only fields are Optional and
callers must handle ``None``. Stdlib-only, Python 3.8 floor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Normalized CI states. A CiSummary.state is always exactly one of these.
CI_SUCCESS = "success"
CI_FAILURE = "failure"
CI_PENDING = "pending"
CI_NONE = "none"


@dataclass(frozen=True)
class RepoRef:
    """A repository address. ``host`` stays None until forge detection (#45)."""

    host: Optional[str]
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class Issue:
    number: int
    title: str
    url: str
    state: str
    body: str
    labels: Tuple[str, ...] = ()
    assignees: Tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""


@dataclass
class PullRequest:
    number: int
    title: str
    url: str
    head_ref: str
    head_sha: str
    base_ref: str
    is_draft: bool
    author: str
    mergeable: Optional[bool] = None  # None = unknown / forge does not report it
    body: str = ""
    updated_at: str = ""
    labels: Tuple[str, ...] = ()
    # GitHub-only raw enums, kept optional so callers stay forge-neutral.
    merge_state: Optional[str] = None
    review_decision: Optional[str] = None
    # Original forge payload, for operations that need the unnormalized shape
    # (e.g. ci_status reading statusCheckRollup on GitHub).
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class CiSummary:
    state: str  # one of CI_SUCCESS / CI_FAILURE / CI_PENDING / CI_NONE
    failing: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)


@dataclass
class ReviewThread:
    thread_id: str
    path: str
    line: Optional[int]
    author: Optional[str]
    body: str  # raw body; display truncation stays with the caller
    url: Optional[str] = None
    outdated: bool = False


@dataclass
class PrComment:
    author: str
    created_at: str
    body: str
    url: Optional[str] = None


@dataclass
class GateSnapshot:
    """Raw gate-query nodes.

    The gate feeds a fingerprint hash, so nodes keep the exact query-shaped
    dicts; normalizing them here would change fingerprints.
    """

    prs: List[Dict[str, Any]] = field(default_factory=list)
    issues: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class AuthStatus:
    ok: bool
    output: str = ""
