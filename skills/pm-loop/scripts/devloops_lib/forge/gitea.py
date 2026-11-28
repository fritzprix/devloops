"""Forgejo and Gitea REST API forge adapter (issue #46).

Implements the ``Forge`` protocol over ``/api/v1`` using Python stdlib ``urllib``
only (Python 3.8 floor).

Minimum supported server versions (enforced at preflight; older versions fail closed):
  - Gitea:   >= 1.21.0
  - Forgejo: >= 7.0.0

Authentication:
  - Reads the API token strictly from the environment variable named by
    ``forge.token_env`` (never stored in config or state).
  - Sends ``Authorization: token <value>``.
  - Never prints or embeds the token value in logs, ``repr``, or error messages.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from devloops_lib.forge.base import ForgeError
from devloops_lib.forge.detect import ForgeDetectionError, register_forge_adapter
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

MIN_GITEA_VERSION: Tuple[int, int, int] = (1, 21, 0)
MIN_FORGEJO_VERSION: Tuple[int, int, int] = (7, 0, 0)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 0.2
DEFAULT_PAGE_SIZE = 50

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_WIP_TITLE_RE = re.compile(r"^\s*(?:wip:|\[wip\])", re.I)

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def parse_semver(version: str) -> Optional[Tuple[int, int, int]]:
    """Parse leading major.minor[.patch] from a server version string."""
    if not version:
        return None
    match = _SEMVER_RE.search(version)
    if not match:
        return None
    major, minor = int(match.group(1)), int(match.group(2))
    patch = int(match.group(3)) if match.group(3) is not None else 0
    return (major, minor, patch)


def min_version_for(forge_type: str) -> Tuple[int, int, int]:
    return MIN_FORGEJO_VERSION if forge_type == "forgejo" else MIN_GITEA_VERSION


def check_min_version(forge_type: str, version: str) -> None:
    """Fail closed if a server version is missing, unparseable, or below the supported floor."""
    minimum = min_version_for(forge_type)
    min_str = ".".join(map(str, minimum))
    parsed = parse_semver(version)
    if parsed is None:
        raise ForgeDetectionError(
            f"{forge_type.capitalize()} server version '{version or '<empty>'}' could not be parsed; "
            f"minimum supported version is {min_str}."
        )
    if parsed < minimum:
        raise ForgeDetectionError(
            f"{forge_type.capitalize()} server version '{version}' is older than the minimum "
            f"supported version {min_str}. Upgrade the server before running devloops."
        )


def _scrub(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "<redacted>")
    return text


class GiteaForge:
    """Forge adapter for Gitea (and base class for Forgejo) over ``/api/v1``."""

    forge_type: str = "gitea"

    def __init__(
        self,
        *,
        base_url: str,
        owner: str,
        name: str,
        token_env: str = "",
        version: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        page_size: int = DEFAULT_PAGE_SIZE,
        opener: Optional[Callable[[urllib.request.Request, float], Tuple[int, Dict[str, str], str]]] = None,
    ) -> None:
        self._version_checked = False
        if version:
            check_min_version(self.forge_type, version)
            self._version_checked = True
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v1" if self.base_url else ""
        self.owner = owner
        self.name = name
        self.token_env = token_env.strip()
        self.version = version
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.page_size = max(1, page_size)
        self._opener = opener
        self._label_cache: Optional[Dict[str, int]] = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self.base_url!r}, "
            f"repo={self.owner}/{self.name!r}, token_env={self.token_env!r})"
        )

    def _read_token(self) -> str:
        if not self.token_env:
            return ""
        return os.environ.get(self.token_env, "").strip()

    def _ensure_min_version(self) -> None:
        if self._version_checked:
            return
        try:
            _, data = self._http_call("GET", "/version", _skip_version_check=True)
        except ForgeError as exc:
            raise ForgeDetectionError(
                f"Could not verify {self.forge_type} server version via /api/v1/version: {exc}"
            ) from exc
        reported = str((data or {}).get("version") or "").strip() if isinstance(data, dict) else ""
        check_min_version(self.forge_type, reported)
        self.version = reported
        self._version_checked = True

    def _http_call(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Any] = None,
        allow_404: bool = False,
        _skip_version_check: bool = False,
    ) -> Tuple[int, Any]:
        if not _skip_version_check and not self._version_checked:
            self._ensure_min_version()
        token = self._read_token()
        url = f"{self.api_url}{path}"
        if params:
            query = urllib.parse.urlencode(
                [(k, v) for k, v in params.items() if v is not None and v != ""]
            )
            if query:
                url = f"{url}?{query}"

        headers = {
            "Accept": "application/json",
            "User-Agent": "devloops-gitea-adapter",
        }
        if token:
            headers["Authorization"] = f"token {token}"

        data: Optional[bytes] = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        idempotent = method.upper() in ("GET", "HEAD", "DELETE", "PUT")
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                if self._opener is not None:
                    status, _resp_headers, raw = self._opener(req, self.timeout)
                else:
                    try:
                        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                            status = int(resp.status)
                            raw = resp.read().decode("utf-8", errors="replace")
                    except urllib.error.HTTPError as exc:
                        status = int(exc.code)
                        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            except Exception as exc:
                last_err = exc
                if idempotent and attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (2 ** attempt))
                    continue
                raise ForgeError(_scrub(f"{self.forge_type} {method} {path} failed: {exc}", token)) from exc

            if status in _RETRYABLE_STATUSES and idempotent and attempt < self.max_retries:
                time.sleep(self.retry_backoff * (2 ** attempt))
                continue

            if allow_404 and status == 404:
                return status, None

            if status >= 400:
                detail = _scrub(raw.strip()[:240], token)
                raise ForgeError(f"{self.forge_type} {method} {path} HTTP {status}: {detail}")

            if not raw.strip():
                return status, None
            try:
                return status, json.loads(raw)
            except ValueError as exc:
                raise ForgeError(
                    _scrub(f"{self.forge_type} {method} {path} invalid JSON: {exc}", token)
                ) from exc

        raise ForgeError(_scrub(f"{self.forge_type} {method} {path} failed: {last_err}", token))

    def _paginate(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        collected: List[Dict[str, Any]] = []
        page = 1
        prev_ids: Optional[Tuple[Any, ...]] = None
        while len(collected) < limit:
            per_page = min(self.page_size, limit - len(collected))
            req_params = {**(params or {}), "page": page, "limit": per_page}
            _, items = self._http_call("GET", path, params=req_params)
            if not isinstance(items, list) or not items:
                break
            cur_ids = tuple(item.get("id") if isinstance(item, dict) else None for item in items)
            if prev_ids is not None and any(cur_ids) and cur_ids == prev_ids:
                break
            collected.extend(items)
            if len(items) < per_page:
                break
            prev_ids = cur_ids
            page += 1
        return collected[:limit]

    @property
    def _repo_prefix(self) -> str:
        return f"/repos/{self.owner}/{self.name}"

    # -- issue dependencies (issue #65) -----------------------------------

    def blocked_by(self, number: int) -> List[Blocker]:
        path = f"{self._repo_prefix}/issues/{number}/dependencies"
        collected: List[Dict[str, Any]] = []
        page = 1
        prev_ids: Optional[Tuple[Any, ...]] = None
        while True:
            status, data = self._http_call(
                "GET",
                path,
                params={"page": page, "limit": self.page_size},
                allow_404=True,
            )
            if status == 404:
                raise ForgeError(
                    f"{self.forge_type} returned 404 for issue dependencies of #{number}. "
                    "Enable issue dependencies for this repository (repository settings -> "
                    "issues -> issue dependencies) and retry."
                )
            if isinstance(data, dict):
                items = data.get("blocked_by") or []
            elif isinstance(data, list):
                items = data
            else:
                items = []
            if not isinstance(items, list) or not items:
                break
            cur_ids = tuple(
                item.get("id") if isinstance(item, dict) else None for item in items
            )
            if prev_ids is not None and any(cur_ids) and cur_ids == prev_ids:
                break
            collected.extend(item for item in items if isinstance(item, dict))
            if len(items) < self.page_size:
                break
            prev_ids = cur_ids
            page += 1
        blockers: List[Blocker] = []
        for item in collected:
            if item.get("number") is None:
                continue
            blockers.append(Blocker(
                number=int(item["number"]),
                state=str(item.get("state") or "open").lower(),
                is_pr=item.get("pull_request") is not None,
            ))
        return blockers

    def add_blocker(self, number: int, blocker_number: int) -> None:
        if any(b.number == blocker_number for b in self.blocked_by(number)):
            return
        self._http_call(
            "POST",
            f"{self._repo_prefix}/issues/{number}/dependencies",
            body={"index": int(blocker_number), "owner": self.owner, "repo": self.name},
        )

    def list_sub_issues(self, number: int) -> List[int]:
        """Gitea/Forgejo have no GitHub-style sub-issues API; always empty (issue #74)."""
        return []

    # -- identity / repository -------------------------------------------

    def whoami(self) -> str:
        _, data = self._http_call("GET", "/user")
        login = (data or {}).get("login") or (data or {}).get("username") or ""
        if not login:
            raise ForgeError(f"{self.forge_type} /user response missing login")
        return str(login)

    def repo_ref(self) -> RepoRef:
        host = (urllib.parse.urlparse(self.base_url).hostname or "").lower() or None
        return RepoRef(host=host, owner=self.owner, name=self.name)

    def auth_check(self) -> AuthStatus:
        if self.token_env and not self._read_token():
            return AuthStatus(ok=False, output=f"token env var {self.token_env} is not set")
        try:
            login = self.whoami()
        except ForgeError as exc:
            return AuthStatus(ok=False, output=str(exc))
        # Probe issue dependencies so a disabled repo feature surfaces at auth
        # time (issue #65 / #91 G1.3), not only on the first blocked_by call.
        try:
            status, _data = self._http_call(
                "GET",
                f"{self._repo_prefix}/issues/1/dependencies",
                params={"page": 1, "limit": 1},
                allow_404=True,
            )
        except ForgeError as exc:
            return AuthStatus(
                ok=True,
                output=f"authenticated as {login}; issue-dependencies probe failed: {exc}",
            )
        if status == 404:
            return AuthStatus(
                ok=True,
                output=(
                    f"authenticated as {login}; issue dependencies appear disabled "
                    f"(GET {self._repo_prefix}/issues/1/dependencies returned 404 — "
                    "enable them under repository settings → issues → issue dependencies)"
                ),
            )
        return AuthStatus(ok=True, output=f"authenticated as {login}")

    # -- labels -----------------------------------------------------------

    def _label_map(self, *, refresh: bool = False) -> Dict[str, int]:
        if self._label_cache is None or refresh:
            raw_labels = self._paginate(f"{self._repo_prefix}/labels", limit=500)
            self._label_cache = {
                str(lbl["name"]): int(lbl["id"])
                for lbl in raw_labels
                if isinstance(lbl, dict) and "name" in lbl and "id" in lbl
            }
        return self._label_cache

    def _resolve_label_ids(self, names: Sequence[str], *, create_missing: bool = False) -> List[int]:
        mapping = self._label_map()
        ids: List[int] = []
        for name in names:
            lbl_id = mapping.get(name)
            if lbl_id is None:
                mapping = self._label_map(refresh=True)
                lbl_id = mapping.get(name)
            if lbl_id is None and create_missing:
                _, created = self._http_call(
                    "POST",
                    f"{self._repo_prefix}/labels",
                    body={"name": name, "color": "#ededed"},
                )
                if isinstance(created, dict) and "id" in created:
                    lbl_id = int(created["id"])
                    mapping[name] = lbl_id
            if lbl_id is not None:
                ids.append(lbl_id)
        return ids

    # -- issues -----------------------------------------------------------

    @staticmethod
    def _parse_issue(raw: Dict[str, Any]) -> Issue:
        labels = tuple(
            str(l["name"]) for l in (raw.get("labels") or []) if isinstance(l, dict) and l.get("name")
        )
        raw_assignees = raw.get("assignees") or ([raw["assignee"]] if raw.get("assignee") else [])
        assignees = tuple(
            str(a["login"]) for a in raw_assignees if isinstance(a, dict) and a.get("login")
        )
        return Issue(
            number=int(raw["number"]),
            title=str(raw.get("title") or ""),
            url=str(raw.get("html_url") or raw.get("url") or ""),
            state=str(raw.get("state") or "open").lower(),
            body=str(raw.get("body") or ""),
            labels=labels,
            assignees=assignees,
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
        )

    def list_issues(
        self,
        state: str = "open",
        labels: Optional[Sequence[str]] = None,
        limit: int = 100,
    ) -> List[Issue]:
        params: Dict[str, Any] = {"state": state.lower(), "type": "issues"}
        if labels:
            params["labels"] = ",".join(labels)
        items = self._paginate(f"{self._repo_prefix}/issues", params=params, limit=limit)
        out = [self._parse_issue(i) for i in items if not i.get("pull_request")]
        if labels:
            wanted = set(labels)
            out = [i for i in out if wanted <= set(i.labels)]
        return out

    def get_issue(self, number: int) -> Issue:
        _, raw = self._http_call("GET", f"{self._repo_prefix}/issues/{number}")
        return self._parse_issue(raw)

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> str:
        payload: Dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = self._resolve_label_ids(labels, create_missing=True)
        _, created = self._http_call("POST", f"{self._repo_prefix}/issues", body=payload)
        return str((created or {}).get("html_url") or (created or {}).get("url") or "")

    def update_issue_body(self, number: int, body: str) -> None:
        self._http_call("PATCH", f"{self._repo_prefix}/issues/{number}", body={"body": body})

    def add_labels(self, number: int, names: Sequence[str]) -> None:
        if not names:
            return
        ids = self._resolve_label_ids(names, create_missing=True)
        if not ids:
            return
        # Additive POST only — never PUT (which replaces the full label set).
        self._http_call("POST", f"{self._repo_prefix}/issues/{number}/labels", body={"labels": ids})

    def ensure_label(self, name: str, *, color: str = "ededed", description: str = "") -> None:
        """Create label if missing; no-op if it already exists (issue #101 R4)."""
        existing = self._resolve_label_ids([name], create_missing=False)
        if existing:
            return
        self._http_call(
            "POST",
            f"{self._repo_prefix}/labels",
            body={
                "name": name,
                "color": "#" + color.lstrip("#"),
                "description": description,
            },
        )

    def remove_labels(self, number: int, names: Sequence[str]) -> None:
        if not names:
            return
        ids = self._resolve_label_ids(names, create_missing=False)
        for lbl_id in ids:
            self._http_call("DELETE", f"{self._repo_prefix}/issues/{number}/labels/{lbl_id}", allow_404=True)

    def add_assignee(self, number: int, login: str) -> None:
        # Gitea PATCH /issues/{n} replaces assignees: read -> union -> write -> re-read.
        issue = self.get_issue(number)
        merged = sorted(set(issue.assignees) | {login})
        self._http_call("PATCH", f"{self._repo_prefix}/issues/{number}", body={"assignees": merged})
        after = self.get_issue(number)
        if login not in after.assignees:
            raise ForgeError(
                f"{self.forge_type} add_assignee race lost on issue #{number}: '{login}' not in {after.assignees}"
            )

    # -- pull requests ----------------------------------------------------

    def merge_pr(self, number: int, expected_head_sha: str) -> None:
        """Merge PR #number only if its head commit matches expected_head_sha (#109)."""
        if not expected_head_sha or not expected_head_sha.strip():
            raise ForgeError("merge_pr requires a non-empty expected_head_sha")
        pr = self.get_pr(number)
        if pr.head_sha != expected_head_sha:
            raise ForgeError(
                f"{self.forge_type} merge_pr head mismatch on PR #{number}: "
                f"expected {expected_head_sha}, got {pr.head_sha}"
            )
        _, repo_data = self._http_call("GET", self._repo_prefix)
        merge_style = str((repo_data or {}).get("default_merge_style") or "merge")
        self._http_call(
            "POST",
            f"{self._repo_prefix}/pulls/{number}/merge",
            body={"Do": merge_style, "head_commit_id": expected_head_sha},
        )

    def _review_decision(self, number: int) -> str:
        _, reviews = self._http_call("GET", f"{self._repo_prefix}/pulls/{number}/reviews", allow_404=True)
        if not isinstance(reviews, list) or not reviews:
            return ""
        latest_by_user: Dict[str, str] = {}
        for rev in reviews:
            state = str(rev.get("state") or "").upper()
            if state not in ("APPROVED", "REQUEST_CHANGES", "CHANGES_REQUESTED"):
                continue
            user = str((rev.get("user") or {}).get("login") or "")
            if user:
                latest_by_user[user] = state
        states = set(latest_by_user.values())
        if "REQUEST_CHANGES" in states or "CHANGES_REQUESTED" in states:
            return "CHANGES_REQUESTED"
        if "APPROVED" in states:
            return "APPROVED"
        return ""

    @staticmethod
    def _is_draft(raw: Dict[str, Any]) -> bool:
        if isinstance(raw.get("draft"), bool):
            return raw["draft"]
        return bool(_WIP_TITLE_RE.match(str(raw.get("title") or "")))

    def _parse_pr(self, raw: Dict[str, Any], *, review_decision: Optional[str] = None) -> PullRequest:
        head = raw.get("head") or {}
        base = raw.get("base") or {}
        mergeable_raw = raw.get("mergeable")
        mergeable = mergeable_raw if isinstance(mergeable_raw, bool) else None
        labels = tuple(
            str(l["name"]) for l in (raw.get("labels") or []) if isinstance(l, dict) and l.get("name")
        )
        return PullRequest(
            number=int(raw["number"]),
            title=str(raw.get("title") or ""),
            url=str(raw.get("html_url") or raw.get("url") or ""),
            head_ref=str(head.get("ref") or ""),
            head_sha=str(head.get("sha") or ""),
            base_ref=str(base.get("ref") or ""),
            is_draft=self._is_draft(raw),
            author=str((raw.get("user") or {}).get("login") or ""),
            mergeable=mergeable,
            body=str(raw.get("body") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            labels=labels,
            merge_state=None,
            review_decision=review_decision if review_decision is not None else "",
            raw=raw,
        )

    def list_prs(
        self,
        state: str = "open",
        author: Optional[str] = None,
        limit: int = 100,
    ) -> List[PullRequest]:
        items = self._paginate(f"{self._repo_prefix}/pulls", params={"state": state.lower()}, limit=limit)
        prs: List[PullRequest] = []
        for raw in items:
            pr = self._parse_pr(raw)
            if author and pr.author != author:
                continue
            prs.append(pr)
        return prs

    def get_pr(self, number: int) -> PullRequest:
        _, raw = self._http_call("GET", f"{self._repo_prefix}/pulls/{number}")
        decision = self._review_decision(number)
        return self._parse_pr(raw, review_decision=decision)

    def ci_status(self, pr: PullRequest, *, required_checks: Sequence[str] = ()) -> CiSummary:
        if not pr.head_sha:
            if required_checks:
                return CiSummary(state=CI_PENDING, failing=[], pending=list(required_checks))
            return CiSummary(state=CI_NONE)
        _, data = self._http_call(
            "GET", f"{self._repo_prefix}/commits/{pr.head_sha}/status", allow_404=True
        )
        statuses = (data or {}).get("statuses") or []
        if not statuses:
            if required_checks:
                return CiSummary(state=CI_PENDING, failing=[], pending=list(required_checks))
            return CiSummary(state=CI_NONE)

        req_set = set(required_checks)
        latest_req: Dict[str, tuple[str, int, Dict[str, Any], str]] = {}
        if req_set:
            for idx, st in enumerate(statuses):
                ctx = str(st.get("context") or st.get("description") or "status")
                if ctx not in req_set:
                    continue
                val = str(st.get("status") or st.get("state") or "").lower()
                ts = str(st.get("created_at") or st.get("updated_at") or "")
                if ts.startswith("0001-01-01"):
                    ts = ""
                key = (ts, idx)
                prev = latest_req.get(ctx)
                if prev is None or key >= (prev[0], prev[1]):
                    latest_req[ctx] = (ts, idx, st, val)

        effective: List[tuple[str, str]] = []
        seen_req: set[str] = set()
        for st in statuses:
            ctx = str(st.get("context") or st.get("description") or "status")
            if ctx in req_set:
                if ctx not in seen_req:
                    seen_req.add(ctx)
                    effective.append((ctx, latest_req[ctx][3]))
            else:
                val = str(st.get("status") or st.get("state") or "").lower()
                effective.append((ctx, val))

        failing: List[str] = []
        pending: List[str] = []
        for ctx, val in effective:
            if val in ("failure", "error"):
                failing.append(ctx)
            elif val == "pending":
                pending.append(ctx)
            # "success" and "warning" count as non-failing/non-pending.

        missing = [
            name
            for name in required_checks
            if (name not in latest_req or latest_req[name][3] != "success")
            and name not in failing
            and name not in pending
        ]
        if missing:
            pending.extend(missing)

        if failing:
            state = CI_FAILURE
        elif pending:
            state = CI_PENDING
        else:
            state = CI_SUCCESS
        return CiSummary(state=state, failing=failing, pending=pending)

    def unresolved_review_threads(self, pr: PullRequest) -> List[ReviewThread]:
        _, reviews = self._http_call("GET", f"{self._repo_prefix}/pulls/{pr.number}/reviews", allow_404=True)
        if not isinstance(reviews, list):
            return []
        threads: List[ReviewThread] = []
        seen_keys: set[Tuple[str, Optional[int], str]] = set()
        for rev in reviews:
            rev_id = rev.get("id")
            if rev_id is None:
                continue
            _, comments = self._http_call(
                "GET", f"{self._repo_prefix}/pulls/{pr.number}/reviews/{rev_id}/comments", allow_404=True
            )
            if not isinstance(comments, list):
                continue
            for c in comments:
                if c.get("resolver") or c.get("is_resolved") or c.get("resolved"):
                    continue
                path = str(c.get("path") or "")
                line_raw = c.get("line") if c.get("line") is not None else c.get("new_position")
                line = int(line_raw) if isinstance(line_raw, int) and line_raw > 0 else None
                tid = str(c.get("thread_id") or c.get("id") or f"{path}:{line}")
                key = (path, line, tid)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                outdated = bool(c.get("outdated") or (line is None and c.get("original_position")))
                threads.append(
                    ReviewThread(
                        thread_id=tid,
                        path=path,
                        line=line,
                        author=(c.get("user") or {}).get("login"),
                        body=str(c.get("body") or ""),
                        url=c.get("html_url") or c.get("url"),
                        outdated=outdated,
                    )
                )
        return threads

    def list_comments(self, number: int) -> List[PrComment]:
        """Comments of an issue OR a pull request (issue #67).

        Gitea and Forgejo answer /issues/{n}/comments for pull requests too.
        """
        items = self._paginate(f"{self._repo_prefix}/issues/{number}/comments", limit=200)
        return [
            PrComment(
                author=str((c.get("user") or {}).get("login") or ""),
                created_at=str(c.get("created_at") or ""),
                body=str(c.get("body") or ""),
                url=c.get("html_url") or c.get("url"),
                association=str(c.get("author_association") or ""),
            )
            for c in items
        ]

    def list_pr_comments(self, number: int) -> List[PrComment]:
        return self.list_comments(number)

    def post_pr_comment(self, number: int, body: str) -> None:
        self._http_call("POST", f"{self._repo_prefix}/issues/{number}/comments", body={"body": body})

    def post_issue_comment(self, number: int, body: str) -> None:
        # Gitea/Forgejo answer the same endpoint for issues (issue #67).
        self._http_call("POST", f"{self._repo_prefix}/issues/{number}/comments", body={"body": body})

    def update_branch(self, number: int) -> None:
        self._http_call("POST", f"{self._repo_prefix}/pulls/{number}/update")

    def ci_logs_failed(self, pr: Any) -> Dict[str, Any]:
        """Return explicit unsupported result with target_url when logs API is unavailable."""
        target_url = ""
        head_sha = getattr(pr, "head_sha", "") if not isinstance(pr, (str, int)) else ""
        if head_sha:
            _, data = self._http_call(
                "GET", f"{self._repo_prefix}/commits/{head_sha}/status", allow_404=True
            )
            for st in (data or {}).get("statuses") or []:
                if str(st.get("status") or st.get("state") or "").lower() in ("failure", "error"):
                    target_url = str(st.get("target_url") or "")
                    if target_url:
                        break
        return {
            "supported": False,
            "status": "unsupported",
            "target_url": target_url,
            "reason": f"{self.forge_type} REST API does not expose failed job logs uniformly; reproduce locally or inspect target_url.",
        }

    def ci_rerun_failed(self, pr: Any) -> Dict[str, Any]:
        """Return explicit unsupported result (no silent success)."""
        url = getattr(pr, "url", "") if not isinstance(pr, (str, int)) else ""
        return {
            "supported": False,
            "status": "unsupported",
            "target_url": url,
            "reason": f"{self.forge_type} REST API does not support rerunning failed CI jobs.",
        }

    # -- gate / refs ------------------------------------------------------

    def gate_snapshot(self, pr_limit: int = 100, issue_limit: int = 100) -> GateSnapshot:
        open_prs = self.list_prs(state="open", limit=pr_limit)
        open_issues = self.list_issues(state="open", limit=issue_limit)
        try:
            from devloops_lib.config import load_config, validate_required_checks

            required_checks = validate_required_checks(load_config())
        except Exception:
            required_checks = []

        pr_nodes: List[Dict[str, Any]] = []
        for pr in open_prs:
            ci = self.ci_status(pr, required_checks=required_checks)
            mergeable_str = (
                "MERGEABLE" if pr.mergeable is True else "CONFLICTING" if pr.mergeable is False else "UNKNOWN"
            )
            pr_nodes.append({
                "number": pr.number,
                "headRefOid": pr.head_sha,
                "mergeable": mergeable_str,
                "updatedAt": pr.updated_at,
                "isDraft": pr.is_draft,
                "author": {"login": pr.author},
                "title": pr.title,
                "bodyText": pr.body,
                "headRefName": pr.head_ref,
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": ci.state.upper()}}}]},
                "comments": {"nodes": []},
            })

        issue_nodes = [
            {
                "number": i.number,
                "updatedAt": i.updated_at,
                "labels": {"nodes": [{"name": lbl} for lbl in i.labels]},
                "assignees": {"nodes": [{"login": a} for a in i.assignees]},
            }
            for i in open_issues
        ]
        return GateSnapshot(prs=pr_nodes, issues=issue_nodes, raw={"forge": self.forge_type})

    def pr_head_ref(self, number: int) -> str:
        return f"refs/pull/{number}/head"


class ForgejoForge(GiteaForge):
    """Thin Forgejo subclass over GiteaForge (enforces Forgejo >= 7.0.0 floor)."""

    forge_type: str = "forgejo"


def _build_gitea_adapter(detected: Dict[str, Any], config: Dict[str, Any]) -> GiteaForge:
    token_env = str((config.get("forge") or {}).get("token_env") or "")
    return GiteaForge(
        base_url=str(detected.get("base_url") or ""),
        owner=str(detected.get("owner") or ""),
        name=str(detected.get("name") or ""),
        token_env=token_env,
        version=str(detected.get("version") or ""),
    )


def _build_forgejo_adapter(detected: Dict[str, Any], config: Dict[str, Any]) -> ForgejoForge:
    token_env = str((config.get("forge") or {}).get("token_env") or "")
    return ForgejoForge(
        base_url=str(detected.get("base_url") or ""),
        owner=str(detected.get("owner") or ""),
        name=str(detected.get("name") or ""),
        token_env=token_env,
        version=str(detected.get("version") or ""),
    )


register_forge_adapter("gitea", _build_gitea_adapter)
register_forge_adapter("forgejo", _build_forgejo_adapter)
