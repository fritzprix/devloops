#!/usr/bin/env python3
"""GitHubForge conformance to the forge contract + gh-transport specifics (issue #44).

The gh CLI is stubbed at the subprocess boundary (the injected run callable),
so these tests never shell out.
"""
import copy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib.forge import GitHubForge  # noqa: E402
from devloops_lib.forge.github import GRAPHQL_GATE_QUERY, THREADS_QUERY  # noqa: E402
from devloops_lib.tick_gate import compute_fingerprint  # noqa: E402
from tests.forge_contract import GhStub, ForgeContract, SEED, USER  # noqa: E402


class TestGitHubForgeContract(ForgeContract):
    def setup_method(self):
        self.stub = GhStub()
        super().setup_method()

    def make_forge(self):
        return GitHubForge(run=self.stub.run)

    def auth_failure_forge(self):
        return GitHubForge(run=GhStub(auth_ok=False).run)

    def label_edit_commands(self):
        return [a for a in self.stub.args_log if a[:2] == ["issue", "edit"]]


class TestGitHubForgeTransport:
    def setup_method(self):
        self.stub = GhStub()
        self.forge = GitHubForge(run=self.stub.run)

    def test_graphql_and_api_calls_never_pass_repo_flag(self):
        self.forge.unresolved_review_threads(self.forge.get_pr(10))
        self.forge.gate_snapshot()
        api_calls = [a for a in self.stub.args_log if a[0] == "api"]
        assert api_calls
        for cmd in api_calls:
            assert "--repo" not in cmd

    def test_repo_override_plumbs_issue_and_pr_commands(self):
        forge = GitHubForge(run=self.stub.run, repo="other/things")
        forge.list_issues()
        forge.get_pr(10)
        for cmd in self.stub.args_log:
            assert "--repo" in cmd and "other/things" in cmd

    def test_auth_check_never_passes_repo_flag(self):
        forge = GitHubForge(run=self.stub.run, repo="other/things")
        status = forge.auth_check()
        assert status.ok
        assert self.stub.args_log == [["auth", "status"]]

    def test_threads_second_page_uses_end_cursor(self):
        self.forge.unresolved_review_threads(self.forge.get_pr(10))
        assert len(self.stub.graphql_calls) == 2
        assert any(any(a.endswith("cursor=c2") for a in cmd) for cmd in self.stub.graphql_calls)

    def test_gate_snapshot_fingerprint_matches_raw_query_path(self):
        """The forge gate path must be byte-identical to the raw GraphQL path.

        Golden test from the recorded GRAPHQL_GATE_QUERY response: migrating
        tick_gate to the forge must not move its fingerprint.
        """
        raw = copy.deepcopy(SEED["gate"])
        gate_data = {
            "prs": raw["data"]["search"]["nodes"],
            "issues": raw["data"]["repository"]["issues"]["nodes"],
        }
        expected = compute_fingerprint(gate_data, USER)

        snap = GitHubForge(run=GhStub().run).gate_snapshot()
        assert compute_fingerprint({"prs": snap.prs, "issues": snap.issues}, USER) == expected

    def test_query_strings_match_predecessors(self):
        """THREADS_QUERY / GRAPHQL_GATE_QUERY stay byte-identical to the
        call sites the adapter replaces (fingerprints and API results depend
        on it)."""
        from devloops_lib import pr_status, tick_gate

        assert GitHubForge.__module__.endswith("forge.github")
        assert THREADS_QUERY == pr_status.THREADS_QUERY
        assert GRAPHQL_GATE_QUERY == tick_gate.GRAPHQL_GATE_QUERY

    def test_gate_query_selects_issue_title(self):
        # G1.1: issue title must be selected so epic/umbrella filters work.
        assert "issues(first: $issueLimit" in GRAPHQL_GATE_QUERY
        # After the issues(...) args, nodes must select title (not only PR title).
        after_issues = GRAPHQL_GATE_QUERY.split("issues(first: $issueLimit", 1)[1]
        nodes_block = after_issues.split("nodes {", 1)[1]
        assert "title" in nodes_block.split("labels(", 1)[0]

    def test_list_sub_issues_returns_sorted_numbers(self):
        # G1.2: GhStub answers GET .../sub_issues for GitHubForge.
        self.stub.sub_issues[1] = [
            {"number": 5, "title": "child b", "state": "CLOSED"},
            {"number": 3, "title": "child a", "state": "OPEN"},
        ]
        assert self.forge.list_sub_issues(1) == [3, 5]
        assert self.forge.list_sub_issues(2) == []

    def test_merge_pr_passes_repo_default_merge_method(self):
        self.stub.default_merge_method = "SQUASH"
        pr = self.forge.get_pr(10)
        self.forge.merge_pr(pr.number, pr.head_sha)
        merge_cmds = [a for a in self.stub.args_log if a[:2] == ["pr", "merge"]]
        assert len(merge_cmds) == 1
        assert "--squash" in merge_cmds[0]

    def test_merge_pr_raises_when_queued_instead_of_merged(self):
        import pytest
        from devloops_lib.forge import ForgeError

        self.stub.queue_merges = True
        pr = self.forge.get_pr(10)
        with pytest.raises(ForgeError, match="did not merge"):
            self.forge.merge_pr(pr.number, pr.head_sha)

    def test_merge_pr_raises_when_already_merged_at_different_head(self):
        import pytest
        from devloops_lib.forge import ForgeError

        pr_raw = self.stub._find_pr(10)
        pr_raw["state"] = "MERGED"
        pr_raw["headRefOid"] = "deadbeef"
        with pytest.raises(ForgeError, match="did not merge"):
            self.forge.merge_pr(10, "different-sha")
