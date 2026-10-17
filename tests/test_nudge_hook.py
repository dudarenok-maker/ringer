#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "ringer_nudge.py"
NUDGE_TEXT = (
    "Ringer routing check: this looks like swarm-shaped work happening inline "
    "(model call/harness/edit loop outside a live Ringer run). Load the ringer "
    "skill and route it as a manifest — a single task is a one-task manifest. "
    "If the user explicitly asked for inline work, proceed."
)


class NudgeHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.ringer_home = Path(self.temp.name) / "ringer"
        self.home.mkdir()
        self.ringer_home.mkdir()

    def run_hook(self, mode: str, payload: object | str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        env["RINGER_HOME"] = str(self.ringer_home)
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, str(HOOK), mode],
            input=stdin,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def pre_bash_payload(
        self, command: str, session_id: str = "session-1", cwd: str | None = None
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        if cwd is not None:
            payload["cwd"] = cwd
        return payload

    def post_edit_payload(
        self,
        file_path: str,
        session_id: str = "session-1",
        tool_name: str = "Edit",
    ) -> dict[str, object]:
        return {
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": {"file_path": file_path},
            "tool_response": {"success": True},
        }

    def assertNudged(self, proc: subprocess.CompletedProcess[str], event_name: str) -> None:
        self.assertEqual(0, proc.returncode)
        data = json.loads(proc.stdout)
        self.assertEqual(
            {
                "hookEventName": event_name,
                "additionalContext": NUDGE_TEXT,
            },
            data["hookSpecificOutput"],
        )

    def assertSilent(self, proc: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(0, proc.returncode)
        self.assertEqual("", proc.stdout)
        self.assertEqual("", proc.stderr)

    def write_active_run(self, pid: int, workdir: str | None = "/tmp/live-ringer-work") -> None:
        path = self.ringer_home / "active-runs.json"
        entry = {
            "pid": pid,
            "identity": "test-agent",
            "run_name": "test-run",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if workdir is not None:  # None models a legacy entry without a workdir
            entry["workdir"] = workdir
        path.write_text(json.dumps({"run-live": entry}), encoding="utf-8")

    def dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_pre_bash_nudges_on_harness_script(self) -> None:
        proc = self.run_hook("pre-bash", self.pre_bash_payload("node probe-simulate.mjs"))
        self.assertNudged(proc, "PreToolUse")

    def test_pre_bash_nudges_on_provider_endpoint(self) -> None:
        payload = self.pre_bash_payload("curl https://api.anthropic.com/v1/messages", "session-2")
        proc = self.run_hook("pre-bash", payload)
        self.assertNudged(proc, "PreToolUse")

    def test_pre_bash_stays_silent_on_ordinary_command(self) -> None:
        proc = self.run_hook("pre-bash", self.pre_bash_payload("ls -la"))
        self.assertSilent(proc)

    def test_pre_bash_stays_silent_when_command_contains_ringer_py(self) -> None:
        proc = self.run_hook("pre-bash", self.pre_bash_payload("python3 ringer.py run manifest.json"))
        self.assertSilent(proc)

    def test_pre_bash_stays_silent_when_cwd_is_inside_live_run_workdir(self) -> None:
        self.write_active_run(os.getpid(), "/tmp/live-ringer-work")
        for cwd in ("/tmp/live-ringer-work", "/tmp/live-ringer-work/tasks/a"):
            payload = self.pre_bash_payload("node probe-simulate.mjs", f"s-{cwd}", cwd=cwd)
            self.assertSilent(self.run_hook("pre-bash", payload))

    def test_pre_bash_nudges_when_live_run_is_elsewhere(self) -> None:
        # An unrelated interactive session must keep its nudge while some other
        # run (e.g. an Open Engine lane) is live.
        self.write_active_run(os.getpid(), "/tmp/live-ringer-work")
        payload = self.pre_bash_payload("node probe-simulate.mjs", cwd="/home/me/project")
        self.assertNudged(self.run_hook("pre-bash", payload), "PreToolUse")

    def test_pre_bash_nudges_for_prefix_sibling_of_run_workdir(self) -> None:
        self.write_active_run(os.getpid(), "/tmp/live-ringer-work")
        payload = self.pre_bash_payload("node probe-simulate.mjs", cwd="/tmp/live-ringer-work2")
        self.assertNudged(self.run_hook("pre-bash", payload), "PreToolUse")

    def test_pre_bash_nudges_when_run_is_dead(self) -> None:
        self.write_active_run(self.dead_pid(), "/tmp/live-ringer-work")
        payload = self.pre_bash_payload("node probe-simulate.mjs", cwd="/tmp/live-ringer-work")
        self.assertNudged(self.run_hook("pre-bash", payload), "PreToolUse")

    def test_pre_bash_nudges_for_legacy_entry_without_workdir(self) -> None:
        self.write_active_run(os.getpid(), None)
        payload = self.pre_bash_payload("node probe-simulate.mjs", cwd="/tmp/live-ringer-work")
        self.assertNudged(self.run_hook("pre-bash", payload), "PreToolUse")

    def test_pre_bash_nudges_when_payload_has_no_cwd(self) -> None:
        self.write_active_run(os.getpid(), "/tmp/live-ringer-work")
        proc = self.run_hook("pre-bash", self.pre_bash_payload("node probe-simulate.mjs"))
        self.assertNudged(proc, "PreToolUse")

    def test_post_edit_silent_only_inside_live_run_workdir(self) -> None:
        self.write_active_run(os.getpid(), "/tmp/live-ringer-work")
        for i in range(8):
            payload = self.post_edit_payload(f"/tmp/f{i % 3}.py", "inside")
            payload["cwd"] = "/tmp/live-ringer-work/sub"
            self.assertSilent(self.run_hook("post-edit", payload))
        for i in range(7):
            payload = self.post_edit_payload(f"/tmp/f{i % 3}.py", "outside")
            payload["cwd"] = "/home/me/project"
            self.assertSilent(self.run_hook("post-edit", payload))
        payload = self.post_edit_payload("/tmp/f2.py", "outside")
        payload["cwd"] = "/home/me/project"
        self.assertNudged(self.run_hook("post-edit", payload), "PostToolUse")

    def load_hook(self):
        spec = importlib.util.spec_from_file_location("ringer_nudge_paths", HOOK)
        assert spec is not None and spec.loader is not None
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        return hook

    def test_path_inside_handles_windows_spellings(self) -> None:
        inside = self.load_hook().path_is_inside
        wd = "C:\\Work\\Run1"
        self.assertTrue(inside("c:\\work\\run1", wd))  # drive + dir case
        self.assertTrue(inside("C:/Work/Run1/", wd))  # forward slashes, trailing slash
        self.assertTrue(inside("C:\\Work\\Run1\\tasks\\a", wd))
        self.assertTrue(inside("C:\\Work\\Run1\\tasks\\..\\x", wd))  # .. resolved
        self.assertTrue(inside("C:\\Work\\Run1", "c:/work/run1/"))
        self.assertFalse(inside("C:\\Work\\Run1\\..\\Other", wd))  # .. escapes
        self.assertFalse(inside("C:\\Work\\Run12", wd))  # prefix sibling
        self.assertFalse(inside("D:\\Work\\Run1", wd))  # other drive
        self.assertFalse(inside("C:\\Work", wd))  # parent
        self.assertFalse(inside("", wd))
        self.assertFalse(inside("C:\\Work\\Run1", ""))

    def test_path_inside_posix_is_case_sensitive(self) -> None:
        inside = self.load_hook().path_is_inside
        self.assertTrue(inside("/a/b/c", "/a/b/"))
        self.assertFalse(inside("/a/B/c", "/a/b"))
        self.assertFalse(inside("/a/bc", "/a/b"))

    def test_pre_bash_dedupes_per_session(self) -> None:
        first = self.run_hook("pre-bash", self.pre_bash_payload("node probe-simulate.mjs"))
        second = self.run_hook("pre-bash", self.pre_bash_payload("curl https://api.openai.com/v1/chat/completions"))
        self.assertNudged(first, "PreToolUse")
        self.assertSilent(second)

    def test_post_edit_nudges_at_eight_edits_and_three_files_once(self) -> None:
        files = [
            "/tmp/a.py",
            "/tmp/a.py",
            "/tmp/b.py",
            "/tmp/b.py",
            "/tmp/a.py",
            "/tmp/b.py",
            "/tmp/a.py",
        ]
        for file_path in files:
            self.assertSilent(self.run_hook("post-edit", self.post_edit_payload(file_path)))

        nudged = self.run_hook("post-edit", self.post_edit_payload("/tmp/c.py"))
        self.assertNudged(nudged, "PostToolUse")

        again = self.run_hook("post-edit", self.post_edit_payload("/tmp/d.py"))
        self.assertSilent(again)

    def test_post_edit_stays_silent_for_seven_edits_and_two_files(self) -> None:
        files = ["/tmp/a.py", "/tmp/b.py", "/tmp/a.py", "/tmp/b.py", "/tmp/a.py", "/tmp/b.py", "/tmp/a.py"]
        for file_path in files:
            self.assertSilent(self.run_hook("post-edit", self.post_edit_payload(file_path, "session-2")))

    @unittest.skipUnless(sys.platform == "win32", "os.kill(pid, 0) is only a Ctrl+C on Windows")
    def test_pid_probe_never_calls_os_kill_on_windows(self) -> None:
        # On Windows os.kill(pid, 0) is GenerateConsoleCtrlEvent(CTRL_C_EVENT)
        # (a Ctrl+C to a pid sharing the caller's console) and raises OSError
        # (WinError 87) for a dead pid, which the hook would swallow without
        # filtering the entry out.
        spec = importlib.util.spec_from_file_location("ringer_nudge_under_test", HOOK)
        assert spec is not None and spec.loader is not None
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        with unittest.mock.patch("os.kill", side_effect=AssertionError("os.kill used as a probe")):
            self.assertTrue(hook.pid_is_alive(os.getpid()))
            self.assertFalse(hook.pid_is_alive(0))
            self.assertFalse(hook.pid_is_alive("not-a-pid"))

    def test_pid_probe_does_not_import_ringer(self) -> None:
        # The hook runs on every probing tool call; importing all of ringer.py
        # costs ~300 ms and drags in its Python 3.12 SystemExit. The probe lives
        # in the small standalone ringer_pid module.
        code = (
            "import importlib.util, os, sys\n"
            "spec = importlib.util.spec_from_file_location('hook', sys.argv[1])\n"
            "hook = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(hook)\n"
            "assert hook.pid_is_alive(os.getpid())\n"
            "assert not hook.pid_is_alive(0)\n"
            "print('ringer' in sys.modules, 'ringer_pid' in sys.modules)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, str(HOOK)],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "HOME": str(self.home), "RINGER_HOME": str(self.ringer_home)},
        )
        self.assertEqual("False True", proc.stdout.strip(), proc.stdout + proc.stderr)

    def load_hook(self):
        spec = importlib.util.spec_from_file_location("ringer_nudge_under_test", HOOK)
        assert spec is not None and spec.loader is not None
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        return hook

    def test_read_live_active_runs_never_rewrites_the_file(self) -> None:
        # The hook holds no _active_runs_lock, so a prune-write could drop a run
        # registered between its read and its write. Ringer's locked paths prune.
        path = self.ringer_home / "active-runs.json"
        live = {"pid": os.getpid(), "workdir": "/live", "run_name": "live"}
        dead = {"pid": self.dead_pid(), "workdir": "/dead", "run_name": "dead"}
        path.write_text(json.dumps({"run-live": live, "run-dead": dead}), encoding="utf-8")
        before = path.read_bytes()
        runs = self.load_hook().read_live_active_runs(self.ringer_home)
        self.assertEqual(["run-live"], list(runs))
        self.assertEqual(before, path.read_bytes())

    def test_register_between_hook_read_and_probe_survives(self) -> None:
        # Forced interleave: a separate process registers a run after the hook
        # has read the file but before it finishes probing.
        path = self.ringer_home / "active-runs.json"
        dead = {"pid": self.dead_pid(), "workdir": "/dead", "run_name": "dead"}
        path.write_text(json.dumps({"run-dead": dead}), encoding="utf-8")
        hook = self.load_hook()
        real_probe = hook.pid_is_alive
        registered = []

        def probe_then_register(pid, *probe_args):
            if not registered:
                code = (
                    "import sys\n"
                    f"sys.path.insert(0, {str(ROOT)!r})\n"
                    "import os, ringer\n"
                    "from pathlib import Path\n"
                    "ringer.register_active_run('lane-C', 'id', 'lane-C', Path('/c'),"
                    " pid=int(sys.argv[1]))\n"
                )
                proc = subprocess.run(
                    [sys.executable, "-c", code, str(os.getpid())],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={
                        **os.environ,
                        "HOME": str(self.home),
                        "USERPROFILE": str(self.home),
                        "RINGER_HOME": str(self.ringer_home),
                        "RINGER_NO_SELF_UPDATE": "1",
                    },
                )
                self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
                registered.append(True)
            return real_probe(pid, *probe_args)

        with unittest.mock.patch.object(hook, "pid_is_alive", probe_then_register):
            hook.read_live_active_runs(self.ringer_home)
        self.assertIn("lane-C", json.loads(path.read_text(encoding="utf-8")))

    def test_malformed_stdin_exits_zero_silently(self) -> None:
        proc = self.run_hook("pre-bash", "{not json")
        self.assertSilent(proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
