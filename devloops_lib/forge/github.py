"""GitHub forge adapter over the ``gh`` CLI (issue #44).

A behavior-preserving port of the 24 ``gh`` call sites this module replaces.
The subprocess boundary is a single injectable callable ``run(args) -> stdout``
(raises ``GhError`` on failure), defaulting to ``devloops_lib.gh.gh``; the
contract suite in ``tests/forge_contract.py`` stubs ``gh`` at this level.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from devloops_lib.forge.base import ForgeError  # noqa: F401  (re-export for callers)
from devloops_lib.forge.models import (
    CI_FAILURE,
    CI_NONE,
    CI_PENDING,
    CI_SUCCESS,
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
from devloops_lib.gh import GhError, gh

# Same check-state enums devloops_lib.pr_status defined before the adapter existed.
FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", "REQUESTED"}

# Byte-identical to pr_status.THREADS_QUERY so the migration cannot change results.
THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id isResolved isOutdated path line
          comments(first: 1) { nodes { author { login } body url } }
        }
      }
    }
  }
}
"""

# Byte-identical to tick_gate.GRAPHQL_GATE_QUERY so gate fingerprints stay stable.
GRAPHQL_GATE_QUERY = """
query($prQuery: String!, $prLimit: Int!, $owner: String!, $name: String!, $issueLimit: Int!) {
  search(query: $prQuery, type: ISSUE, first: $prLimit) {
    nodes {
      ... on PullRequest {
        number
        headRefOid
        mergeable
        updatedAt
        isDraft
        author { login }
        title
        bodyText
        headRefName
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                state
              }
            }
          }
        }
        comments(last: 1) {
          nodes {
            createdAt
          }
        }
      }
    }
  }
  repository(owner: $owner, name: $name) {
    issues(first: $issueLimit, states: [OPEN], orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number
        updatedAt
        labels(first: 20) { nodes { name } }
        assignees(first: 10) { nodes { login } }
        comments(last: 1) {
          nodes {
            createdAt
          }
        }
      }
    }
  }
}
"""

_ISSUE_JSON = "number,title,labels,assignees,body,state,url,createdAt,updatedAt"
_PR_LIST_JSON = (
    "number,title,url,headRefName,headRefOid,baseRefName,isDraft,mergeable,"
    "reviewDecision,updatedAt,labels,author,body,closingIssuesReferences"
)
_PR_VIEW_JSON = (
    "number,title,url,headRefName,headRefOid,baseRefName,isDraft,mergeable,"
    "mergeStateStatus,reviewDecision,statusCheckRollup,commits,comments,body,updatedAt"
)

_MERGEABLE = {"MERGEABLE": True, "CONFLICTING": False}


def _check_state(check: Dict[str, Any]) -> str:
    return (check.get("conclusion") or check.get("state") or check.get("status") or "").upper()


def _label_names(labels: Any) -> Tuple[str, ...]:
    names: List[str] = []
    for lbl in labels or []:
        if isinstance(lbl, dict):
            name = lbl.get("name")
            if name:
                names.append(name)
        elif isinstance(lbl, str):
            names.append(lbl)
    return tuple(names)


def _logins(nodes: Any) -> Tuple[str, ...]:
    out: List[str] = []
    for node in nodes or []:
        if isinstance(node, dict):
            login = node.get("login")
            if login:
                out.append(login)
        elif isinstance(node, str):
            out.append(node)
    return tuple(out)


def _login(author: Any) -> str:
    if isinstance(author, dict):
        return author.get("login") or ""
    return str(author or "")


