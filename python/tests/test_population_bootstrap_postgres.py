"""Issue #92 B2: bounded population-bootstrap command tests.

Uses real migrated PostgreSQL (disposable schema) and fixture manifests only.
No official calls, no protected tags.
"""

from __future__ import annotations

import hashlib
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from domain_test_support import domain_database

from clashlens.bootstrap import (
    BootstrapError,
    bootstrap_population,
    parse_manifest,
)
from clashlens.cli import main

_ALPHABET = "0289PYLQGRJCUV"
RUN_ID = "b2-test-run-1"


def _tag(number: int) -> str:
    suffix = ""
    remainder = number
    for _ in range(4):
        suffix = _ALPHABET[remainder % len(_ALPHABET)] + suffix
        remainder //= len(_ALPHABET)
    return "#Q" + suffix


def _manifest_bytes(tags: list[str]) -> bytes:
    return ("\n".join(tags) + "\n").encode("utf-8")


def _setup(
    directory, connection_info: str, tags: list[str], run_id: str = RUN_ID
) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    raw = _manifest_bytes(tags)
    (directory / "cohort.txt").write_bytes(raw)
    (directory / "dburl").write_bytes(connection_info.encode("utf-8"))
    return {
        "database_url": connection_info,
        "database_url_file": str(directory / "dburl"),
        "cohort_file": str(directory / "cohort.txt"),
        "expected_sha256": hashlib.sha256(raw).hexdigest(),
        "expected_count": len(tags),
        "run_id": run_id,
        "result_file": str(directory / "result.json"),
    }


def _cli_arguments(settings: dict, **overrides) -> list[str]:
    settings = {**settings, **overrides}
    return [
        "bootstrap-population",
        "--database-url-file",
        settings["database_url_file"],
        "--cohort-file",
        settings["cohort_file"],
        "--expected-sha256",
        settings["expected_sha256"],
        "--expected-count",
        str(settings["expected_count"]),
        "--run-id",
        settings["run_id"],
        "--result-file",
        settings["result_file"],
    ]


def _direct(settings: dict, **overrides) -> dict:
    settings = {**settings, **overrides}
    return bootstrap_population(
        database_url=settings["database_url"],
        cohort_file=settings["cohort_file"],
        expected_sha256=settings["expected_sha256"],
        expected_count=settings["expected_count"],
        run_id=settings["run_id"],
        result_file=settings["result_file"],
    )


def _counts(connection_info: str) -> tuple[int, int, int, int]:
    with psycopg.connect(connection_info) as connection:
        players = connection.execute("SELECT count(*) FROM players").fetchone()[0]
        jobs = connection.execute(
            "SELECT count(*) FROM collector_jobs"
        ).fetchone()[0]
        runs = connection.execute(
            "SELECT count(*) FROM population_bootstrap_runs"
        ).fetchone()[0]
        intents = connection.execute(
            "SELECT count(*) FROM global_rankings_intents"
        ).fetchone()[0]
    return players, jobs, runs, intents


def test_parse_manifest_accepts_trailing_newline_and_rejects_variants() -> None:
    tags = [_tag(1), _tag(2)]
    raw = _manifest_bytes(tags)
    digest = hashlib.sha256(raw).hexdigest()
    manifest = parse_manifest(raw, expected_sha256=digest, expected_count=2)
    assert list(manifest.tags) == tags
    assert manifest.raw_sha256 == digest

    bad_cases = [
        b"#2PP\n\n#8QV\n",  # blank line
        b"#2PP\n   \n#8QV\n",  # whitespace-only line
        b"#2PP\n// comment\n",  # comment line
        b"#2PP\n#2pp\n",  # duplicate after normalization
        b"#2PP\n#NOPE!\n",  # malformed tag
    ]
    for bad in bad_cases:
        with pytest.raises(BootstrapError):
            parse_manifest(
                bad,
                expected_sha256=hashlib.sha256(bad).hexdigest(),
                expected_count=2,
            )
    with pytest.raises(BootstrapError):
        parse_manifest(raw, expected_sha256="0" * 64, expected_count=2)
    with pytest.raises(BootstrapError):
        parse_manifest(raw, expected_sha256=digest, expected_count=3)


