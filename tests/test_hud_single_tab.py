#!/usr/bin/env python3
"""Persistent HUD opener behavior."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer  # noqa: E402
from ringer import AppConfig, ArtifactConfig, EvalConfig, ensure_hud_running  # noqa: E402


def config(root: Path) -> AppConfig:
    return AppConfig(
        path=None,
        identity_default=None,
        state_dir=root / "state",
        dashboard_port_base=8787,
        hud_port=8700,
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


class HudSingleTabTests(unittest.TestCase):
    def setUp(self) -> None:
        # An exported RINGER_NO_HUD=1 would make ensure_hud_running return early.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("RINGER_NO_HUD", None)

    def test_already_alive_does_not_open_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            opened: list[str] = []
            original_alive = ringer.hud_is_alive
            original_open = ringer.open_in_browser
            original_popen = ringer.subprocess.Popen
            try:
                ringer.hud_is_alive = lambda _port: True
                ringer.open_in_browser = opened.append

                def fail_popen(*_args: object, **_kwargs: object) -> object:
                    raise AssertionError("should not spawn a HUD that is already alive")

                ringer.subprocess.Popen = fail_popen
                ensure_hud_running(config(Path(temp)), open_browser=True)
            finally:
                ringer.hud_is_alive = original_alive
                ringer.open_in_browser = original_open
                ringer.subprocess.Popen = original_popen
            self.assertEqual([], opened)

    def test_dead_then_alive_opens_once_after_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            opened: list[str] = []
            spawned: list[object] = []
            alive_results = iter([False, True, True])
            original_alive = ringer.hud_is_alive
            original_open = ringer.open_in_browser
            original_popen = ringer.subprocess.Popen
            try:
                ringer.hud_is_alive = lambda _port: next(alive_results)
                ringer.open_in_browser = opened.append

                def fake_popen(*args: object, **_kwargs: object) -> object:
                    spawned.append(args)
                    return object()

                ringer.subprocess.Popen = fake_popen
                ensure_hud_running(config(Path(temp)), open_browser=True)
            finally:
                ringer.hud_is_alive = original_alive
                ringer.open_in_browser = original_open
                ringer.subprocess.Popen = original_popen
            self.assertEqual(1, len(spawned))
            self.assertEqual(["http://127.0.0.1:8700"], opened)


if __name__ == "__main__":
    unittest.main(verbosity=2)