class GitHubForge:
    """``Forge`` protocol implementation backed by the GitHub CLI."""

    def __init__(
        self,
        run: Optional[Callable[[List[str]], str]] = None,
        repo: Optional[str] = None,
    ) -> None:
        # run(args) is the single subprocess boundary; tests stub it.
        self._run = run if run is not None else (lambda args: gh(*args))
        self._repo = repo

    # -- plumbing ---------------------------------------------------------

    def _call(self, *args: str) -> str:
        """Run a gh subcommand that supports --repo (issue/pr commands)."""
        cmd = list(args)
        if self._repo:
            cmd.extend(["--repo", self._repo])
        return self._run(cmd)

    def _api(self, *args: str) -> str:
        """Run a gh api command (gh api does not accept --repo)."""
        return self._run(list(args))

    # -- identity / repository -------------------------------------------

    def whoami(self) -> str:
        return self._api("api", "user", "--jq", ".login").strip()

    def repo_ref(self) -> RepoRef:
        data = json.loads(self._call("repo", "view", "--json", "owner,name"))
        return RepoRef(host=None, owner=data["owner"]["login"], name=data["name"])

    def auth_check(self) -> AuthStatus:
        try:
            out = self._run(["auth", "status"])
        except GhError as e:
            return AuthStatus(ok=False, output=str(e))
        return AuthStatus(ok=True, output=out)

    # -- issues -----------------------------------------------------------

    def list_issues(
        self,
        state: str = "open",
        labels: Optional[Sequence[str]] = None,
        limit: int = 100,
    ) -> List[Issue]:
        cmd = ["issue", "list", "--state", state, "--limit", str(limit), "--json", _ISSUE_JSON]
        for lbl in labels or ():
            cmd.extend(["--label", lbl])
        return [self._parse_issue(i) for i in json.loads(self._call(*cmd) or "[]")]

    def get_issue(self, number: int) -> Issue:
        data = json.loads(self._call("issue", "view", str(number), "--json", _ISSUE_JSON))
        return self._parse_issue(data)

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> str:
        cmd = ["issue", "create", "--title", title, "--body", body]
        for lbl in labels:
            cmd.extend(["--label", lbl])
        return self._call(*cmd).strip()

    def update_issue_body(self, number: int, body: str) -> None:
        self._call("issue", "edit", str(number), "--body", body)

    def add_labels(self, number: int, names: Sequence[str]) -> None:
        cmd = ["issue", "edit", str(number)]
        for lbl in names:
            cmd.extend(["--add-label", lbl])
        self._call(*cmd)

    def list_labels(self) -> List[str]:
        data = json.loads(self._call("label", "list", "--json", "name", "--limit", "1000") or "[]")
        return [d["name"] for d in data]

    def remove_labels(self, number: int, names: Sequence[str]) -> None:
        if not names:
            return
        # GitHub CLI rejects `--remove-label` for labels the repository does not
        # have (G5.1: removing gatekeeper:review-clean failed on repos where the
        # label was never created). Remove only labels the repository has.
        present = set(self.list_labels())
        known = [n for n in names if n in present]
        if not known:
            return
        cmd = ["issue", "edit", str(number)]
        for lbl in known:
            cmd.extend(["--remove-label", lbl])
        self._call(*cmd)

    def add_assignee(self, number: int, login: str) -> None:
        self._call("issue", "edit", str(number), "--add-assignee", login)

    @staticmethod
    def _parse_issue(i: Dict[str, Any]) -> Issue:
        return Issue(
            number=i["number"],
            title=i.get("title") or "",
            url=i.get("url") or "",
            state=(i.get("state") or "OPEN").lower(),
            body=i.get("body") or "",
            labels=_label_names(i.get("labels")),
            assignees=_logins(i.get("assignees")),
            created_at=i.get("createdAt") or "",
            updated_at=i.get("updatedAt") or "",
        )

    # -- pull requests ----------------------------------------------------

    def list_prs(
        self,
        state: str = "open",
        author: Optional[str] = None,
        limit: int = 100,
    ) -> List[PullRequest]:
        cmd = ["pr", "list", "--state", state, "--limit", str(limit), "--json", _PR_LIST_JSON]
        if author:
            cmd.extend(["--author", author])
        return [self._parse_pr(p) for p in json.loads(self._call(*cmd) or "[]")]

    def get_pr(self, number: int) -> PullRequest:
        data = json.loads(self._call("pr", "view", str(number), "--json", _PR_VIEW_JSON))
        return self._parse_pr(data)

    @staticmethod
    def _parse_pr(p: Dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=p["number"],
            title=p.get("title") or "",
            url=p.get("url") or "",
            head_ref=p.get("headRefName") or "",
            head_sha=p.get("headRefOid") or "",
            base_ref=p.get("baseRefName") or "",
            is_draft=bool(p.get("isDraft")),
            author=_login(p.get("author")),
            mergeable=_MERGEABLE.get(p.get("mergeable")),
            body=p.get("body") or "",
            updated_at=p.get("updatedAt") or "",
            labels=_label_names(p.get("labels")),
            merge_state=p.get("mergeStateStatus"),
            review_decision=p.get("reviewDecision"),
            raw=p,
        )

    @staticmethod
    def _check_run_ts(c: Dict[str, Any]) -> str:
        for field in ("startedAt", "createdAt", "completedAt"):
            val = str(c.get(field) or "")
            if val and not val.startswith("0001-01-01"):
                return val
        return ""

    def ci_status(self, pr: PullRequest, *, required_checks: Sequence[str] = ()) -> CiSummary:
        checks = pr.raw.get("statusCheckRollup") or []
        req_set = set(required_checks)
        latest_req: Dict[str, tuple[str, int, Dict[str, Any]]] = {}
        if req_set:
            for idx, c in enumerate(checks):
                name = c.get("name") or c.get("context")
                if not name or name not in req_set:
                    continue
                ts = self._check_run_ts(c)
                key = (ts, idx)
                prev = latest_req.get(name)
                if prev is None or key >= (prev[0], prev[1]):
                    latest_req[name] = (ts, idx, c)
        effective: List[Dict[str, Any]] = []
        seen_req: set[str] = set()
        for c in checks:
            name = c.get("name") or c.get("context")
            if name and name in req_set:
                if name not in seen_req:
                    seen_req.add(name)
                    effective.append(latest_req[name][2])
            else:
                effective.append(c)

        failing = [c.get("name") or c.get("context") for c in effective if _check_state(c) in FAILED]
        pending = [c.get("name") or c.get("context") for c in effective if _check_state(c) in PENDING]
        missing = [
            name
            for name in required_checks
            if (name not in latest_req or _check_state(latest_req[name][2]) != "SUCCESS")
            and name not in failing
            and name not in pending
        ]
        if missing:
            pending = [*pending, *missing]
        if failing:
            state = CI_FAILURE
        elif pending:
            state = CI_PENDING
        elif checks:
            state = CI_SUCCESS
        else:
            state = CI_NONE
        return CiSummary(state=state, failing=failing, pending=pending)

    def unresolved_review_threads(self, pr: PullRequest) -> List[ReviewThread]:
        ref = self.repo_ref()
        threads: List[ReviewThread] = []
        cursor = None
        while True:
            args = [
                "api", "graphql",
                "-f", f"query={THREADS_QUERY}",
                "-F", f"owner={ref.owner}",
                "-F", f"name={ref.name}",
                "-F", f"number={pr.number}",
            ]
            if cursor:
                args.extend(["-F", f"cursor={cursor}"])
            raw = json.loads(self._api(*args))
            section = (
                (raw.get("data") or {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads")
                or {}
            )
            for node in section.get("nodes") or []:
                if node.get("isResolved"):
                    continue
                first = ((node.get("comments") or {}).get("nodes") or [{}])[0]
                threads.append(ReviewThread(
                    thread_id=node["id"],
                    path=node.get("path") or "",
                    line=node.get("line"),
                    author=(first.get("author") or {}).get("login"),
                    body=first.get("body") or "",
                    url=first.get("url"),
                    outdated=bool(node.get("isOutdated")),
                ))
            page_info = section.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
        return threads

    def list_pr_comments(self, number: int) -> List[PrComment]:
        data = json.loads(self._call("pr", "view", str(number), "--json", "comments"))
        out: List[PrComment] = []
        for c in data.get("comments") or []:
            out.append(PrComment(
                author=_login(c.get("author")),
                created_at=c.get("createdAt") or "",
                body=c.get("body") or "",
                url=c.get("url"),
            ))
        return out

    def post_pr_comment(self, number: int, body: str) -> None:
        self._call("pr", "comment", str(number), "--body", body)

    def post_issue_comment(self, number: int, body: str) -> None:
        self._call("issue", "comment", str(number), "--body", body)

    # -- issue dependencies (issue #65) ------------------------------------

    def _owner_repo(self) -> Tuple[str, str]:
        """Owner/name for `gh api` calls, which do not accept --repo."""
        if self._repo:
            owner, _, name = self._repo.partition("/")
            if owner and name:
                return owner, name
            raise ForgeError(f"GitHubForge: cannot split owner/repo from {self._repo!r}")
        full = self.repo_ref().full_name
        owner, _, name = full.partition("/")
        if not owner or not name:
            raise ForgeError("GitHubForge: cannot resolve owner/repo for dependency operations")
        return owner, name

    def blocked_by(self, number: int) -> List[Blocker]:
        owner, repo = self._owner_repo()
        out = self._api(
            "api",
            f"repos/{owner}/{repo}/issues/{number}/dependencies/blocked_by",
            "--paginate",
        )
        try:
            items = json.loads(out or "[]")
        except ValueError as exc:
            raise ForgeError(f"GitHub blocked_by #{number}: invalid JSON: {exc}") from exc
        if isinstance(items, dict):
            items = items.get("blocked_by") or items.get("dependencies") or []
        blockers: List[Blocker] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("number") is None:
                continue
            blockers.append(Blocker(
                number=int(item["number"]),
                state=str(item.get("state") or "open").lower(),
                is_pr="pull_request" in item,
            ))
        return blockers

    def _issue_resource(self, number: int) -> dict:
        """GET repos/.../issues/{n}; GitHub returns PRs here with a pull_request key."""
        owner, repo = self._owner_repo()
        raw = self._api("api", f"repos/{owner}/{repo}/issues/{number}") or "{}"
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ForgeError(f"GitHub issue #{number}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict) or data.get("number") is None:
            raise ForgeError(f"GitHub issue #{number} not found")
        return data

    def _reject_pr_dependency(self, number: int, role: str) -> dict:
        data = self._issue_resource(number)
        if "pull_request" in data:
            raise ForgeError(
                f"GitHub add_blocker: #{number} ({role}) is a pull request; "
                "GitHub issue dependencies are issue-only"
            )
        return data

    def add_blocker(self, number: int, blocker_number: int) -> None:
        # Owner decision (#91): no PR numbers, no body-line fallback.
        self._reject_pr_dependency(number, "target")
        blocker = self._reject_pr_dependency(blocker_number, "blocker")
        if any(b.number == blocker_number for b in self.blocked_by(number)):
            return
        issue_id = blocker.get("id")
        if issue_id is None:
            raise ForgeError(f"GitHub issue #{blocker_number} has no numeric id for dependency link")
        owner, repo = self._owner_repo()
        self._api(
            "api",
            f"repos/{owner}/{repo}/issues/{number}/dependencies/blocked_by",
            "-X", "POST", "-F", f"issue_id={int(issue_id)}",
        )

    def pr_diff(self, number: int) -> str:
        return self._call("pr", "diff", str(number))

    def create_pr(self, *, base: str, title: str, body: str = "", head: Optional[str] = None) -> str:
        args = ["pr", "create", "--base", base, "--title", title, "--body", body]
        if head:
            args.extend(["--head", head])
        return self._call(*args).strip()

    def update_branch(self, number: int) -> None:
        self._call("pr", "update-branch", str(number))

    def ensure_label(self, name: str, *, color: str = "ededed", description: str = "") -> None:
        args = ["label", "create", name, "--color", color.lstrip("#"), "--force"]
        if description:
            args.extend(["--description", description])
        self._call(*args)

    def ci_logs_failed(self, target: str) -> str:
        return self._call("run", "view", str(target), "--log-failed")

    def ci_rerun_failed(self, target: str) -> None:
        self._call("run", "rerun", str(target), "--failed")

    # -- gate / refs --------------------------------------------------------

    def gate_snapshot(self, pr_limit: int = 100, issue_limit: int = 100) -> GateSnapshot:
        ref = self.repo_ref()
        pr_query = f"repo:{ref.full_name} is:pr is:open"
        raw = json.loads(self._api(
            "api", "graphql",
            "-f", f"query={GRAPHQL_GATE_QUERY}",
            "-F", f"prQuery={pr_query}",
            "-F", f"owner={ref.owner}",
            "-F", f"name={ref.name}",
            "-F", f"prLimit={pr_limit}",
            "-F", f"issueLimit={issue_limit}",
        ))
        data = raw.get("data", {})
        return GateSnapshot(
            prs=data.get("search", {}).get("nodes") or [],
            issues=(data.get("repository") or {}).get("issues", {}).get("nodes") or [],
            raw=raw,
        )

    def pr_head_ref(self, number: int) -> str:
        return f"refs/pull/{number}/head"
