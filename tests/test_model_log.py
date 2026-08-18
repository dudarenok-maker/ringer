#!/usr/bin/env python3
"""Local per-model performance log behavior."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ringer import (  # noqa: E402
    AppConfig,
    ArtifactConfig,
    EngineConfig,
    EvalConfig,
    EvalLogger,
    Manifest,
    RingerRunner,
    TaskSpec,
    VerifyResult,
    WorkerResult,
    aggregate_model_log_rows,
    aggregate_model_scoreboard_rows,
    model_log_row_counts_toward_score,
    model_log_row_is_retry,
    read_model_log_rows,
)

LONG_SPEC = (
    "Create the requested artifact in the current working directory, keep the change scoped, "
    "and make the check command able to explain any failure clearly."
)
GOOD_CHECK = (
    "test -s output.txt && grep -q 'ready' output.txt || "
    "{ echo 'FAIL: output.txt missing or does not contain ready'; exit 1; }"
)


def harness_engine(model_default: str = "openrouter/z-ai/glm-5.2") -> EngineConfig:
    return EngineConfig(
        name="opencode",
        bin="/usr/local/bin/opencode",
        args_template=("run", "-m", "{model}", "--dir", "{taskdir}", "{spec}"),
        full_access_args=(),
        sandbox_args=(),
        token_regex=None,
        model_default=model_default,
    )


class ModelLogTests(unittest.TestCase):
    def config(self, root: Path) -> AppConfig:
        return AppConfig(
            path=None,
            identity_default=None,
            state_dir=root / "state",
            dashboard_port_base=8787,
            hud_port=8700,
            hud_app_path=None,
            allow_full_access=False,
            eval=EvalConfig(backend="jsonl", jsonl_path=root / "eval.jsonl"),
            engines={"opencode": harness_engine()},
            artifact=ArtifactConfig(
                enabled=False,
                out_template=str(root / "live.html"),
                report_template=str(root / "report.html"),
                index_out=root / "index.html",
            ),
        )

    def task_obj(self, **extra: object) -> dict[str, object]:
        task: dict[str, object] = {
            "key": "a",
            "spec": LONG_SPEC,
            "check": GOOD_CHECK,
            "engine": "opencode",
            "expect_files": ["output.txt"],
            "verified": "output exists with expected content",
        }
        task.update(extra)
        return task

    def test_task_spec_parses_and_validates_task_type(self) -> None:
        task = TaskSpec.from_obj(self.task_obj(task_type="  code-feature  "))
        self.assertEqual("code-feature", task.task_type)
        self.assertEqual("", TaskSpec.from_obj(self.task_obj()).task_type)
        with self.assertRaisesRegex(ValueError, "task_type must be a string"):
            TaskSpec.from_obj(self.task_obj(task_type=5))

    def test_eval_row_carries_model_task_type_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = Manifest.from_obj(
                {
                    "run_name": "model-log-test",
                    "workdir": str(root / "work"),
                    "tasks": [self.task_obj(task_type="code-feature")],
                }
            )
            runner = RingerRunner(
                manifest,
                config=self.config(root),
                identity="tester",
                dashboard_enabled=False,
            )
            runtime = runner.runtimes[0]
            runner._log_attempt(
                runtime,
                runtime.task.spec,
                True,
                WorkerResult(returncode=0, timed_out=False, tokens=123),
                VerifyResult(ok=True, check_returncode=0, check_timed_out=False, raw_output_excerpt="ok"),
                "PASS",
                456,
            )
            payload = json.loads((root / "eval.jsonl").read_text(encoding="utf-8"))
            self.assertEqual("openrouter/z-ai/glm-5.2", payload["model"])
            self.assertEqual("code-feature", payload["task_type"])
            self.assertIs(payload["retry"], True)
            self.assertIn("model=openrouter/z-ai/glm-5.2", payload["notes"])
            self.assertIn("task_type=code-feature", payload["notes"])
            self.assertIn("retry=true", payload["notes"])

    def test_postgres_params_exclude_local_model_log_keys(self) -> None:
        class FakeConn:
            def __init__(self) -> None:
                self.params: dict[str, object] | None = None

            def execute(self, _sql: str, params: dict[str, object]) -> None:
                self.params = params

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as temp:
            logger = EvalLogger(
                EvalConfig(backend="jsonl", jsonl_path=Path(temp) / "eval.jsonl")
            )
            fake = FakeConn()
            logger._conn = fake
            row = {
                "run_id": "run",
                "pattern": "ringer-py",
                "task_key": "a",
                "spec": "spec",
                "worker_engine": "opencode",
                "shepherd_model": "gpt",
                "verify_method": "executed-check",
                "verdict": "PASS",
                "duration_ms": 1,
                "worker_tokens": 2,
                "notes": "retry=false",
                "orchestrator": "tester",
                "model": "openrouter/x",
                "task_type": "code-feature",
                "retry": False,
            }
            logger.log_attempt(row)
            self.assertIsNotNone(fake.params)
            assert fake.params is not None
            self.assertNotIn("model", fake.params)
            self.assertNotIn("task_type", fake.params)
            self.assertNotIn("retry", fake.params)
            self.assertEqual(
                {
                    "run_id",
                    "pattern",
                    "task_key",
                    "spec",
                    "worker_engine",
                    "shepherd_model",
                    "verify_method",
                    "verdict",
                    "duration_ms",
                    "worker_tokens",
                    "notes",
                    "orchestrator",
                },
                set(fake.params),
            )

    def test_models_aggregation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "eval.jsonl"
            rows = [
                {
                    "run_id": "run1",
                    "task_key": "a",
                    "worker_engine": "opencode",
                    "model": "openrouter/x",
                    "task_type": "code-feature",
                    "verdict": "FAIL",
                    "duration_ms": 100,
                    "worker_tokens": 10,
                    "retry": False,
                    "logged_at": "2026-07-01T10:00:00+00:00",
                },
                {
                    "run_id": "run1",
                    "task_key": "a",
                    "worker_engine": "opencode",
                    "model": "openrouter/x",
                    "task_type": "code-feature",
                    "verdict": "PASS",
                    "duration_ms": 200,
                    "worker_tokens": 20,
                    "retry": True,
                    "logged_at": "2026-07-01T10:01:00+00:00",
                },
                {
                    "run_id": "run2",
                    "task_key": "b",
                    "worker_engine": "opencode",
                    "model": "openrouter/x",
                    "task_type": "code-feature",
                    "verdict": "PASS",
                    "duration_ms": 100,
                    "worker_tokens": 30,
                    "logged_at": "2026-07-03T10:00:00+00:00",
                },
                {
                    "run_id": "run3",
                    "task_key": "c",
                    "worker_engine": "codex",
                    "verdict": "FAIL",
                    "duration_ms": 50,
                    "worker_tokens": None,
                    "logged_at": "2026-06-30T10:00:00+00:00",
                },
                {
                    "run_id": "run4",
                    "task_key": "d",
                    "worker_engine": "opencode",
                    "model": "",
                    "task_type": "",
                    "verdict": "PASS",
                    "duration_ms": 80,
                    "worker_tokens": 5,
                    "notes": "retry=true",
                    "logged_at": "2026-07-04T10:00:00+00:00",
                },
            ]
            path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\nnot json\n",
                encoding="utf-8",
            )

            read_rows, skipped = read_model_log_rows(path, since="2026-07-01")
            self.assertEqual(4, len(read_rows))
            self.assertEqual(1, skipped)
            self.assertTrue(model_log_row_is_retry(read_rows[-1]))

            groups = aggregate_model_log_rows(read_rows)
            by_key = {(group["model"], group["task_type"]): group for group in groups}
            code = by_key[("openrouter/x", "code-feature")]
            self.assertEqual(2, code["tasks"])
            self.assertEqual(3, code["attempts"])
            self.assertEqual(2, code["passed"])
            self.assertEqual(0, code["failed"])
            self.assertEqual(1.0, code["pass_rate"])
            self.assertEqual(0.5, code["first_try_pass_rate"])
            self.assertEqual(150, code["median_duration_ms"])
            self.assertEqual(20, code["median_tokens"])
            self.assertEqual("2026-07-03T10:00:00+00:00", code["last_seen"])

            untyped = aggregate_model_log_rows(
                read_rows,
                model="opencode",
                task_type="(untyped)",
            )
            self.assertEqual(1, len(untyped))
            self.assertEqual("opencode", untyped[0]["model"])
            self.assertEqual("(untyped)", untyped[0]["task_type"])
            self.assertEqual(1, untyped[0]["tasks"])

    def test_since_selects_tasks_by_final_attempt_and_keeps_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "eval.jsonl"
            rows = [
                {
                    "run_id": "rescue-run",
                    "task_key": "task-a",
                    "worker_engine": "opencode",
                    "model": "openrouter/rescue",
                    "task_type": "code-feature",
                    "verdict": "FAIL",
                    "duration_ms": 100,
                    "worker_tokens": 10,
                    "retry": False,
                    "logged_at": "2026-07-01T23:59:00+00:00",
                },
                {
                    "run_id": "rescue-run",
                    "task_key": "task-a",
                    "worker_engine": "opencode",
                    "model": "openrouter/rescue",
                    "task_type": "code-feature",
                    "verdict": "PASS",
                    "duration_ms": 200,
                    "worker_tokens": 20,
                    "retry": True,
                    "logged_at": "2026-07-02T00:01:00+00:00",
                },
                {
                    "run_id": "old-run",
                    "task_key": "task-b",
                    "worker_engine": "opencode",
                    "model": "openrouter/rescue",
                    "task_type": "code-feature",
                    "verdict": "PASS",
                    "duration_ms": 300,
                    "worker_tokens": 30,
                    "retry": False,
                    "logged_at": "2026-07-01T20:00:00+00:00",
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            read_rows, skipped = read_model_log_rows(path, since="2026-07-02")

            self.assertEqual(0, skipped)
            self.assertEqual(["rescue-run", "rescue-run"], [row["run_id"] for row in read_rows])
            groups = aggregate_model_log_rows(read_rows)
            self.assertEqual(1, len(groups))
            group = groups[0]
            self.assertEqual(1, group["tasks"])
            self.assertEqual(2, group["attempts"])
            self.assertEqual(0.0, group["first_try_pass_rate"])
            self.assertEqual(1.0, group["pass_rate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CountsTowardScoreTests(unittest.TestCase):
    """A run the worker never got to start is not evidence about the model.

    The case this exists for: a lane whose CLI exits in four seconds because
    the account is out of credit, relaunched every five minutes for eleven
    hours. Scored as failed tasks those 197 runs reported a 3% pass rate for a
    model that had passed almost everything it was able to begin.
    """

    def test_absent_field_counts(self) -> None:
        # Every producer today omits it. Omission must never exclude a row, or
        # this field would silently rewrite history the day it was added.
        self.assertTrue(model_log_row_counts_toward_score({}))

    def test_explicit_false_excludes(self) -> None:
        self.assertFalse(model_log_row_counts_toward_score({"counts_toward_score": False}))

    def test_explicit_true_counts(self) -> None:
        self.assertTrue(model_log_row_counts_toward_score({"counts_toward_score": True}))

    def test_json_string_and_number_forms(self) -> None:
        for value in ("false", "False", " FALSE ", "0", "no"):
            self.assertFalse(
                model_log_row_counts_toward_score({"counts_toward_score": value}), value
            )
        for value in ("true", "1", "yes", 1, 2.5):
            self.assertTrue(
                model_log_row_counts_toward_score({"counts_toward_score": value}), value
            )

    def test_unparseable_value_counts_rather_than_vanishing(self) -> None:
        # A typo must leave the row in the score, not quietly remove it. The
        # failure mode of the opposite default is invisible: rows disappear and
        # the rate silently improves.
        self.assertTrue(model_log_row_counts_toward_score({"counts_toward_score": {"x": 1}}))
        self.assertTrue(model_log_row_counts_toward_score({"counts_toward_score": None}))

    def _rows(self) -> list[dict[str, object]]:
        base = {
            "worker_engine": "claude",
            "model": "claude-sonnet-5",
            "task_type": "ops",
        }
        rows: list[dict[str, object]] = [
            {
                **base,
                "run_id": "real1",
                "task_key": "real1",
                "verdict": "PASS",
                "duration_ms": 900_000,
                "worker_tokens": 3_000_000,
                "logged_at": "2026-08-17T16:25:00+00:00",
            },
            {
                **base,
                "run_id": "real2",
                "task_key": "real2",
                "verdict": "FAIL",
                "duration_ms": 700_000,
                "worker_tokens": 1_000_000,
                "logged_at": "2026-08-17T17:25:00+00:00",
            },
        ]
        # Six dead launches: four seconds each, zero tokens, never started.
        for i in range(6):
            rows.append(
                {
                    **base,
                    "run_id": f"dead{i}",
                    "task_key": f"dead{i}",
                    "verdict": "FAIL",
                    "duration_ms": 4_400,
                    "worker_tokens": 0,
                    "counts_toward_score": False,
                    "logged_at": f"2026-08-17T1{i}:00:00+00:00",
                }
            )
        return rows

    def test_no_op_rows_are_counted_but_not_scored(self) -> None:
        groups = aggregate_model_log_rows(self._rows(), task_type="ops")
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["tasks"], 2, "scored tasks must exclude the dead launches")
        self.assertEqual(group["not_scored"], 6, "dead launches must still be visible")
        self.assertEqual(group["passed"], 1)
        self.assertEqual(group["failed"], 1)
        self.assertAlmostEqual(group["pass_rate"], 0.5)

    def test_medians_exclude_no_op_rows(self) -> None:
        # This is half the point. Six 4-second, zero-token samples drag both
        # medians to a number no real run ever produced - the observed symptom
        # was a blank/zero Tokens column and a 4s median speed for a lane whose
        # real runs take twenty minutes.
        group = aggregate_model_log_rows(self._rows(), task_type="ops")[0]
        self.assertEqual(group["median_tokens"], 2_000_000)
        self.assertEqual(group["median_duration_ms"], 800_000)

    def test_without_the_field_the_same_rows_score_the_old_way(self) -> None:
        # The control. Strip the marks and the aggregate must go back to what
        # it was, or this is measuring the fixture rather than the feature.
        rows = [
            {k: v for k, v in row.items() if k != "counts_toward_score"}
            for row in self._rows()
        ]
        group = aggregate_model_log_rows(rows, task_type="ops")[0]
        self.assertEqual(group["tasks"], 8)
        self.assertEqual(group["not_scored"], 0)
        self.assertAlmostEqual(group["pass_rate"], 1 / 8)
        self.assertEqual(group["median_tokens"], 0)

    def test_last_seen_still_reflects_no_op_runs(self) -> None:
        # A lane that spent all week unable to start has still been running.
        # Showing it as untouched since last month would be its own lie.
        rows = self._rows()
        rows.append(
            {
                "worker_engine": "claude",
                "model": "claude-sonnet-5",
                "task_type": "ops",
                "run_id": "dead-late",
                "task_key": "dead-late",
                "verdict": "FAIL",
                "duration_ms": 4_400,
                "worker_tokens": 0,
                "counts_toward_score": False,
                "logged_at": "2026-08-18T09:00:00+00:00",
            }
        )
        group = aggregate_model_log_rows(rows, task_type="ops")[0]
        self.assertEqual(group["last_seen"], "2026-08-18T09:00:00+00:00")

    def test_total_tokens_sums_every_attempt_including_retries(self) -> None:
        # The plain case: the total is a sum over ATTEMPTS, not over tasks, so
        # a task that needed three tries costs what all three tries cost. An
        # implementation that summed only the final attempt of each task would
        # under-report every retried task and would still look right on a log
        # where nothing was ever retried.
        rows = [row for row in self._rows() if row.get("counts_toward_score") is not False]
        rows.append(
            {
                "worker_engine": "claude",
                "model": "claude-sonnet-5",
                "task_type": "ops",
                "run_id": "real2",
                "task_key": "real2",
                "verdict": "PASS",
                "duration_ms": 500_000,
                "worker_tokens": 250_000,
                "notes": "retry=true",
                "logged_at": "2026-08-17T18:25:00+00:00",
            }
        )
        group = aggregate_model_log_rows(rows, task_type="ops")[0]
        self.assertEqual(group["attempts"], 3)
        self.assertEqual(group["total_tokens"], 4_250_000)

    def test_total_tokens_includes_no_op_runs_that_the_median_excludes(self) -> None:
        # THE DESIGN DECISION, PINNED. The two token columns count different
        # sets on purpose: the median is a quality statistic and drops runs
        # that never started, while the total is a SPEND figure and must not,
        # because a run that burned tokens before dying still drew on the
        # budget. Observed on the live log: 3.4M tokens sat on rows marked
        # not-scored, essentially all of it on two runs.
        #
        # The fixture's six no-ops are zero-token, so one of them is given real
        # spend here - a fixture of all zeros cannot tell "includes no-ops"
        # apart from "excludes them".
        rows = self._rows()
        for row in rows:
            if row["run_id"] == "dead3":
                row["worker_tokens"] = 500_000
        group = aggregate_model_log_rows(rows, task_type="ops")[0]
        self.assertEqual(group["not_scored"], 6)
        # Unchanged: the median still sees only the two real runs.
        self.assertEqual(group["median_tokens"], 2_000_000)
        # The total sees the dead launch's spend as well.
        self.assertEqual(group["total_tokens"], 4_500_000)

    def test_total_tokens_and_median_are_not_reconcilable(self) -> None:
        # The corollary, stated as a test so nobody "fixes" the divergence
        # later: median x tasks does NOT reproduce the total, and the gap is
        # the no-op spend. If this ever passes trivially because both sides
        # were made to count the same set, the spend column has quietly stopped
        # being a spend column.
        rows = self._rows()
        for row in rows:
            if row["run_id"] == "dead3":
                row["worker_tokens"] = 500_000
        group = aggregate_model_log_rows(rows, task_type="ops")[0]
        self.assertNotEqual(group["total_tokens"], group["median_tokens"] * group["tasks"])

    def test_the_rollup_aggregator_reports_the_same_total(self) -> None:
        # There are TWO aggregators and they feed different surfaces: the CLI
        # table reads this one's sibling, while the HTML scoreboard and the
        # Ringside models tab read this one. The file already carries a comment
        # warning that patching one leaves the other reporting the numbers the
        # first had just stopped reporting - so assert them against each other
        # rather than trusting that warning was heeded.
        rows = self._rows()
        for row in rows:
            if row["run_id"] == "dead3":
                row["worker_tokens"] = 500_000
        grouped = aggregate_model_log_rows(rows, task_type="ops")[0]
        rolled = aggregate_model_scoreboard_rows(rows, task_type="ops")[0]
        self.assertEqual(rolled["total_tokens"], 4_500_000)
        self.assertEqual(rolled["total_tokens"], grouped["total_tokens"])

    def test_total_tokens_is_blank_not_zero_when_nothing_was_recorded(self) -> None:
        # A printed 0 claims a measurement. Rows whose runs never reported a
        # token count at all - the unattributed legacy rows, copilot, anything
        # whose CLI prints no usage - have no spend evidence, and rendering
        # them as "0 tokens" states something the log does not say. None here,
        # blank in every renderer, exactly as the median cell beside it
        # already behaves.
        rows = [
            {
                "worker_engine": "copilot",
                "model": "auto",
                "task_type": "ops",
                "run_id": "r1",
                "task_key": "r1",
                "verdict": "PASS",
                "duration_ms": 1_000,
                "logged_at": "2026-08-17T16:25:00+00:00",
            }
        ]
        group = aggregate_model_log_rows(rows, task_type="ops")[0]
        self.assertIsNone(group["total_tokens"])
        self.assertIsNone(group["median_tokens"])
        self.assertIsNone(aggregate_model_scoreboard_rows(rows, task_type="ops")[0]["total_tokens"])

    def test_a_recorded_zero_is_a_measurement_and_still_prints(self) -> None:
        # The other half of the line above, and the reason "blank when falsy"
        # would be wrong: a run that genuinely reported zero tokens HAS been
        # measured. Only an absent value blanks.
        rows = [
            {
                "worker_engine": "copilot",
                "model": "auto",
                "task_type": "ops",
                "run_id": "r1",
                "task_key": "r1",
                "verdict": "PASS",
                "duration_ms": 1_000,
                "worker_tokens": 0,
                "logged_at": "2026-08-17T16:25:00+00:00",
            }
        ]
        group = aggregate_model_log_rows(rows, task_type="ops")[0]
        self.assertEqual(group["total_tokens"], 0)

    def test_the_token_breakdown_sums_and_reconciles_with_the_total(self) -> None:
        # WHY THE BREAKDOWN EXISTS. `worker_tokens` is input + output + cache
        # read + cache write, and cache reads dominate an agent run - 9.5M of
        # one lane's 22.3M on 2026-08-18. Read against a stated 15M/day cap the
        # total said 140% while that lane was still succeeding at 5/5, because
        # the cap does not count the cached half. The non-cached figure said
        # 85%, which matched reality. A single roll-up cannot answer "how much
        # of my cap is left"; these columns can.
        rows = [
            {
                "worker_engine": "cline",
                "model": "deepseek/deepseek-v4-flash",
                "task_type": "ops",
                "run_id": "r1",
                "task_key": "r1",
                "verdict": "PASS",
                "duration_ms": 1000,
                "worker_tokens": 1_000,
                "worker_tokens_input": 600,
                "worker_tokens_output": 100,
                "worker_tokens_cache_read": 250,
                "worker_tokens_cache_write": 50,
                "logged_at": "2026-08-18T10:00:00+00:00",
            },
            {
                "worker_engine": "cline",
                "model": "deepseek/deepseek-v4-flash",
                "task_type": "ops",
                "run_id": "r2",
                "task_key": "r2",
                "verdict": "PASS",
                "duration_ms": 1000,
                "worker_tokens": 500,
                "worker_tokens_input": 300,
                "worker_tokens_output": 50,
                "worker_tokens_cache_read": 150,
                "worker_tokens_cache_write": 0,
                "logged_at": "2026-08-18T11:00:00+00:00",
            },
        ]
        group = aggregate_model_log_rows(rows, task_type="ops")[0]
        self.assertEqual(group["total_input"], 900)
        self.assertEqual(group["total_output"], 150)
        self.assertEqual(group["total_cache_read"], 400)
        self.assertEqual(group["total_cache_write"], 50)
        # The parts must add up to the roll-up, or one of the two is lying.
        self.assertEqual(
            group["total_tokens"],
            group["total_input"] + group["total_output"]
            + group["total_cache_read"] + group["total_cache_write"],
        )
        # The rollup aggregator feeds the HTML and Ringside surfaces; the other
        # feeds the CLI. Patching one and not the other is the failure this
        # file already carries a warning about.
        rolled = aggregate_model_scoreboard_rows(rows, task_type="ops")[0]
        for field in ("total_input", "total_output", "total_cache_read", "total_cache_write"):
            self.assertEqual(rolled[field], group[field], field)

    def test_the_breakdown_is_blank_not_zero_when_unreported(self) -> None:
        # Most engines report no usage breakdown at all. Zero would claim they
        # ran for free; blank says nothing was measured - the same line the
        # median and total columns already draw.
        rows = [
            {
                "worker_engine": "copilot",
                "model": "auto",
                "task_type": "ops",
                "run_id": "r1",
                "task_key": "r1",
                "verdict": "PASS",
                "duration_ms": 1000,
                "worker_tokens": 42,
                "logged_at": "2026-08-18T10:00:00+00:00",
            }
        ]
        group = aggregate_model_log_rows(rows, task_type="ops")[0]
        self.assertEqual(group["total_tokens"], 42)
        for field in ("total_input", "total_output", "total_cache_read", "total_cache_write"):
            self.assertIsNone(group[field], field)

    def test_a_cache_write_counts_as_fresh_input_not_as_cache(self) -> None:
        # THE GROUPING THAT WAS WRONG FIRST TIME. Anthropic reports
        # `input_tokens` as only the part no cache breakpoint covered, so a
        # claude tool loop reads about two tokens per request: one real run
        # recorded in=276 against cacheWrite=196,277 over 138 calls. Showing
        # 276 as "In" said the lane sent nothing, when it had sent 196k of
        # fresh prompt. A cache WRITE is fresh input by definition - the
        # provider read those tokens for the first time and kept them.
        from ringer import scoreboard_cached_tokens, scoreboard_input_tokens
        claude_shaped = {"total_input": 276, "total_cache_write": 196_277, "total_cache_read": 11_424_250}
        self.assertEqual(scoreboard_input_tokens(claude_shaped), 196_553)
        self.assertEqual(scoreboard_cached_tokens(claude_shaped), 11_424_250)
        # THE CONTROL: the raw field alone is the number that misled, so assert
        # the column is NOT it.
        self.assertNotEqual(scoreboard_input_tokens(claude_shaped), 276)

        # And the column means the same thing on an engine that reports its
        # uncached prompt in `input` and writes nothing - which is the point of
        # regrouping rather than relabelling.
        cline_shaped = {"total_input": 884_495, "total_cache_write": 0, "total_cache_read": 814_336}
        self.assertEqual(scoreboard_input_tokens(cline_shaped), 884_495)
        self.assertEqual(scoreboard_cached_tokens(cline_shaped), 814_336)

        # Nothing recorded stays blank rather than becoming 0.
        self.assertIsNone(scoreboard_input_tokens({"total_input": None, "total_cache_write": None}))
        self.assertIsNone(scoreboard_cached_tokens({"total_cache_read": None}))

    def test_the_three_columns_still_sum_to_the_total(self) -> None:
        # The regrouping moved a component between columns; it must not have
        # lost or duplicated one. In + Out + Cached has to reproduce the
        # roll-up on both engine shapes.
        from ringer import scoreboard_cached_tokens, scoreboard_input_tokens
        for shape in (
            {"total_input": 276, "total_output": 65_445, "total_cache_write": 196_277, "total_cache_read": 11_424_250},
            {"total_input": 884_495, "total_output": 32_154, "total_cache_write": 0, "total_cache_read": 814_336},
        ):
            total = sum(int(shape[k] or 0) for k in
                        ("total_input", "total_output", "total_cache_read", "total_cache_write"))
            self.assertEqual(
                total,
                scoreboard_input_tokens(shape) + int(shape["total_output"]) + scoreboard_cached_tokens(shape),
            )

    def test_a_group_that_never_scored_reports_no_rate(self) -> None:
        rows = [row for row in self._rows() if row.get("counts_toward_score") is False]
        group = aggregate_model_log_rows(rows, task_type="ops")[0]
        self.assertEqual(group["tasks"], 0)
        self.assertEqual(group["not_scored"], 6)
        # The renderer blanks the rates when tasks == 0; the aggregate simply
        # must not claim a pass rate it has no evidence for.
        self.assertEqual(group["passed"], 0)
        self.assertEqual(group["failed"], 0)
