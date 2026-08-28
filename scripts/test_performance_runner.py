from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/performance_runner.py"
SPEC = importlib.util.spec_from_file_location("performance_runner", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class PerformanceRunnerTest(unittest.TestCase):
    def test_known_bad_target_is_rejected_even_as_one_population(self) -> None:
        with self.assertRaisesRegex(ValueError, "post-fix"):
            runner.validate_reset([12_500], False)

    def test_post_fix_flag_requires_bounded_snapshot_and_army_writers(self) -> None:
        self.assertTrue(runner.post_fix_source_ready())
        runner.validate_reset([12_500], True)

    def test_duplicate_mode_uses_fixed_endpoint_mix(self) -> None:
        self.assertEqual(runner.DUPLICATE_EXECUTION_CAP, 25_024)
        self.assertEqual(
            runner._duplicate_endpoint_mix(25_024),
            {
                "profile": 12_500,
                "battle_log": 12_500,
                "global_player_rankings": 24,
            },
        )
        self.assertEqual(
            runner._duplicate_endpoint_mix(6),
            {"profile": 2, "battle_log": 2, "global_player_rankings": 2},
        )

    def test_artifact_validation_rejects_missing_and_old_metrics(self) -> None:
        artifact = {
            "schema_version": runner.ARTIFACT_SCHEMA_VERSION,
            "mode": "duplicate-heavy",
            "started_at": "now",
            "finished_at": "now",
            "provenance": {},
            "collector_probe": None,
            "samples": [
                {"database": {}, "archive_operations": {}, "storage_runway": {}}
            ],
            "army_read_sample": None,
        }
        with self.assertRaisesRegex(ValueError, "digest"):
            runner.validate_artifact(artifact)
        artifact["artifact_digest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest"):
            runner.validate_artifact(artifact)
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        self.assertEqual(artifact["artifact_digest"], runner._artifact_digest(artifact))
        with self.assertRaisesRegex(ValueError, "relation_sizes"):
            runner.validate_artifact(artifact)
        artifact["schema_version"] = 4
        with self.assertRaisesRegex(ValueError, "schema_version"):
            runner.validate_artifact(artifact)

    def test_duplicate_artifact_rejects_old_per_occurrence_shape(self) -> None:
        database_keys = (
            "wal_bytes",
            "sql_statement_calls",
            "application_sql_calls",
            "pending_remote_verification",
            "response_counts_by_endpoint",
            "occurrence_counts_by_endpoint",
            "relations",
            "relation_sizes",
            "relation_stats",
            "affected_relations",
            "queues",
            "queue_age_seconds",
            "queue_residue",
        )
        archive_keys = (
            "get",
            "get_bytes",
            "head",
            "conditional_put",
            "put",
            "put_bytes",
            "conflicts",
        )
        sample = {
            "database": dict.fromkeys(database_keys),
            "archive_operations": dict.fromkeys(archive_keys),
            "storage_runway": {
                "measured_local_growth_bytes": 0,
                "days_to_80_percent": None,
                "checks": {},
            },
            "workload": {
                "response_counts_by_endpoint": {},
                "occurrence_counts_by_endpoint": {},
                "fixture_bytes_by_endpoint": {},
                "exact_bytes": 0,
                "contract": {
                    "expected_occurrences": 25_024,
                    "executed_occurrences": 25_024,
                    "endpoint_mix": {},
                },
            },
        }
        artifact = {
            "schema_version": runner.ARTIFACT_SCHEMA_VERSION,
            "mode": "duplicate-heavy",
            "started_at": "now",
            "finished_at": "now",
            "provenance": {},
            "collector_probe": None,
            "samples": [sample],
            "army_read_sample": None,
        }
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "canonical_content"):
            runner.validate_artifact(artifact)

    def test_army_mode_freezes_production_protocol(self) -> None:
        arguments = runner.parse_arguments(
            ["army-analytics", "--database-url", "unused"]
        )
        self.assertEqual(arguments.army_warmups, 5)
        self.assertEqual(arguments.army_requests, 100)
        self.assertEqual(arguments.analytics_lanes, 4)
        self.assertEqual(
            [(item["selection"], item["lens"]) for item in runner._army_selection_specs()],
            [
                ("top-1000", "offense"),
                ("top-1000", "defense"),
                ("trophies-5000-9999", "offense"),
                ("trophies-5000-9999", "defense"),
                ("streak-top-1000", "offense"),
                ("streak-top-1000", "defense"),
            ],
        )
        self.assertEqual(
            [item["expected_facts"] for item in runner._army_selection_specs()],
            [224_000, 224_000, 2_772_000, 2_772_000, 224_000, 224_000],
        )

    def test_army_source_guard_rejects_old_broad_materialization(self) -> None:
        source = (ROOT / "python/src/clashlens/api_db.py").read_text()
        self.assertTrue(runner._bounded_army_source_ready(source))
        old_shape = """
        def get_army_analytics(self, selection):
            facts = connection.execute(\"\"\"
                SELECT * FROM army_analytics_battle_facts
                WHERE official_season_id=%s AND lens=%s AND is_current
            \"\"\", (selection.season, selection.lens)).fetchall()
            return filter_members_in_python(facts)
        """
        self.assertFalse(runner._bounded_army_source_ready(old_shape))

    def test_army_protocol_rejects_partial_measurement(self) -> None:
        with self.assertRaises(SystemExit):
            runner.parse_arguments(
                [
                    "army-analytics",
                    "--database-url",
                    "unused",
                    "--analytics-lanes",
                    "2",
                ]
            )

    def test_plan_counts_filter_rows_for_each_actual_loop(self) -> None:
        scanned, returned = runner._plan_counts(
            {"Actual Rows": 3, "Actual Loops": 4, "Rows Removed by Filter": 2}
        )
        self.assertEqual((scanned, returned), (20, 3))

    def test_provenance_fingerprint_includes_processing_and_image_inputs(self) -> None:
        arguments = runner.parse_arguments(
            [
                "army-analytics",
                "--database-url",
                "unused",
                "--lanes",
                "7",
                "--image",
                "postgres=sha256:" + "a" * 64,
            ]
        )
        provenance = runner._provenance(arguments)
        self.assertEqual(provenance["configuration"]["lanes"], 7)
        self.assertEqual(
            provenance["configuration"]["images"], ["postgres=sha256:" + "a" * 64]
        )
        self.assertEqual(
            provenance["configuration_fingerprint"],
            runner._sha(
                json.dumps(provenance["configuration"], sort_keys=True).encode()
            ),
        )

    def test_army_mode_rejects_malformed_image_digest(self) -> None:
        with self.assertRaises(SystemExit):
            runner.parse_arguments(
                [
                    "army-analytics",
                    "--database-url",
                    "unused",
                    "--image",
                    "postgres=sha256:not-a-digest",
                ]
            )

    def test_memory_pressure_delta_never_hides_increases(self) -> None:
        self.assertEqual(
            runner._memory_pressure_delta(
                {
                    "process_swap_used_bytes": 10,
                    "process_oom": 2,
                    "process_oom_kill": 1,
                    "database_swap_used_bytes": 4,
                    "database_oom": 0,
                    "database_oom_kill": 0,
                },
                {
                    "process_swap_used_bytes": 14,
                    "process_oom": 3,
                    "process_oom_kill": 1,
                    "database_swap_used_bytes": 4,
                    "database_oom": 0,
                    "database_oom_kill": 1,
                },
            ),
            {
                "process_swap_used_bytes": 4,
                "process_oom": 1,
                "process_oom_kill": 0,
                "database_swap_used_bytes": 0,
                "database_oom": 0,
                "database_oom_kill": 1,
            },
        )

    def test_writer_guard_rejects_row_at_a_time_sql(self) -> None:
        source = """
        def writer(connection, rows):
            for row in rows:
                connection.execute('INSERT INTO entries VALUES (%s)', (row,))
        """
        self.assertFalse(
            runner._bounded_writer_source_ready(source, "writer", "INSERT INTO")
        )

    def test_statement_metrics_use_public_schema(self) -> None:
        source = SCRIPT.read_text()
        self.assertEqual(source.count("FROM public.pg_stat_statements"), 2)
        self.assertNotIn("FROM pg_stat_statements", source)

    def test_non_reset_modes_do_not_validate_reset_population(self) -> None:
        arguments = runner.parse_arguments(
            [
                "duplicate-heavy",
                "--populations",
                "12500",
                "--database-url",
                "unused",
            ]
        )
        self.assertEqual(arguments.populations, [12_500])

    def test_army_fact_limit_is_bounded(self) -> None:
        with self.assertRaises(SystemExit):
            runner.parse_arguments(
                [
                    "reset-boundary",
                    "--populations",
                    "1",
                    "--army-facts",
                    "100001",
                ]
            )

    def test_missing_database_fails_instead_of_emitting_invented_measurements(
        self,
    ) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "duplicate-heavy",
                "--populations",
                "1",
                "--database-url",
                "",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("database-url", completed.stderr)

    def test_runway_uses_measured_user_usable_capacity(self) -> None:
        usage = runner._filesystem_usage(ROOT)
        filesystem = os.statvfs(ROOT)
        raw_capacity = int(filesystem.f_blocks * filesystem.f_frsize)
        available = int(filesystem.f_bavail * filesystem.f_frsize)
        expected_capacity = raw_capacity - max(
            0, int(filesystem.f_bfree - filesystem.f_bavail) * filesystem.f_frsize
        )

        self.assertEqual(usage["raw_capacity_bytes"], raw_capacity)
        self.assertEqual(usage["usable_capacity_bytes"], expected_capacity)
        self.assertEqual(usage["available_bytes"], available)
        runway = runner._runway_inputs(
            usage,
            usage,
            {"relation_sizes": {}, "wal_bytes": 0},
            {},
            {},
            0,
        )
        self.assertEqual(runway["filesystem_capacity_bytes"], expected_capacity)
        self.assertEqual(runway["filesystem_raw_capacity_bytes"], raw_capacity)
        self.assertEqual(runway["target_utilization"], 0.80)
        self.assertEqual(runway["target_used_bytes"], int(expected_capacity * 0.80))
        self.assertTrue(runway["checks"]["usable_capacity_measured"])

    def test_runway_projects_measured_growth_per_interval(self) -> None:
        filesystem = {
            "path": "/tmp",
            "usable_capacity_bytes": 1_000,
            "raw_capacity_bytes": 1_000,
            "used_bytes": 100,
        }
        runway = runner._runway_inputs(
            filesystem,
            filesystem,
            {"relation_sizes": {"players": {"total_bytes": 100}}, "wal_bytes": 50},
            {},
            {},
            0,
            measured_intervals=2,
        )

        self.assertEqual(runway["measured_local_growth_bytes"], 150)
        self.assertEqual(runway["projected_daily_local_growth_bytes"], 21_600)
        self.assertEqual(runway["target_used_bytes"], 800)
        self.assertEqual(runway["target_utilization"], 0.80)

    def test_archive_probe_marker_parses_totals(self) -> None:
        marker = (
            '{"count":4,"head":5,"get":4,"put":1,'
            '"raw_count":4,"raw_head":0,"raw_put":1,"raw_get":1,'
            '"raw_duplicate_bucket_requests":0,'
            '"hash_us":3,"operation_total_us":1500,"stage_put_us":900,'
            '"stage_get_verify_us":400,"local_verify_us":9}'
        )
        parsed = runner._parse_archive_probe_marker(
            "go test noise\n" + runner.ARCHIVE_PROBE_MARKER + marker + "\n"
        )
        self.assertEqual(parsed["count"], 4)
        self.assertEqual(parsed["raw_head"], 0)
        self.assertEqual(parsed["raw_duplicate_bucket_requests"], 0)

    def test_archive_probe_marker_rejects_missing_and_malformed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "emitted 0 markers"):
            runner._parse_archive_probe_marker("no marker here")
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            runner._parse_archive_probe_marker(
                runner.ARCHIVE_PROBE_MARKER + "{not json}"
            )
        with self.assertRaisesRegex(RuntimeError, "integer"):
            runner._parse_archive_probe_marker(
                runner.ARCHIVE_PROBE_MARKER + '{"count":4,"head":5,"get":"4","put":1}'
            )

    def test_archive_counts_real_gets(self) -> None:
        from urllib.request import urlopen

        with runner.archive_server() as archive:
            body = b"fixture"
            digest = runner._sha(body)
            key = f"sha256/{digest[:2]}/{digest}"
            archive[3].objects[key] = body
            with urlopen(f"http://{archive[0]}/evidence/{key}") as response:
                self.assertEqual(response.read(), body)
            self.assertEqual(archive[3].gets, 1)


