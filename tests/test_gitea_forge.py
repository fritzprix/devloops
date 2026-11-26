#!/usr/bin/env python3
"""Contract and unit tests for GiteaForge and ForgejoForge (issue #46)."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib.forge import (
    CI_FAILURE,
    CI_NONE,
    CI_PENDING,
    CI_SUCCESS,
    ForgeDetectionError,
    ForgeError,
    ForgejoForge,
    GiteaForge,
    check_min_version,
    get_forge,
)
from devloops_lib.gatekeeper_readiness import has_pr_ci_workflow
from tests.forge_contract import NAME, OWNER, SEED, USER, ForgeContract


class GiteaFixtureStub:
    """In-memory Gitea/Forgejo /api/v1 HTTP fixture server answering SEED."""

    def __init__(self, *, auth_ok: bool = True, fail_first_n: int = 0, server_version: str = "1.22.0") -> None:
        self.auth_ok = auth_ok
        self.fail_first_n = fail_first_n
        self.server_version = server_version
        self.requests: List[Tuple[str, str, Optional[Any]]] = []
        self._next_issue = 100
        self._next_label_id = 10

        # Seed repo labels from SEED issues/prs
        self.labels: Dict[str, int] = {}
        for name in ("auto:producer", "validated", "enhancement", "bug", "approved", "triage"):
            self._ensure_label(name)

        self.issues: List[Dict[str, Any]] = []
        for i in copy.deepcopy(SEED["issues"]):
            self.issues.append({
                "number": i["number"],
                "title": i["title"],
                "html_url": i["url"],
                "state": i["state"].lower(),
                "body": i["body"],
                "labels": [{"id": self._ensure_label(l["name"]), "name": l["name"]} for l in i["labels"]],
                "assignees": [{"login": a["login"]} for a in i["assignees"]],
                "created_at": i["createdAt"],
                "updated_at": i["updatedAt"],
            })

        self.prs: List[Dict[str, Any]] = []
        self.statuses: Dict[str, List[Dict[str, Any]]] = {}
        self.reviews: Dict[int, List[Dict[str, Any]]] = {}
        self.review_comments: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        self.comments: Dict[int, List[Dict[str, Any]]] = {}

        for p in copy.deepcopy(SEED["prs"]):
            num = p["number"]
            mergeable = (
                True if p["mergeable"] == "MERGEABLE"
                else False if p["mergeable"] == "CONFLICTING"
                else None
            )
            self.prs.append({
                "number": num,
                "title": p["title"],
                "html_url": p["url"],
                "state": p["state"].lower(),
                "body": p["body"],
                "draft": p["isDraft"],
                "mergeable": mergeable,
                "head": {"ref": p["headRefName"], "sha": p["headRefOid"]},
                "base": {"ref": p["baseRefName"]},
                "user": {"login": p["author"]["login"]},
                "labels": [{"id": self._ensure_label(l["name"]), "name": l["name"]} for l in p["labels"]],
                "updated_at": p["updatedAt"],
            })
            st_list = []
            for chk in p.get("statusCheckRollup") or []:
                raw_st = (chk.get("conclusion") or chk.get("state") or "").lower()
                st_list.append({
                    "context": chk.get("name") or chk.get("context"),
                    "status": raw_st,
                    "target_url": f"https://ci.example.com/run/{num}",
                })
            self.statuses[p["headRefOid"]] = st_list
            self.comments[num] = [
                {
                    "user": {"login": c["author"]["login"]},
                    "created_at": c["createdAt"],
                    "body": c["body"],
                    "html_url": c["url"],
                }
                for c in p.get("comments") or []
            ]
            if p.get("reviewDecision") == "APPROVED":
                self.reviews[num] = [{"id": 1, "state": "APPROVED", "user": {"login": "reviewer"}}]
            else:
                self.reviews[num] = []

        # Seed review threads for PR 10 from SEED["threads_pages"]
        self.reviews[10] = [{"id": 101, "state": "COMMENT", "user": {"login": "reviewer"}}]
        rc: List[Dict[str, Any]] = []
        for page in SEED["threads_pages"]:
            for node in page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
                first = node["comments"]["nodes"][0]
                rc.append({
                    "id": node["id"],
                    "thread_id": node["id"],
                    "path": node["path"],
                    "line": node["line"],
                    "user": {"login": first["author"]["login"]},
                    "body": first["body"],
                    "html_url": first["url"],
                    "outdated": node["isOutdated"],
                    "resolver": {"login": "me"} if node["isResolved"] else None,
                })
        self.review_comments[(10, 101)] = rc

        # issue #65: issue 1 is blocked by open issue 2; PR 10 is blocked by
        # open PR 11. dependencies_disabled simulates the repo feature being off.
        self.dependencies_disabled = False
        self.dependencies: Dict[int, Dict[str, List[Dict[str, Any]]]] = {
            1: {"blocked_by": [copy.deepcopy(self.issues[1])], "blocks": []},
            10: {"blocked_by": [dict(self.prs[1], pull_request={"html_url": self.prs[1]["html_url"]})],
                 "blocks": []},
        }

    def _ensure_label(self, name: str) -> int:
        if name not in self.labels:
            self.labels[name] = self._next_label_id
            self._next_label_id += 1
        return self.labels[name]

    def _label_name(self, lbl_id: int) -> str:
        for name, lid in self.labels.items():
            if lid == lbl_id:
                return name
        return f"label-{lbl_id}"

    def _find_issue(self, number: int) -> Optional[Dict[str, Any]]:
        return next((i for i in self.issues if i["number"] == number), None)

    def _find_pr(self, number: int) -> Optional[Dict[str, Any]]:
        return next((p for p in self.prs if p["number"] == number), None)

    def __call__(self, req: urllib.request.Request, timeout: float) -> Tuple[int, Dict[str, str], str]:
        parsed = urllib.parse.urlparse(req.full_url)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        method = req.get_method().upper()
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.requests.append((method, path, body))

        if self.fail_first_n > 0:
            self.fail_first_n -= 1
            return 503, {}, json.dumps({"message": "temporarily unavailable"})

        if method == "GET" and path == "/api/v1/version":
            return 200, {}, json.dumps({"version": self.server_version})

        if not self.auth_ok:
            return 401, {}, json.dumps({"message": "unauthorized"})

        prefix = f"/api/v1/repos/{OWNER}/{NAME}"

        if method == "GET" and path == "/api/v1/user":
            return 200, {}, json.dumps({"login": USER})

        if path == f"{prefix}/labels":
            if method == "GET":
                page = int((qs.get("page") or ["1"])[0])
                if page > 1:
                    return 200, {}, json.dumps([])
                return 200, {}, json.dumps([{"id": lid, "name": n} for n, lid in self.labels.items()])
            if method == "POST":
                lid = self._ensure_label(body["name"])
                return 201, {}, json.dumps({"id": lid, "name": body["name"]})

        if path == f"{prefix}/issues":
            if method == "GET":
                state = (qs.get("state") or ["open"])[0]
                page = int((qs.get("page") or ["1"])[0])
                limit = int((qs.get("limit") or ["50"])[0])
                lbls = set((qs.get("labels") or [""])[0].split(",")) - {""}
                matched = [
                    i for i in self.issues
                    if (state == "all" or i["state"] == state)
                    and (not lbls or lbls <= {l["name"] for l in i["labels"]})
                ]
                start = (page - 1) * limit
                return 200, {}, json.dumps(matched[start:start + limit])
            if method == "POST":
                num = self._next_issue
                self._next_issue += 1
                lbl_objs = [{"id": lid, "name": self._label_name(lid)} for lid in (body.get("labels") or [])]
                item = {
                    "number": num,
                    "title": body["title"],
                    "body": body.get("body", ""),
                    "html_url": f"https://git.example.com/{OWNER}/{NAME}/issues/{num}",
                    "state": "open",
                    "labels": lbl_objs,
                    "assignees": [],
                    "created_at": "2026-02-01T00:00:00Z",
                    "updated_at": "2026-02-01T00:00:00Z",
                }
                self.issues.append(item)
                return 201, {}, json.dumps(item)

        if path.startswith(f"{prefix}/issues/"):
            rest = path[len(f"{prefix}/issues/"):].split("/")
            num = int(rest[0])
            if len(rest) == 1:
                issue = self._find_issue(num)
                if issue is None:
                    return 404, {}, json.dumps({"message": "not found"})
                if method == "GET":
                    return 200, {}, json.dumps(issue)
                if method == "PATCH":
                    if "body" in body:
                        issue["body"] = body["body"]
                    if "assignees" in body:
                        issue["assignees"] = [{"login": a} for a in body["assignees"]]
                    return 200, {}, json.dumps(issue)
            if len(rest) >= 2 and rest[1] == "labels":
                issue = self._find_issue(num)
                if issue is None:
                    return 404, {}, json.dumps({"message": "not found"})
                if method == "POST":
                    existing = {l["id"] for l in issue["labels"]}
                    for lid in body.get("labels") or []:
                        if lid not in existing:
                            issue["labels"].append({"id": lid, "name": self._label_name(lid)})
                    return 200, {}, json.dumps(issue["labels"])
                if method == "DELETE" and len(rest) == 3:
                    lid = int(rest[2])
                    issue["labels"] = [l for l in issue["labels"] if l["id"] != lid]
                    return 204, {}, ""
            if len(rest) >= 2 and rest[1] == "dependencies":
                if self.dependencies_disabled:
                    return 404, {}, json.dumps({"message": "issue dependencies disabled"})
                if self._find_issue(num) is None and self._find_pr(num) is None:
                    return 404, {}, json.dumps({"message": "not found"})
                deps = self.dependencies.setdefault(num, {"blocked_by": [], "blocks": []})
                if method == "GET":
                    page = int((qs.get("page") or ["1"])[0])
                    limit = int((qs.get("limit") or ["50"])[0])
                    start = (page - 1) * limit
                    chunk = deps["blocked_by"][start:start + limit]
                    return 200, {}, json.dumps({"blocked_by": chunk, "blocks": deps.get("blocks") or []})
                if method == "POST":
                    idx = int(body["index"])
                    if not any(d["number"] == idx for d in deps["blocked_by"]):
                        blocker = self._find_pr(idx)
                        if blocker is None:
                            blocker = self._find_issue(idx)
                        if blocker is None:
                            return 404, {}, json.dumps({"message": "blocker not found"})
                        entry = copy.deepcopy(blocker)
                        if self._find_pr(idx) is not None:
                            entry.setdefault("pull_request", {"html_url": blocker["html_url"]})
                        deps["blocked_by"].append(entry)
                    return 200, {}, json.dumps(deps)
            if len(rest) == 2 and rest[1] == "comments":
                if method == "GET":
                    page = int((qs.get("page") or ["1"])[0])
                    if page > 1:
                        return 200, {}, json.dumps([])
                    return 200, {}, json.dumps(self.comments.get(num, []))
                if method == "POST":
                    entry = {
                        "user": {"login": USER},
                        "created_at": "2026-02-02T00:00:00Z",
                        "body": body["body"],
                        "html_url": f"https://git.example.com/{OWNER}/{NAME}/pulls/{num}#comment-new",
                    }
                    self.comments.setdefault(num, []).append(entry)
                    return 201, {}, json.dumps(entry)

        if path == f"{prefix}/pulls" and method == "GET":
            state = (qs.get("state") or ["open"])[0]
            page = int((qs.get("page") or ["1"])[0])
            limit = int((qs.get("limit") or ["50"])[0])
            matched = [p for p in self.prs if state == "all" or p["state"] == state]
            start = (page - 1) * limit
            return 200, {}, json.dumps(matched[start:start + limit])

        if path.startswith(f"{prefix}/pulls/"):
            rest = path[len(f"{prefix}/pulls/"):].split("/")
            num = int(rest[0])
            if len(rest) == 1 and method == "GET":
                pr = self._find_pr(num)
                if pr is None:
                    return 404, {}, json.dumps({"message": "not found"})
                return 200, {}, json.dumps(pr)
            if len(rest) == 2 and rest[1] == "reviews" and method == "GET":
                return 200, {}, json.dumps(self.reviews.get(num, []))
            if len(rest) == 4 and rest[1] == "reviews" and rest[3] == "comments" and method == "GET":
                return 200, {}, json.dumps(self.review_comments.get((num, int(rest[2])), []))
            if len(rest) == 2 and rest[1] == "update" and method == "POST":
                return 200, {}, json.dumps({"ok": True})

        if path.startswith(f"{prefix}/commits/") and path.endswith("/status") and method == "GET":
            sha = path[len(f"{prefix}/commits/"):-len("/status")]
            return 200, {}, json.dumps({"statuses": self.statuses.get(sha, [])})

        return 404, {}, json.dumps({"message": f"unhandled {method} {path}"})


class TestGiteaForgeContract(ForgeContract):
    """Run the #44 contract suite against GiteaForge over recorded HTTP fixtures."""

    def make_forge(self):
        self._stub = GiteaFixtureStub()
        return GiteaForge(
            base_url="https://git.example.com",
            owner=OWNER,
            name=NAME,
            version="1.22.0",
            retry_backoff=0.0,
            opener=self._stub,
        )

    def auth_failure_forge(self):
        return GiteaForge(
            base_url="https://git.example.com",
            owner=OWNER,
            name=NAME,
            retry_backoff=0.0,
            opener=GiteaFixtureStub(auth_ok=False),
        )

    def test_gate_snapshot(self):
        snap = self.forge.gate_snapshot()
        assert {p["number"] for p in snap.prs} >= {10, 11}
        assert [i["number"] for i in snap.issues] == [1, 2]
        assert snap.raw["forge"] == "gitea"

    def test_label_edits_never_use_put(self):
        self.forge.add_labels(1, ["extra"])
        self.forge.remove_labels(1, ["validated"])
        methods = [m for m, p, _ in self._stub.requests if "/labels" in p]
        assert "PUT" not in methods
        assert set(self.forge.get_issue(1).labels) == {"auto:producer", "extra"}


