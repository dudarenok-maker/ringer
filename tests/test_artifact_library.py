#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from unittest import mock
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer  # noqa: E402
from ringer import (  # noqa: E402
    CSP_META_TAG,
    ArtifactConfig,
    Dashboard,
    EngineConfig,
    StateWriter,
    TaskRuntime,
    TaskSpec,
    artifact_library_path,
    artifact_live_path,
    artifact_version_path,
    read_artifact_library,
    update_artifact_library_live,
    reconcile_artifact_library_dead_runs,
)


class ArtifactLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_env = os.environ.copy()
        self.addCleanup(self.restore_env)
        self.root = Path(self.tmp.name)
        os.environ["HOME"] = str(self.root / "home")
        os.environ["RINGER_HOME"] = str(self.root / "ringer-home")
        self.state_dir = self.root / "state"
        self.workdir = self.root / "work"
        self.engine = EngineConfig(
            name="mock",
            bin=sys.executable,
            args_template=("-c", "pass"),
            full_access_args=(),
            sandbox_args=(),
        )
        self.artifact = ArtifactConfig(
            enabled=True,
            out_template=str(self.state_dir / "artifacts" / "{run_id}.html"),
            report_template=str(self.state_dir / "artifacts" / "{run_id}-report.html"),
            index_out=self.state_dir / "artifacts" / "index.html",
        )

    def restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)

    def runtime(self, key: str = "task-one", status: str = "running") -> TaskRuntime:
        taskdir = self.workdir / key
        taskdir.mkdir(parents=True, exist_ok=True)
        log_path = taskdir / "worker.log"
        log_path.write_text(f"{key} log\n", encoding="utf-8")
        runtime = TaskRuntime(
            task=TaskSpec(
                key=key,
                spec="Write the requested file.",
                check="test -s worker.log || { echo FAIL: missing log; exit 1; }",
                engine="mock",
            ),
            taskdir=taskdir,
            log_path=log_path,
            status=status,
            attempts=1,
            spec_short="write file",
        )
        runtime.started_at_monotonic = 1.0
        runtime.ended_at_monotonic = 2.0 if status in {"pass", "fail"} else None
        return runtime

    def writer(
        self,
        *,
        run_id: str = "run-1",
        run_name: str = "Library Run",
        identity: str = "test-agent",
        runtimes: list[TaskRuntime] | None = None,
    ) -> StateWriter:
        return StateWriter(
            run_id,
            run_name,
            identity,
            self.state_dir,
            {"mock": self.engine},
            datetime(2026, 7, 5, tzinfo=timezone.utc),
            runtimes if runtimes is not None else [self.runtime()],
            ringer.threading.RLock(),
            artifact=self.artifact,
        )

    def library_entry(self, run_name: str = "Library Run") -> dict[str, object]:
        library = read_artifact_library(self.state_dir)
        return library["artifacts"][run_name]

    def test_stable_live_path_rewritten_across_flushes(self) -> None:
        runtime = self.runtime(status="running")
        writer = self.writer(runtimes=[runtime])

        writer.flush()
        live_path = artifact_live_path(self.state_dir, "Library Run")
        first = live_path.read_text(encoding="utf-8")
        self.assertTrue(writer.artifact_path.exists())
        self.assertEqual(live_path, Path(self.library_entry()["live_path"]))

        runtime.status = "pass"
        runtime.final_verdict = "PASS"
        runtime.ended_at_monotonic = 3.0
        writer.flush()

        second = live_path.read_text(encoding="utf-8")
        self.assertEqual(live_path, artifact_live_path(self.state_dir, "Library Run"))
        self.assertNotEqual(first, second)
        self.assertIn("finished and checked", second)

    def test_finished_run_appends_version_and_updates_state(self) -> None:
        runtime = self.runtime(status="pass")
        writer = self.writer(runtimes=[runtime])

        writer.finish()

        entry = self.library_entry()
        self.assertEqual("pass", entry["state"])
        self.assertEqual("run-1", entry["current_run_id"])
        self.assertEqual("test-agent", entry["identity"])
        versions = entry["versions"]
        self.assertEqual(1, len(versions))
        self.assertEqual("run-1", versions[0]["run_id"])
        self.assertEqual("pass", versions[0]["outcome"])
        self.assertEqual(1, versions[0]["tasks_pass"])
        self.assertEqual(0, versions[0]["tasks_fail"])
        self.assertTrue(Path(versions[0]["path"]).exists())
        self.assertTrue(Path(versions[0]["report_path"]).exists())

    def test_same_run_name_keeps_one_entry_with_two_versions_and_fresh_live_page(self) -> None:
        first_runtime = self.runtime(key="first-task", status="pass")
        self.writer(run_id="run-one", runtimes=[first_runtime]).finish()

        second_runtime = self.runtime(key="second-task", status="fail")
        second_runtime.final_verdict = "FAIL"
        self.writer(run_id="run-two", runtimes=[second_runtime]).finish()

        library = read_artifact_library(self.state_dir)
        self.assertEqual(["Library Run"], sorted(library["artifacts"]))
        entry = library["artifacts"]["Library Run"]
        self.assertEqual("fail", entry["state"])
        self.assertEqual("run-two", entry["current_run_id"])
        self.assertEqual(["run-two", "run-one"], [item["run_id"] for item in entry["versions"]])
        live_html = Path(entry["live_path"]).read_text(encoding="utf-8")
        self.assertIn("second-task", live_html)
        self.assertNotIn("first-task", live_html)

    def test_version_cap_prunes_oldest_files(self) -> None:
        old_version_paths = []
        for index in range(22):
            run_id = f"run-{index:02d}"
            runtime = self.runtime(key=f"task-{index:02d}", status="pass")
            self.writer(run_id=run_id, runtimes=[runtime]).finish()
            old_version_paths.append(artifact_version_path(self.state_dir, "Library Run", run_id))

        versions = self.library_entry()["versions"]
        self.assertEqual(20, len(versions))
        self.assertEqual("run-21", versions[0]["run_id"])
        self.assertEqual("run-02", versions[-1]["run_id"])
        self.assertFalse(old_version_paths[0].exists())
        self.assertFalse(old_version_paths[1].exists())
        self.assertTrue(old_version_paths[2].exists())

    def test_library_write_is_atomic_and_existing_json_survives_replace_failure(self) -> None:
        update_artifact_library_live(
            self.state_dir,
            run_name="Atomic Run",
            run_id="run-ok",
            identity="agent",
            state="live",
        )
        path = artifact_library_path(self.state_dir)
        before_text = path.read_text(encoding="utf-8")
        before_data = json.loads(before_text)

        with mock.patch("ringer.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                update_artifact_library_live(
                    self.state_dir,
                    run_name="Atomic Run",
                    run_id="run-crash",
                    identity="agent",
                    state="live",
                )

        after_text = path.read_text(encoding="utf-8")
        self.assertEqual(before_text, after_text)
        self.assertEqual(before_data, json.loads(after_text))

    def run_threads_and_collect_errors(self, targets: list[Callable[[], None]]) -> list[BaseException]:
        # A thread target's exception does NOT fail unittest on its own -- it
        # just prints to stderr and the thread dies quietly. An earlier
        # version of test_concurrent_lane_updates_do_not_lose_each_other
        # relied on bare threading.Thread for exactly this reason: it stayed
        # green while a real, adversarially-discovered Windows PermissionError
        # was silently swallowing one thread's write, ~7.5% of the time.
        # Every threaded test in this file MUST route through this helper.
        errors: list[BaseException] = []
        errors_lock = threading.Lock()

        def guarded(fn: Callable[[], None]) -> None:
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - the test wants to see everything
                with errors_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=guarded, args=(fn,)) for fn in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        for thread in threads:
            self.assertFalse(thread.is_alive(), "a thread never finished within the join timeout")
        return errors

    def test_concurrent_lane_updates_do_not_lose_each_other(self) -> None:
        # Reproduces the reported symptom: a lane that finishes rarely
        # (Lane B here) had real run/report/version files on disk but no
        # library.json entry, because a concurrent writer's STALE read
        # (started before Lane B's write landed) wrote LAST and silently
        # reverted it. Slow down the FIRST writer's read so the SECOND
        # writer's update_artifact_library_live call is guaranteed to be
        # in flight -- queued on the lock, pre-fix racing the read-modify-
        # write cycle -- while the first is still inside its own critical
        # section. Both entries must survive regardless of ordering.
        original_read = ringer.read_artifact_library
        lane_a_reading = threading.Event()

        def slow_read(state_dir: Path) -> dict[str, object]:
            result = original_read(state_dir)
            lane_a_reading.set()
            time.sleep(0.3)
            return result

        def write_lane_a() -> None:
            with mock.patch("ringer.read_artifact_library", side_effect=slow_read):
                update_artifact_library_live(
                    self.state_dir,
                    run_name="Lane A",
                    run_id="lane-a-run",
                    identity="agent",
                    state="live",
                )

        def write_lane_b() -> None:
            self.assertTrue(lane_a_reading.wait(timeout=5), "Lane A never started its read")
            update_artifact_library_live(
                self.state_dir,
                run_name="Lane B",
                run_id="lane-b-run",
                identity="agent",
                state="live",
            )

        errors = self.run_threads_and_collect_errors([write_lane_a, write_lane_b])
        self.assertEqual([], errors, "a lane's write raised instead of landing")

        library = read_artifact_library(self.state_dir)
        self.assertEqual({"Lane A", "Lane B"}, set(library["artifacts"]))

    def test_lock_serializes_overlapping_critical_sections(self) -> None:
        active = 0
        max_active = 0
        lock = threading.Lock()

        def hold() -> None:
            nonlocal active, max_active
            with ringer.artifact_library_lock(self.state_dir):
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with lock:
                    active -= 1

        errors = self.run_threads_and_collect_errors([hold] * 4)
        self.assertEqual([], errors)
        self.assertEqual(1, max_active)

        # The lock file is a PERSISTENT OS-lockable file now (msvcrt/flock on
        # a file that is opened, never deleted) -- unlike the earlier
        # create/delete design, its continued existence is expected, not a
        # leak. What actually proves nothing is still held is that a fresh
        # acquisition succeeds immediately rather than waiting out the timeout.
        self.assertTrue(ringer.artifact_library_lock_path(self.state_dir).exists())
        acquire_started = time.monotonic()
        with ringer.artifact_library_lock(self.state_dir):
            pass
        self.assertLess(time.monotonic() - acquire_started, 1.0, "a lock from a finished thread was still held")

    def test_lock_survives_heavy_real_contention_with_no_exceptions_or_lost_entries(self) -> None:
        # Two REAL defects surfaced only under genuine, unslowed, high-volume
        # concurrency -- neither reproduced with a handful of threads doing
        # one write apiece, which is why this test hammers hard rather than
        # settling for a light smoke check:
        #
        # 1. os.replace() (MoveFileEx on Windows) transiently fails with
        #    PermissionError when the destination has been momentarily
        #    touched by another handle (observed: a virus scanner's
        #    real-time scan of the file JUST written) -- self-resolving
        #    within milliseconds, but with no retry it escaped as data loss.
        #    Measured 3-6 failures per 2000 writes at 40-way contention
        #    before atomic_write_text's retry existed.
        # 2. The lock's own give-up-after-timeout fallback ("proceed
        #    unlocked rather than hang the caller") is silent by design --
        #    it raises nothing -- so under contention that genuinely
        #    outlasts the timeout, it reproduces the ORIGINAL lost-update
        #    bug with zero exception raised. A too-short timeout (10s) hit
        #    this reliably under the same 40-way load; the fix widened it
        #    and made the give-up path print to stderr rather than stay
        #    silent (see the loud_on_timeout test below).
        #
        # 20 threads x 25 writes each, no artificial pacing -- the shape
        # that actually reproduced both, run repeatedly during development.
        names = [f"Contended Lane {i:02d}" for i in range(20)]

        def write(name: str) -> Callable[[], None]:
            def _write() -> None:
                for attempt in range(25):
                    update_artifact_library_live(
                        self.state_dir,
                        run_name=name,
                        run_id=f"{name}-run-{attempt}",
                        identity="agent",
                        state="live",
                    )

            return _write

        errors = self.run_threads_and_collect_errors([write(name) for name in names])
        self.assertEqual([], errors, "a write raised under real contention instead of landing or waiting")

        library = read_artifact_library(self.state_dir)
        self.assertEqual(set(names), set(library["artifacts"]))

    def test_lock_timeout_give_up_is_loud_not_silent(self) -> None:
        # The give-up-after-timeout path proceeds WITHOUT the lock by
        # design (see artifact_library_lock's own comment) -- that is only
        # an acceptable trade-off if it is impossible to miss when it
        # actually happens. Force it by making every acquisition attempt
        # fail, then assert the diagnostic actually reaches stderr rather
        # than the operation just silently succeeding unlocked.
        with mock.patch("ringer._lock_fh_windows", return_value=False), mock.patch(
            "ringer._lock_fh_posix", return_value=False
        ), mock.patch("ringer.ARTIFACT_LIBRARY_LOCK_TIMEOUT_S", 0.2), mock.patch(
            "ringer.ARTIFACT_LIBRARY_LOCK_POLL_S", 0.05
        ):
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                update_artifact_library_live(
                    self.state_dir,
                    run_name="Forced Timeout Lane",
                    run_id="forced-timeout-run",
                    identity="agent",
                    state="live",
                )
            self.assertIn("gave up", captured.getvalue())
            self.assertIn("UNLOCKED", captured.getvalue())

        # And the write still landed -- giving up on the LOCK must not mean
        # giving up on the write itself.
        library = read_artifact_library(self.state_dir)
        self.assertIn("Forced Timeout Lane", library["artifacts"])

    def test_startup_reconcile_marks_stale_live_entry_died(self) -> None:
        update_artifact_library_live(
            self.state_dir,
            run_name="Stale Run",
            run_id="dead-run",
            identity="agent",
            state="live",
        )

        reconcile_artifact_library_dead_runs(self.state_dir)

        entry = read_artifact_library(self.state_dir)["artifacts"]["Stale Run"]
        self.assertEqual("died", entry["state"])
        self.assertEqual("dead-run", entry["current_run_id"])

    def test_csp_meta_present_on_live_report_and_wrapper_pages(self) -> None:
        runtime = self.runtime(status="pass")
        writer = self.writer(runtimes=[runtime])
        writer.flush()
        writer.finish()

        live_html = writer.live_path.read_text(encoding="utf-8")
        report_html = writer.report_path.read_text(encoding="utf-8")
        wrapper_path = self.state_dir / "artifacts" / "view" / "run-1" / "task-one--worker.log.html"
        wrapper_html = wrapper_path.read_text(encoding="utf-8")
        self.assertIn(CSP_META_TAG, live_html)
        # finish() rewrites the live page as a final snapshot: no self-refresh.
        self.assertNotIn('<meta http-equiv="refresh"', live_html)
        self.assertIn(CSP_META_TAG, report_html)
        self.assertIn(CSP_META_TAG, wrapper_html)

    def test_dashboard_serves_library_and_artifacts_but_rejects_escapes(self) -> None:
        runtime = self.runtime(status="pass")
        writer = self.writer(runtimes=[runtime])
        writer.finish()
        dashboard = Dashboard(
            state_path=writer.path,
            preferred_port=0,
            open_viewer=False,
        )
        port = dashboard.start()
        self.addCleanup(dashboard.stop)

        with urlopen(f"http://127.0.0.1:{port}/artifacts/library.json", timeout=5) as response:
            self.assertEqual(200, response.status)
            self.assertEqual("application/json; charset=utf-8", response.headers["Content-Type"])
            body = json.loads(response.read().decode("utf-8"))
        self.assertIn("Library Run", body["artifacts"])

        artifact_rel = writer.live_path.relative_to(self.state_dir / "artifacts")
        with urlopen(f"http://127.0.0.1:{port}/artifacts/{artifact_rel.as_posix()}", timeout=5) as response:
            self.assertEqual(200, response.status)
            self.assertEqual("text/html; charset=utf-8", response.headers["Content-Type"])
            self.assertIn(b"Library Run", response.read())

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/artifacts/%2e%2e/runs/run-1.json")
        escape_response = conn.getresponse()
        self.assertEqual(404, escape_response.status)
        escape_response.read()

        conn.request("GET", "/artifacts/live/")
        listing_response = conn.getresponse()
        self.assertEqual(404, listing_response.status)
        listing_response.read()


if __name__ == "__main__":
    unittest.main(verbosity=2)
