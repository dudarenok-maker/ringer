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


def pid_is_alive(pid: int) -> bool:
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