class TestForgejoForgeContract(TestGiteaForgeContract):
    """Run the #44 contract suite against ForgejoForge."""

    def make_forge(self):
        self._stub = GiteaFixtureStub()
        return ForgejoForge(
            base_url="https://forgejo.example.com",
            owner=OWNER,
            name=NAME,
            version="9.0.1",
            retry_backoff=0.0,
            opener=self._stub,
        )

    def auth_failure_forge(self):
        return ForgejoForge(
            base_url="https://forgejo.example.com",
            owner=OWNER,
            name=NAME,
            retry_backoff=0.0,
            opener=GiteaFixtureStub(auth_ok=False, server_version="9.0.1"),
        )

    def test_gate_snapshot(self):
        snap = self.forge.gate_snapshot()
        assert snap.raw["forge"] == "forgejo"


class GiteaForgeBehaviorTest(unittest.TestCase):
    def test_min_version_enforced_fail_closed(self):
        with self.assertRaises(ForgeDetectionError):
            GiteaForge(base_url="https://g.example", owner="o", name="r", version="1.20.9")
        with self.assertRaises(ForgeDetectionError):
            ForgejoForge(base_url="https://f.example", owner="o", name="r", version="6.9.9")
        with self.assertRaises(ForgeDetectionError):
            check_min_version("gitea", "")
        with self.assertRaises(ForgeDetectionError):
            check_min_version("forgejo", "nightly-dev")
        # Supported versions succeed
        check_min_version("gitea", "1.21.0")
        check_min_version("forgejo", "7.0.0")

    def test_explicit_type_without_version_probes_once_and_fails_closed_on_old_or_unreadable(self):
        # Old server rejected on first use
        old_stub = GiteaFixtureStub(server_version="1.20.5")
        old_forge = GiteaForge(base_url="https://g.example", owner=OWNER, name=NAME, opener=old_stub)
        with self.assertRaises(ForgeDetectionError):
            old_forge.whoami()

        # Unparseable version rejected on first use
        bad_stub = GiteaFixtureStub(server_version="unknown")
        bad_forge = ForgejoForge(base_url="https://f.example", owner=OWNER, name=NAME, opener=bad_stub)
        with self.assertRaises(ForgeDetectionError):
            bad_forge.whoami()

        # Valid server version is checked once and cached for subsequent calls
        ok_stub = GiteaFixtureStub(server_version="9.0.1")
        ok_forge = ForgejoForge(base_url="https://f.example", owner=OWNER, name=NAME, opener=ok_stub)
        self.assertEqual(ok_forge.whoami(), USER)
        self.assertEqual(ok_forge.whoami(), USER)
        version_calls = [r for r in ok_stub.requests if r[1] == "/api/v1/version"]
        self.assertEqual(len(version_calls), 1)

    def test_token_never_leaked_in_repr_or_errors(self):
        secret = "tok-very-secret-987654"

        def echoing_error_opener(req, timeout):
            # Even if server echoes the Authorization header in error body, it must be redacted.
            return 500, {}, f"bad token: {secret}"

        with patch.dict(os.environ, {"FORGEJO_API_TOKEN": secret}, clear=False):
            forge = ForgejoForge(
                base_url="https://f.example",
                owner=OWNER,
                name=NAME,
                token_env="FORGEJO_API_TOKEN",
                version="9.0.1",
                max_retries=0,
                opener=echoing_error_opener,
            )
            self.assertNotIn(secret, repr(forge))
            with self.assertRaises(ForgeError) as ctx:
                forge.whoami()
            self.assertNotIn(secret, str(ctx.exception))
            self.assertIn("<redacted>", str(ctx.exception))

    def test_retries_on_503_then_succeeds(self):
        stub = GiteaFixtureStub(fail_first_n=2)
        forge = GiteaForge(
            base_url="https://g.example",
            owner=OWNER,
            name=NAME,
            max_retries=3,
            retry_backoff=0.0,
            opener=stub,
        )
        self.assertEqual(forge.whoami(), USER)

    def test_unsupported_ci_logs_and_rerun_return_explicit_unsupported(self):
        stub = GiteaFixtureStub(server_version="9.0.1")
        forge = ForgejoForge(
            base_url="https://f.example",
            owner=OWNER,
            name=NAME,
            retry_backoff=0.0,
            opener=stub,
        )
        pr10 = forge.get_pr(10)
        logs = forge.ci_logs_failed(pr10)
        self.assertFalse(logs["supported"])
        self.assertEqual(logs["status"], "unsupported")
        self.assertEqual(logs["target_url"], "https://ci.example.com/run/10")
        # Also accepts a string target (as passed by `devloops forge ci logs <target>`).
        self.assertEqual(forge.ci_logs_failed("42")["status"], "unsupported")
        self.assertEqual(forge.ci_rerun_failed("42")["status"], "unsupported")

        rerun = forge.ci_rerun_failed(pr10)
        self.assertFalse(rerun["supported"])
        self.assertEqual(rerun["status"], "unsupported")

    def test_update_branch_posts_update(self):
        stub = GiteaFixtureStub()
        forge = GiteaForge(base_url="https://g.example", owner=OWNER, name=NAME, opener=stub)
        forge.update_branch(10)
        self.assertIn(("POST", f"/api/v1/repos/{OWNER}/{NAME}/pulls/10/update", None), stub.requests)

    def test_dependencies_404_maps_to_setup_error(self):
        # issue #65: a server without the dependencies feature must fail with a
        # clear setup error, never masquerade as an empty blocker list.
        stub = GiteaFixtureStub()
        stub.dependencies_disabled = True
        forge = GiteaForge(
            base_url="https://git.example.com", owner=OWNER, name=NAME,
            version="1.22.0", retry_backoff=0.0, opener=stub,
        )
        with self.assertRaises(ForgeError) as ctx:
            forge.blocked_by(1)
        self.assertIn("Enable issue dependencies", str(ctx.exception))

    def test_blocked_by_paginates_past_first_page(self):
        stub = GiteaFixtureStub()
        # More blockers than page_size so a single request cannot see them all.
        extras = []
        for i in range(5):
            issue = {
                "number": 100 + i, "title": f"Extra {i}", "state": "open",
                "html_url": f"https://git.example.com/{OWNER}/{NAME}/issues/{100 + i}",
                "body": "", "labels": [], "assignees": [],
                "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
                "id": 20000 + i,
            }
            stub.issues.append(issue)
            extras.append(copy.deepcopy(issue))
        stub.dependencies[1] = {
            "blocked_by": [copy.deepcopy(stub.issues[1])] + extras,
            "blocks": [],
        }
        forge = GiteaForge(
            base_url="https://git.example.com", owner=OWNER, name=NAME,
            version="1.22.0", retry_backoff=0.0, page_size=2, opener=stub,
        )
        blockers = forge.blocked_by(1)
        self.assertEqual([b.number for b in blockers], [2, 100, 101, 102, 103, 104])
        dep_gets = [r for r in stub.requests if r[0] == "GET" and "dependencies" in r[1]]
        self.assertGreaterEqual(len(dep_gets), 3)

    def test_auth_check_reports_disabled_dependencies(self):
        stub = GiteaFixtureStub()
        stub.dependencies_disabled = True
        forge = GiteaForge(
            base_url="https://git.example.com", owner=OWNER, name=NAME,
            version="1.22.0", retry_backoff=0.0, opener=stub,
        )
        status = forge.auth_check()
        self.assertTrue(status.ok)
        self.assertIn("issue dependencies appear disabled", status.output)

    def test_get_forge_factory_returns_forgejo_and_gitea(self):
        f = get_forge(
            {"forge": {"type": "forgejo", "url": "https://f.example", "token_env": "F_TOK"}},
            remote_url="https://f.example/acme/widgets.git",
        )
        self.assertIsInstance(f, ForgejoForge)
        g = get_forge(
            {"forge": {"type": "gitea", "url": "https://g.example", "token_env": "G_TOK"}},
            remote_url="https://g.example/acme/widgets.git",
        )
        self.assertIsInstance(g, GiteaForge)

    def test_gatekeeper_readiness_accepts_forgejo_and_gitea_workflows(self):
        for folder in (".forgejo/workflows", ".gitea/workflows"):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as tmp:
                wf = Path(tmp) / folder / "ci.yml"
                wf.parent.mkdir(parents=True)
                wf.write_text("on:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu\n", encoding="utf-8")
                self.assertTrue(has_pr_ci_workflow(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
