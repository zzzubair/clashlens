from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/performance_runner.py"
SPEC = importlib.util.spec_from_file_location("performance_runner", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

SOURCE_SHA = "01" * 20


def _clean_git(*arguments: str) -> str:
    if arguments[0] == "status":
        return ""
    if arguments[0] == "rev-parse":
        return SOURCE_SHA
    raise AssertionError(arguments)


def _test_postgres() -> dict[str, object]:
    return {
        "version": "PostgreSQL 18.6",
        "settings": {"server_version_num": "180006"},
        "applied_migration_versions": list(range(1, 14)),
    }


def _provenance(mode: str = "duplicate-heavy", candidate_receipt: Path | None = None) -> dict:
    argv = [mode]
    if candidate_receipt is not None:
        argv.extend(["--candidate-receipt", str(candidate_receipt)])
    arguments = runner.parse_arguments(argv)
    with mock.patch.object(runner, "_git", side_effect=_clean_git):
        return runner._provenance(arguments, postgres=_test_postgres())


def _valid_artifact(mode: str = "duplicate-heavy") -> dict:
    provenance = _provenance(mode)
    return {
        "schema_version": runner.ARTIFACT_SCHEMA_VERSION,
        "mode": mode,
        "started_at": "2026-08-28T20:00:00+00:00",
        "finished_at": "2026-08-28T20:00:01+00:00",
        "provenance": provenance,
        "execution": provenance["execution"],
        "prepared_candidate_images": provenance["prepared_candidate_images"],
        "candidate_receipt": provenance["candidate_receipt"],
        "official_api_requests": {"count": 0, "source": "committed fixtures"},
        "collector_probe": None,
        "samples": [
            {"database": {}, "archive_operations": {}, "storage_runway": {}}
        ],
        "army_read_sample": None,
        "hard_failures": [],
    }


def _candidate_receipt() -> dict:
    from scripts import deployment_receipt

    migrations = runner._source_migrations()
    fields = {
        name: "1" for name in sorted(deployment_receipt.SAFE_CONFIGURATION_FIELDS)
    }
    configuration = {
        "allowlist_version": "step8-v1",
        "fields": fields,
        "fingerprint": "sha256:"
        + runner._sha(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()),
    }
    images = {
        application: {
            "requested_reference": f"localhost/clashlens-{application}:deployment",
            "identity_type": "image_id",
            "image_id": "sha256:" + "02" * 32,
            "registry_digest": None,
            "source_label": deployment_receipt.CANONICAL_REPOSITORY_URL,
            "revision_label": SOURCE_SHA,
        }
        for application in ("collector", "python", "website")
    }
    result = {
        "schema_version": deployment_receipt.SCHEMA_VERSION,
        "receipt_scope": "candidate-preparation",
        "environment_identity": "fedora-validation",
        "production_deployment_status": "not_asserted",
        "created_at": "2026-08-28T19:00:00+00:00",
        "source": {
            "repository_url": deployment_receipt.CANONICAL_REPOSITORY_URL,
            "revision": SOURCE_SHA,
            "clean": True,
            "clean_check": "git-status-porcelain-v1-with-untracked-files",
        },
        "migrations": [
            {"filename": item["name"], "sha256": item["sha256"], "applied": True}
            for item in migrations
        ],
        "configuration": configuration,
        "application_images": images,
        "database": {
            "contract_version": 5,
            "applied_migration_versions": list(range(1, 14)),
            "server_version": "18.6",
            "server_version_num": "180006",
            "system_identifier": "1234567890",
            "container_name": "step8-postgres",
            "database_name": "clashlens",
            "identity_scope": "disposable_validation_database",
        },
        "runtime_versions": {
            "receipt_python": "3.12.0",
            "podman": "podman version 5.8.4",
            "postgresql": "18.6",
        },
        "official_api_requests": {
            "count": 0,
            "proof": "receipt-command-inspects-images-and-database-only",
        },
    }
    result["receipt_digest"] = deployment_receipt._canonical_digest(result)
    deployment_receipt.validate_receipt(result, require_digest=True)
    return result


class PerformanceRunnerTest(unittest.TestCase):
    def test_known_bad_target_is_rejected_even_as_one_population(self) -> None:
        with self.assertRaisesRegex(ValueError, "post-fix"):
            runner.validate_reset([12_500], False)

    def test_post_fix_flag_requires_bounded_snapshot_and_army_writers(self) -> None:
        self.assertTrue(runner.post_fix_source_ready())
        runner.validate_reset([12_500], True)

    def test_provenance_effective_lanes_matches_each_mode(self) -> None:
        for mode, configured, effective in (
            ("mixed-backfill", 8, 8),
            ("mixed-backfill", 64, 32),
            ("army-analytics", 64, 64),
            ("coordinator-12500", 64, 64),
            ("duplicate-heavy", 64, 64),
        ):
            arguments = runner.parse_arguments([mode, "--lanes", str(configured)])
            with mock.patch.object(runner, "_git", side_effect=_clean_git):
                provenance = runner._provenance(
                    arguments, postgres=_test_postgres()
                )
            self.assertEqual(provenance["configuration"]["lanes"], configured)
            self.assertEqual(provenance["configuration"]["effective_lanes"], effective)

    def test_post_fix_is_part_of_configuration_fingerprint(self) -> None:
        fingerprints = []
        for post_fix in (False, True):
            arguments = runner.parse_arguments(
                ["duplicate-heavy", "--database-url", "unused"]
                + (["--post-fix"] if post_fix else [])
            )
            with mock.patch.object(runner, "_git", side_effect=_clean_git):
                provenance = runner._provenance(
                    arguments, postgres=_test_postgres()
                )
            self.assertEqual(provenance["configuration"]["post_fix"], post_fix)
            self.assertEqual(
                set(provenance["configuration"]), runner.CONFIGURATION_KEYS
            )
            fingerprints.append(provenance["configuration_fingerprint"])
        self.assertNotEqual(*fingerprints)

    def test_result_summary_is_bounded_and_drops_job_identity(self) -> None:
        summary = runner._result_summary(
            [
                {
                    "job_id": 101,
                    "outcome": "processed",
                    "status": "complete",
                    "work_type": "redecode_army",
                    "kind": "backfill",
                    "elapsed_ms": 4.0,
                },
                {
                    "job_id": 102,
                    "outcome": "retrying",
                    "status": "waiting_retry",
                    "work_type": "process_observation",
                    "kind": "live",
                    "elapsed_ms": 8.0,
                },
            ],
            expected=2,
        )
        self.assertNotIn("job_id", json.dumps(summary))
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["retry_count"], 1)
        self.assertEqual(summary["outcomes"]["processed"], 1)
        self.assertEqual(summary["outcomes"]["retrying"], 1)
        self.assertEqual(summary["work_types"]["redecode_army"], 1)

    def test_boundary_admission_evidence_is_exact_and_bounded(self) -> None:
        admitted = {
            "phase": "admit",
            "blocked_before_regular_drain": True,
            "state_before_drain": "regular_draining",
            "regular_nonterminal_before": 2,
            "state_after_admission": "reset_draining",
            "regular_drain_complete": True,
            "reset_drain_complete": False,
            "safe_handoff": False,
            "reset_generation": 1,
            "regular_nonterminal_after": 0,
            "reset_nonterminal_after": 3,
            "membership_count": 3,
            "reset_root_count": 3,
            "regular_allowed_during_reset": False,
            "regular_scheduled_during_reset": 0,
        }
        runner._validate_boundary_admission_evidence(admitted, "admit", 3)
        admitted["membership_count"] = 4
        with self.assertRaisesRegex(ValueError, "contradicts"):
            runner._validate_boundary_admission_evidence(admitted, "admit", 3)

    def test_boundary_admission_probe_retains_only_one_marker(self) -> None:
        handoff = {
            "phase": "handoff",
            "state": "safe_handoff",
            "regular_drain_complete": True,
            "reset_drain_complete": True,
            "safe_handoff": True,
            "reset_generation": 1,
            "handoff_recorded": True,
            "regular_nonterminal_count": 0,
            "reset_nonterminal_count": 0,
            "membership_count": 1,
            "completed_reset_root_count": 1,
            "regular_allowed_after_handoff": True,
            "regular_scheduled_after_handoff": 1,
        }
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "unretained go output\n"
                + runner._ADMISSION_MARKER
                + json.dumps(handoff)
                + "\n"
            ),
            stderr="SECRET-raw-stderr",
        )
        with mock.patch.object(runner.subprocess, "run", return_value=completed):
            self.assertEqual(
                runner._boundary_admission_probe("postgresql://SECRET", "handoff", 1),
                handoff,
            )

    def test_artifact_rejects_arbitrary_hard_failure_code(self) -> None:
        artifact = _valid_artifact()
        artifact["hard_failures"] = ["job 101 failed: SECRET"]
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "hard failures"):
            runner.validate_artifact(artifact)

    def test_artifact_rejects_internal_spool_path(self) -> None:
        artifact = _valid_artifact()
        artifact["_spool_root"] = "/tmp/high-cardinality-path"
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "internal or per-job details"):
            runner.validate_artifact(artifact)

    def test_artifact_rejects_unknown_or_sensitive_nested_fields(self) -> None:
        for section, field, value in (
            (None, "unexpected", "arbitrary"),
            ("provenance", "unexpected", "arbitrary"),
            ("provenance", "database_url", "postgresql://secret@host/db"),
        ):
            with self.subTest(section=section, field=field):
                artifact = _valid_artifact()
                target = artifact if section is None else artifact[section]
                target[field] = value
                artifact["artifact_digest"] = runner._artifact_digest(artifact)
                with self.assertRaises(ValueError):
                    runner.validate_artifact(artifact)

    def test_artifact_rejects_unbounded_nested_sequences(self) -> None:
        artifact = _valid_artifact()
        artifact["provenance"]["host"]["unexpected"] = list(
            range(runner.MAX_RETAINED_SEQUENCE + 1)
        )
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "unbounded sequence"):
            runner.validate_artifact(artifact)

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
        artifact = _valid_artifact()
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
            "wal_retained_bytes",
            "wal_retained_growth_bytes",
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
        artifact = _valid_artifact()
        artifact["samples"] = [sample]
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

    def test_provenance_fingerprint_is_sanitized_and_deterministic(self) -> None:
        arguments = runner.parse_arguments(
            [
                "army-analytics",
                "--database-url",
                "unused",
                "--lanes",
                "7",
            ]
        )
        with mock.patch.object(runner, "_git", side_effect=_clean_git):
            provenance = runner._provenance(arguments, postgres=_test_postgres())
        self.assertEqual(provenance["configuration"]["lanes"], 7)
        self.assertNotIn("images", provenance["configuration"])
        self.assertEqual(
            provenance["configuration_fingerprint"],
            runner._sha(
                json.dumps(
                    provenance["configuration"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
        )

    def test_ambiguous_image_option_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            runner.parse_arguments(
                [
                    "army-analytics",
                    "--database-url",
                    "unused",
                    "--image",
                    "postgres=sha256:" + "a" * 64,
                ]
            )

    def test_dirty_source_is_rejected_before_provenance_is_emitted(self) -> None:
        arguments = runner.parse_arguments(["duplicate-heavy"])
        with mock.patch.object(
            runner, "_git", side_effect=lambda *args: " M scripts/performance_runner.py"
            if args[0] == "status"
            else SOURCE_SHA,
        ), self.assertRaisesRegex(RuntimeError, "clean"):
            runner._provenance(arguments, postgres=_test_postgres())

    def test_execution_images_are_distinct_from_prepared_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            path.write_text(json.dumps(_candidate_receipt()), encoding="utf-8")
            provenance = _provenance(candidate_receipt=path)
        self.assertEqual(provenance["execution"]["kind"], "host")
        self.assertEqual(provenance["execution"]["executor_images"], [])
        self.assertEqual(
            {item["identity_type"] for item in provenance["prepared_candidate_images"]},
            {"prepared_candidate_image_id"},
        )
        self.assertEqual(
            provenance["candidate_receipt"]["receipt_digest"],
            _candidate_receipt()["receipt_digest"],
        )

    def test_candidate_receipt_stale_or_tampered_provenance_is_rejected(self) -> None:
        value = _candidate_receipt()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            value["source"]["revision"] = "03" * 20
            for identity in value["application_images"].values():
                identity["revision_label"] = "03" * 20
            from scripts import deployment_receipt

            value["receipt_digest"] = deployment_receipt._canonical_digest(value)
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "candidate receipt"):
                _provenance(candidate_receipt=path)

            value = _candidate_receipt()
            value["receipt_digest"] = "sha256:" + "0" * 64
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "candidate receipt"):
                _provenance(candidate_receipt=path)

    def test_artifact_validation_rejects_contradictory_or_stale_provenance(self) -> None:
        artifact = _valid_artifact()
        artifact["execution"] = {"kind": "container", "executor_images": []}
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "execution provenance"):
            runner.validate_artifact(artifact)

        artifact = _valid_artifact()
        artifact["provenance"]["runner_sha256"] = "0" * 64
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "runner hash"):
            runner.validate_artifact(artifact)

    def test_artifact_output_is_complete_and_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            runner._write_artifact(path, '{"complete":true}\n')
            self.assertEqual(path.read_text(encoding="utf-8"), '{"complete":true}\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            with self.assertRaisesRegex(RuntimeError, "occupied"):
                runner._write_artifact(path, '{"replacement":true}\n')

    def test_main_retains_coherent_hard_failure_before_nonzero(self) -> None:
        artifact = _valid_artifact()
        artifact["hard_failures"] = ["fixed_acceptance_failure"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hard-failure.json"
            with mock.patch.object(runner, "run", return_value=artifact):
                result = runner.main(
                    [
                        "duplicate-heavy",
                        "--database-url",
                        "postgresql://fixture.invalid/clashlens",
                        "--output",
                        str(path),
                    ]
                )

            self.assertEqual(result, 2)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["hard_failures"],
                ["fixed_acceptance_failure"],
            )

    def test_post_reset_army_failure_is_bounded_and_retained(self) -> None:
        with mock.patch.object(
            runner,
            "_run_army_read_sample",
            side_effect=RuntimeError("SECRET-player-#TAG"),
        ):
            result = runner._retained_army_read_sample(
                "postgresql://fixture.invalid/clashlens", 1
            )

        self.assertEqual(
            result,
            {
                "status": "failed",
                "reason": "army_read_sample_unavailable",
                "hard_failures": ["army_read_sample_unavailable"],
            },
        )
        self.assertNotIn("SECRET", json.dumps(result))

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
            {
                "relation_sizes": {},
                "wal_bytes": 0,
                "wal_retained_growth_bytes": 0,
            },
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
            {
                "relation_sizes": {"players": {"total_bytes": 100}},
                "wal_bytes": 50,
                "wal_retained_growth_bytes": 50,
            },
            {},
            {},
            0,
            measured_intervals=2,
        )

        self.assertEqual(runway["measured_local_growth_bytes"], 150)
        self.assertEqual(runway["projected_daily_local_growth_bytes"], 21_600)
        self.assertEqual(runway["target_used_bytes"], 800)
        self.assertEqual(runway["target_utilization"], 0.80)

    def test_runway_uses_retained_wal_growth_not_generated_lsn_bytes(self) -> None:
        filesystem = {
            "path": "/tmp",
            "usable_capacity_bytes": 1_000,
            "raw_capacity_bytes": 1_000,
            "used_bytes": 100,
        }
        runway = runner._runway_inputs(
            filesystem,
            filesystem,
            {
                "relation_sizes": {},
                "wal_bytes": 500,
                "wal_retained_bytes": 120,
                "wal_retained_growth_bytes": 15,
            },
            {},
            {},
            0,
        )
        self.assertEqual(runway["postgres_wal_bytes"], 500)
        self.assertEqual(runway["postgres_wal_retained_growth_bytes"], 15)
        self.assertEqual(runway["measured_local_growth_bytes"], 15)

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
            if mode == "mixed-backfill":
                command.extend(["--lanes", "8"])
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
            self.assertGreaterEqual(sample["database"]["wal_retained_bytes"], 0)
            self.assertGreaterEqual(sample["database"]["wal_retained_growth_bytes"], 0)
            self.assertGreater(sample["database"]["application_sql_calls"], 0)
            self.assertIn("collector_jobs", sample["database"]["queues"])
            self.assertIn("python_processing_jobs", sample["database"]["queues"])
            if mode != "coordinator-12500":
                self.assertGreater(sample["archive_operations"]["get"], 0)
            queue_rows = sample["database"]["queues"]["python_processing_jobs"]
            if mode == "coordinator-12500":
                workload = sample["workload"]
                self.assertEqual(
                    workload["contract"], {"database_version": 5, "required_version": 5}
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
            if mode == "mixed-backfill":
                workload = sample["workload"]
                self.assertEqual(workload["completion_counts"], {"live": 1, "backfill": 1})
                self.assertEqual(workload["configured_lanes"], 8)
                self.assertEqual(workload["effective_lanes"], 8)
                self.assertEqual(
                    result["provenance"]["configuration"]["effective_lanes"],
                    workload["effective_lanes"],
                )
                self.assertEqual(workload["live_latency_contract"]["passed"], True)
                self.assertEqual(workload["five_minute_contract"]["passed"], True)
                self.assertEqual(workload["hard_failures"], [])
                self.assertEqual(workload["official_api_traffic"]["requests"], 0)
                self.assertEqual(workload["processing_summary"]["kinds"], {"live": 1, "backfill": 1, "other": 0})
                self.assertEqual(workload["processing_summary"]["work_types"]["redecode_army"], 1)
                self.assertEqual(workload["processing_summary"]["outcomes"]["processed"], 2)
                self.assertEqual(workload["database"]["queue_residue"], [])
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
                if mode == "correction":
                    self.assertEqual(
                        [
                            state["generation"]
                            for state in sample["workload"]["fanout_evidence"]["generation_states"]
                            if state["generation"] in {1, 2}
                        ],
                        [1, 2],
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
