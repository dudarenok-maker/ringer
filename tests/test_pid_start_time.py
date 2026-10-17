#!/usr/bin/env python3
"""Process start-time guard against pid reuse in active-runs.json."""
from __future__ import annotations

import ctypes
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

def spawn_sleeper(test: unittest.TestCase) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    def reap() -> None:
        proc.kill()
        proc.wait()

    test.addCleanup(reap)
    return proc


def clock_now_in_probe_base() -> float | None:
    """Now, in the time base process_start_time() reports (independent of it)."""
    if sys.platform == "win32":
        return time.time() + 11644473600.0  # seconds from 1601 to the Unix epoch
    if sys.platform.startswith("linux"):
        with open("/proc/uptime", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    return None


@unittest.skipUnless(HAS_START_TIME, "no start-time source on this platform")
class StartTimeIsRealTests(unittest.TestCase):
    """Pins the probe to a real clock and to real process identity: a probe that
    reads the wrong field or returns a constant must fail here."""

    def test_just_spawned_child_started_within_seconds_of_now(self) -> None:
        proc = spawn_sleeper(self)
        start = ringer_pid.process_start_time(proc.pid)
        now = clock_now_in_probe_base()
        self.assertIsNotNone(start)
        self.assertLess(abs(now - start), 5.0)

    def test_a_different_process_is_not_the_recorded_one(self) -> None:
        a = spawn_sleeper(self)
        start_a = ringer_pid.process_start_time(a.pid)
        time.sleep(1.2)
        b = spawn_sleeper(self)
        start_b = ringer_pid.process_start_time(b.pid)
        self.assertFalse(ringer_pid.pid_is_alive(b.pid, start_a))
        self.assertTrue(ringer_pid.pid_is_alive(b.pid, start_b))


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


class CoerceStartTimeTests(unittest.TestCase):
    def test_usable_values_pass_through(self) -> None:
        self.assertEqual(5.5, ringer_pid.coerce_start_time(5.5))
        self.assertEqual(5.0, ringer_pid.coerce_start_time(5))

    def test_unusable_values_read_as_absent(self) -> None:
        for bad in (True, False, float("nan"), float("inf"), float("-inf"), 0, 0.0, -1, "x", None, [1], {},
                    10**400, -(10**400)):
            with self.subTest(bad=bad):
                self.assertIsNone(ringer_pid.coerce_start_time(bad))

    def test_unusable_start_time_never_marks_a_live_pid_dead(self) -> None:
        for bad in (True, float("nan"), float("inf"), float("-inf"), 0, -1, "x", 10**400, -(10**400)):
            with self.subTest(bad=bad):
                self.assertTrue(ringer_pid.pid_is_alive(os.getpid(), bad))


class BootIdTests(unittest.TestCase):
    def alive(self, recorded, current) -> bool:
        with unittest.mock.patch.object(ringer_pid, "process_boot_id", return_value=current):
            return ringer_pid.pid_is_alive(os.getpid(), None, recorded)

    def test_a_different_boot_reads_dead(self) -> None:
        self.assertFalse(self.alive("boot-a", "boot-b"))

    def test_the_same_boot_reads_alive(self) -> None:
        self.assertTrue(self.alive("boot-a", "boot-a"))

    def test_unreadable_current_boot_is_fail_open(self) -> None:
        self.assertTrue(self.alive("boot-a", None))

    def test_no_recorded_boot_is_pid_only(self) -> None:
        self.assertTrue(self.alive(None, "boot-b"))

    def test_boot_id_is_read_and_stripped(self) -> None:
        with unittest.mock.patch("builtins.open", unittest.mock.mock_open(read_data="abc-123\n")):
            self.assertEqual("abc-123", ringer_pid._read_boot_id())

    def test_unreadable_boot_id_file_is_none(self) -> None:
        with unittest.mock.patch("builtins.open", side_effect=OSError):
            self.assertIsNone(ringer_pid._read_boot_id())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_process_boot_id_is_the_real_kernel_boot_id(self) -> None:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as fh:
            expected = fh.read().strip()
        self.assertTrue(expected)
        self.assertEqual(expected, ringer_pid.process_boot_id())

    def test_process_boot_id_reads_exactly_the_kernel_boot_id_path(self) -> None:
        # Platform-independent: sys.platform is faked only around a pure file
        # read (no pid probe), and open() asserts the path it is handed.
        opened: list = []

        def fake_open(path, *args, **kwargs):
            opened.append(path)
            return unittest.mock.mock_open(read_data="the-boot-id\n")()

        with unittest.mock.patch.object(ringer_pid.sys, "platform", "linux"), \
                unittest.mock.patch("builtins.open", fake_open):
            self.assertEqual("the-boot-id", ringer_pid.process_boot_id())
        self.assertEqual(["/proc/sys/kernel/random/boot_id"], opened)

    def test_no_boot_id_on_windows(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows only")
        self.assertIsNone(ringer_pid.process_boot_id())


@unittest.skipUnless(sys.platform == "win32", "Windows ctypes path")
class WindowsStartTimeSeamTests(unittest.TestCase):
    def test_get_process_times_failure_returns_none_and_closes_handle(self) -> None:
        fake = unittest.mock.MagicMock()
        fake.OpenProcess.return_value = 99
        fake.GetProcessTimes.return_value = 0
        with unittest.mock.patch("ctypes.WinDLL", return_value=fake), \
                unittest.mock.patch.object(ringer_pid, "_nt_process_start_time", return_value=None):
            self.assertIsNone(ringer_pid._windows_process_start_time(4242))
        fake.CloseHandle.assert_called_once_with(99)

    def test_open_process_failure_returns_none(self) -> None:
        fake = unittest.mock.MagicMock()
        fake.OpenProcess.return_value = None
        with unittest.mock.patch("ctypes.WinDLL", return_value=fake), \
                unittest.mock.patch.object(ringer_pid, "_nt_process_start_time", return_value=None):
            self.assertIsNone(ringer_pid._windows_process_start_time(4242))

    def _fake_dlls(self, kernel32, ntdll=None):
        real = ctypes.WinDLL

        def factory(name, *args, **kwargs):
            if name == "kernel32":
                return kernel32
            if name == "ntdll" and ntdll is not None:
                return ntdll
            return real(name, *args, **kwargs)

        return unittest.mock.patch("ctypes.WinDLL", side_effect=factory)

    def test_system_process_start_time_is_readable_even_if_not_openable(self) -> None:
        # pid 4 (System) refuses OpenProcess for a non-elevated user; the
        # handle-free path must still answer.
        self.assertIsNotNone(ringer_pid.process_start_time(4))

    def test_nt_path_matches_get_process_times(self) -> None:
        own = os.getpid()
        via_handle = ringer_pid._windows_process_start_time(own)
        via_nt = ringer_pid._nt_process_start_time(own)
        self.assertIsNotNone(via_nt)
        self.assertLess(abs(via_handle - via_nt), 1e-3)

    def test_open_process_failure_falls_back_to_the_process_table(self) -> None:
        own = os.getpid()
        expected = ringer_pid._windows_process_start_time(own)
        denied = unittest.mock.MagicMock()
        denied.OpenProcess.return_value = None
        with self._fake_dlls(denied):
            got = ringer_pid._windows_process_start_time(own)
        self.assertIsNotNone(got)
        self.assertLess(abs(got - expected), 1e-3)

    def test_open_process_and_process_table_both_failing_is_none(self) -> None:
        denied = unittest.mock.MagicMock()
        denied.OpenProcess.return_value = None
        broken_nt = unittest.mock.MagicMock()
        broken_nt.NtQuerySystemInformation.return_value = -1073741823  # 0xC0000001
        with self._fake_dlls(denied, broken_nt):
            self.assertIsNone(ringer_pid._windows_process_start_time(os.getpid()))

    def test_process_table_larger_than_the_first_buffer_is_grown_into(self) -> None:
        # A tiny first buffer forces the real kernel to answer
        # STATUS_INFO_LENGTH_MISMATCH, so the growth loop runs for real.
        own = os.getpid()
        expected = ringer_pid._nt_process_start_time(own)
        real_nt = ctypes.WinDLL("ntdll")
        real_nt.NtQuerySystemInformation.restype = ctypes.c_long
        real_nt.NtQuerySystemInformation.argtypes = [
            ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
        ]
        spy = unittest.mock.MagicMock()
        spy.NtQuerySystemInformation.side_effect = real_nt.NtQuerySystemInformation
        with self._fake_dlls(unittest.mock.MagicMock(), spy):
            got = ringer_pid._nt_process_start_time(own, initial_size=4096)
        self.assertIsNotNone(got)
        self.assertLess(abs(got - expected), 1e-3)
        self.assertGreaterEqual(spy.NtQuerySystemInformation.call_count, 2)

    def test_permanent_length_mismatch_gives_up_as_none(self) -> None:
        stuck = unittest.mock.MagicMock()
        stuck.NtQuerySystemInformation.return_value = -1073741820  # 0xC0000004
        with self._fake_dlls(unittest.mock.MagicMock(), stuck):
            self.assertIsNone(ringer_pid._nt_process_start_time(os.getpid(), initial_size=4096))
        self.assertEqual(8, stuck.NtQuerySystemInformation.call_count)

    def test_unknown_pid_in_the_process_table_is_none(self) -> None:
        self.assertIsNone(ringer_pid._nt_process_start_time(0x7FFFFFF0))

    def test_unopenable_live_pid_with_a_dead_runs_start_reads_dead(self) -> None:
        # The ghost this guard exists for, with a reuser we cannot open
        # (svchost-style): a live protected pid, a dead run's recorded start.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, 4)
        if handle:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            self.skipTest("elevated runner can open System; no unopenable pid to use")
        live = ringer_pid.process_start_time(4)
        self.assertTrue(ringer_pid.pid_is_alive(4, live))
        self.assertFalse(ringer_pid.pid_is_alive(4, live - 3600.0))

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

    def test_malformed_pid_start_values_keep_a_live_run_and_are_not_written(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf"), 0, -1, True, "x", 10**400, -(10**400)):
            with self.subTest(bad=bad):
                self.write({"live": {"pid": os.getpid(), "pid_start": bad, "workdir": "/w"}})
                runs = ringer.read_active_runs()
                self.assertEqual(["live"], list(runs))
                self.assertNotIn("pid_start", runs["live"])

    def test_overflowing_pid_start_in_raw_json_does_not_break_read_or_register(self) -> None:
        big = "1" + "0" * 400
        self.home.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            '{"huge": {"pid": %d, "pid_start": %s, "workdir": "/w"}}' % (os.getpid(), big),
            encoding="utf-8",
        )
        runs = ringer.read_active_runs()
        self.assertEqual(["huge"], list(runs))
        self.assertNotIn("pid_start", runs["huge"])
        ringer.register_active_run("r1", "id", "name", Path(self.tmp.name))
        self.assertIn("r1", ringer.read_active_runs())

    @unittest.skipUnless(HAS_START_TIME, "no start-time source on this platform")
    def test_register_records_pid_boot_when_available(self) -> None:
        with unittest.mock.patch.object(ringer, "process_boot_id", return_value="boot-a"), \
                unittest.mock.patch.object(ringer_pid, "process_boot_id", return_value="boot-a"):
            ringer.register_active_run("r1", "id", "name", Path(self.tmp.name))
        entry = json.loads(self.path.read_text(encoding="utf-8"))["r1"]
        self.assertEqual("boot-a", entry["pid_boot"])

    def test_entry_from_a_previous_boot_is_pruned(self) -> None:
        self.write({"old": {"pid": os.getpid(), "pid_start": 123.0, "pid_boot": "boot-a", "workdir": "/w"}})
        with unittest.mock.patch.object(ringer_pid, "process_start_time", return_value=123.0), \
                unittest.mock.patch.object(ringer_pid, "process_boot_id", return_value="boot-b"):
            self.assertEqual({}, ringer.read_active_runs())

    def test_prune_keeps_pid_boot_on_surviving_entries(self) -> None:
        self.write({"ok": {"pid": os.getpid(), "pid_start": 123.0, "pid_boot": "boot-a", "workdir": "/w"}})
        with unittest.mock.patch.object(ringer_pid, "process_start_time", return_value=123.0), \
                unittest.mock.patch.object(ringer_pid, "process_boot_id", return_value="boot-a"):
            self.assertEqual("boot-a", ringer.read_active_runs()["ok"]["pid_boot"])


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

    def test_hook_keeps_live_runs_with_malformed_pid_start(self) -> None:
        path = self.home / "active-runs.json"
        path.write_text(
            '{"a": {"pid": %d, "pid_start": NaN}, "b": {"pid": %d, "pid_start": Infinity},'
            ' "c": {"pid": %d, "pid_start": 0}, "d": {"pid": %d, "pid_start": -1},'
            ' "e": {"pid": %d, "pid_start": true}}' % ((os.getpid(),) * 5),
            encoding="utf-8",
        )
        self.assertEqual(["a", "b", "c", "d", "e"], sorted(self.hook.read_live_active_runs(self.home)))

    def test_hook_keeps_live_runs_with_an_overflowing_pid_start(self) -> None:
        path = self.home / "active-runs.json"
        big = "1" + "0" * 400
        path.write_text(
            '{"a": {"pid": %d, "pid_start": %s}, "b": {"pid": %d, "pid_start": -%s}}'
            % (os.getpid(), big, os.getpid(), big),
            encoding="utf-8",
        )
        self.assertEqual(["a", "b"], sorted(self.hook.read_live_active_runs(self.home)))

    def test_hook_checks_the_boot_id_even_without_a_pid_start(self) -> None:
        # Same as prune: an entry whose pid_start was dropped keeps its pid_boot.
        path = self.home / "active-runs.json"
        path.write_text(json.dumps({
            "old": {"pid": os.getpid(), "pid_boot": "boot-a"},
            "same": {"pid": os.getpid(), "pid_boot": "boot-b"},
        }), encoding="utf-8")
        with unittest.mock.patch.object(ringer_pid, "process_boot_id", return_value="boot-b"):
            self.assertEqual(["same"], sorted(self.hook.read_live_active_runs(self.home)))

    def test_hook_drops_a_run_from_a_previous_boot(self) -> None:
        path = self.home / "active-runs.json"
        path.write_text(json.dumps({
            "old": {"pid": os.getpid(), "pid_start": 123.0, "pid_boot": "boot-a"},
            "same": {"pid": os.getpid(), "pid_start": 123.0, "pid_boot": "boot-b"},
        }), encoding="utf-8")
        with unittest.mock.patch.object(ringer_pid, "process_start_time", return_value=123.0), \
                unittest.mock.patch.object(ringer_pid, "process_boot_id", return_value="boot-b"):
            self.assertEqual(["same"], sorted(self.hook.read_live_active_runs(self.home)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