def test_malformed_manifest_fails_before_any_write(
    database_url: str, tmp_path, capsys
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        cases = {
            "malformed": ["#2PP", "#NOPE!", "#8QV"],
            "blank": ["#2PP", "", "#8QV"],
            "comment": ["#2PP", "// cohort", "#8QV"],
            "duplicate": ["#2PP", "#2pp", "#8QV"],
        }
        for name, tags in cases.items():
            raw = _manifest_bytes(tags)
            settings = _setup(tmp_path / name, connection_info, ["#2PP"])
            (tmp_path / name / "cohort.txt").write_bytes(raw)
            arguments = _cli_arguments(
                settings,
                expected_sha256=hashlib.sha256(raw).hexdigest(),
                expected_count=len(tags),
            )
            assert main(arguments) == 1, name
            assert _counts(connection_info) == (0, 0, 0, 0), name
            assert not (tmp_path / name / "result.json").exists(), name
        settings = _setup(tmp_path / "mismatch", connection_info, ["#2PP", "#8QV"])
        assert main(_cli_arguments(settings, expected_sha256="f" * 64)) == 1
        assert main(_cli_arguments(settings, expected_count=3)) == 1
        assert _counts(connection_info) == (0, 0, 0, 0)
        captured = capsys.readouterr()
        for tag in ("#2PP", "#NOPE!", "#8QV"):
            assert tag not in captured.err


def test_bootstrap_registers_inactive_and_enqueues_profile_only(
    database_url: str, tmp_path, capsys
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        tags = [_tag(21), _tag(22), _tag(23)]
        settings = _setup(tmp_path, connection_info, tags)
        assert main(_cli_arguments(settings)) == 0
        captured = capsys.readouterr()

        with psycopg.connect(connection_info) as connection:
            players = connection.execute(
                "SELECT normalized_tag, active, eligibility_state FROM players ORDER BY 1"
            ).fetchall()
            assert players == [(tag, False, "unknown") for tag in sorted(tags)]
            jobs = connection.execute(
                """SELECT DISTINCT work_type, scope, capacity_pool,
                          required_endpoint
                   FROM collector_jobs"""
            ).fetchall()
            assert jobs == [("discovery_profile", "player", "normal", "profile")]
            assert (
                connection.execute(
                    "SELECT count(*) FROM collector_jobs"
                ).fetchone()[0]
                == 3
            )
            run = connection.execute(
                """SELECT run_id, status, players_registered,
                          discovery_jobs_created
                   FROM population_bootstrap_runs"""
            ).fetchone()
            assert run == (RUN_ID, "complete", 3, 3)

        payload = json.loads((tmp_path / "result.json").read_bytes())
        assert payload == {
            "status": "complete",
            "run_id": RUN_ID,
            "manifest_sha256": settings["expected_sha256"],
            "manifest_count": 3,
            "normalized_set_sha256": hashlib.sha256(
                "\n".join(sorted(tags)).encode()
            ).hexdigest(),
            "batch_size": 500,
            "batch_count": 1,
            "players_registered": 3,
            "discovery_jobs_created": 3,
        }
        assert stat.S_IMODE((tmp_path / "result.json").stat().st_mode) == 0o600
        body = (tmp_path / "result.json").read_bytes().decode()
        for tag in tags:
            assert tag not in body
            assert tag not in captured.out


def test_replay_same_run_id_is_idempotent(
    database_url: str, tmp_path
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        settings = _setup(tmp_path, connection_info, [_tag(31), _tag(32)])
        assert main(_cli_arguments(settings)) == 0
        before = _counts(connection_info)
        second = str(tmp_path / "result-second.json")
        assert main(_cli_arguments(settings, result_file=second)) == 0
        assert _counts(connection_info) == before
        assert json.loads((tmp_path / "result-second.json").read_bytes()) == json.loads(
            (tmp_path / "result.json").read_bytes()
        )


def test_concurrent_distinct_run_ids_serialize_fail_closed(
    database_url: str, tmp_path
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        tags = [_tag(91), _tag(92)]
        first = _setup(tmp_path / "race-a", connection_info, tags, run_id="race-a")
        second = _setup(tmp_path / "race-b", connection_info, tags, run_id="race-b")
        barrier = threading.Barrier(2)
        outcomes: dict[str, str] = {}

        def attempt(settings: dict) -> None:
            barrier.wait(timeout=30)
            try:
                _direct(settings)
            except BootstrapError:
                outcomes[settings["run_id"]] = "rejected"
            else:
                outcomes[settings["run_id"]] = "admitted"

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(attempt, (first, second)))
        assert sorted(outcomes.values()) == ["admitted", "rejected"]
        with psycopg.connect(connection_info) as connection:
            runs = connection.execute(
                "SELECT run_id, status FROM population_bootstrap_runs"
            ).fetchall()
            assert len(runs) == 1
            assert runs[0][1] == "complete"
            assert (
                connection.execute("SELECT count(*) FROM players").fetchone()[0]
                == 2
            )


def test_partial_replay_reports_full_converged_aggregate(
    database_url: str, tmp_path
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        tags = [_tag(61), _tag(62), _tag(63)]
        settings = _setup(tmp_path, connection_info, tags)
        set_digest = hashlib.sha256("\n".join(sorted(tags)).encode()).hexdigest()
        with psycopg.connect(connection_info, autocommit=True) as connection:
            # Simulate a crash after an earlier attempt committed one
            # player's discovery job but died before the run-row update.
            connection.execute(
                """INSERT INTO population_bootstrap_runs (
                       run_id, manifest_sha256, manifest_count,
                       normalized_set_sha256, status, batch_size
                   ) VALUES (%s, %s, %s, %s, 'started', 500)""",
                (RUN_ID, settings["expected_sha256"], len(tags), set_digest),
            )
            connection.execute(
                """INSERT INTO players (normalized_tag, active, eligibility_state)
                   SELECT tag, false, 'unknown'
                   FROM unnest(%s::text[]) AS tag""",
                (tags,),
            )
            first_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s", (tags[0],)
            ).fetchone()[0]
            created = connection.execute(
                "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                ([first_id],),
            ).fetchone()[0]
            assert created == 1
        report = _direct(settings)
        assert report["players_registered"] == 3
        assert report["discovery_jobs_created"] == 3
        with psycopg.connect(connection_info) as connection:
            run = connection.execute(
                """SELECT status, players_registered, discovery_jobs_created
                   FROM population_bootstrap_runs WHERE run_id = %s""",
                (RUN_ID,),
            ).fetchone()
            assert run == ("complete", 3, 3)


def test_partial_batch_resume_converges(
    database_url: str, tmp_path
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        tags = [_tag(41), _tag(42), _tag(43)]
        settings = _setup(tmp_path, connection_info, tags)
        set_digest = hashlib.sha256("\n".join(sorted(tags)).encode()).hexdigest()
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(
                """INSERT INTO population_bootstrap_runs (
                       run_id, manifest_sha256, manifest_count,
                       normalized_set_sha256, status, batch_size
                   ) VALUES (%s, %s, %s, %s, 'started', 500)""",
                (RUN_ID, settings["expected_sha256"], len(tags), set_digest),
            )
            connection.execute(
                """INSERT INTO players (normalized_tag, active, eligibility_state)
                   VALUES (%s, false, 'unknown')""",
                (tags[0],),
            )
        assert main(_cli_arguments(settings)) == 0
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute("SELECT count(*) FROM players").fetchone()[0]
                == 3
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM collector_jobs"
                ).fetchone()[0]
                == 3
            )
            run = connection.execute(
                """SELECT status, players_registered, discovery_jobs_created
                   FROM population_bootstrap_runs WHERE run_id = %s""",
                (RUN_ID,),
            ).fetchone()
            assert run == ("complete", 3, 3)


def test_bootstrap_run_collision_fails_closed(
    database_url: str, tmp_path
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        tags = [_tag(51), _tag(52)]
        settings = _setup(tmp_path, connection_info, tags)
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(
                """INSERT INTO population_bootstrap_runs (
                       run_id, manifest_sha256, manifest_count,
                       normalized_set_sha256, status, batch_size
                   ) VALUES ('other-run', %s, 2, %s, 'started', 500)""",
                ("0" * 64, "1" * 64),
            )
        # An earlier different run blocks a fresh run-id.
        with pytest.raises(BootstrapError):
            _direct(settings)
        assert _counts(connection_info) == (0, 0, 1, 0)
        # The same run-id with a different manifest is also a collision.
        other = _setup(tmp_path / "other", connection_info, tags, run_id="other-run")
        with pytest.raises(BootstrapError):
            _direct(other)


def test_fresh_precheck_rejects_nonempty_database(
    database_url: str, tmp_path
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        settings = _setup(tmp_path, connection_info, [_tag(61), _tag(62)])
        settings = {**settings, "result_file": str(tmp_path / "r.json")}

        def attempt(name: str) -> None:
            with pytest.raises(BootstrapError):
                _direct(settings, result_file=str(tmp_path / name))

        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(
                """INSERT INTO players (normalized_tag, active, eligibility_state)
                   VALUES ('#2PP', true, 'eligible')"""
            )
        attempt("result-active.json")
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute("DELETE FROM players")
            player_id = connection.execute(
                """INSERT INTO players (normalized_tag, active, eligibility_state)
                   VALUES ('#8QV', false, 'unknown') RETURNING id"""
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO collector_jobs (
                       work_type, scope, player_id, normalized_tag,
                       capacity_pool, priority, due_at, coalescing_key,
                       required_endpoint, status
                   ) VALUES ('discovery_profile', 'player', %s, '#8QV',
                       'normal', 300, clock_timestamp(), 'precheck-job',
                       'profile', 'complete')""",
                (player_id,),
            )
        attempt("result-job.json")
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute("DELETE FROM collector_jobs")
            connection.execute("DELETE FROM players")
            connection.execute(
                "INSERT INTO global_rankings_intents (cycle_at)"
                " VALUES ('2026-09-08T05:00:00+00')"
            )
        attempt("result-ranking.json")
        assert _counts(connection_info) == (0, 0, 0, 1)


def test_secure_file_handling(database_url: str, tmp_path) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        settings = _setup(tmp_path, connection_info, [_tag(71), _tag(72)])

        link = tmp_path / "cohort-link.txt"
        link.symlink_to(tmp_path / "cohort.txt")
        with pytest.raises(BootstrapError):
            _direct(settings, cohort_file=str(link))
        with pytest.raises(BootstrapError):
            _direct(settings, cohort_file="relative/cohort.txt")
        with pytest.raises(BootstrapError):
            _direct(settings, result_file="relative/result.json")
        occupied = tmp_path / "occupied.json"
        occupied.write_text("{}")
        with pytest.raises(BootstrapError):
            _direct(settings, result_file=str(occupied))
        assert (
            main(_cli_arguments(settings, result_file=str(occupied))) == 1
        )

        oversized = tmp_path / "oversized.txt"
        oversized.write_bytes(b"#2PP\n" * 300000)
        with pytest.raises(BootstrapError):
            _direct(
                settings,
                cohort_file=str(oversized),
                expected_sha256="0" * 64,
                result_file=str(tmp_path / "result-oversized.json"),
            )

        many = [_tag(number) for number in range(20001)]
        many_raw = _manifest_bytes(many)
        many_path = tmp_path / "many.txt"
        many_path.write_bytes(many_raw)
        with pytest.raises(BootstrapError):
            _direct(
                settings,
                cohort_file=str(many_path),
                expected_sha256=hashlib.sha256(many_raw).hexdigest(),
                expected_count=20000,
                result_file=str(tmp_path / "result-many.json"),
            )
        assert _counts(connection_info) == (0, 0, 0, 0)


def test_worker_role_can_execute_bootstrap_statements(
    database_url: str, tmp_path
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        tags = [_tag(81), _tag(82)]
        raw = _manifest_bytes(tags)
        digest = hashlib.sha256(raw).hexdigest()
        set_digest = hashlib.sha256("\n".join(sorted(tags)).encode()).hexdigest()
        with psycopg.connect(connection_info) as connection:
            connection.execute("BEGIN")
            connection.execute("SET LOCAL ROLE clashlens_python_worker")
            connection.execute(
                """INSERT INTO players (normalized_tag, active, eligibility_state)
                   SELECT tag, false, 'unknown'
                   FROM unnest(%s::text[]) AS tag
                   ON CONFLICT (normalized_tag) DO NOTHING""",
                (tags,),
            )
            player_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = ANY(%s::text[])",
                    (tags,),
                ).fetchall()
            ]
            assert len(player_ids) == 2
            created = connection.execute(
                "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                (player_ids,),
            ).fetchone()[0]
            assert created == 2
            assert (
                connection.execute(
                    "SELECT count(*) FROM global_rankings_intents"
                ).fetchone()[0]
                == 0
            )
            connection.execute(
                """INSERT INTO population_bootstrap_runs (
                       run_id, manifest_sha256, manifest_count,
                       normalized_set_sha256, status, batch_size
                   ) VALUES (%s, %s, 2, %s, 'started', 500)""",
                (RUN_ID, digest, set_digest),
            )
            connection.execute(
                """UPDATE population_bootstrap_runs
                   SET status = 'complete', players_registered = 2,
                       discovery_jobs_created = 2,
                       completed_at = clock_timestamp()
                   WHERE run_id = %s""",
                (RUN_ID,),
            )
            # The worker role reads budget aggregates for monitoring but
            # can neither mint nor consume budget units.
            aggregates = connection.execute(
                """SELECT count(*), COALESCE(sum(consumed), 0)
                   FROM collector_endpoint_budgets WHERE run_id = %s""",
                (RUN_ID,),
            ).fetchone()
            assert aggregates == (0, 0)
            for statement in (
                """INSERT INTO collector_endpoint_budgets
                   (run_id, endpoint, cap, consumed, deadline_at)
                   VALUES ('probe', 'profile', 1, 0, clock_timestamp())""",
                """UPDATE collector_endpoint_budgets SET consumed = 1
                   WHERE run_id = 'probe'""",
            ):
                connection.execute("SAVEPOINT budget_boundary")
                try:
                    connection.execute(statement)
                except psycopg.errors.InsufficientPrivilege:
                    connection.execute("ROLLBACK TO SAVEPOINT budget_boundary")
                else:
                    raise AssertionError("worker wrote the budget ledger")
                connection.execute("RELEASE SAVEPOINT budget_boundary")
            connection.execute("ROLLBACK")
        assert _counts(connection_info) == (0, 0, 0, 0)
