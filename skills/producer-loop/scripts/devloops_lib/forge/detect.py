"""Forge detection, persistence, and adapter factory (issue #45).

Resolves which git forge a repository uses from:
  1. Explicit ``forge.type`` in ``devloops.config.yaml`` (skips probing unless verified).
  2. Remote URL (``github.com`` resolves immediately to ``github``).
  3. Unauthenticated HTTPS API probing in strict order:
       - ``/api/forgejo/v1/version`` -> ``forgejo`` (checked before Gitea)
       - ``/api/v1/version``         -> ``gitea``
       - ``/api/v3/meta``            -> ``github`` (GitHub Enterprise Server)
       - ``/api/v4/version``         -> ``gitlab`` (401 counts as a hit; rejected)
     with response-header tie-breakers (``X-GitHub-Request-Id``,
     ``X-GitHub-Enterprise-Version``, ``X-Gitlab-Meta``).

Fails closed (``ForgeDetectionError``) on unknown, unsupported, or conflicting
forges before any forge adapter is constructed or any write occurs.
Never logs or persists API token values.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from devloops_lib.forge.base import Forge, ForgeError
from devloops_lib.forge.github import GitHubForge

SUPPORTED_FORGES = frozenset({"github", "forgejo", "gitea"})
KNOWN_UNSUPPORTED_FORGES = frozenset({"gitlab", "bitbucket"})
PROBE_TIMEOUT_SECONDS = 5.0

_SCP_REMOTE_RE = re.compile(r"^(?:(?P<user>[^@/\s]+)@)?(?P<host>[^:/\s]+):(?P<path>.+)$")


class ForgeDetectionError(ForgeError):
    """Raised when forge detection fails, conflicts with config, or hits an unsupported forge."""


@dataclass(frozen=True)
class ParsedRemote:
    raw: str
    scheme: str
    host: str
    port: Optional[int]
    owner: str
    name: str
    base_url: str


@dataclass(frozen=True)
class ProbeResult:
    forge_type: str
    version: str = ""
    api_url: str = ""


def _strip_dot_git(segment: str) -> str:
    return segment[:-4] if segment.endswith(".git") else segment


def _split_owner_name(path: str) -> Tuple[str, str]:
    cleaned = path.strip("/")
    parts = [p for p in cleaned.split("/") if p]
    if len(parts) < 2:
        return "", ""
    owner = "/".join(parts[:-1])
    name = _strip_dot_git(parts[-1])
    return owner, name


def parse_remote_url(raw: str) -> Optional[ParsedRemote]:
    """Parse https/http, ssh://, or scp-style git remote URLs.

    Strips trailing ``.git`` and preserves non-default ports. For ssh/scp remotes,
    ``base_url`` uses ``https://<host>`` (dropping the SSH port).
    """
    text = (raw or "").strip()
    if not text:
        return None

    if "://" in text:
        parsed = urllib.parse.urlparse(text)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        if not host:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        owner, name = _split_owner_name(parsed.path or "")
        if not owner or not name:
            return None
        if scheme in ("http", "https"):
            host_part = f"{host}:{port}" if port else host
            base_url = f"{scheme}://{host_part}"
        else:
            base_url = f"https://{host}"
        return ParsedRemote(
            raw=text,
            scheme=scheme,
            host=host,
            port=port,
            owner=owner,
            name=name,
            base_url=base_url,
        )

    match = _SCP_REMOTE_RE.match(text)
    if match:
        host = match.group("host").lower()
        owner, name = _split_owner_name(match.group("path"))
        if not owner or not name:
            return None
        return ParsedRemote(
            raw=text,
            scheme="ssh",
            host=host,
            port=None,
            owner=owner,
            name=name,
            base_url=f"https://{host}",
        )

    return None


def _api_url_for(forge_type: str, base_url: str) -> str:
    base = base_url.rstrip("/")
    parsed = urllib.parse.urlparse(base)
    host = (parsed.hostname or "").lower()
    if forge_type == "github":
        if host == "github.com":
            return "https://api.github.com"
        return f"{base}/api/v3" if base else "https://api.github.com"
    if forge_type in ("forgejo", "gitea"):
        return f"{base}/api/v1" if base else ""
    if forge_type == "gitlab":
        return f"{base}/api/v4" if base else ""
    return base


def _http_get(url: str, timeout: float) -> Tuple[int, Dict[str, str], str]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "devloops-forge-detect", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), headers, body
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else ())}
        try:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        except Exception:
            body = ""
        return int(exc.code), headers, body


def _header_tiebreaker(headers: Dict[str, str]) -> Optional[Tuple[str, str]]:
    if "x-github-enterprise-version" in headers:
        return "github", headers["x-github-enterprise-version"].strip()
    if "x-github-request-id" in headers:
        return "github", ""
    if "x-gitlab-meta" in headers:
        return "gitlab", ""
    return None


def _json_version(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if isinstance(data, dict):
        for key in ("version", "installed_version"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def probe_forge(base_url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> Optional[ProbeResult]:
    """Probe a forge host over HTTPS/HTTP and return the first definite match.

    Order is strict: Forgejo before Gitea (Forgejo also answers ``/api/v1/version``),
    then GitHub Enterprise Server (``/api/v3/meta``), then GitLab (``/api/v4/version``,
    where HTTP 401 counts as a hit). Response headers serve as a tie-breaker.
    """
    base = base_url.rstrip("/")
    if not base:
        return None

    probes = (
        ("forgejo", f"{base}/api/forgejo/v1/version", False),
        ("gitea", f"{base}/api/v1/version", False),
        ("github", f"{base}/api/v3/meta", False),
        ("gitlab", f"{base}/api/v4/version", True),
    )
    header_hint: Optional[Tuple[str, str]] = None

    for candidate, url, accept_401 in probes:
        try:
            status, headers, body = _http_get(url, timeout)
        except Exception:
            continue

        if header_hint is None:
            header_hint = _header_tiebreaker(headers)

        if status == 200:
            version = _json_version(body) or headers.get("x-github-enterprise-version", "").strip()
            return ProbeResult(forge_type=candidate, version=version, api_url=_api_url_for(candidate, base))
        if accept_401 and status == 401:
            return ProbeResult(forge_type=candidate, version="", api_url=_api_url_for(candidate, base))

    if header_hint is not None:
        ftype, version = header_hint
        return ProbeResult(forge_type=ftype, version=version, api_url=_api_url_for(ftype, base))
    return None


def read_git_remote_url(remote: str = "origin", cwd: Optional[Path] = None) -> str:
    res = subprocess.run(
        ["git", "remote", "get-url", remote],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd) if cwd else None,
        check=False,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def _normalize_forge_config(config: Optional[Dict[str, Any]]) -> Dict[str, str]:
    forge_cfg = (config or {}).get("forge") or {}
    return {
        "type": str(forge_cfg.get("type") or "auto").strip().lower() or "auto",
        "url": str(forge_cfg.get("url") or "").strip().rstrip("/"),
        "remote": str(forge_cfg.get("remote") or "origin").strip() or "origin",
        "token_env": str(forge_cfg.get("token_env") or "").strip(),
    }


def _host_of(base_url: str, parsed: Optional[ParsedRemote]) -> str:
    if base_url:
        host = (urllib.parse.urlparse(base_url).hostname or "").lower()
        if host:
            return host
    return parsed.host if parsed else "<unknown>"


def _fail_unsupported(forge_type: str, host: str) -> ForgeDetectionError:
    return ForgeDetectionError(
        f"Unsupported forge '{forge_type}' detected on host '{host}'. "
        f"Supported values for forge.type in devloops.config.yaml are: "
        f"{', '.join(sorted(SUPPORTED_FORGES))}."
    )


def _fail_unknown(host: str) -> ForgeDetectionError:
    return ForgeDetectionError(
        f"Could not detect a supported forge on host '{host}'. "
        f"Set forge.type ({' | '.join(sorted(SUPPORTED_FORGES))}) and forge.url in devloops.config.yaml."
    )


def _fail_conflict(explicit: str, detected: str, host: str) -> ForgeDetectionError:
    return ForgeDetectionError(
        f"Configured forge.type='{explicit}' conflicts with detected forge '{detected}' on host '{host}'. "
        f"Update forge.type in devloops.config.yaml to '{detected}' or fix forge.url / git remote."
    )


def _cached_is_valid(
    cached: Any,
    remote_url: str,
    cfg: Dict[str, str],
) -> bool:
    if not isinstance(cached, dict):
        return False
    if cached.get("type") not in SUPPORTED_FORGES:
        return False
    return (
        cached.get("remote_url") == remote_url
        and cached.get("config_type") == cfg["type"]
        and cached.get("config_url") == cfg["url"]
    )


def detect_forge(
    config: Optional[Dict[str, Any]] = None,
    state: Optional[Dict[str, Any]] = None,
    *,
    remote_url: Optional[str] = None,
    probe_fn: Optional[Callable[[str], Optional[ProbeResult]]] = None,
    verify_explicit: bool = False,
    cwd: Optional[Path] = None,
) -> Dict[str, Any]:
    """Detect the repository's forge, persist it in ``state['forge']``, and fail closed.

    When ``state`` already holds a valid ``forge`` record for the same remote URL
    and forge config, it is reused without probing. No token value is ever stored.
    """
    cfg = _normalize_forge_config(config)
    resolved_remote = remote_url if remote_url is not None else read_git_remote_url(cfg["remote"], cwd=cwd)
    parsed = parse_remote_url(resolved_remote)

    if state is not None and _cached_is_valid(state.get("forge"), resolved_remote, cfg):
        return dict(state["forge"])

    base_url = cfg["url"] or (parsed.base_url if parsed else "")
    host = _host_of(base_url, parsed)
    owner = parsed.owner if parsed else ""
    name = parsed.name if parsed else ""
    if not owner or not name:
        proj_repo = str((config or {}).get("project", {}).get("repo") or "").strip()
        if "/" in proj_repo and proj_repo != "owner/name":
            owner, name = proj_repo.split("/", 1)

    explicit = cfg["type"]
    probe = probe_fn or probe_forge
    version = ""

    if explicit != "auto":
        if explicit not in SUPPORTED_FORGES:
            raise _fail_unsupported(explicit, host)
        if host == "github.com" and explicit != "github":
            raise _fail_conflict(explicit, "github", host)
        if verify_explicit and base_url and host != "github.com":
            probed = probe(base_url)
            if probed is not None and probed.forge_type != explicit:
                raise _fail_conflict(explicit, probed.forge_type, host)
            if probed is not None:
                version = probed.version
        forge_type = explicit
        source = "config"
        api_url = _api_url_for(forge_type, base_url or ("https://github.com" if forge_type == "github" else ""))
    else:
        if host == "github.com":
            forge_type = "github"
            base_url = "https://github.com"
            api_url = "https://api.github.com"
            source = "url"
        elif base_url:
            probed = probe(base_url)
            if probed is None:
                raise _fail_unknown(host)
            if probed.forge_type not in SUPPORTED_FORGES:
                raise _fail_unsupported(probed.forge_type, host)
            forge_type = probed.forge_type
            version = probed.version
            api_url = probed.api_url or _api_url_for(forge_type, base_url)
            source = "probe"
        else:
            raise _fail_unknown(host)

    record: Dict[str, Any] = {
        "type": forge_type,
        "base_url": base_url,
        "api_url": api_url,
        "owner": owner,
        "name": name,
        "version": version,
        "detected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "remote_url": resolved_remote,
        "config_type": cfg["type"],
        "config_url": cfg["url"],
    }
    if state is not None:
        state["forge"] = dict(record)
    return record


# Adapter registry so issue #46 (Forgejo/Gitea) can plug in without modifying factory logic.
_ADAPTERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Forge]] = {}


def register_forge_adapter(
    forge_type: str,
    builder: Callable[[Dict[str, Any], Dict[str, Any]], Forge],
) -> None:
    _ADAPTERS[forge_type.lower()] = builder


def _build_github_adapter(detected: Dict[str, Any], _config: Dict[str, Any]) -> Forge:
    owner = detected.get("owner") or ""
    name = detected.get("name") or ""
    repo = f"{owner}/{name}" if owner and name else None
    return GitHubForge(repo=repo)


register_forge_adapter("github", _build_github_adapter)


def get_forge(
    config: Optional[Dict[str, Any]] = None,
    state: Optional[Dict[str, Any]] = None,
    *,
    remote_url: Optional[str] = None,
    probe_fn: Optional[Callable[[str], Optional[ProbeResult]]] = None,
    verify_explicit: bool = False,
    cwd: Optional[Path] = None,
) -> Forge:
    """Detect the forge and construct its adapter; fail closed before constructing anything."""
    cfg = config or {}
    detected = detect_forge(
        cfg,
        state=state,
        remote_url=remote_url,
        probe_fn=probe_fn,
        verify_explicit=verify_explicit,
        cwd=cwd,
    )
    forge_type = detected["type"]
    builder = _ADAPTERS.get(forge_type)
    if builder is None:
        raise ForgeDetectionError(
            f"Forge '{forge_type}' detected on '{detected.get('base_url')}', "
            f"but no adapter is registered for '{forge_type}' yet."
        )
    return builder(detected, cfg)
