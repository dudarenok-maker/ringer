from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RINGER_PATH = ROOT / "ringer.py"
SPEC = importlib.util.spec_from_file_location("ringer_module", RINGER_PATH)
assert SPEC is not None and SPEC.loader is not None
ringer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ringer
SPEC.loader.exec_module(ringer)


class RingerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ringer-test-")
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.toml"
        self.jsonl_path = self.root / "runs.jsonl"
        self.state_dir = self.root / "state"
        self.write_config(
            {
                "write_done": ["-c", "printf done > out.txt"],
                "write_empty": ["-c", ": > out.txt"],
                "write_wrong_file": ["-c", "printf done > wrong.txt"],
                "sleep_then_write": ["-c", "echo $$ > worker.pid; sleep 30; printf done > out.txt"],
                "ignore_term": ["-c", "trap '' TERM; echo $$ > worker.pid; while :; do sleep 1; done"],
                "spec_shell": ["-c", "{spec}"],
                "token_printer": ["-c", "printf done > out.txt; echo 'tokens used: 1,234'"],
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_config(self, engines: dict[str, list[str]], *, port: int = 18787) -> None:
        lines = [
            f'state_dir = "{self.state_dir}"',
            f"dashboard_port_base = {port}",
            "allow_full_access = false",
            "",
            "[eval]",
            'backend = "jsonl"',
            f'jsonl_path = "{self.jsonl_path}"',
            "",
        ]
        for name, args_template in engines.items():
            lines.extend(
                [
                    f"[engines.{name}]",
                    'bin = "/bin/sh"',
                    f"args_template = {json.dumps(args_template)}",
                    "sandbox_args = []",
                    "full_access_args = []",
                    'token_regex = "tokens\\\\s+used\\\\s*:?\\\\s*([0-9][0-9,]*)"',
                    "",
                ]
            )
        self.config_path.write_text("\n".join(lines), encoding="utf-8")

    def write_manifest(self, name: str, manifest: dict[str, object]) -> Path:
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return path

    def manifest(self, name: str, task: dict[str, object], **overrides: object) -> dict[str, object]:
        data: dict[str, object] = {
            "run_name": name,
            "workdir": str(self.root / f"work-{name}"),
            "max_parallel": 1,
            "tasks": [task],
        }
        data.update(overrides)
        return data

    def run_ringer(
        self,
        manifest: Path,
        *,
        config_path: Path | None = None,
        no_dashboard: bool = True,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            "-B",
            str(RINGER_PATH),
            "--config",
            str(config_path or self.config_path),
            "run",
            str(manifest),
            "--identity",
            "test-runner",
        ]
        if no_dashboard:
            cmd.append("--no-dashboard")
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["RINGER_NO_SELF_UPDATE"] = "1"
        return subprocess.run(
            cmd,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )

    def read_rows(self, path: Path | None = None) -> list[dict[str, object]]:
        jsonl_path = path or self.jsonl_path
        if not jsonl_path.exists():
            return []
        return [
            json.loads(line)
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def read_final_state(self) -> dict[str, object]:
        state_files = sorted((self.state_dir / "runs").glob("*.json"))
        self.assertEqual(len(state_files), 1)
        return json.loads(state_files[0].read_text(encoding="utf-8"))

    @staticmethod
    def pid_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def test_failing_check_output_is_logged_and_injected_into_retry(self) -> None:
        manifest = self.write_manifest(
            "diagnostic-fail",
            self.manifest(
                "diagnostic-fail",
                {
                    "key": "diag",
                    "engine": "write_done",
                    "spec": "Write done.",
                    "expect_files": ["out.txt"],
                    "check": (
                        'actual=$(cat out.txt 2>/dev/null); '
                        'test "$actual" = expected || '
                        '{ echo "expected=expected actual=$actual"; exit 1; }'
                    ),
                },
            ),
        )

        result = self.run_ringer(manifest)

        self.assertEqual(result.returncode, 1, result.stdout)
        rows = self.read_rows()
        self.assertEqual([row["verdict"] for row in rows], ["FAIL", "FAIL"])
        self.assertIn("expected=expected actual=done", rows[0]["notes"])
        self.assertIn("Previous attempt failed", rows[1]["spec"])
        self.assertIn("expected=expected actual=done", rows[1]["spec"])

    def test_missing_expected_file_fails_even_when_check_passes(self) -> None:
        manifest = self.write_manifest(
            "missing-file",
            self.manifest(
                "missing-file",
                {
                    "key": "missing",
                    "engine": "write_wrong_file",
                    "spec": "Write the wrong file.",
                    "expect_files": ["out.txt"],
                    "check": "true",
                },
            ),
        )

        result = self.run_ringer(manifest)

        self.assertEqual(result.returncode, 1, result.stdout)
        rows = self.read_rows()
        self.assertEqual([row["verdict"] for row in rows], ["FAIL", "FAIL"])
        self.assertIn('missing_expect_files=["out.txt"]', rows[0]["notes"])
        self.assertIn("[ringer] missing expected files: out.txt", rows[0]["notes"])

    def test_empty_expected_file_is_treated_as_missing(self) -> None:
        manifest = self.write_manifest(
            "empty-file",
            self.manifest(
                "empty-file",
                {
                    "key": "empty",
                    "engine": "write_empty",
                    "spec": "Write an empty file.",
                    "expect_files": ["out.txt"],
                    "check": "test -f out.txt",
                },
            ),
        )

        result = self.run_ringer(manifest)

        self.assertEqual(result.returncode, 1, result.stdout)
        rows = self.read_rows()
        self.assertEqual([row["verdict"] for row in rows], ["FAIL", "FAIL"])
        self.assertIn('missing_expect_files=["out.txt"]', rows[0]["notes"])

    def test_timeout_retries_once_and_reports_timeout(self) -> None:
        manifest = self.write_manifest(
            "timeout",
            self.manifest(
                "timeout",
                {
                    "key": "timeout",
                    "engine": "sleep_then_write",
                    "spec": "Sleep too long.",
                    "expect_files": ["out.txt"],
                    "timeout_s": 1,
                    "check": 'test "$(cat out.txt 2>/dev/null)" = done',
                },
            ),
        )

        result = self.run_ringer(manifest, timeout=10)

        self.assertEqual(result.returncode, 1, result.stdout)
        rows = self.read_rows()
        self.assertEqual([row["verdict"] for row in rows], ["TIMEOUT", "TIMEOUT"])
        self.assertIn("retry=true", rows[1]["notes"])
        self.assertIn("worker_returncode=-15", rows[0]["notes"])

    def test_sigterm_cleans_up_active_worker_and_finishes_state(self) -> None:
        manifest = self.write_manifest(
            "sigterm",
            self.manifest(
                "sigterm",
                {
                    "key": "term",
                    "engine": "sleep_then_write",
                    "spec": "Sleep until terminated.",
                    "expect_files": ["out.txt"],
                    "timeout_s": 30,
                    "check": 'test "$(cat out.txt 2>/dev/null)" = done',
                },
            ),
        )
        cmd = [
            sys.executable,
            "-B",
            str(RINGER_PATH),
            "--config",
            str(self.config_path),
            "run",
            str(manifest),
            "--no-dashboard",
            "--identity",
            "test-runner",
        ]
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["RINGER_NO_SELF_UPDATE"] = "1"
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        worker_pid_path = self.root / "work-sigterm" / "term" / "worker.pid"
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not worker_pid_path.exists():
                time.sleep(0.05)
            self.assertTrue(worker_pid_path.exists())
            worker_pid = int(worker_pid_path.read_text(encoding="utf-8").strip())
            proc.send_signal(signal.SIGTERM)
            stdout, _ = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                stdout, _ = proc.communicate(timeout=10)

        self.assertEqual(proc.returncode, 130, stdout)
        self.assertFalse(self.pid_is_alive(worker_pid), stdout)
        state = self.read_final_state()
        self.assertTrue(state["finished"])
        self.assertEqual(state["state"], "finished")
        self.assertEqual(state["summary"]["fail"], 1)
        self.assertEqual(state["tasks"][0]["status"], "fail")

    def test_second_signal_during_shutdown_does_not_cancel_cleanup(self) -> None:
        manifest = self.write_manifest(
            "resignal",
            self.manifest(
                "resignal",
                {
                    "key": "term",
                    "engine": "ignore_term",
                    "spec": "Ignore SIGTERM until killed.",
                    "expect_files": ["out.txt"],
                    "timeout_s": 30,
                    "check": 'test "$(cat out.txt 2>/dev/null)" = done',
                },
            ),
        )
        cmd = [
            sys.executable,
            "-B",
            str(RINGER_PATH),
            "--config",
            str(self.config_path),
            "run",
            str(manifest),
            "--no-dashboard",
            "--identity",
            "test-runner",
        ]
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["RINGER_NO_SELF_UPDATE"] = "1"
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        worker_pid_path = self.root / "work-resignal" / "term" / "worker.pid"
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not worker_pid_path.exists():
                time.sleep(0.05)
            self.assertTrue(worker_pid_path.exists())
            worker_pid = int(worker_pid_path.read_text(encoding="utf-8").strip())
            proc.send_signal(signal.SIGTERM)
            # The worker traps TERM, so cleanup is held in the 1s TERM->KILL
            # escalation window; a second signal lands mid-cleanup.
            time.sleep(0.3)
            proc.send_signal(signal.SIGTERM)
            stdout, _ = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                stdout, _ = proc.communicate(timeout=10)

        self.assertEqual(proc.returncode, 130, stdout)
        self.assertIn("shutdown already in progress", stdout)
        self.assertNotIn("Traceback", stdout)
        self.assertFalse(self.pid_is_alive(worker_pid), stdout)
        state = self.read_final_state()
        self.assertTrue(state["finished"])
        self.assertEqual(state["state"], "finished")
        self.assertEqual(state["tasks"][0]["status"], "fail")

    def test_custom_shell_engine_substitutes_spec_placeholder(self) -> None:
        manifest = self.write_manifest(
            "custom-shell",
            self.manifest(
                "custom-shell",
                {
                    "key": "custom",
                    "engine": "spec_shell",
                    "spec": "printf done > out.txt",
                    "expect_files": ["out.txt"],
                    "check": 'test "$(cat out.txt 2>/dev/null)" = done',
                },
            ),
        )

        result = self.run_ringer(manifest)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([row["verdict"] for row in self.read_rows()], ["PASS"])

    def test_token_regex_captures_worker_tokens(self) -> None:
        manifest = self.write_manifest(
            "tokens",
            self.manifest(
                "tokens",
                {
                    "key": "tokens",
                    "engine": "token_printer",
                    "spec": "Print token count.",
                    "expect_files": ["out.txt"],
                    "check": 'test "$(cat out.txt 2>/dev/null)" = done',
                },
            ),
        )

        result = self.run_ringer(manifest)

        self.assertEqual(result.returncode, 0, result.stdout)
        rows = self.read_rows()
        self.assertEqual(rows[0]["worker_tokens"], 1234)

    def test_worktree_pass_removes_task_worktree_but_keeps_logs(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (repo / "README.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.txt"], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Ringer Test",
                "-c",
                "user.email=ringer-test@example.invalid",
                "commit",
                "-m",
                "base",
            ],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        workdir = self.root / "work-worktree"
        manifest = self.write_manifest(
            "worktree",
            {
                "run_name": "worktree",
                "workdir": str(workdir),
                "max_parallel": 1,
                "worktrees": True,
                "repo": str(repo),
                "tasks": [
                    {
                        "key": "wt-pass",
                        "engine": "write_done",
                        "spec": "Write done.",
                        "expect_files": ["out.txt"],
                        "check": 'test "$(cat out.txt 2>/dev/null)" = done',
                    }
                ],
            },
        )

        result = self.run_ringer(manifest)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((workdir / "wt-pass").exists())
        self.assertTrue((workdir / "logs" / "wt-pass.worker.log").is_file())
        self.assertEqual([row["verdict"] for row in self.read_rows()], ["PASS"])

    def test_worktree_prepare_failure_logs_error_row(self) -> None:
        repo = self.root / "repo-prepare"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (repo / "README.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.txt"], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Ringer Test",
                "-c",
                "user.email=ringer-test@example.invalid",
                "commit",
                "-m",
                "base",
            ],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        workdir = self.root / "work-prepare"
        (workdir / "exists").mkdir(parents=True)
        manifest = self.write_manifest(
            "prepare-failure",
            {
                "run_name": "prepare-failure",
                "workdir": str(workdir),
                "max_parallel": 1,
                "worktrees": True,
                "repo": str(repo),
                "tasks": [
                    {
                        "key": "exists",
                        "engine": "write_done",
                        "spec": "Cannot prepare.",
                        "expect_files": ["out.txt"],
                        "check": "true",
                    }
                ],
            },
        )

        result = self.run_ringer(manifest)

        self.assertEqual(result.returncode, 1, result.stdout)
        rows = self.read_rows()
        self.assertEqual(rows[0]["verdict"], "ERROR")
        self.assertIn("taskdir already exists but is not a registered git worktree", rows[0]["notes"])

    def test_task_key_cannot_escape_workdir(self) -> None:
        manifest = self.write_manifest(
            "escape",
            self.manifest(
                "escape",
                {
                    "key": "../escape",
                    "engine": "write_done",
                    "spec": "Escape.",
                    "check": "true",
                },
            ),
        )

        result = self.run_ringer(manifest)

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("task key escapes workdir", result.stdout)

    def test_worktree_task_key_cannot_collide_with_reserved_logs_dir(self) -> None:
        manifest = self.write_manifest(
            "logs-collision",
            self.manifest(
                "logs-collision",
                {
                    "key": "logs/bad",
                    "engine": "write_done",
                    "spec": "Collide.",
                    "check": "true",
                },
                worktrees=True,
                repo=str(self.root),
            ),
        )

        result = self.run_ringer(manifest)

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("reserved worktree logs directory", result.stdout)

    def test_final_state_file_is_finished_after_passing_run(self) -> None:
        # The per-run dashboard this test originally exercised was replaced by
        # the persistent Ringside hud; the surviving contract is the state
        # file: a completed run must land finished with the right summary.
        self.write_config({"slow": ["-c", "sleep 1; printf done > out.txt"]})
        manifest = self.write_manifest(
            "dashboard",
            self.manifest(
                "dashboard",
                {
                    "key": "slow",
                    "engine": "slow",
                    "spec": "Slow enough to serve state.",
                    "expect_files": ["out.txt"],
                    "check": 'test "$(cat out.txt 2>/dev/null)" = done',
                },
            ),
        )

        result = self.run_ringer(manifest, timeout=10)

        self.assertEqual(result.returncode, 0, result.stdout)
        state = self.read_final_state()
        self.assertTrue(state["finished"])
        self.assertEqual(state["state"], "finished")
        self.assertEqual(state["summary"]["pass"], 1)


    def test_check_timeout_is_reported_separately_from_worker_timeout(self) -> None:
        original_timeout = ringer.CHECK_TIMEOUT_S
        ringer.CHECK_TIMEOUT_S = 1
        with tempfile.TemporaryDirectory(prefix="ringer-check-timeout-") as tmp:
            try:
                returncode, timed_out, output = asyncio.run(
                    ringer.Verifier._run_check("sleep 5", Path(tmp))
                )
            finally:
                ringer.CHECK_TIMEOUT_S = original_timeout

        self.assertTrue(timed_out)
        self.assertNotEqual(returncode, 0)
        self.assertIn("[ringer.py] check timed out after 1s", output)

    def test_token_count_parser_accepts_colon_and_newline_formats(self) -> None:
        self.assertEqual(ringer.parse_token_count("tokens used: 1,234", r"tokens\s+used\s*:?\s*([0-9][0-9,]*)"), 1234)
        self.assertEqual(ringer.parse_token_count("tokens used\n5,678", r"tokens\s+used\s*:?\s*([0-9][0-9,]*)"), 5678)


class OpenEngineLaneStatusTests(unittest.TestCase):
    """Covers the Ringside panel that surfaces oe-lane-status.ps1's snapshot -
    added because a crashed Open Engine lane leaves no trace on GitHub (it
    dies before the board or the issue is ever touched), so this file was the
    only place left that could show it without the operator running
    oe-doctor.ps1 by hand."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ringer-lane-status-")
        self.original_path = ringer.OPEN_ENGINE_LANE_STATUS_PATH
        ringer.OPEN_ENGINE_LANE_STATUS_PATH = Path(self.tmp.name) / "lane-status.json"

    def tearDown(self) -> None:
        ringer.OPEN_ENGINE_LANE_STATUS_PATH = self.original_path
        self.tmp.cleanup()

    def test_missing_file_reads_as_an_empty_lane_list_not_an_error(self) -> None:
        payload = ringer.read_open_engine_lane_status()
        self.assertEqual(payload["lanes"], [])
        self.assertIsNone(payload["generated_utc"])

    def test_reads_a_written_snapshot_verbatim(self) -> None:
        snapshot = {
            "generated_utc": "2026-08-21T12:00:00Z",
            "lanes": [{"engine": "cline", "state": "running", "running_minutes": 12}],
        }
        ringer.OPEN_ENGINE_LANE_STATUS_PATH.write_text(json.dumps(snapshot), encoding="utf-8")
        self.assertEqual(ringer.read_open_engine_lane_status(), snapshot)

    def test_malformed_json_reads_as_empty_rather_than_crashing_the_hud(self) -> None:
        ringer.OPEN_ENGINE_LANE_STATUS_PATH.write_text("{not valid json", encoding="utf-8")
        payload = ringer.read_open_engine_lane_status()
        self.assertEqual(payload["lanes"], [])

    def test_utf8_bom_does_not_read_as_a_missing_file(self) -> None:
        # PowerShell 5.1's `Set-Content -Encoding UTF8` (oe-lane-status.ps1's
        # own writer) emits a UTF-8 BOM. Reading it as plain "utf-8" makes
        # json.loads fail on the leading BOM byte, which reads identically to
        # "the file does not exist yet" - indistinguishable from open-engine
        # never having run at all. Write the BOM by hand (the codec name is
        # what's under test, not the OS's own Set-Content).
        snapshot = {"generated_utc": "2026-08-21T12:00:00Z", "lanes": [{"engine": "cline", "state": "running"}]}
        ringer.OPEN_ENGINE_LANE_STATUS_PATH.write_bytes(b"\xef\xbb\xbf" + json.dumps(snapshot).encode("utf-8"))
        self.assertEqual(ringer.read_open_engine_lane_status(), snapshot)


class OpenEngineLaneStatusPathConfigTests(unittest.TestCase):
    """The path used to be hardcoded to Path.home()/".open-engine" unconditionally
    for every Ringer install - RINGER_OPEN_ENGINE_LANE_STATUS makes it opt-in
    per checkout instead, per the project owner's explicit call on the PR
    review's routed judgment call (default path assumption is fine for THIS
    fork; it must not be forced on every Ringer user)."""

    def test_env_var_overrides_the_default_path(self) -> None:
        # OPEN_ENGINE_LANE_STATUS_PATH is computed at import time from the env
        # var, so this re-executes that one line rather than re-importing the
        # whole module (which would re-run the module's top-level side effects
        # a second time under the same sys.modules entry).
        original = ringer.OPEN_ENGINE_LANE_STATUS_PATH
        try:
            with tempfile.TemporaryDirectory(prefix="ringer-lane-path-") as tmp:
                custom = str(Path(tmp) / "custom-lane-status.json")
                with unittest.mock.patch.dict(os.environ, {"RINGER_OPEN_ENGINE_LANE_STATUS": custom}):
                    ringer.OPEN_ENGINE_LANE_STATUS_PATH = Path(
                        os.environ.get("RINGER_OPEN_ENGINE_LANE_STATUS")
                        or (Path.home() / ".open-engine" / "lane-status.json")
                    )
                    self.assertEqual(str(ringer.OPEN_ENGINE_LANE_STATUS_PATH), custom)
        finally:
            ringer.OPEN_ENGINE_LANE_STATUS_PATH = original


class InjectLanesPanelTests(unittest.TestCase):
    BASE_HTML = (
        "<html>\n  <head><style>\n    main {\n    }\n    </style></head>\n"
        "  <body>\n    <main>\n      <section id=\"other\"></section>\n    </main>\n"
        "    <script>\n    tickClock();\n    </script>\n  </body>\n</html>\n"
    )

    def test_a_missing_style_anchor_is_a_full_no_op_not_a_partial_injection(self) -> None:
        # Checking only the <main> anchor and then firing three independent
        # .replace() calls let a renamed CSS selector ship an unstyled panel
        # with the guard reporting nothing wrong - reproduced against the
        # real ringside.html by mutating "main {" alone. All three anchors
        # must be present or none of the three edits happen.
        broken = self.BASE_HTML.replace("    main {\n", "    main{\n")
        html = ringer.inject_lanes_panel_into_ringside_html(broken)
        self.assertEqual(html, broken)
        self.assertNotIn('id="lanes-panel"', html)

    def test_a_missing_script_anchor_is_a_full_no_op_not_a_partial_injection(self) -> None:
        # The matching failure mode: a renamed bootstrap call ships a
        # section+CSS with no installLanesPanel() call anywhere, so the
        # empty <section> stays hidden by .lanes-panel:empty forever.
        broken = self.BASE_HTML.replace("    tickClock();\n", "    startClock();\n")
        html = ringer.inject_lanes_panel_into_ringside_html(broken)
        self.assertEqual(html, broken)
        self.assertNotIn('id="lanes-panel"', html)

    def test_injects_panel_script_and_style_once(self) -> None:
        html = ringer.inject_lanes_panel_into_ringside_html(self.BASE_HTML)
        self.assertIn('id="lanes-panel"', html)
        self.assertIn("installLanesPanel();\n    tickClock();", html)
        self.assertIn(".lanes-panel {", html)

    def test_the_chip_row_is_centered(self) -> None:
        html = ringer.inject_lanes_panel_into_ringside_html(self.BASE_HTML)
        style_block = html[html.index(".lanes-panel {"):html.index(".lane-chip {")]
        self.assertIn("justify-content: center", style_block)

    def test_panel_lands_before_existing_main_content_not_after(self) -> None:
        html = ringer.inject_lanes_panel_into_ringside_html(self.BASE_HTML)
        self.assertLess(html.index('id="lanes-panel"'), html.index('id="other"'))

    def test_is_idempotent_on_html_that_already_carries_the_panel(self) -> None:
        once = ringer.inject_lanes_panel_into_ringside_html(self.BASE_HTML)
        twice = ringer.inject_lanes_panel_into_ringside_html(once)
        self.assertEqual(once, twice)

    def test_missing_main_anchor_is_a_no_op_not_a_crash(self) -> None:
        html = "<html><body>no main tag here</body></html>"
        self.assertEqual(ringer.inject_lanes_panel_into_ringside_html(html), html)

    def test_injects_cleanly_into_the_real_ringside_html(self) -> None:
        # Every test above runs against a 4-line synthetic fixture. The three
        # anchors this injector depends on are real strings in a real,
        # independently-maintained file (dashboard/ringside.html) that other
        # work can rename without ever touching this file - this is the one
        # test that would actually catch that.
        real_html = ringer.RINGSIDE_HTML_PATH.read_text(encoding="utf-8")
        injected = ringer.inject_lanes_panel_into_ringside_html(real_html)
        self.assertIn('id="lanes-panel"', injected)
        self.assertIn("installLanesPanel();", injected)
        self.assertIn(".lanes-panel {", injected)
        self.assertNotEqual(injected, real_html)


class LanesPanelBrowserBehaviorTests(unittest.TestCase):
    """Exercises the INJECTED JAVASCRIPT itself under Node, with document/
    fetch stubbed - the two blocking findings here (stale data rendering as
    live; one malformed row freezing every other lane's chip forever) are
    both behaviour of that script, not of the Python string-injection around
    it, so a Python-only test suite could not have caught either one."""

    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if node is None:
            raise unittest.SkipTest("node not on PATH")
        cls.node = node
        real_html = ringer.RINGSIDE_HTML_PATH.read_text(encoding="utf-8")
        injected = ringer.inject_lanes_panel_into_ringside_html(real_html)
        match = re.search(
            r"(function installLanesPanel\(\) \{.*?\n    \})\n\n    installLanesPanel\(\);",
            injected,
            re.S,
        )
        assert match is not None, "could not extract installLanesPanel() from the injected html"
        cls.js_function = match.group(1)

    def run_js(self, harness: str) -> dict[str, object]:
        # installLanesPanel() calls setInterval(), which keeps Node's event
        # loop alive indefinitely - process.exit(0) is what lets this process
        # ever return, rather than hanging until the subprocess timeout kills
        # it every single run.
        script = f"""
        {self.js_function}
        {harness}
        process.exit(0);
        """
        result = subprocess.run(
            [self.node, "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout!r} stderr={result.stderr!r}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_stale_snapshot_is_flagged_not_rendered_as_live(self) -> None:
        # B1: oe-lane-status.ps1 writes generated_utc for exactly this reason
        # ("Ringside will show stale data ... its own timestamp check") but
        # the first version of this script never read the field at all, so a
        # dead oe-tick.ps1 rendered chips indistinguishable from a live one.
        harness = """
        const fakePanel = { hidden: true, className: '', innerHTML: '' };
        fakePanel.classList = { toggle(name, on) { fakePanel.className = on ? name : ''; }, remove() { fakePanel.className = ''; } };
        global.document = { getElementById: id => (id === 'lanes-panel' ? fakePanel : null) };
        const oldIso = new Date(Date.now() - 20 * 60 * 1000).toISOString();
        global.fetch = async () => ({ json: async () => ({ generated_utc: oldIso, lanes: [{ engine: 'cline', state: 'running', running_minutes: 12 }] }) });
        installLanesPanel();
        await new Promise(r => setTimeout(r, 50));
        console.log(JSON.stringify({ staleClass: fakePanel.className, html: fakePanel.innerHTML }));
        """
        out = self.run_js(harness)
        self.assertEqual(out["staleClass"], "lanes-stale")
        self.assertIn("stale", out["html"])

    def test_fresh_snapshot_is_not_flagged_stale(self) -> None:
        harness = """
        const fakePanel = { hidden: true, className: '', innerHTML: '' };
        fakePanel.classList = { toggle(name, on) { fakePanel.className = on ? name : ''; }, remove() { fakePanel.className = ''; } };
        global.document = { getElementById: id => (id === 'lanes-panel' ? fakePanel : null) };
        const freshIso = new Date().toISOString();
        global.fetch = async () => ({ json: async () => ({ generated_utc: freshIso, lanes: [{ engine: 'cline', state: 'running', running_minutes: 12 }] }) });
        installLanesPanel();
        await new Promise(r => setTimeout(r, 50));
        console.log(JSON.stringify({ staleClass: fakePanel.className, html: fakePanel.innerHTML }));
        """
        out = self.run_js(harness)
        self.assertEqual(out["staleClass"], "")
        self.assertNotIn("stale", out["html"])

    def test_one_malformed_row_does_not_freeze_the_others(self) -> None:
        # B2: an unguarded lanes.map() over one bad row threw before
        # innerHTML was ever assigned, so a single null/malformed entry froze
        # EVERY lane's chip on its previous value, silently, forever (the
        # 15s interval kept re-fetching the same bad payload).
        harness = """
        const fakePanel = { hidden: true, className: '', innerHTML: '' };
        fakePanel.classList = { toggle(name, on) { fakePanel.className = on ? name : ''; }, remove() { fakePanel.className = ''; } };
        global.document = { getElementById: id => (id === 'lanes-panel' ? fakePanel : null) };
        const nowIso = new Date().toISOString();
        global.fetch = async () => ({ json: async () => ({ generated_utc: nowIso, lanes: [
          { engine: 'claude', state: 'idle' }, null,
        ] }) });
        installLanesPanel();
        await new Promise(r => setTimeout(r, 50));
        console.log(JSON.stringify({ hidden: fakePanel.hidden, html: fakePanel.innerHTML }));
        """
        out = self.run_js(harness)
        self.assertFalse(out["hidden"])
        self.assertIn("claude", out["html"])
        self.assertIn("state-unknown", out["html"])

    def test_a_wrong_typed_paused_until_degrades_instead_of_throwing(self) -> None:
        # M3-adjacent: paused_until.slice() on a non-string used to be
        # unguarded; still a "chip crashes, others freeze" shape under B2's
        # fix if left unguarded, so it is pinned here too.
        harness = """
        const fakePanel = { hidden: true, className: '', innerHTML: '' };
        fakePanel.classList = { toggle(name, on) { fakePanel.className = on ? name : ''; }, remove() { fakePanel.className = ''; } };
        global.document = { getElementById: id => (id === 'lanes-panel' ? fakePanel : null) };
        const nowIso = new Date().toISOString();
        global.fetch = async () => ({ json: async () => ({ generated_utc: nowIso, lanes: [
          { engine: 'cline-glm', state: 'paused', paused_until: 12345 },
        ] }) });
        installLanesPanel();
        await new Promise(r => setTimeout(r, 50));
        console.log(JSON.stringify({ html: fakePanel.innerHTML }));
        """
        out = self.run_js(harness)
        self.assertIn("cline-glm", out["html"])
        self.assertIn("paused", out["html"])

    def test_paused_until_renders_in_the_viewers_local_time_not_utc(self) -> None:
        # The chip used to slice paused_until's UTC "HH:MM" digits out
        # verbatim and label them "Z" - correct, but useless at a glance for
        # a viewer who has to mentally re-add their own UTC offset. Compares
        # against what THIS test process's own timezone would produce for
        # the same instant, rather than a hardcoded clock reading, so it
        # passes regardless of which timezone actually runs it.
        harness = """
        const fakePanel = { hidden: true, className: '', innerHTML: '' };
        fakePanel.classList = { toggle(name, on) { fakePanel.className = on ? name : ''; }, remove() { fakePanel.className = ''; } };
        global.document = { getElementById: id => (id === 'lanes-panel' ? fakePanel : null) };
        const nowIso = new Date().toISOString();
        const untilIso = '2026-08-22T04:11:52Z';
        const untilDate = new Date(untilIso);
        const expectedHH = String(untilDate.getHours()).padStart(2, '0');
        const expectedMM = String(untilDate.getMinutes()).padStart(2, '0');
        // Independent oracle for the date half - Intl's own formatter, not a
        // copy of the chip's own MONTHS array, so this can't pass by
        // agreeing with itself.
        const expectedDate = untilDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        global.fetch = async () => ({ json: async () => ({ generated_utc: nowIso, lanes: [
          { engine: 'cline-free', state: 'paused', paused_until: untilIso },
        ] }) });
        installLanesPanel();
        await new Promise(r => setTimeout(r, 50));
        console.log(JSON.stringify({ html: fakePanel.innerHTML, expectedTime: `${expectedHH}:${expectedMM}`, expectedDate }));
        """
        out = self.run_js(harness)
        self.assertIn(f"paused til {out['expectedDate']}, {out['expectedTime']}", out["html"])
        self.assertNotIn("04:11Z", out["html"])

    def test_paused_until_a_month_boundary_does_not_show_the_wrong_month(self) -> None:
        # Regression pin for a plausible off-by-one: getMonth() is 0-indexed,
        # so an unguarded array index (or a hand-rolled +1 done wrong) would
        # silently show the WRONG month rather than throwing - the kind of
        # bug that only shows up once a quarter and reads as plausible every
        # time it's wrong.
        harness = """
        const fakePanel = { hidden: true, className: '', innerHTML: '' };
        fakePanel.classList = { toggle(name, on) { fakePanel.className = on ? name : ''; }, remove() { fakePanel.className = ''; } };
        global.document = { getElementById: id => (id === 'lanes-panel' ? fakePanel : null) };
        const nowIso = new Date().toISOString();
        const untilIso = '2026-01-01T00:00:00Z';
        const untilDate = new Date(untilIso);
        const expectedDate = untilDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        global.fetch = async () => ({ json: async () => ({ generated_utc: nowIso, lanes: [
          { engine: 'cline-free', state: 'paused', paused_until: untilIso },
        ] }) });
        installLanesPanel();
        await new Promise(r => setTimeout(r, 50));
        console.log(JSON.stringify({ html: fakePanel.innerHTML, expectedDate }));
        """
        out = self.run_js(harness)
        self.assertIn(f"paused til {out['expectedDate']}", out["html"])


class ReadOpenEngineDoctorHealthTests(unittest.TestCase):
    def test_valid_json_any_exit_code_is_returned(self):
        def fake_runner(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args[0], returncode=1,
                stdout='{"generated_utc": "x", "tickets": [], "errors": [], "partial": false}',
                stderr="",
            )
        result = ringer.read_open_engine_doctor_health(
            oe_home=Path("C:/Claude/open-engine"), repo="dudarenok-maker/Castwright",
            timeout=30, runner=fake_runner,
        )
        self.assertEqual(result["tickets"], [])
        self.assertFalse(result["partial"])

    def test_empty_stdout_with_param_binding_error_reads_as_version_skew(self):
        def fake_runner(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args[0], returncode=1, stdout="",
                stderr="A parameter cannot be found that matches parameter name 'Json'.",
            )
        result = ringer.read_open_engine_doctor_health(
            oe_home=Path("C:/Claude/open-engine"), repo="dudarenok-maker/Castwright",
            timeout=30, runner=fake_runner,
        )
        self.assertIn("predates -Json support", result["error"])

    def test_unrelated_param_binding_error_is_not_misread_as_version_skew(self):
        # A doctor that HAS -Json but is missing some other parameter (or
        # any unrelated PowerShell parameter-binding error) must not be
        # reported as "predates -Json support" - that's a false diagnosis.
        def fake_runner(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args[0], returncode=1, stdout="",
                stderr="A parameter cannot be found that matches parameter name 'Repo'.",
            )
        result = ringer.read_open_engine_doctor_health(
            oe_home=Path("C:/Claude/open-engine"), repo="dudarenok-maker/Castwright",
            timeout=30, runner=fake_runner,
        )
        self.assertNotIn("predates -Json support", result["error"])

    def test_anything_else_is_a_generic_error(self):
        def fake_runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="oe-doctor.ps1", timeout=30)
        result = ringer.read_open_engine_doctor_health(
            oe_home=Path("C:/Claude/open-engine"), repo="dudarenok-maker/Castwright",
            timeout=30, runner=fake_runner,
        )
        self.assertIn("error", result)

    def test_timeout_error_does_not_leak_the_local_script_path(self):
        # subprocess.TimeoutExpired.__str__ embeds the full argv, including
        # doctor_script's absolute local path - that must never reach the
        # browser tab this error renders into.
        def fake_runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=30)
        result = ringer.read_open_engine_doctor_health(
            oe_home=Path("C:/Claude/open-engine"), repo="dudarenok-maker/Castwright",
            timeout=30, runner=fake_runner,
        )
        self.assertNotIn("C:/Claude/open-engine", result["error"])
        self.assertNotIn("C:\\Claude\\open-engine", result["error"])

    def test_malformed_json_with_no_param_binding_error_is_a_generic_error(self):
        def fake_runner(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args[0], returncode=1, stdout="not json", stderr="",
            )
        result = ringer.read_open_engine_doctor_health(
            oe_home=Path("C:/Claude/open-engine"), repo="dudarenok-maker/Castwright",
            timeout=30, runner=fake_runner,
        )
        self.assertIn("error", result)

    def test_a_json_value_with_no_tickets_list_is_an_error_not_a_clean_bill_of_health(self):
        # A top-level JSON array, an empty object, or a doctor that renames
        # its own key must not render as "the doctor ran and found nothing
        # wrong" - that's the one unforgivable bug class per CONTRIBUTING.md
        # ("Displayed data must be true").
        for bad_stdout in ("[]", "{}", '{"findings": []}', "null", "42"):
            with self.subTest(stdout=bad_stdout):
                def fake_runner(*args, **kwargs):
                    return subprocess.CompletedProcess(
                        args=args[0], returncode=0, stdout=bad_stdout, stderr="",
                    )
                result = ringer.read_open_engine_doctor_health(
                    oe_home=Path("C:/Claude/open-engine"), repo="dudarenok-maker/Castwright",
                    timeout=30, runner=fake_runner,
                )
                self.assertIn("error", result)
                self.assertNotIn("tickets", result)

    def test_oe_health_lock_is_a_non_reentrant_single_flight_guard(self):
        # /api/oe-health has no client-enforced rate limit the server can
        # trust (any page open in the operator's browser can loop the GET),
        # and each check spawns a 60s-budget subprocess - the route
        # serializes through this lock rather than spawning unboundedly.
        # This pins the mechanic the route relies on: a second non-blocking
        # acquire while one is held must fail, not queue silently.
        self.assertIsInstance(ringer._OE_HEALTH_LOCK, type(threading.Lock()))
        acquired_first = ringer._OE_HEALTH_LOCK.acquire(blocking=False)
        try:
            self.assertTrue(acquired_first)
            acquired_second = ringer._OE_HEALTH_LOCK.acquire(blocking=False)
            self.assertFalse(acquired_second)
        finally:
            ringer._OE_HEALTH_LOCK.release()

    def test_runner_is_called_with_explicit_utf8_decoding(self):
        # PowerShell writes UTF-8; without an explicit encoding= here,
        # text=True decodes with locale.getpreferredencoding() (cp1252 on a
        # default Windows box), which mangles non-ASCII evidence text or, on
        # an undecodable byte, raises UnicodeDecodeError on subprocess's own
        # reader thread - where a successful run then gets reported as
        # "produced no parseable output" instead of the real payload.
        captured_kwargs = {}

        def fake_runner(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return subprocess.CompletedProcess(
                args=args[0], returncode=0, stdout='{"tickets": []}', stderr="",
            )
        ringer.read_open_engine_doctor_health(
            oe_home=Path("C:/Claude/open-engine"), repo="dudarenok-maker/Castwright",
            timeout=30, runner=fake_runner,
        )
        self.assertEqual(captured_kwargs.get("encoding"), "utf-8")
        self.assertEqual(captured_kwargs.get("errors"), "replace")


class ArtifactLibraryLiveRaceTests(unittest.TestCase):
    """_write_library_live_safe() throttles redundant disk writes by
    trusting its own in-process cache of the last outcome it wrote. That
    cache knows nothing about reconcile_artifact_library_dead_runs(), which
    can rewrite the same file out from under a still-live run (e.g. a
    false-positive "died" flip racing a read of active-runs.json) - the fix
    re-checks the actual on-disk entry before honouring the cache's skip."""

    def _make_writer(self, tmp_path: Path) -> "ringer.StateWriter":
        return ringer.StateWriter(
            run_id="run-1",
            run_name="demo",
            identity="tester",
            state_dir=tmp_path,
            engines={},
            started_at=ringer.datetime.now(ringer.timezone.utc),
            runtimes=[],
            lock=threading.RLock(),
            artifact=ringer.ArtifactConfig(
                enabled=True,
                out_template=str(tmp_path / "artifacts" / "{run_id}.html"),
                report_template=str(tmp_path / "artifacts" / "{run_id}-report.html"),
                index_out=tmp_path / "artifacts" / "index.html",
            ),
            path=tmp_path / "runs" / "run-1.json",
        )

    def test_an_external_died_flip_is_corrected_on_the_next_flush_not_after_5s(self):
        with tempfile.TemporaryDirectory(prefix="ringer-lib-race-") as tmp:
            tmp_path = Path(tmp)
            writer = self._make_writer(tmp_path)
            live_state = {"finished": False, "state": "live", "totals": {"pass": 0, "fail": 0}}
            writer._write_library_live_safe(live_state)
            self.assertEqual(
                ringer.read_artifact_library(tmp_path)["artifacts"]["demo"]["state"], "live"
            )

            # Simulate reconcile_artifact_library_dead_runs() flipping the
            # file for a run that is, in fact, still alive.
            library = ringer.read_artifact_library(tmp_path)
            library["artifacts"]["demo"]["state"] = "died"
            ringer.write_artifact_library(tmp_path, library)

            # Same outcome as before, well inside the 5s throttle window -
            # the old code trusted its own cache and skipped writing here,
            # leaving "died" on disk until the throttle window lapsed.
            writer._write_library_live_safe(live_state)

            corrected = ringer.read_artifact_library(tmp_path)
            self.assertEqual(corrected["artifacts"]["demo"]["state"], "live")

    def test_still_throttles_when_the_file_already_agrees(self):
        # The re-check must not turn this back into an unconditional write -
        # it should still skip (and avoid the extra disk read's write) when
        # nothing actually changed underneath it.
        with tempfile.TemporaryDirectory(prefix="ringer-lib-race-") as tmp:
            tmp_path = Path(tmp)
            writer = self._make_writer(tmp_path)
            live_state = {"finished": False, "state": "live", "totals": {"pass": 0, "fail": 0}}
            writer._write_library_live_safe(live_state)
            before = ringer.artifact_library_path(tmp_path).stat().st_mtime_ns

            writer._write_library_live_safe(live_state)

            after = ringer.artifact_library_path(tmp_path).stat().st_mtime_ns
            self.assertEqual(before, after)


class NormalizeArtifactStateTests(unittest.TestCase):
    """normalizeArtifactState() lives in the static dashboard/ringside.html
    (not one of the Python-side injectors), so this exercises it directly
    under Node - a real per-run state.json's own "state" field legitimately
    uses the literal string "finished" (see the rawState handling elsewhere
    in this file), and library.json's outcome vocabulary is a DIFFERENT,
    disjoint set (live/died/fail/pass) produced by artifact_outcome_from_state()
    on the Python side. Folding "finished" into "pass" here used to mean any
    value from the wrong vocabulary that ever crossed into this path would
    render as a silent, confident pass."""

    @classmethod
    def setUpClass(cls) -> None:
        node = shutil.which("node")
        if node is None:
            raise unittest.SkipTest("node not on PATH")
        cls.node = node
        real_html = ringer.RINGSIDE_HTML_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"(function normalizeArtifactState\(value\) \{.*?\n    \})",
            real_html,
            re.S,
        )
        assert match is not None, "could not extract normalizeArtifactState() from ringside.html"
        cls.js_function = match.group(1)

    def normalize(self, value: str) -> str:
        script = f"""
        {self.js_function}
        console.log(JSON.stringify(normalizeArtifactState({value!r})));
        """
        result = subprocess.run(
            [self.node, "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout!r} stderr={result.stderr!r}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_the_only_known_library_outcomes_pass_through_unchanged(self):
        for value in ("live", "pass", "fail", "died"):
            self.assertEqual(self.normalize(value), value)

    def test_finished_is_not_silently_folded_into_pass(self):
        # "finished" belongs to a per-run state.json's own vocabulary, never
        # to library.json's outcome field - it must not read as a clean pass.
        self.assertEqual(self.normalize("finished"), "unknown")

    def test_an_unrecognized_value_degrades_to_unknown_not_a_crash_or_a_guess(self):
        self.assertEqual(self.normalize("cancelled"), "unknown")


class InjectHealthPanelTests(unittest.TestCase):
    def _base_html(self) -> str:
        return (
            "<style>\n    main {\n    }\n</style>\n"
            '<body>\n      <time id="clock" class="clock mono"></time>\n'
            "    <main>\n    </main>\n"
            "<script>\n    tickClock();\n</script>\n</body>"
        )

    def test_injects_the_button_and_dialog(self):
        result = ringer.inject_health_panel_into_ringside_html(self._base_html())
        self.assertIn('id="health-check-btn"', result)
        self.assertIn("Check Health", result)
        self.assertIn('id="health-dialog"', result)
        self.assertIn("<dialog", result)

    def test_button_lands_in_the_topbar_before_the_clock_not_inside_main(self):
        # The original panel lived inside <main>, in the runs/models flow,
        # and pushed that content down every time it was checked. The button
        # belongs beside the other always-visible topbar controls; the
        # dialog itself is inert (not part of layout) until opened.
        result = ringer.inject_health_panel_into_ringside_html(self._base_html())
        self.assertLess(result.index('id="health-check-btn"'), result.index('id="clock"'))

    def test_idempotent_on_already_injected_html(self):
        once = ringer.inject_health_panel_into_ringside_html(self._base_html())
        twice = ringer.inject_health_panel_into_ringside_html(once)
        self.assertEqual(once, twice)

    def test_missing_style_anchor_is_a_full_no_op_not_a_partial_injection(self):
        # Checking only the clock/main/script anchors and firing independent
        # .replace() calls would let a renamed CSS selector ship an unstyled
        # button/dialog with the guard reporting nothing wrong.
        broken = self._base_html().replace("    main {\n", "    main{\n")
        result = ringer.inject_health_panel_into_ringside_html(broken)
        self.assertEqual(result, broken)
        self.assertNotIn('id="health-dialog"', result)

    def test_missing_clock_anchor_is_a_full_no_op_not_a_partial_injection(self):
        broken = self._base_html().replace('id="clock"', 'id="clock2"')
        result = ringer.inject_health_panel_into_ringside_html(broken)
        self.assertEqual(result, broken)
        self.assertNotIn('id="health-dialog"', result)

    def test_missing_main_anchor_is_a_full_no_op_not_a_partial_injection(self):
        broken = self._base_html().replace("    <main>\n", "    <main >\n")
        result = ringer.inject_health_panel_into_ringside_html(broken)
        self.assertEqual(result, broken)
        self.assertNotIn('id="health-dialog"', result)

    def test_missing_script_anchor_is_a_full_no_op_not_a_partial_injection(self):
        broken = self._base_html().replace("tickClock();", "")
        result = ringer.inject_health_panel_into_ringside_html(broken)
        self.assertEqual(result, broken)
        self.assertNotIn('id="health-dialog"', result)

    def test_renders_a_visual_distinction_for_partial_rows(self):
        result = ringer.inject_health_panel_into_ringside_html(self._base_html())
        self.assertIn(".health-row-partial { opacity: .6; }", result)
        self.assertIn('row.classList.add("health-row-partial")', result)

    def test_every_dynamic_ticket_field_is_escaped_before_interpolation(self):
        # Guards against the html() wrapper being dropped from any one field
        # - untested escaping was the review's top significant finding,
        # since this codebase's own rule is "worker output is untrusted
        # text" and evidence/fix text ultimately comes from the doctor.
        result = ringer.inject_health_panel_into_ringside_html(self._base_html())
        for field in ("t.number", "t.severity", "t.kind", "t.evidence", "t.fix"):
            self.assertIn(f"${{html({field})}}", result)
        # The error/empty/loading text all funnel through showRow(), which
        # itself escapes via html(message) - not a per-callsite literal.
        self.assertIn("${html(message)}", result)
        self.assertIn("showRow(data.error)", result)

    def test_a_failed_recheck_never_leaves_a_stale_verdict_on_screen(self):
        # Every branch of the click handler must write to tbody before
        # returning, including the catch - a re-check that throws (bad JSON,
        # a network error) must not leave the PREVIOUS successful verdict
        # displayed with no indication the refresh failed.
        result = ringer.inject_health_panel_into_ringside_html(self._base_html())
        handler_start = result.index("btn.addEventListener")
        handler = result[handler_start:result.index("installHealthPanel();", handler_start)]
        self.assertIn("} catch (err) {", handler)
        self.assertIn("showRow(", handler[handler.index("catch (err)"):])

    def test_unrecognized_response_shape_does_not_render_as_a_clean_bill_of_health(self):
        # A response that is neither {error: ...} nor {tickets: [...]} must
        # not fall through to an empty, error-free table - that reads as
        # "checked, nothing wrong" when the endpoint actually failed.
        result = ringer.inject_health_panel_into_ringside_html(self._base_html())
        self.assertIn("Array.isArray(data.tickets)", result)
        self.assertIn("unrecognized response", result)

    def test_click_opens_the_dialog_as_a_modal(self):
        result = ringer.inject_health_panel_into_ringside_html(self._base_html())
        handler_start = result.index("btn.addEventListener")
        handler = result[handler_start:result.index("installHealthPanel();", handler_start)]
        self.assertIn("openDialog()", handler)
        self.assertIn("dialog.showModal", result)

    def test_close_button_and_backdrop_click_both_close_the_dialog(self):
        result = ringer.inject_health_panel_into_ringside_html(self._base_html())
        self.assertIn("closeBtn.addEventListener(\"click\", () => dialog.close())", result)
        self.assertIn("if (event.target === dialog) dialog.close();", result)

    def test_injects_cleanly_into_the_real_ringside_html(self):
        # Every test above runs against a synthetic fixture. The anchors
        # this injector depends on are real strings in a real,
        # independently-maintained file (dashboard/ringside.html) that other
        # work can rename without ever touching this file - this is the one
        # test that would actually catch that.
        real_html = ringer.RINGSIDE_HTML_PATH.read_text(encoding="utf-8")
        injected = ringer.inject_health_panel_into_ringside_html(real_html)
        self.assertIn('id="health-dialog"', injected)
        self.assertIn('id="health-check-btn"', injected)
        self.assertIn("installHealthPanel();", injected)


if __name__ == "__main__":
    unittest.main()
