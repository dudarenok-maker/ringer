#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import unittest
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


@unittest.skipUnless(sys.platform == "win32", "Windows process handles")
class WindowsPidSeamTests(unittest.TestCase):
    def test_protected_process_we_cannot_open_reads_alive(self) -> None:
        # The System process (pid 4) refuses PROCESS_QUERY_LIMITED_INFORMATION
        # to a non-elevated caller (ERROR_ACCESS_DENIED). It exists, we just
        # can't open it - same meaning as the POSIX PermissionError branch.
        # (Elevated, the open succeeds and it reads alive via the exit code.)
        self.assertTrue(ringer_pid.pid_is_alive(4))

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
