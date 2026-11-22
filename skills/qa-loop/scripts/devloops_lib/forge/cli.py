"""Forge-neutral CLI subcommand group: ``devloops forge ...`` (issue #47).

Wraps the ``Forge`` adapter with JSON output so agent guides and scripts never
invoke ``gh`` directly. Operations unsupported by the active forge exit with
``EXIT_UNSUPPORTED`` (5) and emit a clear JSON payload explaining why.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from devloops_lib.forge.base import Forge, ForgeError
from devloops_lib.forge.github import GitHubForge
from devloops_lib.forge.models import Issue, PullRequest

EXIT_OK = 0
EXIT_FORGE_ERROR = 3
EXIT_UNSUPPORTED = 5

VERDICT_LABELS = (
    "gatekeeper:approved",
    "gatekeeper:changes-requested",
    "gatekeeper:escalated",
)


def _issue_dict(issue: Issue) -> Dict[str, Any]:
    return {
        "number": issue.number,
        "title": issue.title,
        "url": issue.url,
        "state": issue.state,
        "body": issue.body,
        "labels": list(issue.labels),
        "assignees": list(issue.assignees),
        "createdAt": issue.created_at,
        "updatedAt": issue.updated_at,
    }


def _pr_dict(pr: PullRequest) -> Dict[str, Any]:
    return {
        "number": pr.number,
        "title": pr.title,
        "url": pr.url,
        "headRefName": pr.head_ref,
        "headRefOid": pr.head_sha,
        "baseRefName": pr.base_ref,
        "isDraft": pr.is_draft,
        "author": pr.author,
        "mergeable": pr.mergeable,
        "body": pr.body,
        "updatedAt": pr.updated_at,
        "labels": list(pr.labels),
        "mergeStateStatus": pr.merge_state,
        "reviewDecision": pr.review_decision,
    }


def _read_body(body: Optional[str], body_file: Optional[Path]) -> str:
    if body_file is not None:
        return body_file.read_text(encoding="utf-8")
    return body or ""


def _emit(payload: Any) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def _unsupported(operation: str, reason: str, target_url: str = "") -> int:
    payload: Dict[str, Any] = {
        "status": "unsupported",
        "supported": False,
        "operation": operation,
        "reason": reason,
    }
    if target_url:
        payload["target_url"] = target_url
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.stderr.write(f"devloops forge: unsupported operation '{operation}': {reason}\n")
    return EXIT_UNSUPPORTED


def set_verdict_labels(forge: Forge, number: int, verdict: str) -> Dict[str, Any]:
    """Ensure exactly one gatekeeper verdict label (or none on clean), leaving other labels untouched."""
    norm = verdict.strip().lower()
    if norm == "clean":
        forge.remove_labels(number, ["gatekeeper:changes-requested"])
        return {"number": number, "verdict": "clean", "added": [], "removed": ["gatekeeper:changes-requested"]}

    target = f"gatekeeper:{norm}"
    if target not in VERDICT_LABELS:
        raise ForgeError(f"Unknown verdict '{verdict}'; expected approved, changes-requested, escalated, or clean")

    to_remove = [lbl for lbl in VERDICT_LABELS if lbl != target]
    forge.add_labels(number, [target])
    forge.remove_labels(number, to_remove)
    return {"number": number, "verdict": norm, "added": [target], "removed": to_remove}


def _default_forge() -> Forge:
    return GitHubForge()


def build_forge_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    sub = parser.add_subparsers(dest="forge_group")

    sub.add_parser("whoami", help="Print the authenticated forge user login")

    # issue subcommands
    issue_p = sub.add_parser("issue", help="Issue operations")
    issue_sub = issue_p.add_subparsers(dest="forge_action")

    p_iv = issue_sub.add_parser("view", help="View an issue")
    p_iv.add_argument("number", type=int)

    p_il = issue_sub.add_parser("list", help="List issues")
    p_il.add_argument("--state", default="open", choices=["open", "closed", "all"])
    p_il.add_argument("--label", action="append", default=[])
    p_il.add_argument("--limit", type=int, default=100)

    p_ic = issue_sub.add_parser("create", help="Create an issue")
    p_ic.add_argument("--title", required=True)
    p_ic.add_argument("--body", default=None)
    p_ic.add_argument("--body-file", type=Path, default=None)
    p_ic.add_argument("--label", action="append", default=[])

    p_icom = issue_sub.add_parser("comment", help="Comment on an issue")
    p_icom.add_argument("number", type=int)
    p_icom.add_argument("--body", default=None)
    p_icom.add_argument("--body-file", type=Path, default=None)

    p_ia = issue_sub.add_parser("assign-me", help="Assign an issue to the current user")
    p_ia.add_argument("number", type=int)

    p_ilbl = issue_sub.add_parser("label", help="Add or remove labels on an issue")
    p_ilbl_sub = p_ilbl.add_subparsers(dest="label_action")
    p_ilbl_add = p_ilbl_sub.add_parser("add")
    p_ilbl_add.add_argument("number", type=int)
    p_ilbl_add.add_argument("names", nargs="+")
    p_ilbl_rm = p_ilbl_sub.add_parser("remove")
    p_ilbl_rm.add_argument("number", type=int)
    p_ilbl_rm.add_argument("names", nargs="+")

    # pr subcommands
    pr_p = sub.add_parser("pr", help="Pull request operations")
    pr_sub = pr_p.add_subparsers(dest="forge_action")

    p_pv = pr_sub.add_parser("view", help="View a pull request")
    p_pv.add_argument("number", type=int)

    p_pl = pr_sub.add_parser("list", help="List pull requests")
    p_pl.add_argument("--state", default="open", choices=["open", "closed", "all"])
    p_pl.add_argument("--author", default=None)
    p_pl.add_argument("--limit", type=int, default=100)

    p_pd = pr_sub.add_parser("diff", help="Print unified diff of a pull request")
    p_pd.add_argument("number", type=int)
    p_pd.add_argument("--raw", action="store_true", help="Print raw diff text instead of JSON")

    p_pc = pr_sub.add_parser("create", help="Create a pull request")
    p_pc.add_argument("--base", required=True)
    p_pc.add_argument("--title", required=True)
    p_pc.add_argument("--head", default=None)
    p_pc.add_argument("--body", default=None)
    p_pc.add_argument("--body-file", type=Path, default=None)

    p_pcom = pr_sub.add_parser("comment", help="Comment on a pull request")
    p_pcom.add_argument("number", type=int)
    p_pcom.add_argument("--body", default=None)
    p_pcom.add_argument("--body-file", type=Path, default=None)

    p_plbl = pr_sub.add_parser("label", help="Add, remove, or swap verdict labels on a PR")
    p_plbl_sub = p_plbl.add_subparsers(dest="label_action")
    p_plbl_add = p_plbl_sub.add_parser("add")
    p_plbl_add.add_argument("number", type=int)
    p_plbl_add.add_argument("names", nargs="+")
    p_plbl_rm = p_plbl_sub.add_parser("remove")
    p_plbl_rm.add_argument("number", type=int)
    p_plbl_rm.add_argument("names", nargs="+")
    p_plbl_sv = p_plbl_sub.add_parser("set-verdict")
    p_plbl_sv.add_argument("number", type=int)
    p_plbl_sv.add_argument("verdict", choices=["approved", "changes-requested", "escalated", "clean"])

    p_pub = pr_sub.add_parser("update-branch", help="Update a PR branch with its base")
    p_pub.add_argument("number", type=int)

    p_pchk = pr_sub.add_parser("checks", help="Show normalized CI check status for a PR")
    p_pchk.add_argument("number", type=int)

    # ci subcommands
    ci_p = sub.add_parser("ci", help="CI run log / rerun operations")
    ci_sub = ci_p.add_subparsers(dest="forge_action")

    p_cil = ci_sub.add_parser("logs", help="Fetch CI failure logs")
    p_cil.add_argument("target", help="Run ID")
    p_cil.add_argument("--failed", action="store_true")

    p_cir = ci_sub.add_parser("rerun", help="Rerun failed CI jobs")
    p_cir.add_argument("target", help="Run ID")
    p_cir.add_argument("--failed", action="store_true")

    # label subcommands
    lbl_p = sub.add_parser("label", help="Repository label management")
    lbl_sub = lbl_p.add_subparsers(dest="forge_action")

    p_le = lbl_sub.add_parser("ensure", help="Create a repository label if missing")
    p_le.add_argument("name")
    p_le.add_argument("--color", default="ededed")
    p_le.add_argument("--description", default="")

    return parser


def run_forge_args(args: argparse.Namespace, forge: Optional[Forge] = None) -> int:
    active = forge if forge is not None else _default_forge()
    group = getattr(args, "forge_group", None)
    action = getattr(args, "forge_action", None)

    try:
        if group == "whoami":
            return _emit({"login": active.whoami()})

        if group == "issue":
            if action == "view":
                return _emit(_issue_dict(active.get_issue(args.number)))
            if action == "list":
                items = active.list_issues(state=args.state, labels=args.label or None, limit=args.limit)
                return _emit([_issue_dict(i) for i in items])
            if action == "create":
                url = active.create_issue(args.title, _read_body(args.body, args.body_file), args.label or ())
                return _emit({"url": url})
            if action == "comment":
                if hasattr(active, "post_issue_comment"):
                    active.post_issue_comment(args.number, _read_body(args.body, args.body_file))
                else:
                    active.post_pr_comment(args.number, _read_body(args.body, args.body_file))
                return _emit({"number": args.number, "ok": True})
            if action == "assign-me":
                me = active.whoami()
                active.add_assignee(args.number, me)
                return _emit({"number": args.number, "assignee": me})
            if action == "label":
                if args.label_action == "add":
                    active.add_labels(args.number, args.names)
                    return _emit({"number": args.number, "added": list(args.names)})
                if args.label_action == "remove":
                    active.remove_labels(args.number, args.names)
                    return _emit({"number": args.number, "removed": list(args.names)})

        if group == "pr":
            if action == "view":
                return _emit(_pr_dict(active.get_pr(args.number)))
            if action == "list":
                author = active.whoami() if args.author == "@me" else args.author
                items = active.list_prs(state=args.state, author=author, limit=args.limit)
                return _emit([_pr_dict(p) for p in items])
            if action == "diff":
                fn = getattr(active, "pr_diff", None)
                if fn is None:
                    return _unsupported("pr diff", "adapter does not implement pr_diff")
                diff_text = fn(args.number)
                if getattr(args, "raw", False):
                    sys.stdout.write(diff_text)
                    if diff_text and not diff_text.endswith("\n"):
                        sys.stdout.write("\n")
                    return EXIT_OK
                return _emit({"number": args.number, "diff": diff_text})
            if action == "create":
                fn = getattr(active, "create_pr", None)
                if fn is None:
                    return _unsupported("pr create", "adapter does not implement create_pr")
                url = fn(
                    base=args.base,
                    title=args.title,
                    body=_read_body(args.body, args.body_file),
                    head=args.head,
                )
                return _emit({"url": url})
            if action == "comment":
                active.post_pr_comment(args.number, _read_body(args.body, args.body_file))
                return _emit({"number": args.number, "ok": True})
            if action == "label":
                if args.label_action == "add":
                    active.add_labels(args.number, args.names)
                    return _emit({"number": args.number, "added": list(args.names)})
                if args.label_action == "remove":
                    active.remove_labels(args.number, args.names)
                    return _emit({"number": args.number, "removed": list(args.names)})
                if args.label_action == "set-verdict":
                    return _emit(set_verdict_labels(active, args.number, args.verdict))
            if action == "update-branch":
                fn = getattr(active, "update_branch", None)
                if fn is None:
                    return _unsupported("pr update-branch", "adapter does not implement update_branch")
                fn(args.number)
                return _emit({"number": args.number, "ok": True})
            if action == "checks":
                pr = active.get_pr(args.number)
                ci = active.ci_status(pr)
                return _emit({"number": args.number, **asdict(ci)})

        if group == "ci":
            if action == "logs":
                fn = getattr(active, "ci_logs_failed", None)
                if fn is None:
                    return _unsupported("ci logs", "adapter does not support fetching CI logs")
                res = fn(args.target)
                if isinstance(res, dict) and res.get("supported") is False:
                    return _unsupported("ci logs", res.get("reason", "unsupported"), res.get("target_url", ""))
                return _emit({"target": args.target, "logs": res})
            if action == "rerun":
                fn = getattr(active, "ci_rerun_failed", None)
                if fn is None:
                    return _unsupported("ci rerun", "adapter does not support rerunning CI jobs")
                res = fn(args.target)
                if isinstance(res, dict) and res.get("supported") is False:
                    return _unsupported("ci rerun", res.get("reason", "unsupported"), res.get("target_url", ""))
                return _emit({"target": args.target, "ok": True})

        if group == "label" and action == "ensure":
            fn = getattr(active, "ensure_label", None)
            if fn is None:
                return _unsupported("label ensure", "adapter does not implement ensure_label")
            fn(args.name, color=args.color, description=args.description)
            return _emit({"name": args.name, "ok": True})

        sys.stderr.write("devloops forge: missing or incomplete subcommand\n")
        return 2
    except ForgeError as exc:
        sys.stderr.write(f"devloops forge error: {exc}\n")
        return EXIT_FORGE_ERROR


def forge_main(argv: Sequence[str], forge: Optional[Forge] = None) -> int:
    parser = build_forge_parser(argparse.ArgumentParser(prog="devloops forge"))
    args = parser.parse_args(list(argv))
    if not getattr(args, "forge_group", None):
        parser.print_help()
        return 0
    return run_forge_args(args, forge=forge)
