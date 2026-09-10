from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from datetime import timedelta
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
        "settings": {
            "server_version": "18.6",
            "server_version_num": "180006",
            "shared_buffers": "128MB",
            "work_mem": "4MB",
            "maintenance_work_mem": "64MB",
            "max_connections": "100",
            "track_io_timing": "off",
        },
        "applied_migration_versions": list(range(1, 26)),
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
            {"database": {}, "archive_operations": {}, "storage_runway": {"filesystem_type": "ext4", "inode_model": "finite"}}
        ],
        "army_read_sample": None,
        "hard_failures": [],
    }


def _valid_safe_value(name: str) -> str:
    if name in ("endpoint_budget_enabled", "player_discovery_enabled"):
        return "true"
    if name == "endpoint_budget_run_id":
        return "issue92"
    if name == "endpoint_budget_deadline_at":
        return "2026-09-09T06:00:00Z"
    return "1"


def _candidate_receipt() -> dict:
    from scripts import deployment_receipt

    migrations = runner._source_migrations()
    def _field(name: str) -> str:
        if name in ("endpoint_budget_enabled", "player_discovery_enabled"):
            return "true"
        if name == "endpoint_budget_run_id":
            return "issue92"
        if name == "endpoint_budget_deadline_at":
            return "2026-09-09T06:00:00Z"
        if name == "official_api_proxy_url":
            return "http://100.64.0.1:3128"
        if name == "admission_evidence_run_id":
            return "disabled"
        if name in ("admission_evidence_start", "admission_evidence_end"):
            return "disabled"
        if name in ("admission_evidence_max_events", "admission_evidence_max_selected_entries"):
            return "0"
        return "1"
    fields = {
        name: _field(name)
        for name in sorted(deployment_receipt.SAFE_CONFIGURATION_FIELDS)
    }
    configuration = {
        "allowlist_version": deployment_receipt.CONFIGURATION_ALLOWLIST_VERSION,
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
            "applied_migration_versions": list(range(1, 26)),
            "server_version": "18.6",
            "server_version_num": "180006",
            "system_identifier": "1234567890",
            "container_name": "step8-postgres",
            "database_name": "clashlens",
            "identity_scope": "disposable_validation_database",
        },
        "candidate_resources": {
            "postgres_container": {
                "name": "step8-postgres",
                "scope_label": deployment_receipt.CANDIDATE_SCOPE_LABEL,
            },
            "network": {
                "name": "step8-private",
                "scope_label": deployment_receipt.CANDIDATE_SCOPE_LABEL,
            },
            "volume": {
                "name": "step8-postgres-data",
                "scope_label": deployment_receipt.CANDIDATE_SCOPE_LABEL,
            },
            "application_containers": {
                "collector": {"name": "step8-collector", "present": False},
                "python": {"name": "step8-python-api", "present": False},
                "worker": {"name": "step8-python-worker", "present": False},
                "worker_replicas": [
                    {
                        "name": f"step8-python-worker-{replica}",
                        "present": False,
                    }
                    for replica in range(1, 17)
                ],
                "website": {"name": "step8-website", "present": False},
            },
        },
        "runtime_versions": {
            "receipt_python": "3.12.0",
            "podman": "podman version 5.8.4",
            "postgresql": "18.6",
        },
        "official_api_requests": {
            "count": 0,
            "proof": deployment_receipt.CANDIDATE_RECEIPT_OFFICIAL_API_PROOF,
        },
    }
    result["receipt_digest"] = deployment_receipt._canonical_digest(result)
    deployment_receipt.validate_receipt(result, require_digest=True)
    return result


