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
        with self.assertRaisesRegex(ValueError, "Step 4"):
            runner.validate_reset([12_500], False)

    def test_post_fix_flag_requires_real_source_fix(self) -> None:
        self.assertFalse(runner.post_fix_source_ready())
        with self.assertRaisesRegex(ValueError, "Step 4"):
            runner.validate_reset([12_500], True)

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
            command = [
                sys.executable,
                str(SCRIPT),
                mode,
                "--duplicate-observations",
                "2",
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
            self.assertEqual(result["schema_version"], 4)
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
                        "completed_manifest_jobs": 2,
                        "generation_identities": 2,
                        "publication_signals": 1,
                    },
                )
                self.assertEqual(
                    workload["coordinator_residue"],
                    {"jobs": 0, "corrections": 0, "generations": 0},
                )
                self.assertEqual(
                    workload["generation"]["snapshot_coverage"],
                    {"expected": 12500, "included": 12500, "excluded": 0},
                )
                self.assertEqual(
                    workload["generation"]["army_coverage"],
                    {"expected": 12500, "included": 12500, "excluded": 0},
                )
                self.assertEqual(
                    workload["coordinator_job_counts"],
                    {
                        "build_army_analytics": 1,
                        "build_snapshot": 1,
                    },
                )
                self.assertEqual(workload["snapshot_headers"], 2)
                self.assertEqual(workload["snapshot_entries"], 25000)
                self.assertEqual(workload["queue_residue"], [])
                self.assertEqual(
                    sample["evidence"]["execution_method"],
                    "bounded PostgreSQL-only coordinator cardinality proof",
                )
                continue
            self.assertTrue(
                all("oldest_active_age_seconds" in row for row in queue_rows)
            )
            if mode == "duplicate-heavy":
                operations = sample["workload"]["collector_archive_operations"]
                self.assertTrue(operations["executed"])
                self.assertEqual(operations["count"], 2)
                self.assertEqual(operations["head"], 2)
                self.assertEqual(operations["get"], 1)
                self.assertEqual(operations["raw_put"], 1)
                self.assertEqual(operations["raw_get"], 1)
                self.assertEqual(operations["raw_head"], 0)
                self.assertEqual(operations["raw_duplicate_bucket_requests"], 0)
                self.assertGreaterEqual(operations["operation_total_us"], 0)
                self.assertGreaterEqual(operations["stage_put_us"], 0)
                self.assertEqual(operations["put"], 1)
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
