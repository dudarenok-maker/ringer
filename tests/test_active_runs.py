#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ringer import (  # noqa: E402
    active_runs_path,
    pid_is_alive,
    read_active_runs,
    register_active_run,
    unregister_active_run,
)


class ActiveRunsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_env = os.environ.copy()
        self.addCleanup(self.restore_env)
        os.environ["RINGER_HOME"] = str(Path(self.tmp.name) / "ringer-home")

    def restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)

    def test_register_read_unregister_round_trip_with_ringer_home_override(self) -> None:
        workdir = Path(self.tmp.name) / "work"
        register_active_run("run-1", "agent-a", "Test Run", workdir)

        runs = read_active_runs()
        self.assertEqual(["run-1"], sorted(runs))
        self.assertEqual(os.getpid(), runs["run-1"]["pid"])
        self.assertEqual("agent-a", runs["run-1"]["identity"])
        self.assertEqual("Test Run", runs["run-1"]["run_name"])
        self.assertEqual(str(workdir), runs["run-1"]["workdir"])
        self.assertTrue(runs["run-1"]["started_at"])
        self.assertEqual(
            (Path(os.environ["RINGER_HOME"]) / "active-runs.json").resolve(),
            active_runs_path(),
        )

        unregister_active_run("run-1")
        self.assertEqual({}, read_active_runs())

    def test_atomic_write_leaves_valid_json_and_no_temp_file(self) -> None:
        register_active_run("run-atomic", "agent-a", "Atomic", Path(self.tmp.name))

        marker = active_runs_path()
        self.assertTrue(marker.exists())
        data = json.loads(marker.read_text(encoding="utf-8"))
        self.assertIn("run-atomic", data)
        self.assertEqual([], list(marker.parent.glob("*.tmp")))
        self.assertEqual([], list(marker.parent.glob(".*.tmp")))

    def test_dead_pid_pruned_on_read_and_write(self) -> None:
        marker = active_runs_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "dead-run": {
                        "pid": 99999999,
                        "identity": "old",
                        "run_name": "Dead",
                        "workdir": str(Path(self.tmp.name) / "dead"),
                        "started_at": "2026-07-05T00:00:00+00:00",
                    }
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual({}, read_active_runs())
        self.assertEqual({}, json.loads(marker.read_text(encoding="utf-8")))

        register_active_run("live-run", "agent-a", "Live", Path(self.tmp.name))
        data = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(["live-run"], sorted(data))


class PidIsAliveTests(unittest.TestCase):
    def test_own_process_is_alive(self) -> None:
        self.assertTrue(pid_is_alive(os.getpid()))

    def test_exited_process_is_not_alive(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(pid_is_alive(proc.pid))

    @unittest.skipUnless(sys.platform == "win32", "Windows console control events")
    def test_liveness_probe_never_sends_ctrl_c_to_the_shared_console(self) -> None:
        # On Windows os.kill(pid, 0) is GenerateConsoleCtrlEvent(CTRL_C_EVENT):
        # when the caller shares its console with any other process, every
        # process on that console gets Ctrl+C. register_active_run() probes
        # its OWN pid, so `ringer.py run` launched from any wrapper (a shell
        # script, a background task) interrupted itself and its parent a few
        # seconds after start. Reproduce in a fresh hidden console so the
        # test runner's own console is never hit, even before the fix.
        child = (
            "import os, sys; sys.path.insert(0, sys.argv[1]); "
            "from ringer import pid_is_alive; "
            "print('own', pid_is_alive(os.getpid()), "
            "'parent', pid_is_alive(os.getppid()), flush=True)"
        )
        parent = (
            "import subprocess, sys, time\n"
            "try:\n"
            "    out = subprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2]],\n"
            "                         capture_output=True, text=True, timeout=60)\n"
            "    print(out.stdout.strip(), out.stderr.strip(), flush=True)\n"
            "    time.sleep(1.0)\n"
            "    print('parent survived', flush=True)\n"
            "except KeyboardInterrupt:\n"
            "    print('parent got KeyboardInterrupt', flush=True)\n"
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        result = subprocess.run(
            [sys.executable, "-c", parent, child, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=90,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startupinfo,
        )
        self.assertIn("own True parent True", result.stdout, result.stdout + result.stderr)
        self.assertIn("parent survived", result.stdout, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
