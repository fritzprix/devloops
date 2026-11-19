#!/usr/bin/env python3
"""Tests for forge detection, persistence, fail-closed factory, and doctor integration (issue #45)."""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from typing import Dict, Tuple
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import config as cfg_mod
from devloops_lib import doctor
from devloops_lib.forge import (
    ForgeDetectionError,
    GitHubForge,
    ProbeResult,
    detect_forge,
    get_forge,
    parse_remote_url,
    probe_forge,
)
from devloops_lib.forge import detect as detect_mod


class _StubServer:
    """In-process HTTP server for deterministic probe_forge tests."""

    def __init__(self, routes: Dict[str, Tuple[int, Dict[str, str], str]]) -> None:
        self.routes = routes
        self.seen_paths: list[str] = []

        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parent.seen_paths.append(self.path)
                status, headers, body = parent.routes.get(self.path, (404, {}, "{}"))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, format, *args):  # noqa: A002
                pass

        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


class ParseRemoteUrlTest(unittest.TestCase):
    def test_parses_https_ssh_scp_ports_dot_git_and_ghe(self):
        cases = [
            (
                "https://github.com/octo/repo.git",
                ("https", "github.com", None, "octo", "repo", "https://github.com"),
            ),
            (
                "https://git.example.com:3000/team/app.git",
                ("https", "git.example.com", 3000, "team", "app", "https://git.example.com:3000"),
            ),
            (
                "ssh://git@git.example.com:2222/team/app.git",
                ("ssh", "git.example.com", 2222, "team", "app", "https://git.example.com"),
            ),
            (
                "git@github.com:octo/repo.git",
                ("ssh", "github.com", None, "octo", "repo", "https://github.com"),
            ),
            (
                "git@ghe.corp.internal:platform/service",
                ("ssh", "ghe.corp.internal", None, "platform", "service", "https://ghe.corp.internal"),
            ),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                parsed = parse_remote_url(raw)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(
                    (parsed.scheme, parsed.host, parsed.port, parsed.owner, parsed.name, parsed.base_url),
                    expected,
                )


class ProbeForgeTest(unittest.TestCase):
    def _serve(self, routes: Dict[str, Tuple[int, Dict[str, str], str]]) -> _StubServer:
        srv = _StubServer(routes)
        self.addCleanup(srv.close)
        return srv

    def test_forgejo_checked_before_gitea_when_both_endpoints_answer(self):
        srv = self._serve({
            "/api/forgejo/v1/version": (200, {}, json.dumps({"version": "9.0.1"})),
            "/api/v1/version": (200, {}, json.dumps({"version": "1.22.0"})),
        })
        result = probe_forge(srv.base_url)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.forge_type, "forgejo")
        self.assertEqual(result.version, "9.0.1")
        self.assertEqual(srv.seen_paths, ["/api/forgejo/v1/version"])

    def test_gitea_resolves_when_forgejo_endpoint_404(self):
        srv = self._serve({
            "/api/v1/version": (200, {}, json.dumps({"version": "1.22.3"})),
        })
        result = probe_forge(srv.base_url)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.forge_type, "gitea")
        self.assertEqual(result.version, "1.22.3")

    def test_ghe_meta_resolves_to_github(self):
        srv = self._serve({
            "/api/v3/meta": (200, {"X-GitHub-Enterprise-Version": "3.14.0"}, json.dumps({"installed_version": "3.14.0"})),
        })
        result = probe_forge(srv.base_url)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.forge_type, "github")
        self.assertEqual(result.version, "3.14.0")
        self.assertEqual(result.api_url, f"{srv.base_url}/api/v3")

    def test_gitlab_401_detected_and_rejected_by_detect_forge(self):
        srv = self._serve({
            "/api/v4/version": (401, {"X-Gitlab-Meta": "1"}, json.dumps({"message": "401 Unauthorized"})),
        })
        result = probe_forge(srv.base_url)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.forge_type, "gitlab")

        with self.assertRaises(ForgeDetectionError) as ctx:
            detect_forge(
                {"forge": {"type": "auto", "url": srv.base_url}},
                remote_url=f"{srv.base_url}/org/proj.git",
            )
        self.assertIn("gitlab", str(ctx.exception))
        self.assertIn("forge.type", str(ctx.exception))


