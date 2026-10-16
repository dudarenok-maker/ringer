#!/usr/bin/env python3
"""The HUD must never share its port with another HUD (Windows SO_REUSEADDR bug)."""
from __future__ import annotations

import contextlib
import io
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer  # noqa: E402
from ringer import AppConfig, ArtifactConfig, EvalConfig, PersistentHudServer, ReusableThreadingHTTPServer  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ExclusiveBindTests(unittest.TestCase):
    def test_second_server_cannot_bind_a_port_already_listening(self) -> None:
        port = free_port()
        first = ReusableThreadingHTTPServer(("127.0.0.1", port), ringer.BaseHTTPRequestHandler)
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError):
            second = ReusableThreadingHTTPServer(("127.0.0.1", port), ringer.BaseHTTPRequestHandler)
            second.server_close()

    def test_persistent_hud_start_fails_loudly_when_port_is_held(self) -> None:
        port = free_port()
        holder = ReusableThreadingHTTPServer(("127.0.0.1", port), ringer.BaseHTTPRequestHandler)
        self.addCleanup(holder.server_close)
        with tempfile.TemporaryDirectory() as temp:
            hud = PersistentHudServer(Path(temp), preferred_port=port, open_viewer=False)
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                hud.start()
        self.assertIsNone(hud.httpd)

    def test_restart_after_close_can_rebind_same_port(self) -> None:
        port = free_port()
        first = ReusableThreadingHTTPServer(("127.0.0.1", port), ringer.BaseHTTPRequestHandler)
        first.server_close()
        again = ReusableThreadingHTTPServer(("127.0.0.1", port), ringer.BaseHTTPRequestHandler)
        again.server_close()

    @unittest.skipUnless(sys.platform == "win32", "SO_REUSEADDR double-bind is Windows-specific")
    def test_windows_uses_exclusive_addr_not_reuse_addr(self) -> None:
        self.assertFalse(ReusableThreadingHTTPServer.allow_reuse_address)

    @unittest.skipIf(sys.platform == "win32", "POSIX keeps SO_REUSEADDR for TIME_WAIT")
    def test_posix_keeps_reuse_addr(self) -> None:
        self.assertTrue(ReusableThreadingHTTPServer.allow_reuse_address)


class LoserOfBindRaceTests(unittest.TestCase):
    """Two spawns that both probed 'dead' race to bind; the loser must exit cleanly."""

    def make_config(self, root: Path, port: int) -> AppConfig:
        return AppConfig(
            path=None,
            identity_default=None,
            state_dir=root / "state",
            dashboard_port_base=8787,
            hud_port=port,
            hud_app_path=None,
            allow_full_access=False,
            eval=EvalConfig(backend="jsonl", jsonl_path=root / "eval.jsonl"),
            engines={},
            artifact=ArtifactConfig(
                enabled=False,
                out_template=str(root / "live.html"),
                report_template=str(root / "report.html"),
                index_out=root / "index.html",
            ),
        )

    def test_bind_failure_with_live_hud_is_reported_as_already_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cfg = self.make_config(Path(temp), free_port())
            probes = iter([False, True])  # pre-bind probe misses, post-failure probe hits
            out = io.StringIO()
            with (
                mock.patch.object(ringer, "hud_is_alive", side_effect=lambda _p: next(probes)),
                mock.patch.object(PersistentHudServer, "start", side_effect=RuntimeError("port in use")),
                contextlib.redirect_stdout(out),
            ):
                code = ringer.run_persistent_hud(cfg, port=None, open_viewer=False)
            self.assertEqual(0, code)
            self.assertIn("already running", out.getvalue())

    def test_bind_failure_without_live_hud_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cfg = self.make_config(Path(temp), free_port())
            err = io.StringIO()
            with (
                mock.patch.object(ringer, "hud_is_alive", return_value=False),
                mock.patch.object(PersistentHudServer, "start", side_effect=RuntimeError("port in use")),
                contextlib.redirect_stderr(err),
            ):
                code = ringer.run_persistent_hud(cfg, port=None, open_viewer=False)
            self.assertEqual(1, code)
            self.assertIn("port in use", err.getvalue())


class NoHudEnvTests(unittest.TestCase):
    def test_ringer_no_hud_skips_probe_and_spawn(self) -> None:
        cfg = LoserOfBindRaceTests().make_config(Path(tempfile.gettempdir()), free_port())
        with (
            mock.patch.dict(os.environ, {"RINGER_NO_HUD": "1"}),
            mock.patch.object(ringer, "hud_is_alive", side_effect=AssertionError("probed")),
            mock.patch.object(ringer.subprocess, "Popen", side_effect=AssertionError("spawned")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            ringer.ensure_hud_running(cfg, open_browser=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
