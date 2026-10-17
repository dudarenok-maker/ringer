"""Process-liveness probe shared by ringer.py and hooks/ringer_nudge.py.

Kept as a tiny stdlib-only module (no import-time side effects) so the nudge
hook, which runs on every probing tool call and may run under an older
`python3` than ringer.py supports, can import it without loading ringer.py.
"""
from __future__ import annotations

import os
import sys


def _windows_pid_is_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Mirrors the POSIX PermissionError branch: it exists, we just can't open it.
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        # A process that exited with code 259 reads as alive - an accepted
        # limitation of GetExitCodeProcess.
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


# Seconds. A recorded start time matches the live one within this tolerance:
# the source resolutions are 100 ns (Windows FILETIME) and 10 ms (Linux clock
# ticks), and the value round-trips through JSON exactly, so this only absorbs
# jitter. A genuinely reused pid belongs to a process created after the
# recorded one died, i.e. at least the run's lifetime later.
START_TIME_TOLERANCE_S = 1.0


def _windows_process_start_time(pid: int) -> float | None:
    """Creation time (seconds since 1601-01-01 UTC) via GetProcessTimes."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        return ((created.dwHighDateTime << 32) | created.dwLowDateTime) / 1e7
    finally:
        kernel32.CloseHandle(handle)


def _parse_proc_stat_starttime(text: str, clock_ticks: int) -> float | None:
    # `comm` (field 2) is parenthesised and may itself contain spaces and
    # parens, so split after the LAST ')'. The remainder starts at field 3
    # (state); starttime is field 22, i.e. index 19 of that remainder.
    _, sep, rest = text.rpartition(")")
    if not sep:
        return None
    fields = rest.split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19]) / clock_ticks
    except ValueError:
        return None


def _linux_process_start_time(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        return _parse_proc_stat_starttime(text, os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError):
        return None


def process_start_time(pid: int) -> float | None:
    """An opaque, comparable start time for `pid`, or None if unavailable.

    Only meaningful compared against another value from this same function on
    the same machine (Windows: seconds since 1601; Linux: seconds since boot).
    macOS and other platforms have no cheap stdlib source, so they return None
    and callers fall back to the pid-only check.
    """
    if pid <= 0:
        return None
    try:
        if sys.platform == "win32":
            return _windows_process_start_time(pid)
        if sys.platform.startswith("linux"):
            return _linux_process_start_time(pid)
    except Exception:
        return None
    return None


def pid_is_alive(pid: int, start_time: float | None = None) -> bool:
    """True if `pid` is running; with `start_time`, only if it is the SAME process.

    `start_time` is a value recorded earlier from process_start_time(). A live
    pid whose start time differs was reused by an unrelated process, so the
    recorded one is dead. If no start time was recorded, or the live one cannot
    be read (unsupported platform, access denied, API failure), this degrades
    to the plain pid check: an unverifiable run is never declared dead.
    """
    if not _pid_exists(pid):
        return False
    if start_time is None:
        return True
    live_start = process_start_time(pid)
    if live_start is None:
        return True
    return abs(live_start - start_time) <= START_TIME_TOLERANCE_S


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # NEVER os.kill(pid, 0) here: on Windows signal 0 is CTRL_C_EVENT, so
        # it calls GenerateConsoleCtrlEvent and interrupts every process on the
        # caller's console - register_active_run() probing its own pid killed
        # `ringer.py run` and its launcher whenever they shared a console.
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