class DetectAndPersistTest(unittest.TestCase):
    def test_explicit_forge_type_skips_probing(self):
        probe_mock = MagicMock()
        info = detect_forge(
            {"forge": {"type": "forgejo", "url": "https://git.example.com"}},
            remote_url="https://git.example.com/org/repo.git",
            probe_fn=probe_mock,
        )
        probe_mock.assert_not_called()
        self.assertEqual(info["type"], "forgejo")
        self.assertEqual(info["source"], "config")
        self.assertEqual(info["api_url"], "https://git.example.com/api/v1")

    def test_conflicting_explicit_type_fails_closed(self):
        with self.assertRaises(ForgeDetectionError) as ctx:
            detect_forge(
                {"forge": {"type": "forgejo"}},
                remote_url="https://github.com/org/repo.git",
            )
        self.assertIn("conflicts", str(ctx.exception))

        probe_mock = MagicMock(return_value=ProbeResult("gitea", "1.22"))
        with self.assertRaises(ForgeDetectionError):
            detect_forge(
                {"forge": {"type": "forgejo", "url": "https://git.example.com"}},
                remote_url="https://git.example.com/org/repo.git",
                probe_fn=probe_mock,
                verify_explicit=True,
            )

    def test_unknown_forge_never_constructs_adapter(self):
        probe_mock = MagicMock(return_value=None)
        with patch.object(detect_mod, "GitHubForge") as mock_gh:
            with self.assertRaises(ForgeDetectionError) as ctx:
                get_forge(
                    {"forge": {"type": "auto"}},
                    remote_url="https://unknown.example.com/org/repo.git",
                    probe_fn=probe_mock,
                )
        mock_gh.assert_not_called()
        self.assertIn("unknown.example.com", str(ctx.exception))
        self.assertIn("forge.type", str(ctx.exception))

    def test_persisted_forge_reused_and_invalidated_on_remote_change(self):
        state: dict = {}
        probe_mock = MagicMock(return_value=ProbeResult("forgejo", "9.0", "https://git.example.com/api/v1"))
        cfg = {"forge": {"type": "auto"}}

        first = detect_forge(cfg, state=state, remote_url="https://git.example.com/org/a.git", probe_fn=probe_mock)
        second = detect_forge(cfg, state=state, remote_url="https://git.example.com/org/a.git", probe_fn=probe_mock)
        self.assertEqual(probe_mock.call_count, 1)
        self.assertEqual(first["type"], "forgejo")
        self.assertEqual(second["type"], "forgejo")

        # Changing the remote URL triggers re-detection.
        third = detect_forge(cfg, state=state, remote_url="https://github.com/org/a.git", probe_fn=probe_mock)
        self.assertEqual(third["type"], "github")
        self.assertEqual(third["source"], "url")
        self.assertEqual(state["forge"]["type"], "github")

    def test_get_forge_returns_github_adapter_for_github_remote(self):
        forge = get_forge({"forge": {"type": "auto"}}, remote_url="git@github.com:octo/repo.git")
        self.assertIsInstance(forge, GitHubForge)

    def test_config_default_includes_forge_block(self):
        self.assertEqual(
            cfg_mod.DEFAULT_CONFIG["forge"],
            {"type": "auto", "url": "", "remote": "origin", "token_env": ""},
        )


class DoctorForgeTest(unittest.TestCase):
    def test_doctor_non_github_never_contacts_github_or_logs_token(self):
        secret = "super-secret-forgejo-token-value"
        cfg = {
            "forge": {
                "type": "forgejo",
                "url": "https://git.example.com",
                "token_env": "MY_FORGEJO_TOKEN",
            }
        }
        state: dict = {}
        buf = io.StringIO()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp

        with patch.dict(os.environ, {"MY_FORGEJO_TOKEN": secret}, clear=False), \
                patch("devloops_lib.doctor.urllib.request.urlopen", return_value=mock_resp) as mock_open, \
                patch("devloops_lib.doctor.subprocess.run") as mock_sub, \
                contextlib.redirect_stdout(buf):
            ok = doctor.check_forge(cfg, remote_url="https://git.example.com/org/repo.git")
            detect_forge(cfg, state=state, remote_url="https://git.example.com/org/repo.git")

        self.assertTrue(ok)
        output = buf.getvalue()
        self.assertIn("forgejo", output)
        self.assertNotIn("api.github.com", output)
        self.assertNotIn(secret, output)
        self.assertNotIn(secret, json.dumps(state))
        req_arg = mock_open.call_args.args[0]
        self.assertEqual(req_arg.full_url, "https://git.example.com/api/v1")
        # gh auth must not be called for non-GitHub forges.
        mock_sub.assert_not_called()

    def test_embedded_remote_credentials_never_persisted_to_state(self):
        secret = "ghp_EMBEDDED_SECRET_TOKEN_999"
        state: dict = {}
        info = detect_forge(
            {"forge": {"type": "auto"}},
            state=state,
            remote_url=f"https://oauth2:{secret}@github.com/octo/repo.git",
        )
        self.assertEqual(info["type"], "github")
        self.assertEqual(info["remote_url"], "https://github.com/octo/repo.git")
        self.assertNotIn(secret, json.dumps(state))

    def test_tick_gate_preflight_persists_forge_and_fails_closed(self):
        import tempfile
        from devloops_lib import tick_gate

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(json.dumps({"current": {"issue": 1}}), encoding="utf-8")

            # 1. Valid remote: tick_gate preflight detects and persists state["forge"].
            with patch("devloops_lib.forge.detect.read_git_remote_url", return_value="https://github.com/octo/repo.git"), \
                    patch("devloops_lib.tick_gate.load_config", return_value={"forge": {"type": "auto", "url": "", "remote": "origin"}}), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    tick_gate.main(["--state-file", str(state_file), "--agent-id", "t1"])
            self.assertEqual(ctx.exception.code, tick_gate.EXIT_RUN)
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["forge"]["type"], "github")
            self.assertEqual(saved["forge"]["source"], "url")

            # 2. Unsupported forge: tick_gate exits EXIT_ERROR (3) before preflight_decision runs.
            err_buf = io.StringIO()
            with patch("devloops_lib.forge.detect.read_git_remote_url", return_value="https://gitlab.example.com/org/proj.git"), \
                    patch("devloops_lib.forge.detect.probe_forge", return_value=ProbeResult("gitlab", "17.0")), \
                    patch("devloops_lib.tick_gate.load_config", return_value={"forge": {"type": "auto", "url": "", "remote": "origin"}}), \
                    patch("devloops_lib.tick_gate.preflight_decision") as mock_pd, \
                    contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as ctx2:
                    tick_gate.main(["--state-file", str(state_file), "--agent-id", "t1"])
            self.assertEqual(ctx2.exception.code, tick_gate.EXIT_ERROR)
            mock_pd.assert_not_called()
            self.assertIn("gitlab", err_buf.getvalue())
            self.assertIn("forge.type", err_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
