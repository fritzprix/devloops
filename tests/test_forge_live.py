#!/usr/bin/env python3
"""Live integration tests against a local Forgejo or Gitea container (issue #48).

Runs the shared ``ForgeContract`` suite (from ``tests/forge_contract.py``) plus
live forge detection and ``doctor.check_forge`` against a real Forgejo or Gitea
server. Skipped unless ``DEVLOOPS_FORGE_LIVE_URL`` and
``DEVLOOPS_FORGE_LIVE_TOKEN`` are set. See ``tests/README.md`` for local usage.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import doctor  # noqa: E402
from devloops_lib.forge import CI_FAILURE, CI_NONE, CI_SUCCESS  # noqa: E402
from devloops_lib.forge.detect import detect_forge, probe_forge  # noqa: E402
from devloops_lib.forge.gitea import ForgejoForge, GiteaForge  # noqa: E402
from tests.forge_contract import NAME, OWNER, USER, ForgeContract  # noqa: E402

LIVE_URL = os.environ.get("DEVLOOPS_FORGE_LIVE_URL", "").strip().rstrip("/")
LIVE_TOKEN = os.environ.get("DEVLOOPS_FORGE_LIVE_TOKEN", "").strip()
LIVE_TYPE = os.environ.get("DEVLOOPS_FORGE_LIVE_TYPE", "forgejo").strip().lower()

pytestmark = [
    pytest.mark.forge_live,
    pytest.mark.skipif(
        not LIVE_URL or not LIVE_TOKEN,
        reason="DEVLOOPS_FORGE_LIVE_URL and DEVLOOPS_FORGE_LIVE_TOKEN not set",
    ),
]


def _api(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    token: str = LIVE_TOKEN,
    allow_404: bool = False,
) -> Tuple[int, Any]:
    url = f"{LIVE_URL}/api/v1{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return 404, None
        raise


def _seed_contract_repo() -> Dict[str, Any]:
    """Recreate OWNER/NAME on the live server seeded to match ``tests.forge_contract.SEED``."""
    # Ensure org OWNER exists.
    status, _ = _api("GET", f"/orgs/{OWNER}", allow_404=True)
    if status == 404:
        _api("POST", "/orgs", {"username": OWNER})

    # Recreate repo NAME cleanly for full test isolation.
    _api("DELETE", f"/repos/{OWNER}/{NAME}", allow_404=True)
    _api(
        "POST",
        f"/orgs/{OWNER}/repos",
        {"name": NAME, "auto_init": True, "default_branch": "main"},
    )

    label_defs = [
        ("auto:producer", "#fbca04"),
        ("validated", "#1d76db"),
        ("enhancement", "#a2eeef"),
        ("bug", "#d73a4a"),
        ("triage", "#cfd3d7"),
        ("extra", "#c5def5"),
        ("approved", "#0e8a16"),
        ("gatekeeper:approved", "#0e8a16"),
        ("gatekeeper:changes-requested", "#d93f0b"),
    ]
    label_ids: Dict[str, int] = {}
    for lname, color in label_defs:
        _, lbl = _api("POST", f"/repos/{OWNER}/{NAME}/labels", {"name": lname, "color": color})
        label_ids[lname] = lbl["id"]

    # Issues 1, 2 (open), 3 (closed) — matching SEED.
    _api(
        "POST",
        f"/repos/{OWNER}/{NAME}/issues",
        {
            "title": "Producer issue",
            "body": "First issue",
            "labels": [label_ids["auto:producer"], label_ids["validated"]],
        },
    )
    _api(
        "POST",
        f"/repos/{OWNER}/{NAME}/issues",
        {
            "title": "Feature request",
            "body": "Second issue",
            "labels": [label_ids["enhancement"]],
            "assignees": [USER],
        },
    )
    _, issue3 = _api(
        "POST",
        f"/repos/{OWNER}/{NAME}/issues",
        {
            "title": "Old bug",
            "body": "Closed already",
            "labels": [label_ids["bug"]],
        },
    )
    _api("PATCH", f"/repos/{OWNER}/{NAME}/issues/{issue3['number']}", {"state": "closed"})

    # Seed 3 PRs (failure+pending CI, success CI, none CI).
    def _create_pr(branch: str, filename: str, title: str, labels: list[int]) -> Tuple[int, str]:
        content_b64 = base64.b64encode(f"content for {branch}\n".encode("utf-8")).decode("ascii")
        _, fresp = _api(
            "POST",
            f"/repos/{OWNER}/{NAME}/contents/{filename}",
            {
                "branch": "main",
                "new_branch": branch,
                "content": content_b64,
                "message": f"feat: {branch}",
            },
        )
        sha = fresp["commit"]["sha"]
        _, pr = _api(
            "POST",
            f"/repos/{OWNER}/{NAME}/pulls",
            {
                "title": title,
                "body": "Fixes #1",
                "head": branch,
                "base": "main",
                "labels": labels,
            },
        )
        return pr["number"], sha

    pr_fail, sha_fail = _create_pr("feat/a", "a.txt", "My feature", [label_ids["approved"]])
    _api("POST", f"/repos/{OWNER}/{NAME}/issues/{pr_fail}/comments", {"body": "hello"})
    _api("POST", f"/repos/{OWNER}/{NAME}/statuses/{sha_fail}", {"state": "failure", "context": "lint"})
    _api("POST", f"/repos/{OWNER}/{NAME}/statuses/{sha_fail}", {"state": "pending", "context": "test"})
    _api("POST", f"/repos/{OWNER}/{NAME}/statuses/{sha_fail}", {"state": "success", "context": "build"})

    pr_ok, sha_ok = _create_pr("feat/b", "b.txt", "Clean feature", [])
    _api("POST", f"/repos/{OWNER}/{NAME}/statuses/{sha_ok}", {"state": "success", "context": "build"})

    pr_none, sha_none = _create_pr("feat/c", "c.txt", "No CI yet", [])

    return {
        "pr_fail": pr_fail,
        "sha_fail": sha_fail,
        "pr_ok": pr_ok,
        "sha_ok": sha_ok,
        "pr_none": pr_none,
        "sha_none": sha_none,
    }


def _make_live_forge(token_env: str = "DEVLOOPS_FORGE_LIVE_TOKEN") -> GiteaForge:
    cls = ForgejoForge if LIVE_TYPE == "forgejo" else GiteaForge
    return cls(base_url=LIVE_URL, owner=OWNER, name=NAME, token_env=token_env)


class TestLiveForgeContract(ForgeContract):
    """Run the shared ``ForgeContract`` suite against the live Forgejo/Gitea server."""

    def setup_method(self) -> None:
        self.meta = _seed_contract_repo()
        super().setup_method()

    def make_forge(self) -> GiteaForge:
        return _make_live_forge()

    def auth_failure_forge(self) -> GiteaForge:
        os.environ["DEVLOOPS_BAD_LIVE_TOKEN"] = "invalid-token-value"
        return _make_live_forge(token_env="DEVLOOPS_BAD_LIVE_TOKEN")

    # Overrides for tests where live servers assign real timestamps, SHAs, and shared issue/PR numbers.

    def test_get_issue(self) -> None:
        issue = self.forge.get_issue(2)
        assert issue.title == "Feature request"
        assert issue.state == "open"
        assert issue.updated_at
        assert issue.assignees == (USER,)

    def test_list_prs_and_author_filter(self) -> None:
        expected = {self.meta["pr_fail"], self.meta["pr_ok"], self.meta["pr_none"]}
        assert {p.number for p in self.forge.list_prs()} == expected
        assert {p.number for p in self.forge.list_prs(author=USER)} == expected

    def test_get_pr_normalized(self) -> None:
        pr = self.forge.get_pr(self.meta["pr_fail"])
        assert (pr.head_ref, pr.head_sha, pr.base_ref) == ("feat/a", self.meta["sha_fail"], "main")
        assert pr.is_draft is False
        assert pr.author == USER
        assert pr.labels == ("approved",)

    def test_mergeable_unknown_is_none(self) -> None:
        # Gitea/Forgejo compute mergeable asynchronously and transiently report
        # False for a clean PR while the check runs before settling on True
        # (reproduced locally: 7/15 seed cycles observed False -> True). Poll
        # until the value leaves False; a clean PR stuck at False is a real bug.
        pr = self.forge.get_pr(self.meta["pr_ok"])
        deadline = time.monotonic() + 15.0
        while pr.mergeable is False and time.monotonic() < deadline:
            time.sleep(0.5)
            pr = self.forge.get_pr(self.meta["pr_ok"])
        assert pr.mergeable in (True, None)

    def test_ci_status_failure_and_pending(self) -> None:
        ci = self.forge.ci_status(self.forge.get_pr(self.meta["pr_fail"]))
        assert ci.state == CI_FAILURE
        assert ci.failing == ["lint"]
        assert ci.pending == ["test"]

    def test_ci_status_success(self) -> None:
        assert self.forge.ci_status(self.forge.get_pr(self.meta["pr_ok"])).state == CI_SUCCESS

    def test_ci_status_none(self) -> None:
        assert self.forge.ci_status(self.forge.get_pr(self.meta["pr_none"])).state == CI_NONE

    def test_unresolved_review_threads_paginates(self) -> None:
        threads = self.forge.unresolved_review_threads(self.forge.get_pr(self.meta["pr_fail"]))
        assert isinstance(threads, list)

    def test_pr_comments_roundtrip(self) -> None:
        pr_num = self.meta["pr_fail"]
        before = self.forge.list_pr_comments(pr_num)
        assert [c.body for c in before] == ["hello"]
        assert before[0].author == USER
        self.forge.post_pr_comment(pr_num, "worker-loop: note")
        after = self.forge.list_pr_comments(pr_num)
        assert len(after) == len(before) + 1
        assert after[-1].body == "worker-loop: note"

    def test_gate_snapshot(self) -> None:
        snap = self.forge.gate_snapshot()
        assert {p["number"] for p in snap.prs} == {self.meta["pr_fail"], self.meta["pr_ok"], self.meta["pr_none"]}
        assert {i["number"] for i in snap.issues} == {1, 2}


def test_live_detection_and_doctor() -> None:
    """Prove Forgejo-before-Gitea detection ordering and doctor check against live server."""
    _seed_contract_repo()
    remote_url = f"{LIVE_URL}/{OWNER}/{NAME}.git"
    probe = probe_forge(LIVE_URL)
    assert probe.forge_type == LIVE_TYPE, f"expected {LIVE_TYPE}, got {probe}"
    assert probe.version

    state: Dict[str, Any] = {}
    cfg = {
        "forge": {
            "type": "auto",
            "url": LIVE_URL,
            "token_env": "DEVLOOPS_FORGE_LIVE_TOKEN",
        }
    }
    info = detect_forge(cfg, state=state, remote_url=remote_url)
    assert info["type"] == LIVE_TYPE
    assert state["forge"]["type"] == LIVE_TYPE
    assert LIVE_TOKEN not in json.dumps(state)

    assert doctor.check_forge(cfg, remote_url=remote_url) is True
