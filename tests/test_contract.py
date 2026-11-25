"""Tests for devloops_lib.contract: the shared label, overlap-order, and marker
grammar contract (issue #65)."""
from __future__ import annotations

import unittest
from pathlib import Path

import sys
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import contract  # noqa: E402
from devloops_lib.forge.models import Blocker  # noqa: E402


def comment(body: str, at: str = "2026-01-01T00:00:00Z", author: str = "me") -> dict:
    return {"author": {"login": author}, "body": body, "createdAt": at, "url": "http://x/c"}


# Each contract state and the trigger that makes it apply to a PR.
STATE_TRIGGERS = {
    "needs-owner": ({"labels": [contract.NEEDS_OWNER]}, {}),
    "gatekeeper:escalated": ({"labels": [contract.GATEKEEPER_ESCALATED]}, {}),
    "open-blocker": ({"labels": []}, {"blockers": [Blocker(7, "open")]}),
    "merge-conflict": ({"labels": []}, {"mergeable": "CONFLICTING"}),
    "verdict": ({"labels": [contract.GATEKEEPER_APPROVED]}, {}),
}


class OverlapTest(unittest.TestCase):
    def test_no_state(self):
        self.assertIsNone(contract.first_state([]))
        self.assertIsNone(contract.first_state(["bug"], "MERGEABLE"))

    def test_blocked_base_label_is_gone(self):
        # issue #65: base blocking is derived from open blockers, never a label.
        self.assertFalse(hasattr(contract, "BLOCKED_BASE"))
        self.assertNotIn("blocked:base", contract.STATE_ORDER)
        self.assertIsNone(contract.first_state(["blocked:base"]))

    def test_single_states(self):
        for state, (labels, extra) in STATE_TRIGGERS.items():
            self.assertEqual(contract.first_state(**labels, **extra), state, state)

    def test_closed_blockers_do_not_apply(self):
        self.assertIsNone(contract.first_state([], blockers=[Blocker(7, "closed")]))
        self.assertIsNone(contract.first_state([], "MERGEABLE", [Blocker(7, "CLOSED")]))

    def test_every_pair_resolves_to_the_earlier_state(self):
        for i, a in enumerate(contract.STATE_ORDER):
            for b in contract.STATE_ORDER[i + 1:]:
                labels_a, extra_a = STATE_TRIGGERS[a]
                labels_b, extra_b = STATE_TRIGGERS[b]
                labels = labels_a["labels"] + labels_b["labels"]
                extra = {**extra_a, **extra_b}
                self.assertEqual(
                    contract.first_state(labels, extra.get("mergeable"), extra.get("blockers")), a,
                    f"{a} must outrank {b}")


class OpenBlockersTest(unittest.TestCase):
    class Forge:
        def __init__(self, blockers):
            self._blockers = blockers

        def blocked_by(self, number):
            return list(self._blockers)

    def test_sorted_open_numbers_only(self):
        forge = self.Forge([Blocker(12, "open"), Blocker(5, "closed"), Blocker(3, "open", True)])
        self.assertEqual(contract.open_blockers(forge, 20), [3, 12])

    def test_no_blockers(self):
        self.assertEqual(contract.open_blockers(self.Forge([]), 20), [])


