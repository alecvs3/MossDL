"""Offline tests for the live-run report builder (tests/live/report.py).

Two layers:

* synthetic stage_history + telemetry fixtures that pin the derivation rules
  (stage interval union, solver pairing across both solver tracks, timer
  windowing, concurrency ramp, assertions/verdict);
* the real captured artifacts under ``reports/live/`` -- a successful 5-part
  Kristala run and a 2-part Resonance run whose archive job failed -- which are
  the ground truth for the numbers the report must reproduce.

No network, no engine import, no live download.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.live.report import (  # noqa: E402
    LiveReportBuilder, build_report, load_run_artifacts, marker_of, parse_ts,
    stage_durations, write_report,
)

KRISTALA = ROOT / "reports" / "live" / "20260916_231134_Kristala"
RESONANCE = ROOT / "reports" / "live" / "20260917_031249_Resonance"


def _event(ts: str, message: str, *, subsystem: str = "engine:captcha",
           level: str = "INFO", **context: object) -> dict[str, object]:
    return {"timestamp": ts, "level": level, "subsystem": subsystem,
            "message": message, "context": context}


def _history(*pairs: tuple[str, float]) -> list[dict[str, object]]:
    previous = None
    out = []
    for stage, entered in pairs:
        out.append({"from": previous, "to": stage, "entered_at": entered, "detail": {}})
        previous = stage
    return out


class HelperTests(unittest.TestCase):
    def test_marker_of_strips_task_prefix(self) -> None:
        self.assertEqual(marker_of("[64d1513c] [TIMER_TICK] Countdown: 10s"), "TIMER_TICK")
        self.assertEqual(marker_of("[CAPTCHA_SOLVED_SUCCESS] Challenge x"), "CAPTCHA_SOLVED_SUCCESS")
        self.assertIsNone(marker_of("(lifecycle) Illegal stage transition a -> b"))
        self.assertIsNone(marker_of(""))

    def test_parse_ts_roundtrip(self) -> None:
        self.assertEqual(parse_ts("2026-09-17T03:12:49Z"), 1789614769.0)
        self.assertEqual(parse_ts(12.5), 12.5)
        self.assertIsNone(parse_ts("not-a-timestamp"))
        self.assertIsNone(parse_ts(None))

    def test_stage_durations_last_stage_is_open_ended(self) -> None:
        rows = stage_durations("aa11bb22", "part1", _history(("resolving_metadata", 100.0),
                                                       ("downloading", 130.0),
                                                       ("completed", 190.0)))
        self.assertEqual([r["stage"] for r in rows],
                         ["resolving_metadata", "downloading", "completed"])
        self.assertEqual([r["held_seconds"] for r in rows], [30.0, 60.0, None])


class SyntheticReportTests(unittest.TestCase):
    """Pin the derivation rules against hand-built fixtures."""

    def _result(self) -> dict[str, object]:
        durations = (stage_durations("aa11bb22", "part1", _history(("resolving_metadata", 100.0),
                                                             ("downloading", 110.0),
                                                             ("completed", 140.0)))
                     + stage_durations("cc33dd44", "part2", _history(("resolving_metadata", 130.0),
                                                               ("downloading", 135.0),
                                                               ("completed", 160.0))))
        return {
            "package": {"name": "Pkg", "parts": 2},
            "durations": durations,
            "finals": {
                "aa11bb22": {"name": "part1", "state": "completed", "size": 100,
                       "completed_bytes": 100, "integrity_state": "verified"},
                "cc33dd44": {"name": "part2", "state": "completed", "size": 200,
                       "completed_bytes": 200, "integrity_state": "verified"},
            },
            "archive_jobs": [{"id": "job1", "task_id": "aa11bb22", "state": "completed",
                              "operation": "extract", "format": "part_archive",
                              "created_at": 200.0, "updated_at": 212.5}],
            "destination_files": ["setup.exe"],
            "solves": 2,
        }

    def _timeline(self) -> list[dict[str, object]]:
        return [
            _event("2026-09-17T03:00:00Z", "[aa11bb22] [CAPTCHA_DETECTED] Turnstile",
                   subsystem="plugin:datanodes:engine:captcha", task_id="aa11bb22"),
            _event("2026-09-17T03:00:10Z", "[aa11bb22] [TIMER_DETECTED] Countdown timer detected: 10s",
                   task_id="aa11bb22", seconds=10, source="vue_prop"),
            _event("2026-09-17T03:00:10Z", "[aa11bb22] [TIMER_TICK] Countdown: 10s remaining",
                   level="DEBUG", task_id="aa11bb22", countdown_seconds=10),
            _event("2026-09-17T03:00:18Z", "[aa11bb22] [TIMER_TICK] Countdown: 2s remaining",
                   level="DEBUG", task_id="aa11bb22", countdown_seconds=2),
            _event("2026-09-17T03:00:30Z", "[aa11bb22] [CAPTCHA_BYPASSED] solved",
                   subsystem="plugin:datanodes:engine:captcha", task_id="aa11bb22",
                   duration_ms=30123.4, engine="clearcote"),
            _event("2026-09-17T03:00:31Z", "[CAPTCHA_CHALLENGE] needs verification",
                   level="WARN", task_id="cc33dd44", challenge_id="c9"),
            _event("2026-09-17T03:00:40Z", "[TIMER_SKIP] wait timer skipped: fresh_solve_clearance",
                   subsystem="plugin:datanodes:engine:timer", task_id="aa11bb22",
                   reason="fresh_solve_clearance"),
            _event("2026-09-17T03:01:11Z", "[CAPTCHA_SOLVED_SUCCESS] Challenge c9 solved",
                   challenge_id="c9", solver="clearcote"),
            _event("2026-09-17T03:01:20Z", "[CONCURRENCY_AUDIT] [h1] STREAM_STARTED: x",
                   subsystem="engine:concurrency", host="h1", event="STREAM_STARTED",
                   active_count=1, task_id="aa11bb22:0"),
            _event("2026-09-17T03:01:30Z", "[CONCURRENCY_AUDIT] [h1] PROBE_VERIFIED: x",
                   subsystem="engine:concurrency", host="h1", event="PROBE_VERIFIED",
                   previous_ceiling=1, new_verified_ceiling=2, active_tasks=["aa11bb22:0", "cc33dd44:0"]),
            _event("2026-09-17T03:01:40Z",
                   "[DOWNLOAD_COMPLETED] part1 in 30.0s | Avg: 10.00 MB/s",
                   subsystem="engine:lifecycle", task_id="aa11bb22", avg_speed_mb_s=10.0,
                   peak_speed_mb_s=20.0, stalls_count=2, json_log=None, readable_log=None),
        ]

    def _build(self, **overrides: object) -> dict[str, object]:
        result = self._result()
        result.update(overrides)
        return build_report(result=result, timeline=self._timeline(), hoster="datanodes",
                            destination="D:/dl", run_dir=None, copy_download_logs=False)

    def test_stage_union_is_not_a_naive_sum(self) -> None:
        stages = self._build()["stages"]
        # t1 downloading 110-140 and t2 downloading 135-160 overlap by 5s:
        # naive sum would be 30 + 25 = 55, the union is 50.
        self.assertEqual(stages["downloading"]["seconds"], 50.0)
        self.assertEqual(stages["downloading"]["task_ids"], ["aa11bb22", "cc33dd44"])
        self.assertEqual(stages["resolving_metadata"]["seconds"], 15.0)
        self.assertEqual(stages["completed"]["seconds"], 0.0)

    def test_per_task_carries_stage_seconds_and_throughput(self) -> None:
        per_task = {t["task_id"]: t for t in self._build()["per_task"]}
        self.assertEqual(per_task["aa11bb22"]["stage_seconds"],
                         {"resolving_metadata": 10.0, "downloading": 30.0, "completed": 0.0})
        self.assertEqual(per_task["aa11bb22"]["mb_s_avg"], 10.0)
        self.assertEqual(per_task["aa11bb22"]["mb_s_peak"], 20.0)
        self.assertEqual(per_task["aa11bb22"]["stalls"], 2)
        self.assertIsNone(per_task["cc33dd44"]["mb_s_avg"])

    def test_both_solver_tracks_are_paired_independently(self) -> None:
        solvers = self._build()["solvers"]
        self.assertEqual(len(solvers), 2)
        provider = next(s for s in solvers if s["track"] == "provider")
        engine = next(s for s in solvers if s["track"] == "engine")
        self.assertEqual((provider["task_id"], provider["outcome"]), ("aa11bb22", "solved"))
        self.assertEqual((provider["duration_ms"], provider["duration_exact"]), (30123.4, True))
        # engine track: [CAPTCHA_SOLVED_SUCCESS] has no task_id, so it must be
        # matched on challenge_id; duration falls back to the timestamp delta.
        self.assertEqual((engine["task_id"], engine["challenge_id"]), ("cc33dd44", "c9"))
        self.assertEqual((engine["duration_ms"], engine["duration_exact"]), (40000.0, False))
        self.assertEqual(engine["solver"], "clearcote")

    def test_timer_wait_is_bounded_by_the_next_timer_event(self) -> None:
        timers = self._build()["timers"]
        detected = next(t for t in timers if t["marker"] == "TIMER_DETECTED")
        self.assertEqual((detected["countdown_seconds"], detected["waited_seconds"]), (10, 8.0))
        self.assertEqual(detected["source"], "vue_prop")
        skipped = next(t for t in timers if t["marker"] == "TIMER_SKIP")
        self.assertEqual((skipped["countdown_seconds"], skipped["waited_seconds"]), (0, 0.0))
        self.assertEqual(skipped["source"], "timer_skip:fresh_solve_clearance")

    def test_concurrency_ramp_tracks_and_backfills_the_ceiling(self) -> None:
        ramp = self._build()["concurrency_ramp"]
        self.assertEqual([(r["event"], r["active_streams"], r["ceiling"]) for r in ramp],
                         [("STREAM_STARTED", 1, 1), ("PROBE_VERIFIED", 2, 2)])

    def test_all_assertions_pass_on_a_clean_run(self) -> None:
        report = self._build()
        failed = [a for a in report["assertions"] if not a["passed"]]
        self.assertEqual(failed, [], f"unexpected failures: {failed}")
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["archive_jobs"][0]["seconds"], 12.5)
        # Notes never gate the verdict, but every one must name its condition.
        self.assertEqual({n["code"] for n in report["notes"]},
                         {"stage_intervals_open_ended", "task_without_download_log",
                          "benchmark_log_missing", "download_log_ref_missing"})
        self.assertTrue(all(n.get("reason") for n in report["notes"]))

    def test_failed_archive_job_flips_the_verdict(self) -> None:
        jobs = [{"id": "job1", "task_id": "aa11bb22", "state": "failed", "operation": "extract",
                 "error": "Could not open archive", "created_at": 200.0, "updated_at": 200.1}]
        report = self._build(archive_jobs=jobs)
        check = next(a for a in report["assertions"] if a["id"] == "archive_jobs_completed")
        self.assertFalse(check["passed"])
        self.assertIn("job1", check["detail"])
        self.assertEqual(report["verdict"], "fail")

    def test_short_bytes_and_unverified_integrity_are_caught(self) -> None:
        finals = {"aa11bb22": {"name": "part1", "state": "completed", "size": 100,
                         "completed_bytes": 99, "integrity_state": "failed"}}
        report = self._build(finals=finals)
        ids = {a["id"]: a["passed"] for a in report["assertions"]}
        self.assertFalse(ids["bytes_match_size"])
        self.assertFalse(ids["integrity_verified"])
        self.assertEqual(report["verdict"], "fail")

    def test_error_level_telemetry_fails_the_clean_check(self) -> None:
        timeline = self._timeline() + [
            _event("2026-09-17T03:02:00Z", "RPC resume_task failed: ValueError",
                   subsystem="engine:rpc", level="ERROR")]
        report = build_report(result=self._result(), timeline=timeline, run_dir=None,
                              copy_download_logs=False)
        check = next(a for a in report["assertions"] if a["id"] == "telemetry_clean")
        self.assertFalse(check["passed"])
        self.assertIn("engine:rpc", check["detail"])

    def test_traceback_in_message_fails_the_clean_check(self) -> None:
        timeline = self._timeline() + [
            _event("2026-09-17T03:02:00Z", "Traceback (most recent call last): boom",
                   subsystem="engine:transfer", level="WARN")]
        report = build_report(result=self._result(), timeline=timeline, run_dir=None,
                              copy_download_logs=False)
        self.assertFalse(next(a for a in report["assertions"]
                              if a["id"] == "telemetry_clean")["passed"])

    def test_solves_must_match_observed_challenges(self) -> None:
        self.assertFalse(next(a for a in self._build(solves=0)["assertions"]
                              if a["id"] == "solves_iff_challenge")["passed"])
        report = build_report(result=dict(self._result(), solves=3), timeline=[],
                              run_dir=None, copy_download_logs=False)
        self.assertFalse(next(a for a in report["assertions"]
                              if a["id"] == "solves_iff_challenge")["passed"])

    def test_solver_timeout_is_reported_and_asserted(self) -> None:
        timeline = self._timeline() + [
            _event("2026-09-17T03:03:00Z", "[ff77aa88] [CAPTCHA_DETECTED] x",
                   subsystem="plugin:datanodes:engine:captcha", task_id="ff77aa88"),
            _event("2026-09-17T03:04:00Z", "[ff77aa88] [CHALLENGE_TIMEOUT] solver timed out",
                   subsystem="plugin:datanodes:engine:captcha", level="WARN",
                   task_id="ff77aa88", engine="clearcote")]
        report = build_report(result=self._result(), timeline=timeline, run_dir=None,
                              copy_download_logs=False)
        timed_out = next(s for s in report["solvers"] if s["task_id"] == "ff77aa88")
        self.assertEqual((timed_out["outcome"], timed_out["duration_ms"]), ("timeout", 60000.0))
        self.assertFalse(next(a for a in report["assertions"]
                              if a["id"] == "no_solver_timeout")["passed"])

    def test_unmatched_records_are_noted_never_dropped(self) -> None:
        timeline = [
            _event("2026-09-17T03:00:00Z", "[CAPTCHA_SOLVED_SUCCESS] Challenge zz solved",
                   challenge_id="zz", solver="clearcote"),
            _event("2026-09-17T03:00:05Z", "[dd55ee66] [CAPTCHA_DETECTED] x",
                   subsystem="plugin:datanodes:engine:captcha", task_id="dd55ee66"),
            _event("2026-09-17T03:00:06Z", "[TIMER_DETECTED] no task here", seconds=5),
        ]
        builder = LiveReportBuilder(result=self._result(), timeline=timeline,
                                    run_dir=None, copy_download_logs=False)
        report = builder.build()
        codes = {n["code"] for n in report["notes"]}
        self.assertIn("solver_outcome_without_open_challenge", codes)
        self.assertIn("solver_challenge_never_resolved", codes)
        self.assertIn("timer_event_without_task", codes)
        self.assertIn("task_without_download_log", codes)
        self.assertTrue(all(note.get("reason") for note in report["notes"]))
        # the orphan and the never-resolved episode are still emitted, not dropped
        self.assertEqual(len(report["solvers"]), 2)

    def test_write_report_is_reloadable(self) -> None:
        import tempfile
        report = self._build()
        with tempfile.TemporaryDirectory() as tmp:
            path = write_report(report, Path(tmp))
            self.assertEqual(path.name, "report.json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["verdict"], "pass")


@unittest.skipUnless(KRISTALA.is_dir() and RESONANCE.is_dir(),
                     "captured live run artifacts are not present")
class CapturedRunTests(unittest.TestCase):
    """Ground truth: real artifacts from two completed live runs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.kristala = build_report(hoster="datanodes", destination="N:\\dl's", run_dir=None,
                                    copy_download_logs=False, **load_run_artifacts(KRISTALA))
        cls.resonance = build_report(hoster="datanodes", destination="N:\\dl's", run_dir=None,
                                     copy_download_logs=False, **load_run_artifacts(RESONANCE))

    def test_schema_keys_are_complete(self) -> None:
        for report in (self.kristala, self.resonance):
            self.assertEqual(
                set(report),
                {"schema", "run", "stages", "per_task", "solvers", "timers",
                 "concurrency_ramp", "archive_jobs", "downloads", "assertions",
                 "notes", "utilization", "resolve_phase", "verdict"})
            self.assertEqual(set(report["run"]),
                             {"started", "wall_seconds", "package", "parts", "hoster",
                              "destination"})

    def test_kristala_run_header(self) -> None:
        run = self.kristala["run"]
        self.assertRegex(run["package"], r"^Kristala_--_[\w.-]+_--_$")  # the release site tag varies
        self.assertEqual(run["parts"], 5)
        self.assertEqual(run["started"], "2026-09-16T23:11:34Z")
        self.assertEqual(run["wall_seconds"], 546.0)

    def test_kristala_per_part_throughput_matches_benchmark_logs(self) -> None:
        by_id = {t["task_id"][:8]: t for t in self.kristala["per_task"]}
        self.assertEqual(len(by_id), 5)
        part1 = by_id["fb37956e"]
        self.assertRegex(part1["name"], r"^Kristala_--_[\w.-]+_--_\.part1\.rar$")
        self.assertEqual((part1["size"], part1["bytes"]), (2097152000, 2097152000))
        self.assertEqual((part1["mb_s_avg"], part1["mb_s_peak"], part1["mb_s_p95"]),
                         (18.77, 75.86, 67.02))
        self.assertEqual(part1["seconds"], 106.57)
        for task in self.kristala["per_task"]:
            self.assertEqual(task["state"], "completed")
            self.assertLessEqual(task["mb_s_avg"], task["mb_s_peak"])
            self.assertLessEqual(task["mb_s_p95"], task["mb_s_peak"])

    def test_kristala_downloading_union_is_below_the_naive_sum(self) -> None:
        stages = self.kristala["stages"]
        naive = sum(t["stage_seconds"].get("downloading", 0.0)
                    for t in self.kristala["per_task"])
        self.assertEqual(stages["downloading"]["seconds"], 464.462)
        self.assertLess(stages["downloading"]["seconds"], naive)
        self.assertEqual(len(stages["downloading"]["task_ids"]), 5)
        self.assertEqual(stages["unraring_extracting"]["seconds"], 10.14)

    def test_kristala_solver_tracks(self) -> None:
        solvers = self.kristala["solvers"]
        self.assertEqual(len(solvers), 10)
        self.assertEqual(sum(1 for s in solvers if s["track"] == "engine"), 3)
        self.assertEqual(sum(1 for s in solvers if s["outcome"] == "timeout"), 2)
        exact = [s for s in solvers if s["duration_exact"]]
        self.assertEqual(len(exact), 4)
        self.assertTrue(all(30000 < s["duration_ms"] < 60000 for s in exact))
        self.assertTrue(all(s["solver"] in {"clearcote", None} for s in solvers))

    def test_kristala_concurrency_ramp_climbs_one_to_five(self) -> None:
        ramp = self.kristala["concurrency_ramp"]
        self.assertTrue(all(step["host"] == "node42.datanodes.to" for step in ramp))
        self.assertEqual([step["ceiling"] for step in ramp], [1, 1, 2, 2, 3, 3, 4, 4, 5])
        verified = [s["active_streams"] for s in ramp if s["event"] == "PROBE_VERIFIED"]
        self.assertEqual(verified, [2, 3, 4, 5])

    def test_kristala_archive_job_completed(self) -> None:
        jobs = self.kristala["archive_jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["state"], "completed")
        self.assertEqual(jobs[0]["seconds"], 10.156)
        self.assertEqual(jobs[0]["stage_detail"]["operation"], "extract")

    def test_kristala_downloads_reference_every_part(self) -> None:
        downloads = self.kristala["downloads"]
        self.assertEqual(len(downloads), 5)
        self.assertTrue(all(d["source_json"] and d["source_log"] for d in downloads))
        self.assertEqual({d["task_id"] for d in downloads},
                         {t["task_id"] for t in self.kristala["per_task"]})

    def test_kristala_assertions_isolate_the_two_real_defects(self) -> None:
        results = {a["id"]: a["passed"] for a in self.kristala["assertions"]}
        self.assertTrue(results["all_tasks_completed"])
        self.assertTrue(results["integrity_verified"])
        self.assertTrue(results["archive_jobs_completed"])
        self.assertTrue(results["destination_files_present"])
        self.assertTrue(results["bytes_match_size"])
        self.assertTrue(results["solves_iff_challenge"])
        # Two real regressions this run captured: the solver timed out twice and
        # the engine logged ERROR-level RPC failures while siblings resolved.
        self.assertFalse(results["no_solver_timeout"])
        self.assertFalse(results["telemetry_clean"])
        self.assertEqual(self.kristala["verdict"], "fail")

    def test_resonance_two_parts_completed_but_extraction_failed(self) -> None:
        report = self.resonance
        self.assertEqual(report["run"]["parts"], 2)
        self.assertEqual([t["state"] for t in report["per_task"]], ["completed", "completed"])
        self.assertEqual([t["mb_s_avg"] for t in report["per_task"]], [50.49, 46.13])
        self.assertEqual(report["archive_jobs"][0]["state"], "failed")
        self.assertEqual(report["archive_jobs"][0]["stage_detail"]["error"],
                         "Could not open archive")
        results = {a["id"]: a["passed"] for a in report["assertions"]}
        self.assertFalse(results["archive_jobs_completed"])
        self.assertTrue(results["no_solver_timeout"])
        self.assertTrue(results["telemetry_clean"])
        self.assertEqual(report["verdict"], "fail")

    def test_resonance_wait_timer_countdown_was_honoured(self) -> None:
        armed = [t for t in self.resonance["timers"] if t["marker"] == "TIMER_ARMED"]
        self.assertEqual(len(armed), 1)
        self.assertEqual((armed[0]["countdown_seconds"], armed[0]["waited_seconds"]), (10, 10.0))
        self.assertEqual(armed[0]["source"], "vue_prop")
        detected = [t for t in self.resonance["timers"] if t["marker"] == "TIMER_DETECTED"]
        self.assertEqual({t["source"] for t in detected}, {"vue_prop", "html:text_wait"})
        self.assertTrue(all(t["waited_seconds"] <= t["countdown_seconds"] for t in detected))

    def test_resonance_solver_durations_come_from_the_engine_context(self) -> None:
        provider = [s for s in self.resonance["solvers"] if s["track"] == "provider"]
        self.assertEqual(len(provider), 2)
        self.assertEqual(sorted(s["duration_ms"] for s in provider), [36538.6, 37631.8])
        self.assertTrue(all(s["duration_exact"] for s in provider))
        engine = [s for s in self.resonance["solvers"] if s["track"] == "engine"]
        self.assertEqual([s["challenge_id"] for s in engine],
                         ["0b4f4ca6-a2ef-4a20-a81f-1ff4dbef3a05"])

    def test_notes_only_flag_known_benign_conditions(self) -> None:
        self.assertEqual({n["code"] for n in self.resonance["notes"]},
                         {"stage_intervals_open_ended"})
        self.assertEqual({n["code"] for n in self.kristala["notes"]},
                         {"stage_intervals_open_ended", "solver_challenge_never_resolved"})


if __name__ == "__main__":
    unittest.main()
