#!/usr/bin/env python3
"""Process start-time guard against pid reuse in active-runs.json."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ringer  # noqa: E402
import ringer_pid  # noqa: E402

HOOK = ROOT / "hooks" / "ringer_nudge.py"
HAS_START_TIME = ringer_pid.process_start_time(os.getpid()) is not None


class StartTimeProbeTests(unittest.TestCase):
    def test_own_start_time_is_stable_across_calls(self) -> None:
        first = ringer_pid.process_start_time(os.getpid())
        time.sleep(0.05)
        self.assertEqual(first, ringer_pid.process_start_time(os.getpid()))

    def test_exited_child_is_not_alive_even_with_a_recorded_start_time(self) -> None:
        # (process_start_time itself may still answer while Popen holds the
        # exited child's handle open on Windows; liveness must not.)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        start = ringer_pid.process_start_time(proc.pid)
        proc.wait()
        self.assertFalse(ringer_pid.pid_is_alive(proc.pid, start))
        self.assertFalse(ringer_pid.pid_is_alive(proc.pid))

    def test_non_positive_pid_has_no_start_time(self) -> None:
        self.assertIsNone(ringer_pid.process_start_time(0))
        self.assertIsNone(ringer_pid.process_start_time(-3))

    @unittest.skipUnless(HAS_START_TIME, "no start-time source on this platform")
    def test_matching_start_time_reads_alive(self) -> None:
        start = ringer_pid.process_start_time(os.getpid())
        self.assertTrue(ringer_pid.pid_is_alive(os.getpid(), start))

    @unittest.skipUnless(HAS_START_TIME, "no start-time source on this platform")
    def test_mismatched_start_time_reads_dead_as_a_reused_pid(self) -> None:
        # The reuse case: the pid is live, but it is a different process than
        # the one that was recorded.
        start = ringer_pid.process_start_time(os.getpid())
        self.assertFalse(ringer_pid.pid_is_alive(os.getpid(), start - 3600.0))
        self.assertFalse(ringer_pid.pid_is_alive(os.getpid(), start + 3600.0))

    @unittest.skipUnless(HAS_START_TIME, "no start-time source on this platform")
    def test_sub_tolerance_difference_still_matches(self) -> None:
        start = ringer_pid.process_start_time(os.getpid())
        self.assertTrue(ringer_pid.pid_is_alive(os.getpid(), start + 0.25))

    def test_no_recorded_start_time_is_pid_only(self) -> None:
        self.assertTrue(ringer_pid.pid_is_alive(os.getpid(), None))
        self.assertTrue(ringer_pid.pid_is_alive(os.getpid()))

    def test_unreadable_live_start_time_falls_back_to_pid_only(self) -> None:
        # A recorded start we cannot verify must not mark a live run dead.
        with unittest.mock.patch.object(ringer_pid, "process_start_time", return_value=None):
            self.assertTrue(ringer_pid.pid_is_alive(os.getpid(), 1.0))

    def test_unsupported_platform_falls_back_to_pid_only(self) -> None:
        # _pid_exists is stubbed: with sys.platform faked, the real probe would
        # take the POSIX os.kill(pid, 0) branch, a Ctrl+C on real Windows.
        with unittest.mock.patch.object(ringer_pid.sys, "platform", "darwin"), \
                unittest.mock.patch.object(ringer_pid, "_pid_exists", return_value=True):
            self.assertIsNone(ringer_pid.process_start_time(os.getpid()))
            self.assertTrue(ringer_pid.pid_is_alive(os.getpid(), 1.0))


class LinuxStatParseTests(unittest.TestCase):
    def parse(self, text: str) -> float | None:
        return ringer_pid._parse_proc_stat_starttime(text, clock_ticks=100)

    def test_parses_field_22(self) -> None:
        fields = ["S"] + ["0"] * 18 + ["123456"] + ["0"] * 5
        self.assertEqual(1234.56, self.parse("42 (python3) " + " ".join(fields)))

    def test_comm_with_spaces_and_parens(self) -> None:
        fields = ["S"] + ["0"] * 18 + ["500"] + ["0"] * 5
        self.assertEqual(5.0, self.parse("42 (a) b (c d) " + " ".join(fields)))

    def test_garbage_is_none(self) -> None:
        self.assertIsNone(self.parse("nonsense"))
        self.assertIsNone(self.parse("42 (x) S 1 2"))


@unittest.skipUnless(sys.platform == "win32", "Windows ctypes path")
class WindowsStartTimeSeamTests(unittest.TestCase):
    def test_get_process_times_failure_returns_none_and_closes_handle(self) -> None:
        fake = unittest.mock.MagicMock()
        fake.OpenProcess.return_value = 99
        fake.GetProcessTimes.return_value = 0
        with unittest.mock.patch("ctypes.WinDLL", return_value=fake):
            self.assertIsNone(ringer_pid._windows_process_start_time(4242))
        fake.CloseHandle.assert_called_once_with(99)

    def test_open_process_failure_returns_none(self) -> None:
        fake = unittest.mock.MagicMock()
        fake.OpenProcess.return_value = None
        with unittest.mock.patch("ctypes.WinDLL", return_value=fake):
            self.assertIsNone(ringer_pid._windows_process_start_time(4242))

    def test_no_handle_leak(self) -> None:
        from test_ringer_pid import process_handle_count

        ringer_pid.process_start_time(os.getpid())
        before = process_handle_count()
        for _ in range(500):
            ringer_pid.process_start_time(os.getpid())
        self.assertLess(process_handle_count() - before, 50)


class ActiveRunsStartTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        old = os.environ.copy()
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["RINGER_HOME"] = str(self.home)
        self.path = self.home / "active-runs.json"

    def write(self, entries: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(entries), encoding="utf-8")

    @unittest.skipUnless(HAS_START_TIME, "no start-time source on this platform")
    def test_register_records_pid_start(self) -> None:
        ringer.register_active_run("r1", "id", "name", Path(self.tmp.name))
        entry = json.loads(self.path.read_text(encoding="utf-8"))["r1"]
        self.assertEqual(ringer_pid.process_start_time(os.getpid()), entry["pid_start"])
        self.assertIn("r1", ringer.read_active_runs())

    @unittest.skipUnless(HAS_START_TIME, "no start-time source on this platform")
    def test_reused_pid_entry_is_pruned_on_read(self) -> None:
        start = ringer_pid.process_start_time(os.getpid())
        self.write({"stale": {"pid": os.getpid(), "pid_start": start - 3600.0, "workdir": "/w"}})
        self.assertEqual({}, ringer.read_active_runs())

    @unittest.skipUnless(HAS_START_TIME, "no start-time source on this platform")
    def test_prune_keeps_pid_start_on_surviving_entries(self) -> None:
        start = ringer_pid.process_start_time(os.getpid())
        self.write({"ok": {"pid": os.getpid(), "pid_start": start, "workdir": "/w"}})
        self.assertEqual(start, ringer.read_active_runs()["ok"]["pid_start"])

    def test_legacy_entry_without_start_time_is_pid_only(self) -> None:
        self.write({"old": {"pid": os.getpid(), "workdir": "/w"}})
        runs = ringer.read_active_runs()
        self.assertEqual(["old"], list(runs))
        self.assertNotIn("pid_start", runs["old"])

    def test_malformed_pid_start_is_ignored(self) -> None:
        self.write({"odd": {"pid": os.getpid(), "pid_start": "soon", "workdir": "/w"}})
        self.assertEqual(["odd"], list(ringer.read_active_runs()))


class NudgeHookStartTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location("ringer_nudge_st", HOOK)
        assert spec is not None and spec.loader is not None
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)

    @unittest.skipUnless(HAS_START_TIME, "no start-time source on this platform")
    def test_hook_drops_reused_pid_and_stays_read_only(self) -> None:
        start = ringer_pid.process_start_time(os.getpid())
        path = self.home / "active-runs.json"
        path.write_text(json.dumps({
            "stale": {"pid": os.getpid(), "pid_start": start - 3600.0, "workdir": "/s"},
            "live": {"pid": os.getpid(), "pid_start": start, "workdir": "/l"},
            "legacy": {"pid": os.getpid(), "workdir": "/g"},
        }), encoding="utf-8")
        before = path.read_bytes()
        runs = self.hook.read_live_active_runs(self.home)
        self.assertEqual(["legacy", "live"], sorted(runs))
        self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