class MarkerVocabularyTest(unittest.TestCase):
    def test_gatekeeper_verdicts_parse(self):
        for v in contract.GATEKEEPER_VERDICTS:
            parsed = contract.parse_marker(f"<!-- gatekeeper-loop: round=1 verdict={v} head=sha -->")
            self.assertIsNotNone(parsed, v)
            self.assertEqual(parsed[0], "gatekeeper-loop")
            self.assertEqual(parsed[1]["verdict"], v)

    def test_gatekeeper_needs_owner_kind_parses(self):
        parsed = contract.parse_marker("<!-- gatekeeper-loop: kind=needs-owner reason=x head=sha -->")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[1]["kind"], "needs-owner")

    def test_gatekeeper_needs_human_kind_is_gone(self):
        self.assertIsNone(contract.parse_marker("<!-- gatekeeper-loop: kind=needs-human reason=x head=sha -->"))

    def test_gatekeeper_clean_blocked_by(self):
        parsed = contract.parse_marker("<!-- gatekeeper-loop: verdict=clean head=sha base=main blocked-by=#42 -->")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[1]["blocked-by"], "#42")
        self.assertIsNone(contract.parse_marker("<!-- gatekeeper-loop: verdict=clean head=sha blocked-by=soon -->"))

    def test_worker_kinds_parse(self):
        for k in contract.WORKER_KINDS:
            parsed = contract.parse_marker(f"<!-- worker-loop: {k} round=1 head=sha -->")
            self.assertIsNotNone(parsed, k)
            self.assertEqual(parsed[1]["kind"], k)

    def test_worker_unknown_kind_ignored(self):
        self.assertIsNone(contract.parse_marker("<!-- worker-loop: merge round=1 -->"))
        self.assertIsNone(contract.parse_marker("<!-- worker-loop: response=2 round=1 -->"))

    def test_qa_markers_parse(self):
        parsed = contract.parse_marker("<!-- qa-loop: kind=verdict round=1 verdict=accepted head=sha -->")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[1]["kind"], "verdict")
        parsed = contract.parse_marker("<!-- qa-loop: kind=post-merge pr=7 build=sha check=t -->")
        self.assertIsNotNone(parsed)
        parsed = contract.parse_marker("<!-- qa-loop: kind=needs-owner reason=x head=sha -->")
        self.assertIsNotNone(parsed)

    def test_qa_unknown_verdict_or_kind_ignored(self):
        self.assertIsNone(contract.parse_marker("<!-- qa-loop: kind=verdict verdict=maybe head=sha -->"))
        self.assertIsNone(contract.parse_marker("<!-- qa-loop: kind=approve head=sha -->"))

    def test_anchor_bom_and_quote_rules(self):
        self.assertIsNotNone(contract.parse_marker("<!-- gatekeeper-loop: verdict=approve -->"))
        self.assertIsNotNone(contract.parse_marker("\ufeff\n  <!-- gatekeeper-loop: verdict=approve -->"))
        self.assertIsNone(contract.parse_marker("> <!-- gatekeeper-loop: verdict=approve -->"))
        self.assertIsNone(contract.parse_marker("Previous:\n<!-- gatekeeper-loop: verdict=approve -->"))
        self.assertIsNone(contract.parse_marker("plain comment"))

    def test_marker_comment_never_an_owner_comment_including_unknown_kind(self):
        self.assertTrue(contract.is_marker("<!-- qa-loop: kind=verdict verdict=accepted head=sha -->"))
        self.assertTrue(contract.is_marker("<!-- gatekeeper-loop: kind=needs-human head=sha -->"))
        self.assertFalse(contract.is_marker("just text"))


class ParseMarkersTest(unittest.TestCase):
    def test_author_filter_and_order(self):
        comments = [
            comment("<!-- qa-loop: kind=verdict round=2 verdict=accepted head=sha -->", "2026-01-03T00:00:00Z"),
            comment("<!-- gatekeeper-loop: round=1 verdict=changes-requested head=sha -->", "2026-01-01T00:00:00Z"),
            comment("<!-- gatekeeper-loop: round=2 verdict=clean head=sha -->", "2026-01-02T00:00:00Z", author="x"),
        ]
        markers = contract.parse_markers(comments, "me")
        self.assertEqual([m["createdAt"] for m in markers], ["2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"])
        self.assertEqual([m["loop"] for m in markers], ["gatekeeper-loop", "qa-loop"])
        self.assertEqual(contract.parse_markers(comments, "me", loop="qa-loop")[0]["kind"], "verdict")


class SingleDefinitionTest(unittest.TestCase):
    def test_consumers_use_the_contract_pattern(self):
        from devloops_lib import pr_status
        self.assertIs(pr_status.START_MARKER, contract.MARKER)

    def test_config_and_forge_labels_are_the_contract_labels(self):
        from devloops_lib import config
        from devloops_lib.forge import cli
        self.assertEqual(config.GATEKEEPER_APPROVED_LABEL, contract.GATEKEEPER_APPROVED)
        self.assertEqual(cli.VERDICT_LABELS, contract.GATEKEEPER_VERDICT_LABELS)
        self.assertEqual(config.DEFAULT_CONFIG["labels"],
                         {"producer": contract.PRODUCER, "validated": contract.VALIDATED,
                          "rejected": contract.REJECTED, "approved": contract.APPROVED})

    def test_issue_label_honours_config_override(self):
        cfg = {"labels": {"producer": "bot:found"}}
        self.assertEqual(contract.issue_label(cfg, "producer"), "bot:found")
        self.assertEqual(contract.issue_label({}, "producer"), contract.PRODUCER)


if __name__ == "__main__":
    unittest.main()
