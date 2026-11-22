#!/usr/bin/env python3
"""Live integration tests against a local Forgejo or Gitea container (issue #48).

Skipped unless ``DEVLOOPS_FORGE_LIVE_URL`` and ``DEVLOOPS_FORGE_LIVE_TOKEN`` are
set. See ``tests/README.md`` for the pinned ``docker run`` command to run these
locally; CI runs them in the ``forge-integration`` job of
``.github/workflows/ci.yml``.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import doctor  # noqa: E402
from devloops_lib.forge import CI_FAILURE, CI_PENDING, CI_SUCCESS, ForgeError  # noqa: E402
from devloops_lib.forge.detect import detect_forge, probe_forge  # noqa: E402
from devloops_lib.forge.gitea import ForgejoForge, GiteaForge  # noqa: E402

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
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, (json.loads(raw) if raw.strip() else None)


@pytest.fixture(scope="module")
def seeded_forge() -> Dict[str, Any]:
    """Create a fresh repo on the live server with labels, issues, a PR, and commit statuses."""
    _, user = _api("GET", "/user")
    owner = user["login"]
    repo_name = f"devloops-it-{uuid.uuid4().hex[:8]}"

    _api(
        "POST",
        "/user/repos",
        {"name": repo_name, "auto_init": True, "default_branch": "main"},
    )

    # Seed labels (including approved and gatekeeper:*).
    label_defs = [
        ("approved", "#0e8a16"),
        ("auto:producer", "#fbca04"),
        ("validated", "#1d76db"),
        ("enhancement", "#a2eeef"),
        ("gatekeeper:approved", "#0e8a16"),
        ("gatekeeper:changes-requested", "#d93f0b"),
        ("unrelated-keep", "#c5def5"),
    ]
    label_ids: Dict[str, int] = {}
    for name, color in label_defs:
        _, lbl = _api("POST", f"/repos/{owner}/{repo_name}/labels", {"name": name, "color": color})
        label_ids[name] = lbl["id"]

    # Seed an open issue.
    _, issue1 = _api(
        "POST",
        f"/repos/{owner}/{repo_name}/issues",
        {
            "title": "Seeded feature issue",
            "body": "Acceptance criteria: works on live forge.",
            "labels": [label_ids["approved"], label_ids["enhancement"]],
        },
    )

    # Create a feature branch with a commit via the contents API, then open a PR.
    content_b64 = base64.b64encode(b"hello from integration test\n").decode("ascii")
    _, file_resp = _api(
        "POST",
        f"/repos/{owner}/{repo_name}/contents/feature.txt",
        {
            "branch": "main",
            "new_branch": "feat/live-test",
            "content": content_b64,
            "message": "feat: add feature.txt for live integration test",
        },
    )
    head_sha = file_resp["commit"]["sha"]

    _, pr1 = _api(
        "POST",
        f"/repos/{owner}/{repo_name}/pulls",
        {
            "title": "feat: live integration PR",
            "body": f"Fixes #{issue1['number']}",
            "head": "feat/live-test",
            "base": "main",
            "labels": [label_ids["unrelated-keep"]],
        },
    )

    # Post commit statuses to simulate CI without a runner.
    _api(
        "POST",
        f"/repos/{owner}/{repo_name}/statuses/{head_sha}",
        {"state": "success", "context": "unit", "description": "all unit tests passed"},
    )

    forge_cls = ForgejoForge if LIVE_TYPE == "forgejo" else GiteaForge
    forge = forge_cls(
        base_url=LIVE_URL,
        owner=owner,
        name=repo_name,
        token_env="DEVLOOPS_FORGE_LIVE_TOKEN",
    )
    return {
        "forge": forge,
        "owner": owner,
        "repo": repo_name,
        "login": owner,
        "issue_number": issue1["number"],
        "pr_number": pr1["number"],
        "head_sha": head_sha,
    }


def test_live_detection_and_doctor(seeded_forge: Dict[str, Any]) -> None:
    """Prove Forgejo-before-Gitea detection ordering and doctor check against live server."""
    remote_url = f"{LIVE_URL}/{seeded_forge['owner']}/{seeded_forge['repo']}.git"
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


def test_live_contract_auth_and_repo(seeded_forge: Dict[str, Any]) -> None:
    forge: GiteaForge = seeded_forge["forge"]
    assert forge.whoami() == seeded_forge["login"]
    status = forge.auth_check()
    assert status.ok is True
    assert seeded_forge["login"] in status.output

    repo = forge.get_repo()
    assert repo.owner == seeded_forge["owner"]
    assert repo.name == seeded_forge["repo"]


def test_live_contract_issues(seeded_forge: Dict[str, Any]) -> None:
    forge: GiteaForge = seeded_forge["forge"]
    issues = forge.list_issues(state="open")
    numbers = [i.number for i in issues]
    assert seeded_forge["issue_number"] in numbers

    fetched = forge.get_issue(seeded_forge["issue_number"])
    assert fetched.title == "Seeded feature issue"
    assert "approved" in fetched.labels

    created_url = forge.create_issue("Created via adapter", "Body text", labels=["enhancement"])
    assert created_url


def test_live_contract_pr_comments_and_ci(seeded_forge: Dict[str, Any]) -> None:
    forge: GiteaForge = seeded_forge["forge"]
    pr_num = seeded_forge["pr_number"]

    prs = forge.list_prs(state="open")
    assert any(p.number == pr_num for p in prs)

    pr = forge.get_pr(pr_num)
    assert pr.head_ref == "feat/live-test"
    assert pr.base_ref == "main"
    assert pr.head_sha == seeded_forge["head_sha"]

    forge.post_pr_comment(pr_num, "<!-- worker-loop: note -->\nlive test comment")
    comments = forge.list_pr_comments(pr_num)
    assert any("live test comment" in c.body for c in comments)

    ci = forge.get_ci_status(pr_num, pr.head_sha)
    assert ci.state == CI_SUCCESS
    assert not ci.failing
    assert not ci.pending

    # Post a failing status and verify failure takes precedence.
    _api(
        "POST",
        f"/repos/{seeded_forge['owner']}/{seeded_forge['repo']}/statuses/{pr.head_sha}",
        {"state": "failure", "context": "lint", "description": "lint failed"},
    )
    ci_fail = forge.get_ci_status(pr_num, pr.head_sha)
    assert ci_fail.state == CI_FAILURE
    assert "lint" in ci_fail.failing


def test_live_labels_preserve_unrelated_labels(seeded_forge: Dict[str, Any]) -> None:
    """Adding and removing verdict labels must never drop unrelated labels on the live server."""
    forge: GiteaForge = seeded_forge["forge"]
    pr_num = seeded_forge["pr_number"]

    forge.add_labels(pr_num, ["gatekeeper:changes-requested"])
    pr = forge.get_pr(pr_num)
    assert "unrelated-keep" in pr.labels
    assert "gatekeeper:changes-requested" in pr.labels

    forge.remove_labels(pr_num, ["gatekeeper:changes-requested"])
    forge.add_labels(pr_num, ["gatekeeper:approved"])
    pr_after = forge.get_pr(pr_num)
    assert "unrelated-keep" in pr_after.labels
    assert "gatekeeper:approved" in pr_after.labels
    assert "gatekeeper:changes-requested" not in pr_after.labels


def test_live_gate_snapshot_and_bad_token(seeded_forge: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    forge: GiteaForge = seeded_forge["forge"]
    snap = forge.gate_snapshot()
    assert snap.prs
    assert snap.issues

    monkeypatch.setenv("DEVLOOPS_BAD_LIVE_TOKEN", "invalid-token-value")
    forge_cls = ForgejoForge if LIVE_TYPE == "forgejo" else GiteaForge
    bad_forge = forge_cls(
        base_url=LIVE_URL,
        owner=seeded_forge["owner"],
        name=seeded_forge["repo"],
        token_env="DEVLOOPS_BAD_LIVE_TOKEN",
    )
    bad_status = bad_forge.auth_check()
    assert bad_status.ok is False
    with pytest.raises(ForgeError):
        bad_forge.whoami()
