#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer  # noqa: E402
import ringer_pid  # noqa: E402


def process_handle_count() -> int:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    count = wintypes.DWORD()
    if not kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)):
        raise OSError(ctypes.get_last_error(), "GetProcessHandleCount failed")
    return count.value


class RingerPidModuleTests(unittest.TestCase):
    def test_ringer_reexports_the_standalone_probe(self) -> None:
        self.assertIs(ringer.pid_is_alive, ringer_pid.pid_is_alive)

    def test_non_positive_pid_is_not_alive(self) -> None:
        self.assertFalse(ringer_pid.pid_is_alive(0))
        self.assertFalse(ringer_pid.pid_is_alive(-5))


class RingerSiblingImportTests(unittest.TestCase):
    """ringer.py must find ringer_pid.py however it is launched."""

    def run_py(self, args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "RINGER_NO_SELF_UPDATE": "1", "PYTHONPATH": ""},
        )

    def test_isolated_mode_from_another_cwd_still_imports_the_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            for flags in (["-I"], ["-P"]):
                proc = self.run_py([*flags, str(ROOT / "ringer.py"), "--help"], other)
                self.assertEqual(0, proc.returncode, f"{flags}: {proc.stderr}")

    def test_safe_path_env_still_imports_the_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "ringer.py"), "--help"],
                cwd=other,
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "RINGER_NO_SELF_UPDATE": "1", "PYTHONSAFEPATH": "1", "PYTHONPATH": ""},
            )
            self.assertEqual(0, proc.returncode, proc.stderr)

    def test_loading_ringer_by_path_from_another_cwd_imports_the_sibling(self) -> None:
        # What a test runner's by-path collection does, stdlib only (the suite
        # must not need pytest). sys.modules registration is required or the
        # module's @dataclass definitions raise.
        loader = (
            "import importlib.util,sys;"
            "s=importlib.util.spec_from_file_location('ringer_under_test', sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);"
            "sys.modules[s.name]=m;"
            "s.loader.exec_module(m)"
        )
        with tempfile.TemporaryDirectory() as other:
            proc = self.run_py(["-c", loader, str(ROOT / "ringer.py")], other)
            self.assertEqual(0, proc.returncode, proc.stderr[-800:])

    def test_standalone_copy_without_the_sibling_names_ringer_pid(self) -> None:
        with tempfile.TemporaryDirectory() as lone:
            shutil.copy(ROOT / "ringer.py", lone)
            proc = self.run_py([str(Path(lone) / "ringer.py"), "--help"], lone)
            self.assertNotEqual(0, proc.returncode)
            self.assertIn("ringer_pid.py", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)


@unittest.skipUnless(sys.platform == "win32", "Windows process handles")
class WindowsPidSeamTests(unittest.TestCase):
    def test_protected_process_we_cannot_open_reads_alive(self) -> None:
        # The System process (pid 4) refuses PROCESS_QUERY_LIMITED_INFORMATION
        # to a non-elevated caller (ERROR_ACCESS_DENIED). It exists, we just
        # can't open it - same meaning as the POSIX PermissionError branch.
        # (Elevated, the open succeeds and it reads alive via the exit code.)
        self.assertTrue(ringer_pid.pid_is_alive(4))

    def open_process_failing_with(self, last_error: int) -> bool:
        # Pins the OpenProcess failure mapping regardless of elevation (pid 4
        # opens fine on an elevated runner, so the test above cannot).
        fake_kernel32 = unittest.mock.MagicMock()
        fake_kernel32.OpenProcess.return_value = None
        with unittest.mock.patch("ctypes.WinDLL", return_value=fake_kernel32), \
                unittest.mock.patch("ctypes.get_last_error", return_value=last_error):
            return ringer_pid._windows_pid_is_alive(4242)

    def test_open_process_access_denied_reads_alive(self) -> None:
        self.assertTrue(self.open_process_failing_with(5))  # ERROR_ACCESS_DENIED

    def test_open_process_invalid_parameter_reads_dead(self) -> None:
        self.assertFalse(self.open_process_failing_with(87))  # ERROR_INVALID_PARAMETER

    def test_probe_does_not_leak_process_handles(self) -> None:
        ringer_pid.pid_is_alive(os.getpid())  # warm any lazy loader state
        before = process_handle_count()
        for _ in range(500):
            ringer_pid.pid_is_alive(os.getpid())
        after = process_handle_count()
        self.assertLess(after - before, 50, f"handles {before} -> {after}")

    def test_dead_pid_probe_does_not_leak_process_handles(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        before = process_handle_count()
        for _ in range(500):
            self.assertFalse(ringer_pid.pid_is_alive(proc.pid))
        after = process_handle_count()
        self.assertLess(after - before, 50, f"handles {before} -> {after}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
