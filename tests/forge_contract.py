"""Contract suite every ``Forge`` implementation must pass (issue #44).

Reused by the Forgejo/Gitea adapter (#46) and the Forgejo CI integration (#48).

Exports:
- SEED          shared seed data in raw forge-payload shape
- FakeForge     in-memory ``Forge`` implementation over a copy of SEED
- GhStub        fake subprocess-level ``gh`` runner answering SEED
- ForgeContract pytest-collected mixin defining the contract tests
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional

from devloops_lib.forge import (
    CI_FAILURE,
    CI_NONE,
    CI_PENDING,
    CI_SUCCESS,
    ForgeError,
)

USER = "tester"
OWNER = "acme"
NAME = "widgets"


def _url(kind: str, number: int) -> str:
    return f"https://github.com/{OWNER}/{NAME}/{kind}/{number}"


SEED: Dict[str, Any] = {
    "user": USER,
    "repo": {"owner": {"login": OWNER}, "name": NAME},
    # gh issue list/view JSON shape
    "issues": [
        {
            "number": 1, "title": "Producer issue", "url": _url("issues", 1),
            "state": "OPEN", "body": "First issue",
            "labels": [{"name": "auto:producer"}, {"name": "validated"}],
            "assignees": [], "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z",
        },
        {
            "number": 2, "title": "Feature request", "url": _url("issues", 2),
            "state": "OPEN", "body": "Second issue",
            "labels": [{"name": "enhancement"}],
            "assignees": [{"login": USER}], "createdAt": "2026-01-03T00:00:00Z",
            "updatedAt": "2026-01-04T00:00:00Z",
        },
        {
            "number": 3, "title": "Old bug", "url": _url("issues", 3),
            "state": "CLOSED", "body": "Closed already",
            "labels": [{"name": "bug"}],
            "assignees": [], "createdAt": "2025-12-01T00:00:00Z",
            "updatedAt": "2025-12-02T00:00:00Z",
        },
    ],
    # gh pr view JSON shape (list output is the same dicts; extra fields are inert)
    "prs": [
        {
            "number": 10, "title": "My feature", "url": _url("pull", 10),
            "headRefName": "feat/a", "headRefOid": "aaaa1111", "baseRefName": "main",
            "isDraft": False, "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY",
            "reviewDecision": "", "updatedAt": "2026-01-05T00:00:00Z",
            "labels": [{"name": "approved"}], "author": {"login": USER},
            "body": "Fixes #1", "closingIssuesReferences": [{"number": 1}],
            "state": "OPEN",
            "statusCheckRollup": [
                {"name": "lint", "conclusion": "FAILURE"},
                {"name": "test", "state": "PENDING"},
                {"name": "build", "conclusion": "SUCCESS"},
            ],
            "commits": [{"committedDate": "2026-01-05T00:00:00Z"}],
            "comments": [
                {"author": {"login": USER}, "createdAt": "2026-01-05T01:00:00Z",
                 "body": "hello", "url": _url("pull", 10) + "#issuecomment-1"},
            ],
        },
        {
            "number": 11, "title": "Their fix", "url": _url("pull", 11),
            "headRefName": "fix/b", "headRefOid": "bbbb2222", "baseRefName": "main",
            "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "reviewDecision": "APPROVED", "updatedAt": "2026-01-06T00:00:00Z",
            "labels": [], "author": {"login": "other"},
            "body": "Their body", "closingIssuesReferences": [],
            "state": "OPEN",
            "statusCheckRollup": [{"name": "test", "conclusion": "SUCCESS"}],
            "commits": [], "comments": [],
        },
        {
            "number": 12, "title": "Draft work", "url": _url("pull", 12),
            "headRefName": "wip/c", "headRefOid": "cccc3333", "baseRefName": "main",
            "isDraft": True, "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND",
            "reviewDecision": "", "updatedAt": "2026-01-07T00:00:00Z",
            "labels": [], "author": {"login": USER},
            "body": "WIP", "closingIssuesReferences": [],
            "state": "OPEN", "statusCheckRollup": [], "commits": [], "comments": [],
        },
        {
            "number": 13, "title": "Unknown state", "url": _url("pull", 13),
            "headRefName": "fix/d", "headRefOid": "dddd4444", "baseRefName": "main",
            "isDraft": False, "mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN",
            "reviewDecision": "", "updatedAt": "2026-01-08T00:00:00Z",
            "labels": [], "author": {"login": "other"},
            "body": "", "closingIssuesReferences": [],
            "state": "OPEN", "statusCheckRollup": [], "commits": [], "comments": [],
        },
    ],
    # reviewThreads GraphQL pages for PR 10
    "threads_pages": [
        {
            "data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
                "nodes": [
                    {"id": "T0", "isResolved": True, "isOutdated": False,
                     "path": "z.py", "line": 9,
                     "comments": {"nodes": [{"author": {"login": "reviewer"},
                                             "body": "resolved", "url": "https://x/t0"}]}},
                    {"id": "T1", "isResolved": False, "isOutdated": False,
                     "path": "a.py", "line": 3,
                     "comments": {"nodes": [{"author": {"login": "reviewer"},
                                             "body": "Fix this", "url": "https://x/t1"}]}},
                ],
            }}}},
        },
        {
            "data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {"id": "T2", "isResolved": False, "isOutdated": True,
                     "path": "b.py", "line": None,
                     "comments": {"nodes": [{"author": {"login": "second"},
                                             "body": "Stale note", "url": "https://x/t2"}]}},
                ],
            }}}},
        },
    ],
    # tick_gate.GRAPHQL_GATE_QUERY response shape
    "gate": {
        "data": {
            "search": {"nodes": [
                {
                    "number": 10, "headRefOid": "aaaa1111", "mergeable": "CONFLICTING",
                    "updatedAt": "2026-01-05T00:00:00Z", "isDraft": False,
                    "author": {"login": USER}, "title": "My feature",
                    "bodyText": "Fixes #1", "headRefName": "feat/a",
                    "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]},
                    "comments": {"nodes": [{"createdAt": "2026-01-05T01:00:00Z"}]},
                },
                {
                    "number": 11, "headRefOid": "bbbb2222", "mergeable": "MERGEABLE",
                    "updatedAt": "2026-01-06T00:00:00Z", "isDraft": False,
                    "author": {"login": "other"}, "title": "Their fix",
                    "bodyText": "Their body", "headRefName": "fix/b",
                    "commits": {"nodes": []}, "comments": {"nodes": []},
                },
            ]},
            "repository": {"issues": {"nodes": [
                {"number": 1, "updatedAt": "2026-01-02T00:00:00Z",
                 "labels": {"nodes": [{"name": "auto:producer"}, {"name": "validated"}]},
                 "assignees": {"nodes": []}},
                {"number": 2, "updatedAt": "2026-01-04T00:00:00Z",
                 "labels": {"nodes": [{"name": "enhancement"}]},
                 "assignees": {"nodes": [{"login": USER}]}},
            ]}},
        },
    },
}


class FakeForge:
    """In-memory Forge implementation over a copy of SEED."""

    def __init__(self, auth_ok: bool = True) -> None:
        self.issues = copy.deepcopy(SEED["issues"])
        self.prs = copy.deepcopy(SEED["prs"])
        self.threads_pages = copy.deepcopy(SEED["threads_pages"])
        self.gate = copy.deepcopy(SEED["gate"])
        self._auth_ok = auth_ok
        self._next_issue = 100
        # issue #65: blocked-by dependencies, raw forge-payload shape.
        # Issue 1 is blocked by open issue 2; PR 10 is blocked by open PR 11.
        self.dependencies: Dict[int, List[Dict[str, Any]]] = {
            1: [dict(self.issues[1])],
            10: [dict(self.prs[1], pull_request={"url": _url("pull", 11)})],
        }

    def _issue(self, number: int) -> Dict[str, Any]:
        for i in self.issues:
            if i["number"] == number:
                return i
        raise ForgeError(f"issue {number} not found")

    def _pr(self, number: int) -> Dict[str, Any]:
        for p in self.prs:
            if p["number"] == number:
                return p
        raise ForgeError(f"pr {number} not found")

    def whoami(self) -> str:
        return SEED["user"]

    def repo_ref(self):
        from devloops_lib.forge.models import RepoRef
        return RepoRef(host=None, owner=OWNER, name=NAME)

    def auth_check(self):
        from devloops_lib.forge.models import AuthStatus
        return AuthStatus(ok=self._auth_ok, output="ok" if self._auth_ok else "not logged in")

    def list_issues(self, state: str = "open", labels=None, limit: int = 100):
        from devloops_lib.forge.models import Issue
        out = []
        for i in self.issues:
            if state != "all" and i["state"].lower() != state.lower():
                continue
            names = {l["name"] for l in i["labels"]}
            if labels and not set(labels) <= names:
                continue
            out.append(Issue(
                number=i["number"], title=i["title"], url=i["url"],
                state=i["state"].lower(), body=i["body"],
                labels=tuple(l["name"] for l in i["labels"]),
                assignees=tuple(a["login"] for a in i["assignees"]),
                created_at=i["createdAt"], updated_at=i["updatedAt"],
            ))
            if len(out) >= limit:
                break
        return out

    def get_issue(self, number: int):
        return [i for i in self.list_issues(state="all") if i.number == number][0]

    def create_issue(self, title: str, body: str, labels=()) -> str:
        number = self._next_issue
        self._next_issue += 1
        self.issues.append({
            "number": number, "title": title, "url": _url("issues", number),
            "state": "OPEN", "body": body,
            "labels": [{"name": l} for l in labels], "assignees": [],
            "createdAt": "2026-02-01T00:00:00Z", "updatedAt": "2026-02-01T00:00:00Z",
        })
        return _url("issues", number)

    def update_issue_body(self, number: int, body: str) -> None:
        self._issue(number)["body"] = body

    def add_labels(self, number: int, names) -> None:
        issue = self._issue(number)
        existing = {l["name"] for l in issue["labels"]}
        for name in names:
            if name not in existing:
                issue["labels"].append({"name": name})

    def remove_labels(self, number: int, names) -> None:
        issue = self._issue(number)
        issue["labels"] = [l for l in issue["labels"] if l["name"] not in set(names)]

    def add_assignee(self, number: int, login: str) -> None:
        assignees = self._issue(number)["assignees"]
        if not any(a["login"] == login for a in assignees):
            assignees.append({"login": login})

    def list_prs(self, state: str = "open", author: Optional[str] = None, limit: int = 100):
        from devloops_lib.forge import GitHubForge
        out = []
        for p in self.prs:
            if state != "all" and p["state"].lower() != state.lower():
                continue
            if author and p["author"]["login"] != author:
                continue
            out.append(GitHubForge._parse_pr(p))
            if len(out) >= limit:
                break
        return out

    def get_pr(self, number: int):
        from devloops_lib.forge import GitHubForge
        return GitHubForge._parse_pr(self._pr(number))

    def ci_status(self, pr):
        from devloops_lib.forge.models import (
            CI_FAILURE,
            CI_NONE,
            CI_PENDING,
            CI_SUCCESS,
            CiSummary,
        )
        checks = pr.raw.get("statusCheckRollup") or []

        def state_of(c):
            return (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()

        failing = [
            c.get("name") or c.get("context")
            for c in checks
            if state_of(c) in {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
        ]
        pending = [
            c.get("name") or c.get("context")
            for c in checks
            if state_of(c) in {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", "REQUESTED"}
        ]
        if failing:
            state = CI_FAILURE
        elif pending:
            state = CI_PENDING
        elif checks:
            state = CI_SUCCESS
        else:
            state = CI_NONE
        return CiSummary(state=state, failing=failing, pending=pending)

    def unresolved_review_threads(self, pr):
        from devloops_lib.forge.models import ReviewThread
        out = []
        for page in self.threads_pages:
            section = page["data"]["repository"]["pullRequest"]["reviewThreads"]
            for node in section["nodes"]:
                if node["isResolved"]:
                    continue
                first = node["comments"]["nodes"][0]
                out.append(ReviewThread(
                    thread_id=node["id"], path=node["path"], line=node["line"],
                    author=first["author"]["login"], body=first["body"],
                    url=first["url"], outdated=node["isOutdated"],
                ))
        return out

    def list_pr_comments(self, number: int):
        return self.list_comments(number)

    def list_comments(self, number: int):
        # Issue #67: one read answers issues and pull requests.
        from devloops_lib.forge.models import PrComment
        try:
            item: Dict[str, Any] = self._pr(number)
        except ForgeError:
            item = self._issue(number)
        return [PrComment(author=c["author"]["login"], created_at=c["createdAt"],
                          body=c["body"], url=c["url"]) for c in item.get("comments") or []]

    def post_pr_comment(self, number: int, body: str) -> None:
        self._pr(number)["comments"].append({
            "author": {"login": SEED["user"]}, "createdAt": "2026-02-02T00:00:00Z",
            "body": body, "url": _url("pull", number) + "#issuecomment-new",
        })

    def post_issue_comment(self, number: int, body: str) -> None:
        issue = self._issue(number)
        issue.setdefault("comments", []).append({
            "author": {"login": SEED["user"]}, "createdAt": "2026-02-02T00:00:00Z",
            "body": body, "url": _url("issues", number) + "#issuecomment-new",
        })

    # -- issue dependencies (issue #65) ------------------------------------

    def _get_any(self, number: int) -> Dict[str, Any]:
        try:
            return self._issue(number)
        except ForgeError:
            return self._pr(number)

    def blocked_by(self, number: int):
        from devloops_lib.forge.models import Blocker
        return [
            Blocker(
                number=d["number"],
                state=str(d.get("state", "open")).lower(),
                is_pr="pull_request" in d,
            )
            for d in self.dependencies.get(number, [])
        ]

    def add_blocker(self, number: int, blocker_number: int) -> None:
        self._get_any(number)  # unknown target raises ForgeError
        blocker = self._get_any(blocker_number)  # unknown blocker raises ForgeError
        deps = self.dependencies.setdefault(number, [])
        if any(d["number"] == blocker_number for d in deps):
            return
        item = dict(blocker)
        if any(p["number"] == blocker_number for p in self.prs):
            item.setdefault("pull_request", {"url": _url("pull", blocker_number)})
        deps.append(item)

    def gate_snapshot(self, pr_limit: int = 100, issue_limit: int = 100):
        from devloops_lib.forge.models import GateSnapshot
        data = self.gate["data"]
        return GateSnapshot(
            prs=data["search"]["nodes"],
            issues=data["repository"]["issues"]["nodes"],
            raw=copy.deepcopy(self.gate),
        )

    def pr_head_ref(self, number: int) -> str:
        return f"refs/pull/{number}/head"


class GhStub:
    """Fake subprocess-level gh runner answering SEED; records every argv."""

    def __init__(self, auth_ok: bool = True) -> None:
        self.issues = copy.deepcopy(SEED["issues"])
        self.prs = copy.deepcopy(SEED["prs"])
        self.threads_pages = copy.deepcopy(SEED["threads_pages"])
        self.gate = copy.deepcopy(SEED["gate"])
        self.auth_ok = auth_ok
        self.args_log: List[List[str]] = []
        self.graphql_calls: List[List[str]] = []
        self._next_issue = 100
        # issue #65: blocked-by dependencies, raw GitHub REST shape.
        self.dependencies: Dict[int, List[Dict[str, Any]]] = {
            1: [copy.deepcopy(self.issues[1])],
            10: [dict(copy.deepcopy(self.prs[1]), pull_request={"url": _url("pull", 11)})],
        }

    def run(self, args: List[str]) -> str:
        self.args_log.append(list(args))
        argv = [a for a in args if not a.startswith("-")]
        head = tuple(argv[:2]) if argv else ()
        if head == ("api", "user"):
            return SEED["user"] + "\n"
        if head == ("repo", "view"):
            return json.dumps(copy.deepcopy(SEED["repo"]))
        if head == ("auth", "status"):
            if not self.auth_ok:
                raise ForgeError("gh auth status failed (code 1): not logged in")
            return "github.com\n  Logged in to github.com account tester\n"
        if head == ("issue", "list"):
            return json.dumps(self._issue_list(args))
        if head == ("issue", "view"):
            number = int(argv[2])
            return json.dumps(self._find_issue(number))
        if head == ("issue", "create"):
            return self._issue_create(args)
        if head == ("issue", "edit"):
            return self._issue_edit(argv[2], args)
        if head == ("issue", "comment"):
            return self._issue_comment(argv[2], args)
        if head == ("pr", "list"):
            return json.dumps(self._pr_list(args))
        if head == ("pr", "view"):
            return json.dumps(self._find_pr(int(argv[2])))
        if head == ("pr", "comment"):
            return self._pr_comment(argv[2], args)
        if head == ("api", "graphql"):
            return self._graphql(args)
        if argv and argv[0] == "api" and len(argv) > 1 and argv[1].startswith("repos/"):
            return self._api_repo(argv[1], args)
        raise ForgeError(f"unexpected gh args: {args}")

    # -- helpers ----------------------------------------------------------

    def _find_issue(self, number: int) -> Dict[str, Any]:
        for i in self.issues:
            if i["number"] == number:
                return i
        raise ForgeError(f"issue {number} not found")

    def _find_pr(self, number: int) -> Dict[str, Any]:
        for p in self.prs:
            if p["number"] == number:
                return p
        raise ForgeError(f"pr {number} not found")

    def _opt(self, args: List[str], flag: str) -> Optional[str]:
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                return args[idx + 1]
        return None

    def _opts(self, args: List[str], flag: str) -> List[str]:
        return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]

    def _issue_list(self, args: List[str]) -> List[Dict[str, Any]]:
        state = self._opt(args, "--state") or "open"
        labels = set(self._opts(args, "--label"))
        out = []
        for i in self.issues:
            if state != "all" and i["state"].lower() != state.lower():
                continue
            if labels and not labels <= {l["name"] for l in i["labels"]}:
                continue
            out.append(i)
        return out

    def _issue_create(self, args: List[str]) -> str:
        number = self._next_issue
        self._next_issue += 1
        self.issues.append({
            "number": number, "title": self._opt(args, "--title") or "",
            "url": _url("issues", number), "state": "OPEN",
            "body": self._opt(args, "--body") or "",
            "labels": [{"name": l} for l in self._opts(args, "--label")],
            "assignees": [], "createdAt": "2026-02-01T00:00:00Z",
            "updatedAt": "2026-02-01T00:00:00Z",
        })
        return _url("issues", number) + "\n"

    def _issue_edit(self, number_s: str, args: List[str]) -> str:
        issue = self._find_issue(int(number_s))
        body = self._opt(args, "--body")
        if body is not None:
            issue["body"] = body
        for name in self._opts(args, "--add-label"):
            if name not in {l["name"] for l in issue["labels"]}:
                issue["labels"].append({"name": name})
        for name in self._opts(args, "--remove-label"):
            issue["labels"] = [l for l in issue["labels"] if l["name"] != name]
        assignee = self._opt(args, "--add-assignee")
        if assignee and not any(a["login"] == assignee for a in issue["assignees"]):
            issue["assignees"].append({"login": assignee})
        return json.dumps({"url": issue["url"]})

    def _pr_list(self, args: List[str]) -> List[Dict[str, Any]]:
        state = self._opt(args, "--state") or "open"
        author = self._opt(args, "--author")
        return [
            p for p in self.prs
            if (state == "all" or p["state"].lower() == state.lower())
            and (not author or p["author"]["login"] == author)
        ]

    def _pr_comment(self, number_s: str, args: List[str]) -> str:
        pr = self._find_pr(int(number_s))
        url = pr["url"] + f"#issuecomment-{len(pr['comments']) + 1}"
        pr["comments"].append({
            "author": {"login": SEED["user"]}, "createdAt": "2026-02-02T00:00:00Z",
            "body": self._opt(args, "--body") or "", "url": url,
        })
        return url + "\n"

    def _issue_comment(self, number_s: str, args: List[str]) -> str:
        issue = self._find_issue(int(number_s))
        comments = issue.setdefault("comments", [])
        url = issue["url"] + f"#issuecomment-{len(comments) + 1}"
        comments.append({
            "author": {"login": SEED["user"]}, "createdAt": "2026-02-02T00:00:00Z",
            "body": self._opt(args, "--body") or "", "url": url,
        })
        return url + "\n"

    def _graphql(self, args: List[str]) -> str:
        self.graphql_calls.append(list(args))
        joined = " ".join(args)
        if "reviewThreads" in joined:
            cursor = self._opt(args, "cursor=") or self._cursor_from_args(args)
            if cursor == "c2":
                return json.dumps(self.threads_pages[1])
            return json.dumps(self.threads_pages[0])
        if "search(query" in joined:
            return json.dumps(self.gate)
        raise ForgeError(f"unknown graphql query: {joined[:120]}")

    def _cursor_from_args(self, args: List[str]) -> Optional[str]:
        for a in args:
            if a.startswith("cursor="):
                return a[len("cursor="):]
        return None

    def _issue_id(self, number: int) -> int:
        return 10000 + number

    def _find_issue_or_pr(self, number: int) -> Dict[str, Any]:
        """GitHub issues API returns PRs too, with a pull_request field."""
        try:
            return dict(self._find_issue(number))
        except ForgeError:
            item = dict(self._find_pr(number))
            item.setdefault("pull_request", {"url": item["url"]})
            return item

    def _api_repo(self, path: str, args: List[str]) -> str:
        parts = path.split("/")
        if len(parts) >= 5 and parts[0] == "repos" and parts[3] == "issues":
            number = int(parts[4])
            if len(parts) == 5:
                item = self._find_issue_or_pr(number)
                item.setdefault("id", self._issue_id(number))
                return json.dumps(item)
            if len(parts) == 6 and parts[5] == "comments":
                # gh api repos/.../issues/{n}/comments answers for issues AND pull
                # requests (issue #67); comments come back in REST shape.
                item: Dict[str, Any] = {}
                for i in self.issues:
                    if i["number"] == number:
                        item = i
                        break
                else:
                    item = self._find_pr(number)
                out = []
                for c in item.get("comments") or []:
                    author = (c.get("author") or {}).get("login") or ""
                    out.append({
                        "user": {"login": author},
                        "created_at": c.get("createdAt", ""),
                        "body": c.get("body", ""),
                        "html_url": c.get("url"),
                        "author_association": "OWNER" if author == SEED["user"] else "NONE",
                    })
                return json.dumps(out)
            if parts[5] == "dependencies":
                if "-X" in args and "POST" in args:
                    # Match verified GitHub API: PR numbers are refused (422).
                    if any(p["number"] == number for p in self.prs):
                        raise ForgeError("HTTP 422: Source issue may only be an issue")
                    issue_id = None
                    for a in args:
                        if a.startswith("issue_id="):
                            issue_id = int(a[len("issue_id="):])
                    deps = self.dependencies.setdefault(number, [])
                    for item in self.issues + self.prs:
                        if self._issue_id(item["number"]) == issue_id:
                            if any(p["number"] == item["number"] for p in self.prs):
                                raise ForgeError("HTTP 422: Target issue may only be an issue")
                            if item["number"] not in [d["number"] for d in deps]:
                                deps.append(copy.deepcopy(item))
                            break
                    return json.dumps(deps)
                return json.dumps(self.dependencies.get(number, []))
        raise ForgeError(f"unexpected gh api path: {path}")


class ForgeContract:
    """Mixin defining the contract every Forge implementation must pass.

    Subclasses implement ``make_forge`` and ``auth_failure_forge``; optionally
    ``label_edit_commands`` exposes recorded raw commands to assert the label
    operations never send a full replacement set.
    """

    def setup_method(self) -> None:
        self.forge = self.make_forge()

    def make_forge(self):  # pragma: no cover
        raise NotImplementedError

    def auth_failure_forge(self):  # pragma: no cover
        raise NotImplementedError

    def label_edit_commands(self) -> List[List[str]]:
        return []

    # -- identity / repository -------------------------------------------

    def test_whoami(self):
        assert self.forge.whoami() == USER

    def test_repo_ref(self):
        ref = self.forge.repo_ref()
        assert (ref.owner, ref.name, ref.full_name) == (OWNER, NAME, f"{OWNER}/{NAME}")

    def test_auth_check_ok(self):
        assert self.forge.auth_check().ok is True

    def test_auth_check_failure(self):
        assert self.auth_failure_forge().auth_check().ok is False

    # -- issues -----------------------------------------------------------

    def test_list_issues_open_only(self):
        numbers = {i.number for i in self.forge.list_issues()}
        assert numbers == {1, 2}

    def test_list_issues_state_all(self):
        numbers = {i.number for i in self.forge.list_issues(state="all")}
        assert numbers == {1, 2, 3}

    def test_list_issues_label_filter(self):
        issues = self.forge.list_issues(labels=["auto:producer"])
        assert [i.number for i in issues] == [1]
        assert issues[0].labels == ("auto:producer", "validated")
        assert issues[0].assignees == ()

    def test_get_issue(self):
        issue = self.forge.get_issue(2)
        assert issue.title == "Feature request"
        assert issue.state == "open"
        assert issue.updated_at == "2026-01-04T00:00:00Z"
        assert issue.assignees == (USER,)

    def test_create_issue_persists(self):
        url = self.forge.create_issue("New one", "body text", ["bug", "triage"])
        assert isinstance(url, str) and url
        created = [i for i in self.forge.list_issues() if i.title == "New one"]
        assert len(created) == 1
        assert set(created[0].labels) >= {"bug", "triage"}

    def test_update_issue_body(self):
        self.forge.update_issue_body(1, "new body")
        assert self.forge.get_issue(1).body == "new body"

    def test_add_labels_is_additive(self):
        self.forge.add_labels(1, ["extra"])
        assert set(self.forge.get_issue(1).labels) == {"auto:producer", "validated", "extra"}

    def test_remove_labels_is_subtractive(self):
        self.forge.remove_labels(1, ["validated"])
        assert set(self.forge.get_issue(1).labels) == {"auto:producer"}

    def test_label_ops_never_replace_set(self):
        self.forge.add_labels(1, ["extra"])
        self.forge.remove_labels(1, ["validated"])
        for cmd in self.label_edit_commands():
            assert "--label" not in cmd

    def test_add_assignee(self):
        self.forge.add_assignee(1, USER)
        assert self.forge.get_issue(1).assignees == (USER,)

    # -- pull requests ----------------------------------------------------

    def test_list_prs_and_author_filter(self):
        assert {p.number for p in self.forge.list_prs()} == {10, 11, 12, 13}
        assert {p.number for p in self.forge.list_prs(author=USER)} == {10, 12}

    def test_get_pr_normalized(self):
        pr = self.forge.get_pr(10)
        assert (pr.head_ref, pr.head_sha, pr.base_ref) == ("feat/a", "aaaa1111", "main")
        assert pr.is_draft is False
        assert pr.mergeable is False  # CONFLICTING
        assert pr.author == USER
        assert pr.labels == ("approved",)
        assert pr.review_decision == ""  # forge-specific raw value passes through

    def test_mergeable_unknown_is_none(self):
        assert self.forge.get_pr(13).mergeable is None
        assert self.forge.get_pr(11).mergeable is True

    def test_ci_status_failure_and_pending(self):
        ci = self.forge.ci_status(self.forge.get_pr(10))
        assert ci.state == CI_FAILURE
        assert ci.failing == ["lint"]
        assert ci.pending == ["test"]

    def test_ci_status_success(self):
        assert self.forge.ci_status(self.forge.get_pr(11)).state == CI_SUCCESS

    def test_ci_status_none(self):
        assert self.forge.ci_status(self.forge.get_pr(12)).state == CI_NONE

    def test_unresolved_review_threads_paginates(self):
        threads = self.forge.unresolved_review_threads(self.forge.get_pr(10))
        assert [t.thread_id for t in threads] == ["T1", "T2"]
        assert threads[0].author == "reviewer"
        assert threads[0].body == "Fix this"
        assert threads[1].outdated is True

    def test_pr_comments_roundtrip(self):
        before = self.forge.list_pr_comments(10)
        assert [c.body for c in before] == ["hello"]
        assert before[0].author == USER
        self.forge.post_pr_comment(10, "worker-loop: note")
        after = self.forge.list_pr_comments(10)
        assert len(after) == len(before) + 1
        assert after[-1].body == "worker-loop: note"

    def test_issue_comments_roundtrip(self):
        # Issue #67 AC: the one comment read must answer for an ISSUE number
        # on every binding, not only pull-request numbers.
        before = self.forge.list_comments(2)
        assert [c.body for c in before] == []
        self.forge.post_issue_comment(2, "worker-loop: issue note")
        after = self.forge.list_comments(2)
        assert len(after) == len(before) + 1
        assert after[-1].body == "worker-loop: issue note"
        assert after[-1].author == USER

    # -- gate / refs --------------------------------------------------------

    def test_gate_snapshot(self):
        snap = self.forge.gate_snapshot()
        assert [p["number"] for p in snap.prs] == [10, 11]
        assert [i["number"] for i in snap.issues] == [1, 2]

    def test_pr_head_ref_mentions_number(self):
        assert "42" in self.forge.pr_head_ref(42)

    # -- issue dependencies (issue #65) -------------------------------------
    # Self-contained: every binding (in-memory fakes and the live server)
    # numbers its items differently, so these tests create their own
    # dependencies instead of reading seeded ones.

    def test_blocked_by_empty_when_no_dependencies(self):
        assert self.forge.blocked_by(2) == []

    def test_add_blocker_issue_number_and_read_back(self):
        self.forge.add_blocker(2, 1)  # open issue 1 blocks issue 2
        assert [(b.number, b.state, b.is_pr) for b in self.forge.blocked_by(2)] == [(1, "open", False)]
        self.forge.add_blocker(2, 1)  # safe to call twice
        assert len([b for b in self.forge.blocked_by(2) if b.number == 1]) == 1

    def supports_pr_dependencies(self) -> bool:
        """GitHub issue dependencies are issue-only (owner decision on #91)."""
        return self.forge.__class__.__name__ != "GitHubForge"

    def test_add_blocker_pr_number(self):
        numbers = sorted(p.number for p in self.forge.list_prs())
        assert len(numbers) >= 2
        target, blocker = numbers[0], numbers[1]
        if not self.supports_pr_dependencies():
            import pytest
            with pytest.raises(ForgeError, match="pull request|may only be an issue|issue-only"):
                self.forge.add_blocker(target, blocker)
            return
        self.forge.add_blocker(target, blocker)
        blockers = {b.number: b for b in self.forge.blocked_by(target)}
        assert blockers[blocker].state == "open"
        assert blockers[blocker].is_pr is True

    def test_add_blocker_closed_blocker_state(self):
        self.forge.add_blocker(2, 3)  # closed issue 3 blocks issue 2
        assert [b.state for b in self.forge.blocked_by(2) if b.number == 3] == ["closed"]