def _valid_duplicate_artifact(observations: int = 6, cycles: int = 1) -> dict:
    cycle_counts = runner._duplicate_endpoint_mix(observations)
    endpoint_counts = {endpoint: count * cycles for endpoint, count in cycle_counts.items()}
    profile_window = max(1, cycle_counts["profile"] // 200)
    parsed_profile = (cycle_counts["profile"] + profile_window - 1) // profile_window
    unique_hashes = parsed_profile + int(cycle_counts["battle_log"] > 0) + int(
        cycle_counts["global_player_rankings"] > 0
    )
    fixture_bytes = {
        "profile": len(runner._profile_body(runner._tag(1))),
        "battle_log": len(runner.BATTLE_FIXTURE),
        "global_player_rankings": len(runner.RANKING_FIXTURE),
    }
    exact_bytes = sum(endpoint_counts[key] * fixture_bytes[key] for key in endpoint_counts)
    archived_bytes = sum(
        unique_count * fixture_bytes[key]
        for key, unique_count in {
            "profile": parsed_profile,
            "battle_log": int(cycle_counts["battle_log"] > 0),
            "global_player_rankings": int(cycle_counts["global_player_rankings"] > 0),
        }.items()
    )
    relation_stats = {
        "collector_observations": {"total_bytes": 100},
        "parsed_source_payloads": {"total_bytes": 100},
        "archive_catalogue": {"total_bytes": 100},
        "python_processing_jobs": {"total_bytes": 100},
    }
    database = {
        "wal_bytes": 1,
        "wal_retained_bytes": 1,
        "wal_retained_growth_bytes": 0,
        "sql_statement_calls": 1,
        "application_sql_calls": 1,
        "pending_remote_verification": 0,
        "response_counts_by_endpoint": dict(endpoint_counts),
        "occurrence_counts_by_endpoint": dict(endpoint_counts),
        "relations": {key: 100 for key in relation_stats},
        "relation_sizes": dict(relation_stats),
        "relation_stats": relation_stats,
        "affected_relations": list(relation_stats),
        "queues": {"collector_jobs": [], "python_processing_jobs": []},
        "queue_age_seconds": {"collector_jobs": None, "python_processing_jobs": None},
        "queue_residue": [],
    }
    summary = runner._result_summary(
        [{"outcome": "processed"}] * (observations * cycles),
        expected=observations * cycles,
    )
    workload = {
        "observations": observations,
        "official_responses": observations * cycles,
        "executed_observations": observations * cycles,
        "measured_cycles": cycles,
        "cycle_elapsed_seconds": [1.0] * cycles,
        "median_cycle_seconds": 1.0,
        "daily_288_cycle_projection_seconds": 288.0,
        "aggregation_factor": 1.0,
        "aggregation_method": "exact bounded cycle",
        "endpoint_mix": dict(endpoint_counts),
        "response_counts_by_endpoint": dict(endpoint_counts),
        "occurrence_counts_by_endpoint": dict(endpoint_counts),
        "fixture_bytes_by_endpoint": fixture_bytes,
        "exact_bytes": exact_bytes,
        "official_api_traffic": {"requests": 0, "source": "committed fixtures"},
        "canonical_content": {
            "parsed_payloads_by_endpoint": {
                "profile": parsed_profile,
                "battle_log": int(cycle_counts["battle_log"] > 0),
                "global_player_rankings": int(cycle_counts["global_player_rankings"] > 0),
            },
            "profile_semantic_versions": parsed_profile,
            "profile_occurrence_effects": cycle_counts["profile"] * cycles,
            "battle_canonical_rows": 1,
            "battle_occurrence_rows": cycle_counts["battle_log"] * 2 * cycles,
            "ranking_canonical_rows": 1,
            "ranking_occurrence_links": cycle_counts["global_player_rankings"] * 200 * cycles,
        },
        "contract": {
            "expected_occurrences": runner.DUPLICATE_EXECUTION_CAP,
            "executed_occurrences": observations,
            "matches_expected": observations == runner.DUPLICATE_EXECUTION_CAP,
            "endpoint_mix": dict(runner.DUPLICATE_ENDPOINT_MIX),
        },
        "fixture_discoveries_prequalified": 1,
        "processing_summary": summary,
        "stage_metrics": {
            stage: {"average_ms": 1.0}
            for stage in runner._DUPLICATE_LATENCY_STAGES.values()
        },
        "spool": {
            "final_bytes": archived_bytes,
            "final_objects": unique_hashes,
            "temporary_bytes": 0,
            "temporary_objects": 0,
            "abandoned_temp_bytes": 0,
            "abandoned_temp_objects": 0,
            "reserved_bytes": 0,
            "reserved_objects": 0,
            "high_water_bytes": archived_bytes,
            "free_inodes": 100,
            "allocated_blocks": 1,
        },
        "evidence_counters": {
            "local_hits": observations * cycles - unique_hashes,
            "local_misses": unique_hashes,
            "repairs": unique_hashes,
            "provider_errors": 0,
        },
        "collector_archive_operations": {
            "executed": True,
            "test": "TestS3ArchiveDuplicateStoreProbe",
            "count": observations,
            "head": observations,
            "get": observations - 1,
            "put": 1,
            "raw_count": observations,
            "raw_head": 0,
            "raw_put": 1,
            "raw_get": 1,
            "raw_duplicate_bucket_requests": 0,
            "hash_us": 1,
            "operation_total_us": 1,
            "stage_put_us": 1,
            "stage_get_verify_us": 1,
            "local_verify_us": 1,
            "elapsed_seconds": 1.0,
        },
    }
    archive = {
        "get": unique_hashes,
        "get_bytes": archived_bytes,
        "head": 0,
        "conditional_put": 0,
        "put": 0,
        "put_bytes": 0,
        "conflicts": 0,
    }
    artifact = _valid_artifact()
    artifact["provenance"]["configuration"]["duplicate_observations"] = observations
    artifact["provenance"]["configuration"]["duplicate_cycles"] = cycles
    artifact["provenance"]["configuration_fingerprint"] = runner._sha(
        json.dumps(
            artifact["provenance"]["configuration"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    artifact["samples"] = [
        {
            "workload": workload,
            "database": database,
            "archive_operations": archive,
            "storage_runway": {
                "measured_local_growth_bytes": 0,
                "days_to_80_percent": None,
                "checks": {},
                "filesystem_type": "ext4",
                "inode_model": "finite",
            },
            "evidence": {
                "response_count": observations * cycles,
                "executed_responses": observations * cycles,
                "projected_responses": 0,
                "execution_method": "exact bounded cycle",
                "distinct_hashes": unique_hashes,
                "novelty_rate": unique_hashes / (observations * cycles),
                "exact_bytes": exact_bytes,
                "archived_bytes": archived_bytes,
                "pending_verification_count": 0,
                "pending_verification_age_seconds": None,
                "orphan_count": 0,
                "orphan_bytes": 0,
                "local_hits": observations * cycles - unique_hashes,
                "local_misses": unique_hashes,
                "repairs": unique_hashes,
                "provider_errors": 0,
                "retries": 0,
                "concurrency_lanes": 32,
                "latency_ms": {
                    **{name: 1.0 for name in runner._DUPLICATE_LATENCY_STAGES},
                    "collector_hashing_us": 1,
                    "collector_operation_total_us": 1,
                    "collector_remote_put_us": 1,
                    "collector_get_verify_us": 1,
                    "collector_local_verify_us": 1,
                },
            },
            "spool": {
                "final_bytes": archived_bytes,
                "temporary_bytes": 0,
                "high_water_bytes": archived_bytes,
                "final_object_count": unique_hashes,
                "temporary_object_count": 0,
                "live_reservations": 0,
                "allocated_blocks": 1,
                "free_inodes": 100,
                "filesystem_type": "ext4",
                "inode_model": "finite",
            },
            "elapsed_seconds": 1.0,
            "cpu_seconds": 1.0,
            "peak_rss_kib": 1,
        }
    ]
    artifact["artifact_digest"] = runner._artifact_digest(artifact)
    return artifact


def _army_memory() -> dict[str, int]:
    return {
        "host_swap_used_bytes": 0,
        "process_cgroup_available": 1,
        "process_swap_used_bytes": 0,
        "process_oom": 0,
        "process_oom_kill": 0,
        "database_cgroup_available": 1,
        "database_swap_used_bytes": 0,
        "database_oom": 0,
        "database_oom_kill": 0,
    }


def _army_plan(selection: str, lens: str, identity: str, statement_id: int) -> dict:
    shape = runner._ARMY_QUERY_SHAPES[(selection, identity)]
    return {
        "correlation": {
            "selection": selection,
            "lens": lens,
            "statement_id": statement_id,
        },
        "sql": identity,
        "parameters": {"arity": len(shape), "types": list(shape)},
        "rows_scanned": 1,
        "rows_returned": 1,
        "explain_analyze_buffers": [
            {
                "Plan": {
                    "Node Type": "Aggregate",
                    "Actual Rows": 1,
                    "Actual Loops": 1,
                },
                "Planning Time": 1.0,
                "Execution Time": 1.0,
            }
        ],
    }


def _valid_army_artifact() -> dict:
    duplicate = _valid_duplicate_artifact(runner.DUPLICATE_EXECUTION_CAP)
    collection = deepcopy(duplicate["samples"][0]["workload"])
    collection["cycle_elapsed_seconds"] = 1.0
    memory = _army_memory()
    delta = {
        key: 0
        for key in runner._ARMY_MEMORY_DELTA_KEYS
    }
    selections = []
    for spec in runner._army_selection_specs():
        selections.append(
            {
                "selection": spec["selection"],
                "lens": spec["lens"],
                "warmups": runner.STEP5_WARMUPS,
                "requests": runner.STEP5_REQUESTS,
                "forced_miss_seconds": 1.0,
                "forced_miss_target_seconds": runner.STEP5_FORCED_MISS_TARGET_SECONDS,
                "forced_miss_passed": True,
                "forced_miss_memory_before": deepcopy(memory),
                "forced_miss_memory_after": deepcopy(memory),
                "forced_miss_memory_delta": dict(delta),
                "p95_ms": 10.0,
                "min_ms": 10.0,
                "max_ms": 10.0,
                "latencies_ms": [10.0] * runner.STEP5_REQUESTS,
                "selected_fact_count": spec["expected_facts"],
                "expected_fact_count": spec["expected_facts"],
                "troop_keys": list(runner.STEP5_TROOP_KEYS),
                "peak_rss_kib": 1,
                "endpoint_sql": [
                    _army_plan(spec["selection"], spec["lens"], identity, index)
                    for index, identity in enumerate(
                        runner._ARMY_QUERY_ORDER[spec["selection"]], 1
                    )
                ],
                "target_ms": runner.STEP5_P95_TARGET_MS,
                "target_passed": True,
            }
        )
    pairs = [(spec["selection"], spec["lens"]) for spec in runner._army_selection_specs()]
    lanes = [
        {
            "selection": selection,
            "lens": lens,
            "warmups": runner.STEP5_WARMUPS,
            "requests": runner.STEP5_REQUESTS,
            "p95_ms": 10.0,
            "selected_fact_count": spec["expected_facts"],
            "troop_keys": list(runner.STEP5_TROOP_KEYS),
            "overlap_measurements": runner.STEP5_REQUESTS,
            "target_ms": runner.STEP5_P95_TARGET_MS,
            "target_passed": True,
        }
        for (selection, lens), spec in zip(
            pairs, runner._army_selection_specs(), strict=True
        )
    ]
    account = {
        "warmups": runner.STEP5_WARMUPS,
        "requests": runner.STEP5_REQUESTS,
        "p95_ms": 10.0,
        "min_ms": 10.0,
        "max_ms": 10.0,
        "latencies_ms": [10.0] * runner.STEP5_REQUESTS,
        "target_ms": runner.STEP5_P95_TARGET_MS,
        "target_passed": True,
        "overlap_measurements": runner.STEP5_REQUESTS,
    }
    mixed = {
        "analytics_lanes": lanes,
        "account": account,
        "overlap_counts": {
            f"{selection}/{lens}": runner.STEP5_REQUESTS
            for selection, lens in pairs
        },
        "account_overlap_measurements": runner.STEP5_REQUESTS,
        "collection_cycle": collection,
        "hard_failures": [],
    }
    collection_counts = collection["occurrence_counts_by_endpoint"]
    database = deepcopy(duplicate["samples"][0]["database"])
    for relation in runner.STEP5_STATISTICS_RELATIONS:
        database["relations"][relation] = 100
        database["relation_sizes"][relation] = {"total_bytes": 100}
        database["relation_stats"][relation] = {
            "total_bytes": 100,
            "last_analyze": "2026-08-28T20:00:00+00:00",
        }
        if relation in runner.AFFECTED_RELATIONS:
            database["affected_relations"].append(relation)
    database["response_counts_by_endpoint"] = dict(collection_counts)
    database["occurrence_counts_by_endpoint"] = dict(collection_counts)
    database["response_counts_by_endpoint"]["profile"] += 1
    database["occurrence_counts_by_endpoint"]["profile"] += 1
    army = {
        "status": "passed",
        "failed_phase": None,
        "failure": None,
        "protocol": {
            "population": runner.STEP5_POPULATION,
            "query_work_mem": "256MB",
            "days": runner.STEP5_DAYS,
            "facts_per_member_day_per_lens": runner.STEP5_FACTS_PER_MEMBER_DAY,
            "selected_members": runner.STEP5_SELECTED_MEMBERS,
            "missing_trophy_rate": f"1/{runner.STEP5_MISSING_TROPHY_RATE}",
            "troop_keys": len(runner.STEP5_TROOP_KEYS),
            "warmups": runner.STEP5_WARMUPS,
            "requests": runner.STEP5_REQUESTS,
            "p95_target_ms": runner.STEP5_P95_TARGET_MS,
            "forced_miss_target_seconds": runner.STEP5_FORCED_MISS_TARGET_SECONDS,
            "forced_miss_pool_max_size": 2,
            "forced_miss_read_snapshot": "repeatable_read_exported",
            "mixed_lane_pool_max_size": runner.STEP5_MIXED_LANE_POOL_MAX_SIZE,
            "mixed_lane_read_snapshot": "repeatable_read_exported",
            "analytics_lanes": runner.STEP5_ANALYTICS_LANES,
            "duplicate_cycle_observations": runner.DUPLICATE_EXECUTION_CAP,
        },
        "seed": {
            "population": runner.STEP5_POPULATION,
            "days": runner.STEP5_DAYS,
            "facts_per_lens": runner.STEP5_POPULATION * runner.STEP5_DAYS * runner.STEP5_FACTS_PER_MEMBER_DAY,
            "missing_trophies_per_lens": runner.STEP5_POPULATION * runner.STEP5_DAYS * runner.STEP5_FACTS_PER_MEMBER_DAY // runner.STEP5_MISSING_TROPHY_RATE,
            "snapshots": runner.STEP5_DAYS,
            "snapshot_entries": runner.STEP5_DAYS * runner.STEP5_SELECTED_MEMBERS,
            "completed_days": runner.STEP5_DAYS,
            "selected_facts_per_lens": runner.STEP5_SELECTED_MEMBERS * runner.STEP5_DAYS * runner.STEP5_FACTS_PER_MEMBER_DAY,
            "troop_keys": len(runner.STEP5_TROOP_KEYS),
        },
        "statistics_readiness": {
            "relations": list(runner.STEP5_STATISTICS_RELATIONS),
            "readiness_timeout_seconds": runner.STEP5_STATISTICS_TIMEOUT_SECONDS,
            "analyze_completed": True,
            "active_analyzes": 0,
            "ready": True,
        },
        "selections": selections,
        "mixed_load": mixed,
        "database": database,
        "postgres": _test_postgres(),
        "elapsed_seconds": 1.0,
        "cpu_seconds": 1.0,
        "peak_rss_kib": 1,
        "memory_pressure_before": deepcopy(memory),
        "memory_pressure_after": deepcopy(memory),
        "memory_pressure_delta": delta,
        "hard_failures": [],
        "queue_drained": True,
    }
    provenance = _provenance("army-analytics")
    artifact = {
        "schema_version": runner.ARTIFACT_SCHEMA_VERSION,
        "mode": "army-analytics",
        "started_at": "2026-08-28T20:00:00+00:00",
        "finished_at": "2026-08-28T20:00:01+00:00",
        "provenance": provenance,
        "execution": provenance["execution"],
        "prepared_candidate_images": provenance["prepared_candidate_images"],
        "candidate_receipt": provenance["candidate_receipt"],
        "official_api_requests": {"count": 0, "source": "committed fixtures"},
        "collector_probe": None,
        "samples": [],
        "army_read_sample": army,
        "hard_failures": [],
    }
    artifact["artifact_digest"] = runner._artifact_digest(artifact)
    return artifact


def _valid_partial_army_artifact() -> dict:
    artifact = _valid_army_artifact()
    sample = artifact["army_read_sample"]
    sample["status"] = "failed"
    sample["failed_phase"] = "selection_reads"
    sample["failure"] = "request_timeout"
    sample["selections"] = sample["selections"][:2]
    sample["mixed_load"] = None
    sample["hard_failures"] = ["army_read_sample_unavailable"]
    artifact["hard_failures"] = ["army_read_sample_unavailable"]
    artifact["artifact_digest"] = runner._artifact_digest(artifact)
    return artifact


class PerformanceRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        if self._testMethodName == (
            "test_dirty_source_is_rejected_before_provenance_is_emitted"
        ):
            return
        source_patch = mock.patch.object(
            runner, "_clean_source", return_value=SOURCE_SHA
        )
        source_patch.start()
        self.addCleanup(source_patch.stop)

    def test_army_validation_allows_only_the_overlapped_pair_to_swap(self) -> None:
        artifact = _valid_army_artifact()
        sample = deepcopy(artifact["army_read_sample"]["selections"][0])
        plans = sample["endpoint_sql"]
        plans[-2], plans[-1] = plans[-1], plans[-2]
        for statement_id, plan in enumerate(plans, 1):
            plan["correlation"]["statement_id"] = statement_id
        runner._validate_army_selection(
            sample, runner._army_selection_specs()[0], "army-read"
        )

        duplicate = deepcopy(sample)
        duplicate["endpoint_sql"][-1] = duplicate["endpoint_sql"][-2]
        with self.assertRaises(ValueError):
            runner._validate_army_selection(
                duplicate, runner._army_selection_specs()[0], "army-read"
            )

        reordered = deepcopy(sample)
        reordered["endpoint_sql"][-3], reordered["endpoint_sql"][-2] = (
            reordered["endpoint_sql"][-2],
            reordered["endpoint_sql"][-3],
        )
        for statement_id, plan in enumerate(reordered["endpoint_sql"], 1):
            plan["correlation"]["statement_id"] = statement_id
        with self.assertRaises(ValueError):
            runner._validate_army_selection(
                reordered, runner._army_selection_specs()[0], "army-read"
            )

    def test_step5_troop_keys_use_retained_lexical_order(self) -> None:
        self.assertEqual(
            list(runner.STEP5_TROOP_KEYS),
            sorted(runner.STEP5_TROOP_KEYS),
        )

    def test_step5_partial_collection_cycle_artifact_is_valid(self) -> None:
        artifact = _valid_army_artifact()
        sample = artifact["army_read_sample"]
        cycle = sample["mixed_load"]["collection_cycle"]
        cycle["processing_summary"] = runner._result_summary(
            [{"outcome": "processed"}] * (runner.DUPLICATE_EXECUTION_CAP - 1),
            expected=runner.DUPLICATE_EXECUTION_CAP,
        )
        cycle["canonical_content"]["profile_occurrence_effects"] -= 1
        failures = ["step5_collection_result_count_mismatch"]
        sample["mixed_load"]["hard_failures"] = failures
        sample["hard_failures"] = failures
        sample["status"] = "failed"
        artifact["hard_failures"] = failures
        artifact["artifact_digest"] = runner._artifact_digest(artifact)

        runner.validate_artifact(artifact)

    def test_step5_partial_timeout_artifact_is_bounded_and_valid(self) -> None:
        artifact = _valid_partial_army_artifact()
        runner.validate_artifact(artifact)

        for mutate in (
            lambda sample: sample.__setitem__("failure", "SECRET-player-#TAG"),
            lambda sample: sample["selections"].reverse(),
            lambda sample: sample.__setitem__(
                "mixed_load", _valid_army_artifact()["army_read_sample"]["mixed_load"]
            ),
            lambda sample: sample.__setitem__("status", "passed"),
        ):
            changed = _valid_partial_army_artifact()
            mutate(changed["army_read_sample"])
            changed["artifact_digest"] = runner._artifact_digest(changed)
            with self.assertRaises(ValueError):
                runner.validate_artifact(changed)

        changed = _valid_partial_army_artifact()
        completed = changed["army_read_sample"]["selections"][0]
        completed.update(
            {
                "latencies_ms": [250.0] * runner.STEP5_REQUESTS,
                "p95_ms": 250.0,
                "min_ms": 250.0,
                "max_ms": 250.0,
                "target_passed": False,
            }
        )
        changed["artifact_digest"] = runner._artifact_digest(changed)
        with self.assertRaises(ValueError):
            runner.validate_artifact(changed)
        expected_failures = [
            "step5_p95_exceeded",
            "army_read_sample_unavailable",
        ]
        changed["army_read_sample"]["hard_failures"] = expected_failures
        changed["hard_failures"] = expected_failures
        changed["artifact_digest"] = runner._artifact_digest(changed)
        runner.validate_artifact(changed)

        for field, value in (
            ("readiness_timeout_seconds", 601),
            ("relations", ["army_analytics_battle_facts"]),
            ("active_analyzes", 1),
        ):
            changed = _valid_army_artifact()
            changed["army_read_sample"]["statistics_readiness"][field] = value
            changed["artifact_digest"] = runner._artifact_digest(changed)
            with self.assertRaises(ValueError):
                runner.validate_artifact(changed)

        for relation_map in ("relations", "relation_sizes", "relation_stats"):
            changed = _valid_army_artifact()
            del changed["army_read_sample"]["database"][relation_map][
                runner.STEP5_STATISTICS_RELATIONS[0]
            ]
            changed["artifact_digest"] = runner._artifact_digest(changed)
            with self.assertRaises(ValueError):
                runner.validate_artifact(changed)

        changed = _valid_army_artifact()
        changed["army_read_sample"]["database"]["relation_stats"][
            runner.STEP5_STATISTICS_RELATIONS[0]
        ]["last_analyze"] = None
        changed["artifact_digest"] = runner._artifact_digest(changed)
        with self.assertRaises(ValueError):
            runner.validate_artifact(changed)

        for failure, completed, active in (
            ("statistics_timeout", False, 0),
            ("statistics_timeout", True, None),
            ("statistics_not_ready", True, 1),
            ("statistics_unavailable", None, None),
            ("statistics_unavailable", True, None),
        ):
            changed = _valid_partial_army_artifact()
            sample = changed["army_read_sample"]
            sample["failed_phase"] = "statistics_readiness"
            sample["failure"] = failure
            sample["selections"] = []
            sample["statistics_readiness"].update(
                {
                    "analyze_completed": completed,
                    "active_analyzes": active,
                    "ready": False,
                }
            )
            changed["artifact_digest"] = runner._artifact_digest(changed)
            runner.validate_artifact(changed)

    def test_step5_statistics_deadline_and_unavailable_facts_are_explicit(self) -> None:
        with (
            mock.patch.object(
                runner.time, "monotonic", side_effect=[100.0, 700.0]
            ),
            mock.patch("psycopg.connect") as connect,
        ):
            readiness, failure = runner._prepare_step5_statistics("unused")
        connect.assert_not_called()
        self.assertEqual(failure, "statistics_timeout")
        self.assertEqual(readiness["analyze_completed"], False)
        self.assertIsNone(readiness["active_analyzes"])
        self.assertFalse(readiness["ready"])

        with (
            mock.patch.object(
                runner.time, "monotonic", side_effect=[100.0, 100.0, 100.0]
            ),
            mock.patch(
                "psycopg.connect", side_effect=RuntimeError("SECRET-player-#TAG")
            ),
        ):
            readiness, failure = runner._prepare_step5_statistics("unused")
        self.assertEqual(failure, "statistics_unavailable")
        self.assertIsNone(readiness["analyze_completed"])
        self.assertIsNone(readiness["active_analyzes"])
        self.assertNotIn("SECRET", json.dumps(readiness))
        self.assertNotIn("#TAG", json.dumps(readiness))

    def test_step5_failures_snapshot_before_schema_cleanup(self) -> None:
        import psycopg
        from psycopg_pool import PoolTimeout

        complete = _valid_army_artifact()["army_read_sample"]
        for error, expected_failure in (
            (
                psycopg.errors.TransactionTimeout("SECRET-player-#TAG"),
                "request_timeout",
            ),
            (PoolTimeout("SECRET-player-#TAG"), "request_timeout"),
            (RuntimeError("SECRET-player-#TAG"), "workload_error"),
        ):
            with self.subTest(error=type(error).__name__):
                alive = {"value": False}
                domain = mock.MagicMock()
                domain.__enter__.side_effect = lambda alive=alive: (
                    alive.__setitem__("value", True) or "postgresql://isolated"
                )
                domain.__exit__.side_effect = lambda *_, alive=alive: alive.__setitem__(
                    "value", False
                )
                archive = mock.MagicMock()
                archive.__enter__.return_value = object()
                readiness = deepcopy(complete["statistics_readiness"])
                database = deepcopy(complete["database"])

                def snapshot(
                    *_args: object,
                    alive: dict[str, bool] = alive,
                    database: dict = database,
                ) -> dict:
                    self.assertTrue(alive["value"])
                    return deepcopy(database)

                with (
                    mock.patch(
                        "domain_test_support.domain_database", return_value=domain
                    ),
                    mock.patch.object(runner, "archive_server", return_value=archive),
                    mock.patch.object(
                        runner,
                        "_memory_pressure",
                        side_effect=[
                            deepcopy(complete["memory_pressure_before"]),
                            deepcopy(complete["memory_pressure_after"]),
                        ],
                    ),
                    mock.patch.object(
                        runner, "_start_metrics", return_value=("0/0", None, 0)
                    ),
                    mock.patch.object(runner, "_relation_snapshot", return_value={}),
                    mock.patch.object(
                        runner,
                        "_seed_step5_army_database",
                        return_value=deepcopy(complete["seed"]),
                    ),
                    mock.patch.object(
                        runner,
                        "_prepare_step5_statistics",
                        return_value=(readiness, None),
                    ),
                    mock.patch.object(
                        runner,
                        "_measure_army_pair",
                        side_effect=[
                            deepcopy(complete["selections"][0]),
                            deepcopy(complete["selections"][1]),
                            error,
                        ],
                    ),
                    mock.patch.object(runner, "_run_step5_overlap") as overlap,
                    mock.patch.object(runner, "_db_snapshot", side_effect=snapshot),
                    mock.patch.object(
                        runner,
                        "_postgres_provenance",
                        return_value=deepcopy(complete["postgres"]),
                    ),
                ):
                    result = runner._run_step5_army("postgresql://disposable")

                overlap.assert_not_called()
                self.assertFalse(alive["value"])
                self.assertEqual(result["failed_phase"], "selection_reads")
                self.assertEqual(result["failure"], expected_failure)
                self.assertEqual(len(result["selections"]), 2)
                self.assertIsNone(result["mixed_load"])
                self.assertEqual(
                    result["hard_failures"], ["army_read_sample_unavailable"]
                )
                self.assertNotIn("SECRET", json.dumps(result))
                self.assertNotIn("#TAG", json.dumps(result))
                artifact = _valid_army_artifact()
                artifact["army_read_sample"] = result
                artifact["hard_failures"] = result["hard_failures"]
                artifact["artifact_digest"] = runner._artifact_digest(artifact)
                runner.validate_artifact(artifact)

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

    def test_duplicate_acceptance_facts_map_to_bounded_failures(self) -> None:
        clean = {
            "processing_summary": runner._result_summary(
                [{"outcome": "processed"}], expected=1
            )
        }
        self.assertEqual(
            runner._duplicate_hard_failure_codes(clean, {"queue_residue": []}),
            [],
        )

        retrying = {
            "processing_summary": runner._result_summary(
                [{"outcome": "retrying"}], expected=1
            )
        }
        failures = runner._duplicate_hard_failure_codes(
            retrying,
            {"queue_residue": [{"queue": "python_processing_jobs"}]},
        )
        self.assertEqual(
            failures,
            ["fixed_acceptance_failure", "queue_residue"],
        )
        self.assertTrue(set(failures).issubset(runner.ALLOWED_HARD_FAILURE_CODES))

    def test_duplicate_artifact_requires_derived_hard_failures(self) -> None:
        artifact = _valid_duplicate_artifact()
        runner.validate_artifact(artifact)

        artifact["samples"][0]["workload"]["processing_summary"] = runner._result_summary(
            [{"outcome": "lease_lost"}] * 6, expected=6
        )
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "hard failures are incomplete"):
            runner.validate_artifact(artifact)

        artifact["hard_failures"] = ["fixed_acceptance_failure"]
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        runner.validate_artifact(artifact)

        artifact = _valid_duplicate_artifact()
        artifact["samples"][0]["workload"]["processing_summary"] = (
            runner._result_summary([{"outcome": "processed"}] * 5, expected=6)
        )
        artifact["samples"][0]["workload"]["canonical_content"][
            "profile_occurrence_effects"
        ] -= 1
        artifact["hard_failures"] = ["fixed_acceptance_failure"]
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        runner.validate_artifact(artifact)

        artifact = _valid_duplicate_artifact()
        artifact["samples"][0]["workload"]["canonical_content"][
            "profile_occurrence_effects"
        ] -= 1
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaises(ValueError):
            runner.validate_artifact(artifact)

        artifact = _valid_duplicate_artifact()
        sample = artifact["samples"][0]
        workload = sample["workload"]
        workload["processing_summary"] = runner._result_summary(
            [{"outcome": "processed"}] * 5 + [{"outcome": "failed"}],
            expected=6,
        )
        workload["canonical_content"]["profile_occurrence_effects"] -= 1
        failed_bytes = workload["fixture_bytes_by_endpoint"]["profile"]
        workload["evidence_counters"]["repairs"] -= 1
        workload["spool"]["final_objects"] -= 1
        workload["spool"]["final_bytes"] -= failed_bytes
        sample["evidence"]["repairs"] -= 1
        sample["spool"]["final_object_count"] -= 1
        sample["spool"]["final_bytes"] -= failed_bytes
        sample["database"]["queue_residue"] = [
            {"queue": "python_processing_jobs"}
        ]
        artifact["hard_failures"] = [
            "fixed_acceptance_failure",
            "queue_residue",
        ]
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        runner.validate_artifact(artifact)

        repairs = sample["evidence"]["local_misses"] + 1
        workload["evidence_counters"]["repairs"] = repairs
        sample["evidence"]["repairs"] = repairs
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(
            ValueError, "local/archive/hash counters disagree"
        ):
            runner.validate_artifact(artifact)

        artifact = _valid_duplicate_artifact()
        artifact["samples"][0]["database"]["queue_residue"] = [
            {"queue": "python_processing_jobs"}
        ]
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "hard failures are incomplete"):
            runner.validate_artifact(artifact)

        artifact["hard_failures"].append("queue_residue")
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        runner.validate_artifact(artifact)

    def test_duplicate_observation_cap_is_rejected_before_probe(self) -> None:
        with self.assertRaises(SystemExit):
            runner.parse_arguments(
                [
                    "duplicate-heavy",
                    "--database-url",
                    "unused",
                    "--duplicate-observations",
                    str(runner.DUPLICATE_EXECUTION_CAP + 1),
                ]
            )

    def test_duplicate_two_cycle_contract_is_per_cycle(self) -> None:
        artifact = _valid_duplicate_artifact(
            runner.DUPLICATE_EXECUTION_CAP, cycles=2
        )
        runner.validate_artifact(artifact)
        workload = artifact["samples"][0]["workload"]
        self.assertEqual(
            workload["executed_observations"], 2 * runner.DUPLICATE_EXECUTION_CAP
        )
        self.assertEqual(
            workload["contract"]["executed_occurrences"],
            runner.DUPLICATE_EXECUTION_CAP,
        )
        workload["contract"]["executed_occurrences"] *= 2
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaises(ValueError):
            runner.validate_artifact(artifact)

    def test_duplicate_direct_reconciliations_are_required(self) -> None:
        mutations = (
            ("local misses", lambda sample: sample["evidence"].__setitem__("local_misses", 3)),
            ("archive gets", lambda sample: sample["archive_operations"].__setitem__("get", 3)),
            ("repairs", lambda sample: sample["evidence"].__setitem__("repairs", 3)),
            ("distinct hashes", lambda sample: sample["evidence"].__setitem__("distinct_hashes", 3)),
            ("final objects", lambda sample: sample["spool"].__setitem__("final_object_count", 3)),
            ("archived bytes", lambda sample: sample["evidence"].__setitem__("archived_bytes", 3)),
            ("archive bytes", lambda sample: sample["archive_operations"].__setitem__("get_bytes", 3)),
            ("final bytes", lambda sample: sample["spool"].__setitem__("final_bytes", 3)),
            ("provider residue", lambda sample: sample["evidence"].__setitem__("provider_errors", 1)),
            ("temporary residue", lambda sample: sample["workload"]["spool"].__setitem__("temporary_bytes", 1)),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                artifact = _valid_duplicate_artifact()
                mutate(artifact["samples"][0])
                artifact["artifact_digest"] = runner._artifact_digest(artifact)
                with self.assertRaises(ValueError):
                    runner.validate_artifact(artifact)

    def test_exact_postgres_settings_are_required(self) -> None:
        artifact = _valid_duplicate_artifact()
        artifact["provenance"]["postgres"]["settings"].pop("work_mem")
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaises(ValueError):
            runner.validate_artifact(artifact)

    def test_reset_semantics_reconcile_population_readiness_resources_and_failures(self) -> None:
        summary = runner._result_summary([{"outcome": "processed"}], expected=1)
        empty = runner._result_summary([], expected=0)
        workload = {
            "population": 1,
            "hard_failures": [],
            "official_responses": 1,
            "processing_summary": {
                "official": summary,
                "dependent": empty,
                "correction": empty,
                "total": summary,
            },
            "fact_counts": {
                "ranked_day_versions": 1,
                "snapshot_headers": 2,
                "snapshot_entries": 2,
            },
            "fanout_evidence": {
                "expected": {
                    "ranked_day_versions": 1,
                    "snapshot_headers": 2,
                    "snapshot_entries": 2,
                },
                "generation_states": [
                    {
                        "generation": 1,
                        "snapshot_state": "published",
                        "army_state": "published",
                    }
                ],
            },
            "queue_residue": [],
        }
        sample = {
            "workload": workload,
            "database": {"queue_residue": []},
            "elapsed_seconds": 1.0,
            "cpu_seconds": 1.0,
            "peak_rss_kib": 1,
        }
        config = {"populations": [1]}
        runner._validate_reset_semantics([sample], config, "reset-boundary", [])

        incomplete = deepcopy(sample)
        incomplete["workload"]["fanout_evidence"]["generation_states"] = []
        incomplete["workload"]["hard_failures"] = [
            "reset_generation_count_mismatch"
        ]
        runner._validate_reset_semantics(
            [incomplete],
            config,
            "reset-boundary",
            ["reset_generation_count_mismatch"],
        )

        extra = deepcopy(sample)
        extra["workload"]["fanout_evidence"]["generation_states"].append(
            {
                "generation": 2,
                "snapshot_state": "published",
                "army_state": "published",
            }
        )
        extra["workload"]["hard_failures"] = ["reset_generation_count_mismatch"]
        runner._validate_reset_semantics(
            [extra],
            config,
            "reset-boundary",
            ["reset_generation_count_mismatch"],
        )

        workload["fact_counts"]["snapshot_entries"] = 1
        with self.assertRaises(ValueError):
            runner._validate_reset_semantics([sample], config, "reset-boundary", [])

    def test_mixed_semantics_reconcile_jobs_latency_resources_memory_and_residue(self) -> None:
        summary = runner._result_summary(
            [
                {"outcome": "processed", "status": "complete", "kind": "live"},
                {
                    "outcome": "processed",
                    "status": "complete",
                    "kind": "backfill",
                },
            ],
            expected=2,
        )
        memory = _army_memory()
        workload = {
            "live_jobs": 1,
            "backfill_jobs": 1,
            "configured_lanes": 8,
            "effective_lanes": 8,
            "official_responses": 2,
            "completion_counts": {"live": 1, "backfill": 1},
            "completion_order": ["live", "backfill"],
            "elapsed_seconds": 1.0,
            "cpu_seconds": 1.0,
            "peak_rss_kib": 1,
            "live_queue_latency_seconds": {
                "count": 1,
                "p95": 1.0,
                "maximum": 1.0,
                "collection_maximum": 1.0,
            },
            "live_latency_contract": {
                "target_seconds": 300.0,
                "p95_seconds": 1.0,
                "maximum_seconds": 1.0,
                "collection_maximum_seconds": 1.0,
                "passed": True,
            },
            "five_minute_contract": {
                "target_seconds": 300.0,
                "elapsed_seconds": 1.0,
                "passed": True,
            },
            "memory_pressure_before": deepcopy(memory),
            "memory_pressure_after": deepcopy(memory),
            "memory_pressure_delta": {
                key: 0 for key in runner._ARMY_MEMORY_DELTA_KEYS
            },
            "oldest_active_queue_age_seconds": None,
            "processing_summary": summary,
            "official_api_traffic": {"requests": 0, "source": "committed fixtures"},
            "hard_failures": [],
        }
        sample = {
            "workload": workload,
            "database": {
                "queue_age_seconds": {
                    "collector_jobs": None,
                    "python_processing_jobs": None,
                },
                "queue_residue": [],
            },
        }
        runner._validate_mixed_semantics(
            sample,
            {"live_jobs": 1, "backfill_jobs": 1, "lanes": 8, "effective_lanes": 8},
            [],
        )

        incomplete = deepcopy(sample)
        incomplete["workload"]["completion_counts"] = {"live": 1, "backfill": 0}
        incomplete["workload"]["completion_order"] = None
        incomplete["workload"]["processing_summary"] = runner._result_summary(
            [{"outcome": "processed", "status": "complete", "kind": "live"}],
            expected=2,
        )
        incomplete["workload"]["hard_failures"] = [
            "mixed_result_count_mismatch"
        ]
        runner._validate_mixed_semantics(
            incomplete,
            {"live_jobs": 1, "backfill_jobs": 1, "lanes": 8, "effective_lanes": 8},
            ["mixed_result_count_mismatch"],
        )
        incomplete["workload"]["completion_counts"]["backfill"] = 1
        with self.assertRaises(ValueError):
            runner._validate_mixed_semantics(
                incomplete,
                {"live_jobs": 1, "backfill_jobs": 1, "lanes": 8, "effective_lanes": 8},
                ["mixed_result_count_mismatch"],
            )

        unavailable = deepcopy(sample)
        unavailable["workload"]["memory_pressure_before"][
            "process_cgroup_available"
        ] = 0
        unavailable["workload"]["memory_pressure_after"][
            "process_cgroup_available"
        ] = 0
        unavailable["workload"]["hard_failures"] = ["memory_pressure_unavailable"]
        runner._validate_mixed_semantics(
            unavailable,
            {"live_jobs": 1, "backfill_jobs": 1, "lanes": 8, "effective_lanes": 8},
            ["memory_pressure_unavailable"],
        )

        increased = deepcopy(sample)
        increased["workload"]["memory_pressure_after"]["process_swap_used_bytes"] = 1
        increased["workload"]["memory_pressure_delta"]["process_swap_used_bytes"] = 1
        increased["workload"]["hard_failures"] = ["memory_pressure_increased"]
        runner._validate_mixed_semantics(
            increased,
            {"live_jobs": 1, "backfill_jobs": 1, "lanes": 8, "effective_lanes": 8},
            ["memory_pressure_increased"],
        )

        workload["five_minute_contract"]["elapsed_seconds"] = 2.0
        with self.assertRaises(ValueError):
            runner._validate_mixed_semantics(
                sample,
                {"live_jobs": 1, "backfill_jobs": 1, "lanes": 8, "effective_lanes": 8},
                [],
            )

    def test_generated_like_army_artifact_uses_public_production_plans(self) -> None:
        artifact = _valid_army_artifact()
        runner.validate_artifact(artifact)
        plan = artifact["army_read_sample"]["selections"][0]["endpoint_sql"][0]
        plan["sql"] = "SELECT secret FROM private_table"
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaises(ValueError):
            runner.validate_artifact(artifact)

    def test_top_1000_cohort_plan_shape_matches_production_query(self) -> None:
        query = """
            SELECT count(*) FROM leaderboard_snapshot_entries
            WHERE snapshot_id = %s
              AND position BETWEEN %s AND %s
              AND NOT (freshness = 'fresh' AND confidence = 'confirmed')
        """
        identity = runner._army_query_identity(query)
        self.assertEqual(identity, "army_analytics.cohort_quality")
        self.assertEqual(
            runner._army_parameter_shape((123, 1, 1000)),
            ("int", "int", "int"),
        )
        self.assertEqual(
            runner._ARMY_QUERY_SHAPES[("top-1000", identity)],
            ("int", "int", "int"),
        )
        plan = _army_plan("top-1000", "offense", identity, 1)
        self.assertEqual(plan["parameters"]["types"], ["int", "int", "int"])

    def test_streak_member_and_cohort_queries_have_distinct_identities(self) -> None:
        member_query = """
            SELECT player_id FROM leaderboard_snapshot_entries
            WHERE snapshot_id=ANY(%s::bigint[]) AND position<=%s
              AND freshness='fresh' AND confidence='confirmed'
            GROUP BY player_id HAVING count(DISTINCT snapshot_id)=%s
        """
        cohort_query = """
            SELECT count(*) FROM (
                SELECT player_id
                FROM leaderboard_snapshot_entries
                WHERE snapshot_id = ANY(%s::bigint[])
                  AND position <= %s
                GROUP BY player_id
                HAVING count(DISTINCT snapshot_id) = %s
                   AND bool_or(
                       NOT (freshness = 'fresh' AND confidence = 'confirmed')
                   )
            ) AS excluded
        """
        member_identity = runner._army_query_identity(member_query)
        cohort_identity = runner._army_query_identity(cohort_query)
        self.assertEqual(member_identity, "army_analytics.streak_members")
        self.assertEqual(cohort_identity, "army_analytics.cohort_quality")
        self.assertEqual(
            runner._army_parameter_shape(([101, 202], 1000, 28)),
            runner._ARMY_QUERY_SHAPES[("streak-top-1000", cohort_identity)],
        )
        self.assertEqual(
            runner._ARMY_QUERY_ORDER["streak-top-1000"][6], cohort_identity
        )

    def test_real_explain_buffers_are_sanitized_before_artifact_validation(self) -> None:
        artifact = _valid_army_artifact()
        plan = artifact["army_read_sample"]["selections"][0]["endpoint_sql"][0]
        raw = deepcopy(plan["explain_analyze_buffers"])
        raw[0]["Plan"].update(
            {
                "Parallel Aware": False,
                "Relation Name": "leaderboard_snapshot_entries",
                "Actual Total Time": 0.1,
                "Filter": "secret high-cardinality detail",
                "Shared Hit Blocks": 8,
                "Shared Read Blocks": 1,
                "Shared Dirtied Blocks": 0,
                "Shared Written Blocks": 0,
                "Local Hit Blocks": 0,
                "Local Read Blocks": 0,
                "Local Dirtied Blocks": 0,
                "Local Written Blocks": 0,
                "Temp Read Blocks": 0,
                "Temp Written Blocks": 0,
                "I/O Read Time": 0.0,
                "I/O Write Time": 0.0,
            }
        )
        raw[0]["Planning"] = {"Shared Hit Blocks": 1}
        sanitized = runner._public_explain_payload(raw)
        self.assertNotIn("Shared Hit Blocks", json.dumps(sanitized))
        self.assertNotIn("Relation Name", json.dumps(sanitized))
        self.assertNotIn("secret high-cardinality detail", json.dumps(sanitized))
        self.assertNotIn("Planning", sanitized[0])
        plan["explain_analyze_buffers"] = sanitized
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        runner.validate_artifact(artifact)

    def test_pg18_jit_explain_metadata_is_discarded(self) -> None:
        raw = [
            {
                "Plan": {
                    "Node Type": "Aggregate",
                    "Actual Rows": 1,
                    "Actual Loops": 1,
                },
                "Planning Time": 1.0,
                "JIT": {
                    "Functions": 3,
                    "Options": {
                        "Inlining": False,
                        "Optimization": False,
                        "Expressions": True,
                        "Deforming": True,
                    },
                    "Timing": {
                        "Generation": 0.1,
                        "Inlining": 0.0,
                        "Optimization": 0.0,
                        "Emission": 0.1,
                    },
                },
                "Execution Time": 1.0,
            }
        ]

        sanitized = runner._public_explain_payload(raw)

        self.assertEqual(
            set(sanitized[0]), {"Plan", "Planning Time", "Execution Time"}
        )
        self.assertNotIn("JIT", json.dumps(sanitized))

        raw[0]["JIT"]["Functions"] = 4
        self.assertNotIn("JIT", json.dumps(runner._public_explain_payload(raw)))

    def test_pg18_raw_condition_metadata_is_discarded(self) -> None:
        expression = (
            "(player_id = ANY ('{"
            + ",".join(str(index) for index in range(1, 1200))
            + "}'::bigint[]))"
        )
        raw = [
            {
                "Plan": {
                    "Node Type": "Bitmap Heap Scan",
                    "Actual Rows": 1,
                    "Actual Loops": 1,
                    "Recheck Cond": expression,
                    "Index Cond": "https://internal.example/index/#P00001",
                    "Filter": "tag=#P00001 secret=not-public",
                    "Rows Removed by Index Recheck": 0,
                    "Actual Total Time": -1.0,
                },
                "Planning Time": 1.0,
                "Execution Time": 1.0,
            }
        ]

        sanitized = runner._public_explain_payload(raw)

        self.assertEqual(
            set(sanitized[0]["Plan"]),
            {"Node Type", "Actual Rows", "Actual Loops"},
        )
        rendered = json.dumps(sanitized)
        for value in (
            expression,
            "https://internal.example/index/#P00001",
            "tag=#P00001 secret=not-public",
        ):
            self.assertNotIn(value, rendered)

    def test_discarded_explain_metadata_stays_bounded(self) -> None:
        def raw_with(value: object) -> list[dict[str, object]]:
            return [
                {
                    "Plan": {
                        "Node Type": "Aggregate",
                        "Actual Rows": 1,
                        "Actual Loops": 1,
                        "Raw Detail": value,
                    },
                    "Planning Time": 1.0,
                    "Execution Time": 1.0,
                }
            ]

        nested: object = "detail"
        for _ in range(runner.MAX_EXPLAIN_DETAIL_DEPTH + 1):
            nested = {"nested": nested}
        cases = (
            (nested, "too deep"),
            ("x" * (runner.MAX_EXPLAIN_DETAIL_TEXT + 1), "invalid"),
            (float("inf"), "invalid"),
            (float("nan"), "invalid"),
            ((1, 2), "invalid"),
            (list(range(runner.MAX_EXPLAIN_DETAIL_SEQUENCE + 1)), "unbounded"),
            ({str(index): index for index in range(65)}, "unbounded"),
        )
        for value, message in cases:
            with self.subTest(value=type(value).__name__), self.assertRaisesRegex(
                ValueError, message
            ):
                runner._public_explain_payload(raw_with(value))

    def test_nested_reset_army_plan_is_sanitized_and_tampering_rejected(self) -> None:
        raw = [
            {
                "Plan": {
                    "Node Type": "Seq Scan",
                    "Actual Rows": 1,
                    "Actual Loops": 1,
                    "Relation Name": "army_analytics_battle_facts",
                    "Shared Hit Blocks": 8,
                    "Shared Read Blocks": 1,
                },
                "Planning Time": 1.0,
                "Triggers": [],
                "Execution Time": 1.0,
            }
        ]
        retained = runner._public_explain_payload(raw)[0]
        self.assertNotIn("Shared Hit Blocks", json.dumps(retained))
        sample = {
            "status": "passed",
            "selections": [
                {
                    "selection": selection,
                    "synthetic_fact_limit": 10,
                    "rows_scanned": 1,
                    "rows_returned": 1,
                    "latency_ms": 1.0,
                    "endpoint": {
                        "status": "returned",
                        "returned_fact_count": 1,
                        "latency_ms": 1.0,
                    },
                    "explain_analyze_buffers": deepcopy(retained),
                }
                for selection in (
                    "top-1000",
                    "trophies-5000-9999",
                    "streak-top-1000",
                )
            ],
        }
        runner._validate_nested_army_read_sample(sample, "army_read_sample")
        sample["selections"][0]["explain_analyze_buffers"]["Plan"][
            "Shared Hit Blocks"
        ] = 9
        with self.assertRaisesRegex(ValueError, "raw EXPLAIN"):
            runner._validate_nested_army_read_sample(sample, "army_read_sample")

    def test_army_plan_and_provenance_mutations_are_rejected(self) -> None:
        mutations = (
            lambda army: army["selections"][0]["endpoint_sql"][0]["parameters"].__setitem__(
                "types", ["str"]
            ),
            lambda army: army["selections"][0]["endpoint_sql"][0][
                "explain_analyze_buffers"
            ][0].__setitem__("Filter", "secret"),
            lambda army: army["postgres"]["settings"].__setitem__("password", "secret"),
            lambda army: army["selections"][0].__setitem__("p95_ms", float("nan")),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                artifact = _valid_army_artifact()
                mutate(artifact["army_read_sample"])
                artifact["artifact_digest"] = runner._artifact_digest(artifact)
                with self.assertRaises(ValueError):
                    runner.validate_artifact(artifact)

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

    def test_boundary_admission_timeout_is_bounded_and_returns_nonzero(self) -> None:
        def run_with_boundary_probe(_arguments: object) -> dict[str, object]:
            runner._boundary_admission_probe(
                "postgresql://fixture.invalid/clashlens", "admit", 1
            )
            raise AssertionError("timed-out boundary probe unexpectedly returned")

        psycopg = mock.Mock(Error=RuntimeError)
        with (
            mock.patch.dict(sys.modules, {"psycopg": psycopg}),
            mock.patch.object(
                runner.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["go", "test"], 660),
            ) as run_process,
            mock.patch.object(runner, "run", side_effect=run_with_boundary_probe),
            mock.patch("sys.stderr"),
        ):
            result = runner.main(
                [
                    "duplicate-heavy",
                    "--database-url",
                    "postgresql://fixture.invalid/clashlens",
                ]
            )

        self.assertEqual(result, 2)
        command = run_process.call_args.args[0]
        self.assertIn("-timeout=600s", command)
        self.assertEqual(run_process.call_args.kwargs["timeout"], 660)

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

    def test_artifact_schema_10_requires_explicit_capacity_meaning(self) -> None:
        self.assertEqual(runner.ARTIFACT_SCHEMA_VERSION, 10)
        artifact = _valid_duplicate_artifact(20)
        runner.validate_artifact(artifact)
        sample = artifact["samples"][0]
        self.assertEqual(sample["spool"]["filesystem_type"], "ext4")
        self.assertEqual(sample["spool"]["inode_model"], "finite")
        self.assertEqual(sample["storage_runway"]["filesystem_type"], "ext4")
        # Missing model facts are not defaulted to finite/dynamic.
        bad = dict(sample["spool"])
        del bad["inode_model"]
        sample["spool"] = bad
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "spool"):
            runner.validate_artifact(artifact)
        # Contradictory dynamic-without-btrfs is rejected.
        artifact = _valid_duplicate_artifact(20)
        artifact["samples"][0]["spool"]["inode_model"] = "dynamic"
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "contradicts"):
            runner.validate_artifact(artifact)
        # Old schema 9 artifacts are rejected, not silently upgraded.
        artifact = _valid_duplicate_artifact(20)
        artifact["schema_version"] = 9
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
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
                "filesystem_type": "ext4",
                "inode_model": "finite",
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

    def test_step5_overlap_uses_an_unseeded_fixture_day(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "battle_fixture=_battle_fixture_for_day(overlap_day)",
            source,
        )
        overlap_day = runner.BOUNDARY + timedelta(days=runner.STEP5_DAYS + 1)
        shifted = runner._battle_fixture_for_day(overlap_day)
        self.assertEqual(len(shifted), len(runner.BATTLE_FIXTURE))
        self.assertNotIn(runner.DAY_START.strftime("%Y-%m-%d").encode(), shifted)
        self.assertIn(overlap_day.strftime("%Y-%m-%d").encode(), shifted)

    def test_step5_overlap_warms_reads_before_collection_processing(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(runner.STEP5_MIXED_LANE_POOL_MAX_SIZE, 2)
        wait = source.index("analytics_warmups_resolved.wait(120)")
        failed = source.index("analytics_warmups_failed.is_set()", wait)
        timer = source.index("started = time.perf_counter()", wait)
        collection = source.index("result = _run_duplicate(", timer)
        serialized = source.index("with analytics_warmup_lock:", collection)
        warmup = source.index('"mixed warmup"', collection)
        ready = source.index("analytics_warmups_resolved.set()", warmup)
        measured_overlap = source.index("processing_started.wait()", ready)

        self.assertLess(wait, timer)
        self.assertLess(failed, timer)
        self.assertLess(timer, collection)
        self.assertLess(serialized, warmup)
        self.assertLess(warmup, ready)
        self.assertLess(ready, measured_overlap)
        self.assertIn(
            "connection_info, max_size=STEP5_MIXED_LANE_POOL_MAX_SIZE",
            source,
        )
        self.assertIn(
            "analytics_warmups_failed.set()\n"
            "                analytics_warmups_resolved.set()",
            source,
        )

    def test_step5_overlap_does_not_start_collection_after_failed_warmup(self) -> None:
        pool_sizes: list[int] = []
        warmup_calls = [0]

        class FailingDatabase:
            def __init__(self, _connection_info: str, *, max_size: int) -> None:
                pool_sizes.append(max_size)

            def get_army_analytics(self, *_args: object, **_kwargs: object) -> None:
                warmup_calls[0] += 1
                raise RuntimeError("cold cache fill failed")

            def close(self) -> None:
                pass

        with mock.patch(
            "clashlens.api_db.ApiDatabase", FailingDatabase
        ), mock.patch.object(runner, "_run_duplicate") as duplicate, mock.patch.object(
            runner, "_run_account_read_gate", return_value={}
        ), self.assertRaisesRegex(RuntimeError, "analytics warmup failed"):
            runner._run_step5_overlap(
                "unused", object(), runner._army_selection_specs()
            )

        duplicate.assert_not_called()
        self.assertEqual(warmup_calls, [1])
        self.assertEqual(pool_sizes, [runner.STEP5_MIXED_LANE_POOL_MAX_SIZE] * 4)

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

    def test_candidate_receipt_v2_is_validated_in_full_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            path.write_text(json.dumps(_candidate_receipt()), encoding="utf-8")
            provenance = _provenance(candidate_receipt=path)
            artifact = _valid_duplicate_artifact(20)
            artifact["provenance"] = provenance
            artifact["execution"] = provenance["execution"]
            artifact["prepared_candidate_images"] = provenance[
                "prepared_candidate_images"
            ]
            artifact["candidate_receipt"] = provenance["candidate_receipt"]
            artifact["artifact_digest"] = runner._artifact_digest(artifact)
            runner.validate_artifact(artifact)

            artifact["candidate_receipt"]["schema_version"] = 1
            artifact["artifact_digest"] = runner._artifact_digest(artifact)
            with self.assertRaisesRegex(ValueError, "candidate receipt"):
                runner.validate_artifact(artifact)

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
        artifact["provenance"]["runner_sha256"] = "not-a-hash"
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "runner hash"):
            runner.validate_artifact(artifact)

        artifact = _valid_artifact()
        artifact["provenance"]["source_sha"] = "0" * 40
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        with self.assertRaisesRegex(ValueError, "clean exact revision"):
            runner.validate_artifact(artifact)

        artifact = _valid_artifact()
        artifact["provenance"]["runner_sha256"] = "1" * 64
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
        artifact = _valid_partial_army_artifact()
        runner.validate_artifact(artifact)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hard-failure.json"
            with mock.patch.object(runner, "run", return_value=artifact):
                result = runner.main(
                    [
                        "army-analytics",
                        "--database-url",
                        "postgresql://fixture.invalid/clashlens",
                        "--output",
                        str(path),
                    ]
                )

            self.assertEqual(result, 2)
            retained = json.loads(path.read_text(encoding="utf-8"))
            runner.validate_artifact(retained)
            self.assertEqual(retained["hard_failures"], ["army_read_sample_unavailable"])

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

        before = _army_memory()
        before["process_cgroup_available"] = 0
        after = deepcopy(before)
        after["process_swap_used_bytes"] = 1
        delta = runner._memory_pressure_delta(before, after)
        self.assertEqual(
            runner._memory_pressure_failure_codes(before, after, delta),
            ["memory_pressure_unavailable", "memory_pressure_increased"],
        )

    def test_forced_miss_failure_codes_match_each_gate(self) -> None:
        memory = _army_memory()
        cases = (
            (
                "elapsed",
                runner.STEP5_FORCED_MISS_TARGET_SECONDS,
                deepcopy(memory),
                deepcopy(memory),
                {key: 0 for key in runner._ARMY_MEMORY_DELTA_KEYS},
                ["step5_forced_miss_exceeded"],
            ),
            (
                "cgroup",
                1.0,
                {**memory, "database_cgroup_available": 0},
                {**memory, "database_cgroup_available": 0},
                {key: 0 for key in runner._ARMY_MEMORY_DELTA_KEYS},
                ["step5_cgroup_unavailable"],
            ),
            (
                "memory",
                1.0,
                deepcopy(memory),
                {**memory, "process_swap_used_bytes": 1},
                {
                    **{key: 0 for key in runner._ARMY_MEMORY_DELTA_KEYS},
                    "process_swap_used_bytes": 1,
                },
                ["step5_memory_pressure_increased"],
            ),
        )
        for name, seconds, before, after, delta, expected in cases:
            with self.subTest(gate=name):
                self.assertEqual(
                    runner._army_forced_miss_failures(seconds, before, after, delta),
                    expected,
                )
                self.assertIs(
                    runner._army_forced_miss_passed(seconds, before, after, delta),
                    False,
                )

                artifact = _valid_army_artifact()
                army = artifact["army_read_sample"]
                selection = army["selections"][0]
                selection["forced_miss_seconds"] = seconds
                selection["forced_miss_memory_before"] = before
                selection["forced_miss_memory_after"] = after
                selection["forced_miss_memory_delta"] = delta
                selection["forced_miss_passed"] = False
                army["status"] = "failed"
                army["hard_failures"] = expected
                runner._validate_army_semantics(
                    army,
                    artifact["provenance"],
                    expected,
                    "army sample",
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
        filesystem = os.statvfs(ROOT)
        raw_capacity = int(filesystem.f_blocks * filesystem.f_frsize)
        available = int(filesystem.f_bavail * filesystem.f_frsize)
        expected_capacity = raw_capacity - max(
            0, int(filesystem.f_bfree - filesystem.f_bavail) * filesystem.f_frsize
        )

        # statvfs is live state; keep the helper and expected values on one
        # snapshot so filesystem churn cannot move one block between reads.
        with mock.patch.object(runner.os, "statvfs", return_value=filesystem):
            usage = runner._filesystem_usage(ROOT)

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
    def test_step5_statistics_readiness_analyzes_exact_relations(self) -> None:
        import psycopg
        from domain_test_support import domain_database

        with domain_database(
            os.environ["CLASHLENS_TEST_DATABASE_URL"], include_coordinator=True
        ) as connection_info:
            readiness, failure = runner._prepare_step5_statistics(connection_info)
            with psycopg.connect(connection_info) as connection:
                analyzed = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT relname
                        FROM pg_stat_all_tables
                        WHERE schemaname = current_schema()
                          AND relname = ANY(%s::text[])
                          AND last_analyze IS NOT NULL
                        """,
                        (list(runner.STEP5_STATISTICS_RELATIONS),),
                    ).fetchall()
                }

        self.assertIsNone(failure)
        self.assertEqual(
            readiness,
            {
                "relations": list(runner.STEP5_STATISTICS_RELATIONS),
                "readiness_timeout_seconds": runner.STEP5_STATISTICS_TIMEOUT_SECONDS,
                "analyze_completed": True,
                "active_analyzes": 0,
                "ready": True,
            },
        )
        self.assertEqual(analyzed, set(runner.STEP5_STATISTICS_RELATIONS))

    def test_overlap_fixture_does_not_enqueue_legacy_army_publication(self) -> None:
        import psycopg
        from domain_test_support import domain_database

        with (
            domain_database(
                os.environ["CLASHLENS_TEST_DATABASE_URL"], include_coordinator=True
            ) as connection_info,
            runner.archive_server() as archive,
        ):
            with psycopg.connect(connection_info) as connection:
                player_id = connection.execute(
                    """
                    INSERT INTO players (normalized_tag, active, eligibility_state)
                    VALUES ('#SEED', true, 'eligible')
                    RETURNING id
                    """
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO ranked_day_versions (
                        player_id, ranked_day_start, ranked_day_end,
                        official_season_id, season_day_number,
                        season_anchor_rule_version, reconciliation_rule_version,
                        result_hash, version, state, confidence,
                        evidence_complete, reconciled, coverage_complete
                    ) VALUES (
                        %s, %s, %s, '1783918800', 1,
                        'runner-anchor-v1', 'runner-reconciliation-v1',
                        repeat('a', 64), 1, 'Complete', 'exact',
                        true, true, true
                    )
                    """,
                    (
                        player_id,
                        runner.DAY_START,
                        runner.BOUNDARY,
                    ),
                )
                connection.commit()

            overlap_day = runner.BOUNDARY + timedelta(days=runner.STEP5_DAYS + 1)
            workload = runner._run_duplicate(
                connection_info,
                archive,
                6,
                observation_start=overlap_day,
                battle_fixture=runner._battle_fixture_for_day(overlap_day),
            )

            with psycopg.connect(connection_info) as connection:
                legacy_army_jobs = connection.execute(
                    """
                    SELECT count(*)
                    FROM python_processing_jobs
                    WHERE work_type = 'build_army_analytics'
                    """
                ).fetchone()[0]

            self.assertEqual(legacy_army_jobs, 0)
            self.assertEqual(
                workload["processing_summary"]["outcomes"]["processed"], 6
            )

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
                "battle_canonical_rows": 4,
                "battle_occurrence_rows": 0,
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

    def test_mixed_workload_emits_memory_pressure_failures(self) -> None:
        from domain_test_support import domain_database

        before = _army_memory()
        before["process_cgroup_available"] = 0
        after = deepcopy(before)
        after["process_swap_used_bytes"] = 1
        with (
            domain_database(
                os.environ["CLASHLENS_TEST_DATABASE_URL"], include_coordinator=True
            ) as connection_info,
            runner.archive_server() as archive,
            mock.patch.object(runner, "_memory_pressure", side_effect=[before, after]),
        ):
            workload = runner._run_mixed(connection_info, archive, 1, 1)

        self.assertEqual(
            workload["processing_summary"]["outcomes"]["processed"],
            2,
        )
        self.assertEqual(
            workload["hard_failures"],
            ["memory_pressure_unavailable", "memory_pressure_increased"],
        )

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
            if mode in {runner.STEP5_MODE, runner.NORMAL_CAPACITY_MODE}:
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

                failed = deepcopy(result)
                failed_workload = failed["samples"][0]["workload"]
                failed_workload["completion_counts"] = {"live": 1, "backfill": 0}
                failed_workload["completion_order"] = None
                failed_workload["completion_order_complete"] = False
                failed_workload["live_first_completion_index"] = None
                failed_workload["processing_summary"] = runner._result_summary(
                    [
                        {
                            "outcome": "processed",
                            "status": "complete",
                            "kind": "live",
                            "work_type": "process_observation",
                        }
                    ],
                    expected=2,
                )
                failed_workload["hard_failures"] = [
                    "mixed_result_count_mismatch"
                ]
                failed["hard_failures"] = ["mixed_result_count_mismatch"]
                failed["artifact_digest"] = runner._artifact_digest(failed)
                runner.validate_artifact(failed)
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

                failed = deepcopy(result)
                failed_workload = failed["samples"][0]["workload"]
                failed_workload["status"] = "failed"
                failed_workload["fanout_evidence"]["generation_states"] = []
                failed_workload["fanout_evidence"][
                    "snapshot_entries_per_population"
                ] = 0
                failed_workload["fanout_evidence"]["matches_expected"] = False
                failed_workload["hard_failures"] = [
                    "reset_generation_count_mismatch"
                ]
                failed["hard_failures"] = ["reset_generation_count_mismatch"]
                failed["artifact_digest"] = runner._artifact_digest(failed)
                runner.validate_artifact(failed)

                extra = deepcopy(result)
                extra_workload = extra["samples"][0]["workload"]
                generation_states = extra_workload["fanout_evidence"][
                    "generation_states"
                ]
                generation_states.append(
                    {
                        "generation": len(generation_states) + 1,
                        "snapshot_state": "published",
                        "army_state": "published",
                    }
                )
                extra_workload["status"] = "failed"
                extra_workload["fanout_evidence"][
                    "snapshot_entries_per_population"
                ] = 2 * len(generation_states)
                extra_workload["fanout_evidence"]["matches_expected"] = False
                extra_workload["hard_failures"] = [
                    "reset_generation_count_mismatch"
                ]
                extra["hard_failures"] = ["reset_generation_count_mismatch"]
                extra["artifact_digest"] = runner._artifact_digest(extra)
                runner.validate_artifact(extra)


def _capacity_marker(**overrides: object) -> dict:
    marker = {
        "players": 12833,
        "lanes": 32,
        "profile_attempts": 12833,
        "battlelog_attempts": 12833,
        "ranking_attempts": 1,
        "ordinary_attempts": 25667,
        "retry_injected": 402,
        "retry_parents": 402,
        "retry_missing": 0,
        "retry_duplicate": 0,
        "retries_executed": 402,
        "retry_budget": 4333,
        "worker_errors": 0,
        "keys": 4,
        "per_key_rps": 25,
        "aggregate_rps": 100,
        "per_key_max": 25,
        "aggregate_max": 100,
        "official_requests": 26069,
        "official_bytes": 100000,
        "s3_ordinary_put": 12800,
        "s3_ordinary_head": 100,
        "s3_ordinary_get": 13000,
        "s3_retry_put": 36,
        "s3_retry_head": 0,
        "s3_retry_get": 402,
        "budget_cap_profile": 12833,
        "budget_cap_battlelog": 13235,
        "budget_cap_rankings": 1,
        "budget_used_profile": 12833,
        "budget_used_battlelog": 13235,
        "budget_used_rankings": 1,
        "http_profile": 12833,
        "http_battlelog": 13235,
        "http_rankings": 1,
        "spool_peak_bytes": 100000,
        "spool_final_bytes": 90000,
        "spool_temp_bytes": 0,
        "spool_reserved_bytes": 0,
        "spool_final_objects": 12800,
        "spool_temp_objects": 0,
        "spool_reserved_objects": 0,
        "spool_alloc_bytes": 110000,
        "pg_growth_bytes": 1000000,
        "pg_peak_bytes": 1200000,
        "wal_bytes": 2000000,
        "wal_retained_bytes": 2100000,
        "wal_retained_peak_bytes": 2200000,
        "seed_ms": 5000,
        "ordinary_ms": 200000,
        "drain_ms": 210000,
        "wall_ms": 220000,
        "sweep_delta": 0,
        "regular_scheduled_dry": 0,
        "reset_members": 2,
        "regular_allowed_during_reset": False,
        "reset_gate_dry": True,
        "host_id": "workstation|cpu8|mem16384MB",
    }
    marker.update(overrides)
    return marker
def _valid_capacity_workload() -> dict:
    ordinary = runner.CAPACITY_ORDINARY_ATTEMPTS
    return {
        "observations": ordinary,
        "official_responses": ordinary + 402,
        "executed_observations": ordinary + 402,
        "ordinary_attempts": ordinary,
        "players": 12833,
        "retry_tranche_attempted": 402,
        "retry_tranche_processed": 402,
        "total_attempts": ordinary + 402,
        "endpoint_mix": dict(runner.CAPACITY_ENDPOINT_MIX),
        "response_counts_by_endpoint": dict(runner.CAPACITY_ENDPOINT_MIX),
        "occurrence_counts_by_endpoint": dict(runner.CAPACITY_ENDPOINT_MIX),
        "official_loopback_requests": ordinary + 402,
        "official_remote_requests": 0,
        "official_bytes": 100000,
        "s3_ordinary": {"put": 12800, "head": 100, "get": 13000},
        "s3_retry": {"put": 36, "head": 0, "get": 402},
        "aggregation_method": "exact integrated collector run",
        "lanes": 32,
        "normal_keys": 4,
        "per_key_rps": 25,
        "aggregate_rps": 100,
        "rate_maxima": {"per_key": 25, "aggregate": 100},
        "interactive_attempts": 0,
        "archive_origin_host": "127.0.0.1",
        "archive_operations": {
            "get": 40000,
            "get_bytes": 90000,
            "head": 100,
            "conditional_put": 12836,
            "put": 12836,
            "put_bytes": 50000,
            "conflicts": 0,
        },
        "archive_objects": 12836,
        "archive_stored_bytes": 50000,
        "processing_summary": {"count": ordinary, "expected_count": ordinary},
        "retry_summary": {"count": 402, "expected_count": 402},
        "downstream_summary": runner._result_summary(
            [{"outcome": "processed"}] * ordinary
            + [{"outcome": "classified"}] * 402,
            expected=ordinary + 402,
        ),
        "retry_lineage": {
            "attempted": 402,
            "processed": 402,
            "missing": 0,
            "duplicates": 0,
        },
        "reset_exclusion": {
            "regular_allowed": False,
            "regular_scheduled_dry": 0,
            "sweep_delta": 0,
            "reset_members": 2,
        },
        "capacity_probe": {"executed": True},
        "seed_seconds": 5.0,
        "drain_seconds": 200.0,
        "retry_seconds": 10.0,
        "downstream_seconds": 20.0,
        "wall_seconds": 220.0,
        "probe_wall_seconds": 220.0,
        "pg_growth_bytes": 1000000,
        "pg_peak_bytes": 1200000,
        "wal_bytes": 2000000,
        "wal_retained_bytes": 2100000,
        "wal_retained_peak_bytes": 2200000,
        "spool_peak_bytes": 100000,
        "spool_final_bytes": 90000,
        "spool_temp_bytes": 0,
        "spool_reserved_bytes": 0,
        "spool_final_objects": 12800,
        "spool_temp_objects": 0,
        "spool_reserved_objects": 0,
        "spool_alloc_bytes": 110000,
        "spool_fs": {
            "filesystem_type": "ext4",
            "inode_model": "finite",
            "free_inodes": 100,
        },
        "spool_dir": "/tmp/capacity-spool-test",
        "downstream_spool": {"final_bytes": 50, "high_water_bytes": 60},
        "budget_caps": {
            "profile": 12833,
            "battle_log": 13235,
            "global_player_rankings": 1,
        },
        "budget_consumed": {
            "profile": 12833,
            "battle_log": 13235,
            "global_player_rankings": 1,
        },
        "http_attempts": {
            "profile": 12833,
            "battle_log": 13235,
            "global_player_rankings": 1,
        },
        "go_output_bytes": 100,
        "worker_errors": 0,
        "status": "complete",
        "failure": None,
        "host_id": "workstation|cpu8|mem16384MB",
        "hard_failures": [],
    }


def _valid_capacity_sample() -> dict:
    workload = _valid_capacity_workload()
    combined = workload["endpoint_mix"]
    required = (
        "collector_observations",
        "parsed_source_payloads",
        "archive_catalogue",
        "python_processing_jobs",
    )
    return {
        "workload": workload,
        "database": {
            "wal_bytes": 2000000,
            "wal_retained_bytes": 20,
            "wal_retained_growth_bytes": 5,
            "sql_statement_calls": 7,
            "application_sql_calls": 9,
            "pending_remote_verification": 0,
            "response_counts_by_endpoint": dict(combined),
            "occurrence_counts_by_endpoint": dict(combined),
            "relations": {name: 100 for name in required},
            "relation_sizes": {
                name: {
                    "table_bytes": 1,
                    "index_bytes": 1,
                    "toast_bytes": 1,
                    "total_bytes": 100,
                }
                for name in required
            },
            "relation_stats": {name: {} for name in required},
            "affected_relations": list(required),
            "queues": {},
            "queue_age_seconds": {},
            "queue_residue": [],
        },
        "archive_operations": {
            "get": 40000,
            "get_bytes": 90000,
            "head": 100,
            "conditional_put": 12836,
            "put": 12836,
            "put_bytes": 50000,
            "conflicts": 0,
        },
        "storage_runway": {
            "measured_local_growth_bytes": 1,
            "days_to_80_percent": 2,
            "checks": [],
            "filesystem_type": "ext4",
            "inode_model": "finite",
        },
        "evidence": {
            "response_count": workload["official_responses"],
            "executed_responses": workload["executed_observations"],
            "exact_bytes": workload["official_bytes"],
            "execution_method": workload["aggregation_method"],
            "archived_bytes": workload["archive_stored_bytes"],
            "retries": 402,
            "downstream_processed": workload["official_responses"],
            "downstream_seconds": 20.0,
            "retry_seconds": 10.0,
            "concurrency_lanes": 32,
            "archive_objects": 12836,
        },
        "spool": {
            "final_bytes": 90000,
            "temporary_bytes": 0,
            "high_water_bytes": 100000,
            "final_object_count": 12800,
            "temporary_object_count": 0,
            "live_reservations": 0,
            "allocated_blocks": 195,
            "free_inodes": 100,
            "filesystem_type": "ext4",
            "inode_model": "finite",
        },
        "elapsed_seconds": 220.0,
        "cpu_seconds": 1.0,
        "peak_rss_kib": 10,
    }


def _failed_capacity_sample() -> dict:
    sample = _valid_capacity_sample()
    incomplete = runner._failed_capacity_workload("probe_timeout")
    incomplete["archive_operations"] = dict(sample["archive_operations"])
    sample["workload"] = incomplete
    sample["evidence"] = {
        "response_count": 0,
        "executed_responses": 0,
        "exact_bytes": 0,
        "execution_method": "exact integrated collector run",
        "retries": 0,
        "downstream_processed": 0,
        "downstream_seconds": 0.0,
        "retry_seconds": 0.0,
        "concurrency_lanes": 32,
        "archive_objects": 0,
    }
    sample["spool"] = {
        "final_bytes": 0,
        "temporary_bytes": 0,
        "high_water_bytes": 0,
        "final_object_count": 0,
        "temporary_object_count": 0,
        "live_reservations": 0,
        "allocated_blocks": 0,
        "free_inodes": None,
        "filesystem_type": "unknown",
        "inode_model": "unknown",
    }
    return sample


class NormalCapacityTest(unittest.TestCase):
    def test_retired_fixed_window_runner_cannot_start_traffic(self) -> None:
        with mock.patch.object(runner, "run") as execute, mock.patch("sys.stderr"):
            self.assertEqual(runner.main(["normal-capacity", "--database-url", "postgresql://unused"]), 2)
        execute.assert_not_called()

    def setUp(self) -> None:
        source_patch = mock.patch.object(
            runner, "_clean_source", return_value=SOURCE_SHA
        )
        source_patch.start()
        self.addCleanup(source_patch.stop)

    def test_fixed_workload_constants_are_exact(self) -> None:
        self.assertEqual(
            runner.CAPACITY_ENDPOINT_MIX,
            {"profile": 12_833, "battle_log": 12_833, "global_player_rankings": 1},
        )
        self.assertEqual(runner.CAPACITY_ORDINARY_ATTEMPTS, 25_667)
        self.assertEqual(runner.CAPACITY_PLAYERS, 12_833)
        self.assertEqual(runner.CAPACITY_RETRY_BUDGET, 4_333)
        self.assertEqual(runner.CAPACITY_RETRY_MINIMUM, 402)
        self.assertEqual(runner.CAPACITY_TOTAL_CAP, 30_000)
        self.assertEqual(
            runner.CAPACITY_ORDINARY_ATTEMPTS + runner.CAPACITY_RETRY_BUDGET,
            runner.CAPACITY_TOTAL_CAP,
        )
        self.assertEqual((runner.CAPACITY_LANES, runner.CAPACITY_KEYS), (32, 4))
        self.assertEqual(
            (runner.CAPACITY_PER_KEY_RPS, runner.CAPACITY_AGGREGATE_RPS),
            (25, 100),
        )
        self.assertIn(runner.NORMAL_CAPACITY_MODE, runner.MODES)

    def test_duplicate_execution_plan_covers_both_modes(self) -> None:
        executed, mix = runner._duplicate_execution_plan(
            runner.DUPLICATE_EXECUTION_CAP
        )
        self.assertEqual(executed, runner.DUPLICATE_EXECUTION_CAP)
        self.assertEqual(mix, dict(runner.DUPLICATE_ENDPOINT_MIX))
        executed, mix = runner._duplicate_execution_plan(6)
        self.assertEqual(executed, 6)
        self.assertEqual(
            mix, {"profile": 2, "battle_log": 2, "global_player_rankings": 2}
        )
        executed, mix = runner._duplicate_execution_plan(
            runner.CAPACITY_ORDINARY_ATTEMPTS, dict(runner.CAPACITY_ENDPOINT_MIX)
        )
        self.assertEqual(executed, runner.CAPACITY_ORDINARY_ATTEMPTS)
        self.assertEqual(mix, dict(runner.CAPACITY_ENDPOINT_MIX))

    def test_tags_are_reversible_and_parser_valid(self) -> None:
        from clashlens.profile import normalize_player_tag, parse_profile

        self.assertEqual(runner._tag(0), "#P00000")
        self.assertEqual(runner._tag(1), "#P00002")
        self.assertEqual(runner._tag(13), "#P0000V")
        self.assertEqual(runner._tag(14), "#P00020")
        for index in (0, 1, 2, 12, 13, 14, 100, 12832, 12833):
            tag = runner._tag(index)
            self.assertEqual(normalize_player_tag(tag), tag)
            self.assertEqual(runner._capacity_tag_index(tag), index)
        with self.assertRaises(ValueError):
            runner._capacity_tag_index("#C00001")
        with self.assertRaises(ValueError):
            runner._capacity_tag_index("not-a-tag")
        body = runner._profile_body(runner._tag(7), 0)
        profile = parse_profile(
            body,
            expected_tag=runner._tag(7),
            observed_at=runner.DAY_START,
            endpoint_version="profile-v1",
        )
        self.assertEqual(profile.normalized_tag, runner._tag(7))

    def test_integrated_marker_shape_is_exact_and_bounded(self) -> None:
        marker = json.dumps(_capacity_marker())
        output = "noise\n" + runner._CAPACITY_PROBE_MARKER + marker + "\n"
        with mock.patch.object(
            runner,
            "_run_bounded_process",
            return_value=(0, output, len(output), False, False),
        ):
            evidence = runner._capacity_probe(
                "postgresql://SECRET", 4333, Path("/tmp/spool"), "127.0.0.1:9", 60.0
            )
        self.assertEqual(evidence["ordinary_attempts"], 25667)
        self.assertEqual(evidence["per_key_max"], 25)
        self.assertEqual(evidence["budget_used_battlelog"], 13235)
        self.assertEqual(evidence["lanes"], 32)
        self.assertIn("elapsed_seconds", evidence)
        for bad in (
            "",
            runner._CAPACITY_PROBE_MARKER + "not-json\n",
            runner._CAPACITY_PROBE_MARKER + "x" * 4097 + "\n",
            runner._CAPACITY_PROBE_MARKER
            + json.dumps({"keys": 4, "per_key_max": 25})
            + "\n",
        ):
            with self.assertRaises(RuntimeError):
                runner._parse_capacity_probe_marker(bad)
        doubled = (
            "x\n"
            + runner._CAPACITY_PROBE_MARKER
            + marker
            + "\n"
            + runner._CAPACITY_PROBE_MARKER
            + marker
            + "\n"
        )
        with self.assertRaises(RuntimeError):
            runner._parse_capacity_probe_marker(doubled)

    def test_vacuous_evidence_is_rejected(self) -> None:
        for overrides, _label in (
            ({"retry_parents": 0, "retries_executed": 0}, "zero parents"),
            ({"per_key_max": 0, "aggregate_max": 0}, "vacuous rates"),
            ({"regular_allowed_during_reset": True}, "admitted reset"),
            (
                {
                    "ordinary_attempts": 0,
                    "profile_attempts": 0,
                    "battlelog_attempts": 0,
                    "ranking_attempts": 0,
                },
                "empty ordinary",
            ),
            ({"worker_errors": 1}, "unexplained worker error"),
            ({"s3_ordinary_put": 0, "s3_retry_put": 0}, "vacuous S3 counts"),
            ({"budget_used_battlelog": 0}, "budget mismatch"),
            ({"http_battlelog": 13236}, "over-cap loopback total"),
        ):
            wrong = dict(_capacity_marker(**overrides))
            with (
                mock.patch.object(
                    runner,
                    "_run_bounded_process",
                    return_value=(
                        0,
                        runner._CAPACITY_PROBE_MARKER + json.dumps(wrong) + "\n",
                        100,
                        False,
                        False,
                    ),
                ),
                self.assertRaises(runner._CapacityProbeFailure, msg=_label),
            ):
                runner._capacity_probe(
                    "postgresql://SECRET", 4333, Path("/tmp/spool"), "127.0.0.1:9", 60.0
                )

    def test_bounded_process_enforces_timeout_and_size(self) -> None:
        _code, _text, _size, timed_out, size_exceeded = runner._run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=dict(os.environ),
            timeout_seconds=1.0,
            output_cap_bytes=1 << 20,
        )
        self.assertTrue(timed_out)
        self.assertFalse(size_exceeded)
        _code, _text, size, timed_out, size_exceeded = runner._run_bounded_process(
            [sys.executable, "-c", "import sys,time\nfor _ in range(400): sys.stdout.write('x' * 100); time.sleep(0.005)"],
            env=dict(os.environ),
            timeout_seconds=30.0,
            output_cap_bytes=10_000,
        )
        self.assertFalse(timed_out)
        self.assertTrue(size_exceeded)
        self.assertLess(size, 1_000_000)

    def test_overlong_host_identity_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            runner._parse_capacity_probe_marker(
                runner._CAPACITY_PROBE_MARKER
                + json.dumps(_capacity_marker(host_id="h" * 257))
                + "\n"
            )

    def test_capacity_probe_timeout_returns_nonzero(self) -> None:
        def run_with_capacity_probe(_arguments: object) -> dict[str, object]:
            runner._capacity_probe(
                "postgresql://fixture.invalid/clashlens",
                4333,
                Path("/tmp/spool"),
                "127.0.0.1:9",
                60.0,
            )
            raise AssertionError("timed-out capacity probe unexpectedly returned")

        psycopg = mock.Mock(Error=RuntimeError)
        with (
            mock.patch.dict(sys.modules, {"psycopg": psycopg}),
            mock.patch.object(
                runner, "_run_bounded_process", return_value=(-1, "", 0, True, False)
            ),
            mock.patch.object(runner, "run", side_effect=run_with_capacity_probe),
            mock.patch("sys.stderr"),
        ):
            result = runner.main(
                [
                    "normal-capacity",
                    "--database-url",
                    "postgresql://fixture.invalid/clashlens",
                ]
            )
        self.assertEqual(result, 2)

    def test_clean_workload_maps_to_no_failure_codes(self) -> None:
        self.assertEqual(runner._capacity_hard_failure_codes(_valid_capacity_workload()), [])
        self.assertEqual(
            runner._capacity_hard_failure_codes(_valid_capacity_workload(), {"queue_residue": []}),
            [],
        )

    def test_count_off_by_one_is_rejected(self) -> None:
        full = _valid_capacity_workload()
        self.assertEqual(full["total_attempts"], 26069)
        self.assertLessEqual(full["total_attempts"], runner.CAPACITY_TOTAL_CAP)
        over = _valid_capacity_workload()
        over["total_attempts"] = runner.CAPACITY_TOTAL_CAP + 1
        self.assertIn(
            "capacity_count_exceeded",
            runner._capacity_hard_failure_codes(over),
        )
        short = _valid_capacity_workload()
        short["total_attempts"] -= 1
        self.assertIn(
            "capacity_count_exceeded",
            runner._capacity_hard_failure_codes(short),
        )
        zero_parents = _valid_capacity_workload()
        zero_parents["retry_summary"] = {"count": 0, "expected_count": 0}
        zero_parents["retry_lineage"] = {
            "attempted": 0,
            "processed": 0,
            "missing": 0,
            "duplicates": 0,
        }
        self.assertIn(
            "capacity_count_exceeded",
            runner._capacity_hard_failure_codes(zero_parents),
        )

    def test_ordinary_over_300s_is_a_deadline_failure(self) -> None:
        slow = _valid_capacity_workload()
        slow["drain_seconds"] = 522.0
        self.assertIn(
            "capacity_deadline_exceeded",
            runner._capacity_hard_failure_codes(slow),
        )
        runner._validate_capacity_protocol(
            slow, {"capacity_retry_budget": 4333}, "label"
        )

    def test_combined_footprint_over_384mib_is_rejected(self) -> None:
        fitting = _valid_capacity_workload()
        fitting["spool_peak_bytes"] = 64 << 20
        fitting["pg_peak_bytes"] = 320 << 20
        fitting["go_output_bytes"] = 0
        self.assertNotIn(
            "capacity_total_exceeded",
            runner._capacity_hard_failure_codes(fitting),
        )
        bloated = _valid_capacity_workload()
        bloated["spool_peak_bytes"] = 64 << 20
        bloated["pg_peak_bytes"] = 320 << 20
        bloated["go_output_bytes"] = 17 << 20
        self.assertIn(
            "capacity_total_exceeded",
            runner._capacity_hard_failure_codes(bloated),
        )

    def test_lane_rate_mix_lineage_interactive_reset_and_spool_codes(self) -> None:
        cases = (
            ("lanes", 31, "capacity_lane_mismatch"),
            ("interactive_attempts", 1, "capacity_interactive_use"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                workload = _valid_capacity_workload()
                workload[field] = value
                self.assertIn(code, runner._capacity_hard_failure_codes(workload))
        workload = _valid_capacity_workload()
        workload["rate_maxima"] = {"per_key": 26, "aggregate": 100}
        self.assertIn(
            "capacity_rate_exceeded",
            runner._capacity_hard_failure_codes(workload),
        )
        workload = _valid_capacity_workload()
        workload["endpoint_mix"] = dict(runner.CAPACITY_ENDPOINT_MIX)
        workload["endpoint_mix"]["profile"] -= 1
        self.assertIn(
            "capacity_mix_mismatch",
            runner._capacity_hard_failure_codes(workload),
        )
        workload = _valid_capacity_workload()
        workload["retry_lineage"] = {
            "attempted": 402,
            "processed": 401,
            "missing": 1,
            "duplicates": 0,
        }
        self.assertIn(
            "capacity_lineage_mismatch",
            runner._capacity_hard_failure_codes(workload),
        )
        workload = _valid_capacity_workload()
        workload["reset_exclusion"] = {
            "regular_allowed": True,
            "regular_scheduled_dry": 0,
            "sweep_delta": 0,
            "reset_members": 2,
        }
        self.assertIn(
            "capacity_reset_admitted",
            runner._capacity_hard_failure_codes(workload),
        )
        workload = _valid_capacity_workload()
        workload["spool_peak_bytes"] = runner.CAPACITY_SPOOL_BYTES + 1
        self.assertIn(
            "capacity_spool_exceeded",
            runner._capacity_hard_failure_codes(workload),
        )
        # Relation growth alone is reported evidence; only the peak is
        # enforced, so a projected 105MB growth under a 110MB peak passes.
        workload = _valid_capacity_workload()
        workload["pg_growth_bytes"] = 105_000_000
        workload["pg_peak_bytes"] = 110_000_000
        workload["wal_bytes"] = 5_000_000
        self.assertNotIn(
            "capacity_pg_exceeded",
            runner._capacity_hard_failure_codes(workload),
        )
        over = _valid_capacity_workload()
        over["pg_peak_bytes"] = runner.CAPACITY_PG_PEAK_BYTES + 1
        self.assertIn(
            "capacity_pg_exceeded",
            runner._capacity_hard_failure_codes(over),
        )
        workload = _valid_capacity_workload()
        workload["archive_operations"] = dict(workload["archive_operations"])
        workload["archive_operations"]["conflicts"] = 1
        self.assertIn(
            "capacity_evidence_incomplete",
            runner._capacity_hard_failure_codes(workload),
        )
        workload = _valid_capacity_workload()
        workload["processing_summary"] = {"count": 1, "expected_count": 2}
        self.assertIn(
            "fixed_acceptance_failure",
            runner._capacity_hard_failure_codes(workload),
        )
        workload = _valid_capacity_workload()
        workload["downstream_summary"] = {"count": 1, "expected_count": 2}
        self.assertIn(
            "fixed_acceptance_failure",
            runner._capacity_hard_failure_codes(workload),
        )
        self.assertIn(
            "queue_residue",
            runner._capacity_hard_failure_codes(
                _valid_capacity_workload(), {"queue_residue": [{"count": 1}]}
            ),
        )

    def test_capacity_protocol_rejects_contradictions(self) -> None:
        config = {"capacity_retry_budget": 4333}
        runner._validate_capacity_protocol(_valid_capacity_workload(), config, "label")
        bad_remote = _valid_capacity_workload()
        bad_remote["official_remote_requests"] = 1
        with self.assertRaisesRegex(ValueError, "remote official"):
            runner._validate_capacity_protocol(bad_remote, config, "label")
        bad_budget = _valid_capacity_workload()
        bad_budget["budget_consumed"] = dict(bad_budget["budget_consumed"])
        bad_budget["budget_consumed"]["battle_log"] = 0
        with self.assertRaisesRegex(ValueError, "budget"):
            runner._validate_capacity_protocol(bad_budget, config, "label")
        bad_origin = _valid_capacity_workload()
        bad_origin["archive_origin_host"] = "archive.example"
        sample = _valid_capacity_sample()
        sample["workload"] = bad_origin
        with self.assertRaisesRegex(ValueError, "loopback"):
            runner._validate_capacity_sample_semantics(sample, config, [], "label")

    def test_capacity_sample_semantics_require_exact_evidence_and_failures(self) -> None:
        config = {"capacity_retry_budget": 4333}
        sample = _valid_capacity_sample()
        runner._validate_capacity_sample_semantics(sample, config, [], "sample 0")
        bloated = deepcopy(sample)
        bloated["spool"]["high_water_bytes"] = 100001
        with self.assertRaisesRegex(ValueError, "disagrees"):
            runner._validate_capacity_sample_semantics(bloated, config, [], "sample 0")
        residue = deepcopy(sample)
        residue["database"]["queue_residue"] = [{"count": 1}]
        with self.assertRaisesRegex(ValueError, "incomplete"):
            runner._validate_capacity_sample_semantics(residue, config, [], "sample 0")
        runner._validate_capacity_sample_semantics(
            residue, config, ["queue_residue"], "sample 0"
        )

    def test_failed_probe_artifact_is_retained_and_valid(self) -> None:
        for reason, primary in (
            ("probe_timeout", "capacity_deadline_exceeded"),
            ("probe_error", "capacity_evidence_incomplete"),
            ("probe_output_exceeded", "capacity_evidence_incomplete"),
            ("downstream_error", "capacity_evidence_incomplete"),
        ):
            workload = runner._failed_capacity_workload(reason)
            self.assertEqual(workload["status"], "incomplete")
            self.assertIn(primary, workload["hard_failures"])
            runner._validate_capacity_protocol(
                workload, {"capacity_retry_budget": 4333}, "label"
            )
        sample = _failed_capacity_sample()
        runner._validate_capacity_sample_semantics(
            sample, {"capacity_retry_budget": 4333}, sample["workload"]["hard_failures"], "sample 0"
        )
        with self.assertRaises(ValueError):
            runner._failed_capacity_workload("bogus_reason")

    def test_capacity_assembly_wires_real_measurements(self) -> None:
        workload = _valid_capacity_workload()
        measurements = deepcopy(_valid_capacity_sample()["database"])
        spool_dir = Path(tempfile.mkdtemp(prefix="capacity-assembly-test-"))
        workload["spool_dir"] = str(spool_dir)
        handler = mock.Mock()
        handler.gets = 1
        handler.get_bytes = 2
        handler.heads = 3
        handler.conditional_puts = 4
        handler.puts = 12836
        handler.put_bytes = 50000
        handler.conflicts = 0
        handler.objects = {f"key{i}": b"b" for i in range(12836)}
        archive = ("127.0.0.1:9", "", "", handler)
        workload["archive_operations"] = {
            "get": 40000,
            "get_bytes": 90000,
            "head": 100,
            "conditional_put": 12836,
            "put": 12836,
            "put_bytes": 50000,
            "conflicts": 0,
        }
        with (
            mock.patch.object(
                runner, "_orphan_metrics", return_value={"count": 7, "bytes": 8}
            ),
            mock.patch.object(runner, "_pending_age_seconds", return_value=1.5),
        ):
            sample = runner._capacity_sample(
                workload,
                measurements,
                "postgresql://stub",
                {},
                {
                    "path": "/",
                    "usable_capacity_bytes": 10**9,
                    "raw_capacity_bytes": 10**9,
                    "used_bytes": 10**6,
                    "filesystem_type": "ext4",
                    "inode_model": "finite",
                },
                0.0,
                0.0,
                archive,
                spool_dir,
            )
        self.assertTrue(spool_dir.exists())
        shutil.rmtree(spool_dir, ignore_errors=True)
        self.assertIn("spool_dir", workload)
        workload["endpoint_mix"] = dict(workload["endpoint_mix"])
        self.assertEqual(sample["evidence"]["orphan_count"], 7)
        self.assertEqual(sample["evidence"]["archive_objects"], 12836)
        self.assertEqual(sample["evidence"]["retries"], 402)
        self.assertEqual(sample["archive_operations"]["conflicts"], 0)
        self.assertEqual(
            sample["spool"]["allocated_blocks"], 110000 // 512
        )
        runner._validate_capacity_sample_semantics(
            sample, {"capacity_retry_budget": 4333}, [], "sample 0"
        )

    def test_spool_cleanup_refuses_untrusted_paths(self) -> None:
        victim = Path(tempfile.mkdtemp(prefix="capacity-cleanup-victim-"))
        (victim / "sentinel").write_text("keep")
        self.addCleanup(shutil.rmtree, victim, True)
        home = Path.home()
        cases = [
            "",
            ".",
            "/",
            "~",
            str(runner.ROOT),
            str(home),
            str(Path.cwd()),
            "/tmp/capacity-spool-not-ours",
            "/tmp/capacity-spool-missing-0123456789abcdef",
            victim / "capacity-spool-planted",
            None,
            123,
        ]
        for case in cases:
            with self.subTest(case=str(case)):
                self.assertFalse(runner._remove_owned_spool_dir(case))
        self.assertTrue((victim / "sentinel").exists())
        owned = Path(tempfile.mkdtemp(prefix="capacity-spool-"))
        (owned / "sentinel").write_text("owned")
        self.assertTrue(runner._remove_owned_spool_dir(owned))
        self.assertFalse(owned.exists())
        self.assertFalse(runner._remove_owned_spool_dir(owned))

    def test_incomplete_assembly_preserves_cwd_and_sentinel(self) -> None:
        previous = Path.cwd()
        workdir = Path(tempfile.mkdtemp(prefix="capacity-cwd-"))
        self.addCleanup(shutil.rmtree, workdir, True)
        (workdir / "sentinel").write_text("keep")
        owned = Path(tempfile.mkdtemp(prefix="capacity-spool-"))
        (owned / "owned-sentinel").write_text("owned")
        os.chdir(workdir)
        self.addCleanup(os.chdir, previous)
        workload = runner._failed_capacity_workload("probe_timeout")
        self.assertEqual(workload["spool_dir"], "")
        handler = mock.Mock()
        handler.gets = 0
        handler.get_bytes = 0
        handler.heads = 0
        handler.conditional_puts = 0
        handler.puts = 0
        handler.put_bytes = 0
        handler.conflicts = 0
        handler.objects = {}
        archive = ("127.0.0.1:9", "", "", handler)
        with (
            mock.patch.object(
                runner, "_orphan_metrics", return_value={"count": 0, "bytes": 0}
            ),
            mock.patch.object(runner, "_pending_age_seconds", return_value=None),
        ):
            sample = runner._capacity_sample(
                workload,
                _valid_capacity_sample()["database"],
                "postgresql://stub",
                {},
                {
                    "path": "/",
                    "usable_capacity_bytes": 10**9,
                    "raw_capacity_bytes": 10**9,
                    "used_bytes": 10**6,
                    "filesystem_type": "ext4",
                    "inode_model": "finite",
                },
                0.0,
                0.0,
                archive,
                owned,
            )
        # Assembly removes nothing: the CWD sentinel and the owned dir both
        # survive; only the run-level guarded cleanup removes owned paths.
        self.assertTrue((workdir / "sentinel").exists())
        self.assertEqual(Path.cwd(), workdir)
        self.assertTrue((owned / "owned-sentinel").exists())
        self.assertEqual(sample["workload"]["status"], "incomplete")
        self.assertTrue(runner._remove_owned_spool_dir(owned))
        self.assertFalse(owned.exists())

    def test_capacity_sample_call_matches_run_mode(self) -> None:
        import inspect

        self.assertEqual(
            list(inspect.signature(runner._capacity_sample).parameters),
            [
                "workload",
                "measurements",
                "connection_info",
                "relation_start",
                "filesystem_before",
                "cpu_start",
                "elapsed_start",
                "archive",
                "spool_dir",
            ],
        )

    def test_capacity_artifact_validates_end_to_end(self) -> None:
        artifact = _valid_artifact("normal-capacity")
        workload = _valid_capacity_workload()
        measurements = deepcopy(_valid_capacity_sample()["database"])
        owned = Path(tempfile.mkdtemp(prefix="capacity-spool-"))
        self.addCleanup(shutil.rmtree, owned, True)
        handler = mock.Mock()
        handler.gets = 40000
        handler.get_bytes = 90000
        handler.heads = 100
        handler.conditional_puts = 12836
        handler.puts = 12836
        handler.put_bytes = 50000
        handler.conflicts = 0
        handler.objects = {f"key{i}": b"b" for i in range(12836)}
        archive = ("127.0.0.1:9", "", "", handler)
        workload["archive_operations"] = {
            "get": 40000,
            "get_bytes": 90000,
            "head": 100,
            "conditional_put": 12836,
            "put": 12836,
            "put_bytes": 50000,
            "conflicts": 0,
        }
        with (
            mock.patch.object(
                runner, "_orphan_metrics", return_value={"count": 0, "bytes": 0}
            ),
            mock.patch.object(runner, "_pending_age_seconds", return_value=1.5),
        ):
            assembled = runner._capacity_sample(
                workload,
                measurements,
                "postgresql://stub",
                {},
                {
                    "path": "/",
                    "usable_capacity_bytes": 10**9,
                    "raw_capacity_bytes": 10**9,
                    "used_bytes": 10**6,
                    "filesystem_type": "ext4",
                    "inode_model": "finite",
                },
                0.0,
                0.0,
                archive,
                owned,
            )
        artifact["samples"] = [assembled]
        artifact["hard_failures"] = []
        configuration = artifact["provenance"]["configuration"]
        configuration["capacity_retry_budget"] = 4333
        artifact["provenance"]["configuration_fingerprint"] = runner._sha(
            json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
        )
        artifact["artifact_digest"] = runner._artifact_digest(artifact)
        runner.validate_artifact(artifact)
        incomplete = _valid_artifact("normal-capacity")
        incomplete["samples"] = [_failed_capacity_sample()]
        incomplete["hard_failures"] = list(
            _failed_capacity_sample()["workload"]["hard_failures"]
        )
        incomplete["artifact_digest"] = runner._artifact_digest(incomplete)
        runner.validate_artifact(incomplete)
        mixed = deepcopy(artifact)
        mixed["samples"][0]["workload"]["endpoint_mix"] = dict(
            runner.CAPACITY_ENDPOINT_MIX
        )
        mixed["samples"][0]["workload"]["endpoint_mix"]["profile"] -= 1
        mixed["artifact_digest"] = runner._artifact_digest(mixed)
        with self.assertRaises(ValueError):
            runner.validate_artifact(mixed)

    def test_capacity_arguments_require_32_lanes_and_bounded_tranche(self) -> None:
        arguments = runner.parse_arguments(["normal-capacity"])
        self.assertEqual(arguments.lanes, 32)
        self.assertEqual(arguments.capacity_retries, runner.CAPACITY_RETRY_BUDGET)
        with self.assertRaises(SystemExit):
            runner.parse_arguments(["normal-capacity", "--lanes", "31"])
        with self.assertRaises(SystemExit):
            runner.parse_arguments(
                ["normal-capacity", "--capacity-retries", str(runner.CAPACITY_RETRY_BUDGET + 1)]
            )
        with self.assertRaises(SystemExit):
            runner.parse_arguments(["normal-capacity", "--capacity-retries", "401"])
        arguments = runner.parse_arguments(
            ["normal-capacity", "--capacity-retries", str(runner.CAPACITY_RETRY_MINIMUM)]
        )
        self.assertEqual(arguments.capacity_retries, runner.CAPACITY_RETRY_MINIMUM)

    def test_owned_cleanup_accepts_explicit_prefixes(self) -> None:
        first = Path(tempfile.mkdtemp(prefix="capacity-spool-"))
        (first / "sentinel").write_text("owned")
        second = Path(tempfile.mkdtemp(prefix="clashlens-perf-spool-"))
        (second / "sentinel").write_text("owned")
        try:
            self.assertTrue(
                runner._remove_owned_spool_dir(
                    first, prefixes=("capacity-spool-", "clashlens-perf-spool-")
                )
            )
            self.assertFalse(first.exists())
            self.assertTrue(
                runner._remove_owned_spool_dir(
                    second, prefixes=("capacity-spool-", "clashlens-perf-spool-")
                )
            )
            self.assertFalse(second.exists())
        finally:
            shutil.rmtree(first, ignore_errors=True)
            shutil.rmtree(second, ignore_errors=True)
        third = Path(tempfile.mkdtemp(prefix="clashlens-perf-spool-"))
        try:
            self.assertFalse(runner._remove_owned_spool_dir(third))
            self.assertTrue(third.exists())
        finally:
            shutil.rmtree(third, ignore_errors=True)

    def test_run_normal_capacity_returns_owned_triple_and_exact_schema(self) -> None:
        marker = _capacity_marker()
        evidence = dict(
            marker, elapsed_seconds=1.0, go_output_bytes=100, spool_peak_bytes=100
        )
        downstream_root = Path(tempfile.mkdtemp(prefix="clashlens-perf-spool-"))
        (downstream_root / "body").write_bytes(b"x" * 3000)
        summary = runner._result_summary(
            [{"outcome": "processed", "elapsed_ms": 1.0}] * 26069, expected=26069
        )
        stats = {
            "final_bytes": 50,
            "high_water_bytes": 60,
            "allocated_peak_bytes": 4096,
        }
        archive = ("127.0.0.1:9", "", "", mock.Mock())
        with (
            mock.patch.object(runner, "_capacity_probe", return_value=evidence),
            mock.patch.object(
                runner,
                "_drain_downstream",
                return_value=(summary, stats, 5.0, str(downstream_root)),
            ) as drain,
        ):
            workload, owned, identity, active = runner._run_normal_capacity(
                "postgresql://stub", archive, 4333, 600.0
            )
        self.assertFalse(active)
        self.assertEqual(drain.call_args.args[-1], owned)
        self.assertEqual(set(workload), set(runner._CAPACITY_WORKLOAD_KEYS))
        self.assertTrue(Path(owned).exists())
        self.assertEqual(identity, (owned.lstat().st_dev, owned.lstat().st_ino))
        # Downstream allocated peak (4096 for the 3000-byte file) merges
        # into the retained spool peak evidence.
        self.assertEqual(workload["spool_peak_bytes"], 4096)
        shutil.rmtree(owned, ignore_errors=True)
        shutil.rmtree(downstream_root, ignore_errors=True)

    def test_expired_deadline_fails_fast_without_drain(self) -> None:
        # A 60s budget is already exhausted by the 60s downstream reserve,
        # so the drain must not launch (the old max(60, remaining) floor
        # would have launched it anyway).
        marker = _capacity_marker()
        evidence = dict(marker, elapsed_seconds=0.0, go_output_bytes=100)
        archive = ("127.0.0.1:9", "", "", mock.Mock())
        drain = mock.Mock(side_effect=AssertionError("drain must not launch"))
        with (
            mock.patch.object(runner, "_capacity_probe", return_value=evidence),
            mock.patch.object(runner, "_drain_downstream", drain),
        ):
            workload, _owned, _identity, active = runner._run_normal_capacity(
                "postgresql://stub", archive, 4333, 60.0
            )
        self.assertFalse(active)
        drain.assert_not_called()
        self.assertEqual(workload["status"], "incomplete")
        self.assertEqual(workload["failure"], "probe_timeout")
        self.assertIn(
            "capacity_deadline_exceeded", workload["hard_failures"]
        )

    def test_pg_growth_projection_governed_by_peak_caps(self) -> None:
        workload = _valid_capacity_workload()
        workload["pg_growth_bytes"] = 105_000_000
        workload["pg_peak_bytes"] = 110_000_000
        workload["wal_bytes"] = 5_000_000
        codes = runner._capacity_hard_failure_codes(workload)
        self.assertNotIn("capacity_pg_exceeded", codes)
        over = _valid_capacity_workload()
        over["pg_peak_bytes"] = runner.CAPACITY_PG_PEAK_BYTES + 1
        self.assertIn(
            "capacity_pg_exceeded", runner._capacity_hard_failure_codes(over)
        )

    def test_bounded_process_reaps_descendant_group(self) -> None:
        probe_file = Path(tempfile.mkdtemp(prefix="capacity-descendant-"))
        self.addCleanup(shutil.rmtree, probe_file, True)
        witness = probe_file / "witness.log"
        grandchild = (
            "import sys,time\n"
            "while True:\n"
            '    open(sys.argv[1],"a").write("z")\n'
            "    time.sleep(0.02)\n"
        )
        leader = (
            "import subprocess,sys\n"
            "subprocess.Popen([sys.executable,\"-c\"," + repr(grandchild) + ",sys.argv[1]])\n"
        )
        command = [sys.executable, "-c", leader, str(witness)]
        results: dict[str, object] = {}

        def target() -> None:
            results["out"] = runner._run_bounded_process(
                command, dict(os.environ), 5.0, 1 << 20
            )

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(60)
        self.assertFalse(worker.is_alive(), "bounded process hung on live descendants")
        _code, _text, total, _timed, _sized = results["out"]
        self.assertLessEqual(total, (1 << 20) + 65536)
        # The grandchild demonstrably ran, then the group kill stopped it.
        self.assertTrue(witness.exists())
        first_size = witness.stat().st_size
        self.assertGreater(first_size, 0)
        time.sleep(1.0)
        self.assertEqual(
            witness.stat().st_size,
            first_size,
            "descendant survived the process-group kill",
        )

    def test_capacity_processor_reads_warm_spool_without_remote_repair(self) -> None:
        from clashlens.spool import Spool

        with tempfile.TemporaryDirectory(prefix="capacity-spool-") as directory:
            root = Path(directory)
            body = b'{"items":[]}'
            digest = hashlib.sha256(body).hexdigest()
            seeded = Spool(directory, max_body_bytes=1 << 20, max_bytes=runner.CAPACITY_SPOOL_BYTES, max_objects=30000)
            seeded.publish(body, digest)
            seeded.close()
            with runner.archive_server() as archive, mock.patch("clashlens.db.Database"):
                database, _processor, _metrics, reader = runner._processor(
                    "postgresql://stub", archive, capacity_spool=root
                )
                try:
                    result = reader.read_verified(f"s3://evidence/sha256/{digest[:2]}/{digest}", digest)
                    self.assertEqual(result.body, body)
                    self.assertEqual(archive[3].gets, 0)
                    self.assertEqual(reader.spool.max_bytes, runner.CAPACITY_SPOOL_BYTES)
                    self.assertEqual(reader.spool.max_body_bytes, 1 << 20)
                finally:
                    database.close()
                    reader.spool.close()
            self.assertEqual((root / "sha256" / digest[:2] / digest).read_bytes(), body)

    def test_downstream_drain_preserves_shared_collector_spool(self) -> None:
        downstream_root = Path(tempfile.mkdtemp(prefix="capacity-spool-"))
        self.addCleanup(shutil.rmtree, downstream_root, True)
        (downstream_root / "body").write_bytes(b"x" * 3000)

        def immediate(job_id: int, *, owner: str, lease_seconds: int):
            return mock.Mock(job_id=job_id, outcome="processed", category=None)

        processor = mock.Mock()
        processor.process_job = immediate
        processor.process_once.return_value = None
        database = mock.Mock()
        spool = mock.Mock()
        spool.stats.return_value = {"final_bytes": 50, "high_water_bytes": 60}
        spool.spool.root = str(downstream_root)
        connected = mock.MagicMock()
        connected.execute.return_value.fetchall.return_value = [(1,), (2,)]
        psycopg = mock.MagicMock()
        psycopg.connect.return_value.__enter__.return_value = connected
        with mock.patch.object(
            runner, "_processor", return_value=(database, processor, None, spool)
        ), mock.patch.dict(sys.modules, {"psycopg": psycopg}):
            summary, stats, _elapsed, root = runner._drain_downstream(
                "postgresql://stub", mock.Mock(), 2, 60.0, downstream_root
            )
        self.assertEqual(summary["count"], 2)
        self.assertEqual(stats["allocated_peak_bytes"], 4096)
        self.assertEqual(root, str(downstream_root))
        self.assertEqual((downstream_root / "body").read_bytes(), b"x" * 3000)
        spool.spool.close.assert_called_once()

    def test_downstream_drain_cancels_hung_futures(self) -> None:
        import threading

        stop = threading.Event()

        def stuck(job_id: int, *, owner: str, lease_seconds: int):
            stop.wait(5)
            return mock.Mock(job_id=job_id, outcome="processed", category=None)

        processor = mock.Mock()
        processor.process_job = stuck
        database = mock.Mock()
        spool = mock.Mock()
        spool.stats.return_value = {"final_bytes": 1, "high_water_bytes": 2}
        connected = mock.MagicMock()
        connected.execute.return_value.fetchall.return_value = [(1,), (2,)]
        psycopg = mock.MagicMock()
        psycopg.connect.return_value.__enter__.return_value = connected
        with mock.patch.object(
            runner, "_processor", return_value=(database, processor, None, spool)
        ), mock.patch.dict(
            sys.modules, {"psycopg": psycopg}
        ), self.assertRaises(
            runner._CapacityProbeFailure
        ) as raised:
            runner._drain_downstream("postgresql://stub", mock.Mock(), 2, 1.0, Path("unused"))
        self.assertEqual(raised.exception.reason, "probe_timeout")
        self.assertTrue(raised.exception.active)
        stop.set()

    def test_run_normal_capacity_tuple_annotation(self) -> None:
        import inspect

        annotation = inspect.signature(runner._run_normal_capacity).return_annotation
        self.assertIn("tuple", str(annotation))

    def test_protocol_rejects_total_contradictions(self) -> None:
        config = {"capacity_retry_budget": 4333}
        base = _valid_capacity_workload()
        bad = dict(base, observations=0)
        with self.assertRaisesRegex(ValueError, "observations"):
            runner._validate_capacity_protocol(bad, config, "label")
        bad = dict(base, official_responses=base["official_responses"] + 1)
        with self.assertRaisesRegex(ValueError, "response"):
            runner._validate_capacity_protocol(bad, config, "label")
        bad = dict(base, executed_observations=0)
        with self.assertRaisesRegex(ValueError, "response"):
            runner._validate_capacity_protocol(bad, config, "label")

    def test_protocol_rejects_top_retry_identity_mismatch(self) -> None:
        config = {"capacity_retry_budget": 4333}
        bad = _valid_capacity_workload()
        bad["retry_tranche_attempted"] = 1
        with self.assertRaisesRegex(ValueError, "top retry"):
            runner._validate_capacity_protocol(bad, config, "label")
        bad = _valid_capacity_workload()
        bad["retry_tranche_processed"] = 1
        with self.assertRaisesRegex(ValueError, "top retry"):
            runner._validate_capacity_protocol(bad, config, "label")

    def test_protocol_rejects_empty_host_identity(self) -> None:
        config = {"capacity_retry_budget": 4333}
        for bad_host in ("", None):
            bad = _valid_capacity_workload()
            bad["host_id"] = bad_host
            with self.assertRaisesRegex(ValueError, "host"):
                runner._validate_capacity_protocol(bad, config, "label")

    def test_protocol_rejects_downstream_distribution_shortfall(self) -> None:
        config = {"capacity_retry_budget": 4333}
        short = _valid_capacity_workload()
        summary = dict(short["downstream_summary"])
        outcomes = dict(summary["outcomes"])
        outcomes["processed"] -= 1
        outcomes["other"] += 1
        summary["outcomes"] = outcomes
        short["downstream_summary"] = summary
        with self.assertRaisesRegex(ValueError, "downstream"):
            runner._validate_capacity_protocol(short, config, "label")
        failed = _valid_capacity_workload()
        summary = dict(failed["downstream_summary"])
        summary["failed_count"] = 1
        failed["downstream_summary"] = summary
        with self.assertRaisesRegex(ValueError, "downstream"):
            runner._validate_capacity_protocol(failed, config, "label")

    def test_drain_timeout_leaves_db_and_spool_untouched(self) -> None:
        stop = threading.Event()

        def stuck(job_id: int, *, owner: str, lease_seconds: int):
            stop.wait(30)
            return mock.Mock(job_id=job_id, outcome="processed", category=None)

        processor = mock.Mock()
        processor.process_job = stuck
        database = mock.Mock()
        downstream_root = Path(tempfile.mkdtemp(prefix="clashlens-perf-spool-"))
        spool = mock.Mock()
        spool.stats.return_value = {"final_bytes": 1, "high_water_bytes": 2}
        spool.spool.root = str(downstream_root)
        connected = mock.MagicMock()
        connected.execute.return_value.fetchall.return_value = [(1,)]
        psycopg = mock.MagicMock()
        psycopg.connect.return_value.__enter__.return_value = connected
        try:
            with mock.patch.object(
                runner, "_processor", return_value=(database, processor, None, spool)
            ), mock.patch.dict(
                sys.modules, {"psycopg": psycopg}
            ), self.assertRaises(
                runner._CapacityProbeFailure
            ):
                runner._drain_downstream("postgresql://stub", mock.Mock(), 1, 1.0, downstream_root)
            database.close.assert_not_called()
            self.assertTrue(downstream_root.exists())
        finally:
            stop.set()
            shutil.rmtree(downstream_root, ignore_errors=True)

    def test_expired_setup_assembly_without_spool_dir(self) -> None:
        previous = Path.cwd()
        workdir = Path(tempfile.mkdtemp(prefix="capacity-cwd-"))
        self.addCleanup(shutil.rmtree, workdir, True)
        (workdir / "sentinel").write_text("keep")
        os.chdir(workdir)
        self.addCleanup(os.chdir, previous)
        workload = runner._failed_capacity_workload("probe_timeout")
        workload["spool_dir"] = ""
        handler = mock.Mock()
        handler.gets = 0
        handler.get_bytes = 0
        handler.heads = 0
        handler.conditional_puts = 0
        handler.puts = 0
        handler.put_bytes = 0
        handler.conflicts = 0
        handler.objects = {}
        archive = ("127.0.0.1:9", "", "", handler)
        with (
            mock.patch.object(
                runner, "_orphan_metrics", return_value={"count": 0, "bytes": 0}
            ),
            mock.patch.object(runner, "_pending_age_seconds", return_value=None),
        ):
            sample = runner._incomplete_capacity_sample(
                workload,
                _valid_capacity_sample()["database"],
                archive,
                {
                    "path": "/",
                    "usable_capacity_bytes": 10**9,
                    "raw_capacity_bytes": 10**9,
                    "used_bytes": 10**6,
                    "filesystem_type": "ext4",
                    "inode_model": "finite",
                },
                {},
            )
        runner._validate_capacity_sample_semantics(
            sample, {"capacity_retry_budget": 4333}, sample["workload"]["hard_failures"], "sample 0"
        )
        self.assertTrue((workdir / "sentinel").exists())
        self.assertEqual(Path.cwd(), workdir)

    def test_capacity_sample_rejects_none_spool_dir(self) -> None:
        with self.assertRaises(ValueError):
            runner._capacity_sample(
                _valid_capacity_workload(),
                _valid_capacity_sample()["database"],
                "postgresql://stub",
                {},
                {
                    "path": "/",
                    "usable_capacity_bytes": 10**9,
                    "raw_capacity_bytes": 10**9,
                    "used_bytes": 10**6,
                    "filesystem_type": "ext4",
                    "inode_model": "finite",
                },
                0.0,
                0.0,
                ("127.0.0.1:9", "", "", mock.Mock()),
                None,
            )

    def test_direct_script_discovers_slice_d_tests(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "test_performance_runner.py"),
                "NormalCapacityTest.test_fixed_workload_constants_are_exact",
            ],
            check=False,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        self.assertIn("OK", completed.stderr)

    def test_workload_archive_binding_matches_evidence(self) -> None:
        workload = _valid_capacity_workload()
        workload["archive_objects"] = 0
        sample = _valid_capacity_sample()
        sample["workload"] = workload
        with self.assertRaisesRegex(ValueError, "archive"):
            runner._validate_capacity_sample_semantics(
                sample, {"capacity_retry_budget": 4333}, [], "sample 0"
            )


    def test_expired_setup_keeps_valid_incomplete_artifact(self) -> None:
        import contextlib

        handler = mock.Mock()
        handler.gets = 0
        handler.get_bytes = 0
        handler.heads = 0
        handler.conditional_puts = 0
        handler.puts = 0
        handler.put_bytes = 0
        handler.conflicts = 0
        handler.objects = {}

        @contextlib.contextmanager
        def fake_database(url: str, **kwargs: object):
            del url, kwargs
            yield "postgresql://stub-info"

        @contextlib.contextmanager
        def fake_archive():
            yield ("127.0.0.1:9", "", "", handler)

        domain_support = mock.Mock()
        domain_support.domain_database = fake_database
        database = deepcopy(_valid_capacity_sample()["database"])
        filesystem = {
            "path": "/",
            "usable_capacity_bytes": 10**9,
            "raw_capacity_bytes": 10**9,
            "used_bytes": 10**6,
            "filesystem_type": "ext4",
            "inode_model": "finite",
        }
        arguments = runner.parse_arguments(
            ["normal-capacity", "--database-url", "postgresql://stub"]
        )
        with (
            mock.patch.dict(sys.modules, {"domain_test_support": domain_support}),
            mock.patch.object(runner, "archive_server", fake_archive),
            mock.patch.object(
                runner, "_collector_probe", return_value={"executed": False, "reason": "test"}
            ),
            mock.patch.object(runner, "_start_metrics", return_value=("0/0", None, 0)),
            mock.patch.object(runner, "_relation_snapshot", return_value={}),
            mock.patch.object(runner, "_filesystem_usage", return_value=filesystem),
            mock.patch.object(runner, "_postgres_provenance", return_value=_test_postgres()),
            mock.patch.object(runner, "_db_snapshot", return_value=database),
            mock.patch.object(runner, "_pending_age_seconds", return_value=None),
            mock.patch.object(
                runner,
                "_capacity_sample",
                side_effect=AssertionError("full assembly must not run without a spool"),
            ),
            mock.patch.object(runner, "CAPACITY_WALL_SECONDS", 100),
        ):
            artifact = runner.run(arguments)
        self.assertEqual(artifact["samples"][0]["workload"]["status"], "incomplete")
        self.assertTrue(artifact["hard_failures"])

    def test_unquiesced_drain_holds_before_any_evidence_work(self) -> None:
        import contextlib

        db_context = mock.MagicMock()
        db_context.__enter__.return_value = "postgresql://stub-info"
        archive_context = mock.MagicMock()
        archive_context.__enter__.return_value = ("127.0.0.1:9", "", "", mock.Mock())
        domain_support = mock.Mock()
        domain_support.domain_database = mock.Mock(return_value=db_context)
        failed = runner._failed_capacity_workload("probe_timeout")
        arguments = runner.parse_arguments(
            ["normal-capacity", "--database-url", "postgresql://stub"]
        )
        forbidden = mock.Mock(side_effect=AssertionError("evidence work must not run"))
        patches = (
            mock.patch.dict(sys.modules, {"domain_test_support": domain_support}),
            mock.patch.object(runner, "archive_server", return_value=archive_context),
            mock.patch.object(
                runner, "_collector_probe", return_value={"executed": False, "reason": "test"}
            ),
            mock.patch.object(runner, "_start_metrics", return_value=("0/0", None, 0)),
            mock.patch.object(runner, "_relation_snapshot", return_value={}),
            mock.patch.object(
                runner,
                "_filesystem_usage",
                return_value={
                    "path": "/",
                    "usable_capacity_bytes": 10**9,
                    "raw_capacity_bytes": 10**9,
                    "used_bytes": 10**6,
                    "filesystem_type": "ext4",
                    "inode_model": "finite",
                },
            ),
            mock.patch.object(runner, "_postgres_provenance", return_value=_test_postgres()),
            mock.patch.object(
                runner, "_run_normal_capacity", return_value=(failed, None, None, True)
            ),
            mock.patch.object(runner, "_db_snapshot", forbidden),
            mock.patch.object(runner, "_capacity_sample", forbidden),
            mock.patch.object(runner, "_incomplete_capacity_sample", forbidden),
            mock.patch.object(runner, "_pending_age_seconds", forbidden),
            mock.patch.object(runner, "_remove_owned_spool_dir", forbidden),
        )
        errors: list[BaseException] = []
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)

            def target() -> None:
                try:
                    runner.run(arguments)
                except BaseException as error:  # noqa: BLE001 - record the hang outcome
                    errors.append(error)

            holder = threading.Thread(target=target, daemon=True)
            holder.start()
            holder.join(timeout=5)
            # The hold never returns on its own: the thread stays alive,
            # no evidence work ran, and no context exits ran either.
            self.assertTrue(holder.is_alive(), "run() did not hold for the wrapper kill")
            self.assertFalse(errors, f"hold raised instead of waiting: {errors!r}")
            db_context.__exit__.assert_not_called()
            archive_context.__exit__.assert_not_called()

    def test_broken_stderr_still_holds_for_wrapper_kill(self) -> None:
        import contextlib

        db_context = mock.MagicMock()
        db_context.__enter__.return_value = "postgresql://stub-info"
        archive_context = mock.MagicMock()
        archive_context.__enter__.return_value = ("127.0.0.1:9", "", "", mock.Mock())
        domain_support = mock.Mock()
        domain_support.domain_database = mock.Mock(return_value=db_context)
        failed = runner._failed_capacity_workload("probe_timeout")
        arguments = runner.parse_arguments(
            ["normal-capacity", "--database-url", "postgresql://stub"]
        )
        broken = mock.Mock()
        broken.write.side_effect = OSError("broken pipe")
        forbidden = mock.Mock(side_effect=AssertionError("evidence work must not run"))
        patches = (
            mock.patch.dict(sys.modules, {"domain_test_support": domain_support}),
            mock.patch.object(runner, "archive_server", return_value=archive_context),
            mock.patch.object(
                runner, "_collector_probe", return_value={"executed": False, "reason": "test"}
            ),
            mock.patch.object(runner, "_start_metrics", return_value=("0/0", None, 0)),
            mock.patch.object(runner, "_relation_snapshot", return_value={}),
            mock.patch.object(
                runner,
                "_filesystem_usage",
                return_value={
                    "path": "/",
                    "usable_capacity_bytes": 10**9,
                    "raw_capacity_bytes": 10**9,
                    "used_bytes": 10**6,
                    "filesystem_type": "ext4",
                    "inode_model": "finite",
                },
            ),
            mock.patch.object(runner, "_postgres_provenance", return_value=_test_postgres()),
            mock.patch.object(
                runner, "_run_normal_capacity", return_value=(failed, None, None, True)
            ),
            mock.patch.object(runner.sys, "stderr", broken),
            mock.patch.object(runner, "_db_snapshot", forbidden),
            mock.patch.object(runner, "_remove_owned_spool_dir", forbidden),
        )
        errors: list[BaseException] = []
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)

            def target() -> None:
                try:
                    runner.run(arguments)
                except BaseException as error:  # noqa: BLE001 - record the hang outcome
                    errors.append(error)

            holder = threading.Thread(target=target, daemon=True)
            holder.start()
            holder.join(timeout=5)
            # A broken diagnostic stream must not prevent the guaranteed
            # hold: the thread stays alive and no teardown hooks run.
            self.assertTrue(holder.is_alive(), "run() did not hold for the wrapper kill")
            self.assertFalse(errors, f"hold raised instead of waiting: {errors!r}")
            db_context.__exit__.assert_not_called()
            archive_context.__exit__.assert_not_called()


if __name__ == "__main__":
    unittest.main()