@unittest.skipUnless(
    os.environ.get("CLASHLENS_TEST_DATABASE_URL"),
    "set CLASHLENS_TEST_DATABASE_URL for real PostgreSQL workload tests",
)
class PerformanceRunnerPostgresTest(unittest.TestCase):
    def test_duplicate_canonical_metrics_follow_schema_seam(self) -> None:
        from domain_test_support import domain_database

        metrics = []
        for include_coordinator in (False, True):
            with (
                domain_database(
                    os.environ["CLASHLENS_TEST_DATABASE_URL"],
                    include_coordinator=include_coordinator,
                ) as connection_info,
                runner.archive_server() as archive,
            ):
                workload = runner._run_duplicate(connection_info, archive, 6)
            metrics.append(workload["canonical_content"])

        self.assertEqual(metrics[0].keys(), metrics[1].keys())
        self.assertEqual(
            metrics[0],
            {
                "parsed_payloads_by_endpoint": {
                    "battle_log": 1,
                    "global_player_rankings": 1,
                    "profile": 2,
                },
                "profile_semantic_versions": 2,
                "profile_occurrence_effects": 2,
                "battle_canonical_rows": 0,
                "battle_occurrence_rows": 4,
                "ranking_canonical_rows": 0,
                "ranking_occurrence_links": 400,
            },
        )
        self.assertEqual(
            metrics[1],
            {
                **metrics[0],
                "battle_canonical_rows": 2,
                "ranking_canonical_rows": 200,
            },
        )

    def test_duplicate_exact_bytes_counts_each_variant_in_each_cycle(self) -> None:
        import json

        from domain_test_support import domain_database

        original_profile_body = runner._profile_body
        original_fixture_body = runner._duplicate_fixture_body
        captured_lengths: list[int] = []

        def varied_profile_body(tag: str, variant: int = 0) -> bytes:
            source = json.loads(original_profile_body(tag, 0))
            source["name"] += "x" * variant
            return json.dumps(source, separators=(",", ":")).encode()

        def capture_fixture_body(*args, **kwargs):
            tag, body = original_fixture_body(*args, **kwargs)
            captured_lengths.append(len(body))
            return tag, body

        runner._profile_body = varied_profile_body
        runner._duplicate_fixture_body = capture_fixture_body
        try:
            with (
                domain_database(
                    os.environ["CLASHLENS_TEST_DATABASE_URL"], include_coordinator=True
                ) as connection_info,
                runner.archive_server() as archive,
            ):
                workload = runner._run_duplicate(connection_info, archive, 6, cycles=2)
        finally:
            runner._profile_body = original_profile_body
            runner._duplicate_fixture_body = original_fixture_body

        self.assertEqual(len(captured_lengths), 12)
        self.assertNotEqual(captured_lengths[0], captured_lengths[1])
        self.assertEqual(workload["exact_bytes"], sum(captured_lengths))

    def test_connection_execute_is_counted_once_by_cursor_hook(self) -> None:
        import psycopg

        with (
            runner.count_sql_calls() as count,
            psycopg.connect(os.environ["CLASHLENS_TEST_DATABASE_URL"]) as connection,
        ):
            connection.execute("SELECT 1").fetchone()
        self.assertEqual(count[0], 1)

    def test_all_modes_execute_isolated_real_workloads(self) -> None:
        for mode in runner.MODES:
            if mode == runner.STEP5_MODE:
                continue
            command = [
                sys.executable,
                str(SCRIPT),
                mode,
                "--duplicate-observations",
                "6",
                "--live-jobs",
                "1",
                "--backfill-jobs",
                "1",
                "--skip-collector-probe",
            ]
            if mode != "coordinator-12500":
                command.extend(["--populations", "1"])
            completed = subprocess.run(
                command,
                cwd=ROOT,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            result = json.loads(completed.stdout)
            self.assertEqual(result["schema_version"], runner.ARTIFACT_SCHEMA_VERSION)
            sample = result["samples"][0]
            self.assertGreater(sample["database"]["wal_bytes"], 0)
            self.assertGreater(sample["database"]["application_sql_calls"], 0)
            self.assertIn("collector_jobs", sample["database"]["queues"])
            self.assertIn("python_processing_jobs", sample["database"]["queues"])
            if mode != "coordinator-12500":
                self.assertGreater(sample["archive_operations"]["get"], 0)
            queue_rows = sample["database"]["queues"]["python_processing_jobs"]
            if mode == "coordinator-12500":
                workload = sample["workload"]
                self.assertEqual(
                    workload["contract"], {"database_version": 4, "required_version": 4}
                )
                self.assertEqual(
                    workload["coverage"],
                    {"expected": 12500, "included": 12500, "excluded": 0},
                )
                self.assertEqual(workload["manifest_publication"]["manifest_count"], 2)
                self.assertEqual(
                    workload["manifest_publication"]["manifest_rows"], 25000
                )
                self.assertEqual(workload["publication_identities"], 2)
                self.assertEqual(workload["generation"]["snapshot_state"], "published")
                self.assertEqual(workload["generation"]["army_state"], "published")
                self.assertEqual(
                    workload["coordinator_links"],
                    {
                        "sealed_manifests": 2,
                        "completed_manifest_jobs": 3,
                        "generation_identities": 2,
                        "publication_signals": 1,
                    },
                )
                self.assertEqual(
                    workload["coordinator_residue"],
                    {"jobs": 0, "corrections": 0, "generations": 0},
                )
                snapshot_coverage = workload["generation"]["snapshot_coverage"]
                self.assertEqual(snapshot_coverage["expected_population_count"], 12500)
                self.assertEqual(snapshot_coverage["included_entry_count"], 12500)
                army_coverage = workload["generation"]["army_coverage"]
                self.assertEqual(
                    {
                        key: army_coverage[key]
                        for key in ("expected", "included", "excluded")
                    },
                    {"expected": 12500, "included": 12500, "excluded": 0},
                )
                self.assertEqual(
                    workload["coordinator_job_counts"],
                    {
                        "build_analytics": 1,
                        "build_army_analytics": 1,
                        "build_snapshot": 1,
                    },
                )
                self.assertEqual(workload["snapshot_headers"], 2)
                self.assertEqual(workload["snapshot_entries"], 25000)
                self.assertEqual(workload["queue_residue"], [])
                self.assertEqual(
                    sample["evidence"]["execution_method"],
                    "real Python snapshot, analytics, and army writers",
                )
                continue
            self.assertTrue(
                all("oldest_active_age_seconds" in row for row in queue_rows)
            )
            if mode == "duplicate-heavy":
                operations = sample["workload"]["collector_archive_operations"]
                self.assertTrue(operations["executed"])
                self.assertEqual(operations["count"], 6)
                self.assertEqual(operations["head"], 6)
                self.assertEqual(operations["get"], 5)
                self.assertEqual(operations["raw_put"], 1)
                self.assertEqual(operations["raw_get"], 1)
                self.assertEqual(operations["raw_head"], 0)
                self.assertEqual(operations["raw_duplicate_bucket_requests"], 0)
                self.assertGreaterEqual(operations["operation_total_us"], 0)
                self.assertGreaterEqual(operations["stage_put_us"], 0)
                self.assertEqual(operations["put"], 1)
                self.assertEqual(
                    sample["workload"]["occurrence_counts_by_endpoint"],
                    {"profile": 2, "battle_log": 2, "global_player_rankings": 2},
                )
                self.assertEqual(
                    sample["database"]["response_counts_by_endpoint"],
                    {"profile": 2, "battle_log": 2, "global_player_rankings": 2},
                )
                self.assertGreater(operations["elapsed_seconds"], 0)
            if mode in {"reset-boundary", "correction"}:
                self.assertTrue(
                    sample["workload"]["fanout_evidence"]["matches_expected"]
                )
                army = result["army_read_sample"]
                self.assertGreater(army["database"]["wal_bytes"], 0)
                self.assertGreater(army["database"]["application_sql_calls"], 0)
                for key in ("elapsed_seconds", "cpu_seconds", "peak_rss_kib"):
                    self.assertIn(key, army)
                reads = army["selections"]
                self.assertEqual(len(reads), 3)
                self.assertTrue(
                    all(read["rows_scanned"] >= read["rows_returned"] for read in reads)
                )
                self.assertTrue(all(read["rows_returned"] > 0 for read in reads))
                self.assertTrue(
                    all(read["endpoint"]["status"] == "returned" for read in reads)
                )
                self.assertTrue(
                    all("Plan" in read["explain_analyze_buffers"] for read in reads)
                )
                self.assertLess(
                    sample["workload"]["fact_counts"]["snapshot_entries"], 1000
                )


if __name__ == "__main__":
    unittest.main()
