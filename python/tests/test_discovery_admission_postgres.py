"""The collector's admission pass asks for profile proof only for players with
a waiting discovery check (migration 0147), with 0084's outcomes."""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from time import perf_counter
from typing import Any

import psycopg
from domain_test_support import domain_database

MIGRATION_0084 = (
    Path(__file__).parents[2] / "deploy" / "migrations" / "0084_eligibility_due_state.sql"
)
WEEK = datetime(2026, 11, 23, 5, tzinfo=UTC)  # a Monday Reset
INSTANT = datetime(2026, 11, 25, 7, 40, tzinfo=UTC)  # the Wednesday of the timeouts


def _install_0084(connection: psycopg.Connection[Any]) -> None:
    """0084's admission pass, as clashlens_admit_discovery_profiles_0084."""
    source = MIGRATION_0084.read_text(encoding="utf-8")
    start = source.index(
        "CREATE OR REPLACE FUNCTION clashlens_admit_discovery_profiles(instant timestamptz)"
    )
    body = source[start : source.index("$$;", start) + 3]
    connection.execute(
        body.replace(
            "clashlens_admit_discovery_profiles(", "clashlens_admit_discovery_profiles_0084(", 1
        )
    )


def _observation(
    connection: psycopg.Connection[Any], player: tuple[int, str], key: str, at: datetime,
    status: int = 200, endpoint: str = "profile",
) -> int:
    return connection.execute(
        """
        INSERT INTO collector_observations (
            occurrence_key, player_id, normalized_tag, endpoint, request_started_at,
            response_completed_at, http_status, response_hash, collector_version, key_label,
            evidence_headers)
        VALUES (%s, %s, %s, %s, %s, %s, %s, encode(sha256(convert_to(%s, 'UTF8')), 'hex'),
                'test', 'regular', '{}')
        RETURNING id
        """,
        (key, player[0], player[1], endpoint, at, at, status, key),
    ).fetchone()[0]


def _profile(
    connection: psycopg.Connection[Any], player: tuple[int, str], observation: int,
    at: datetime, state: str,
) -> None:
    connection.execute(
        """
        WITH version AS (
            INSERT INTO player_profile_versions (
                player_id, observation_id, normalized_tag, endpoint_version, schema_version,
                parser_version, observed_at, source_http_status, name, trophies,
                league_tier_id, league_tier_name, eligibility_state, profile_json)
            VALUES (%(player)s, %(observation)s, %(tag)s, 'profile-v1', 'profile-schema-v1',
                    'v4', %(at)s, 200, 'case', 5000, 105000036, 'Legend I', %(state)s, '{}')
            RETURNING id)
        INSERT INTO player_profile_effects (
            profile_version_id, observation_id, effect_kind, observed_at, source_http_status,
            endpoint_version, schema_version, parser_version)
        SELECT id, %(observation)s, 'current_profile', %(at)s, 200, 'profile-v1',
               'profile-schema-v1', 'v4'
        FROM version
        """,
        {"player": player[0], "observation": observation, "tag": player[1], "at": at,
         "state": state},
    )


def _response_state(
    connection: psycopg.Connection[Any], player: tuple[int, str], observation: int | None,
    seen_at: datetime, success_at: datetime | None, not_found_at: datetime | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO collector_response_state (
            scope, identity_key, endpoint, player_id, normalized_tag, last_response_hash,
            last_content_fingerprint, last_occurrence_key, last_applied_occurrence_key,
            last_seen_at, last_observation_id, last_success_at, last_not_found_at)
        VALUES ('player', %(tag)s, 'profile', %(player)s, %(tag)s, repeat('a', 64),
                repeat('a', 64), 'state', 'state', %(seen)s, %(observation)s, %(success)s,
                %(not_found)s)
        """,
        {"tag": player[1], "player": player[0], "observation": observation,
         "seen": seen_at, "success": success_at, "not_found": not_found_at},
    )


# How a player's profile answers this week, each against 0084's proof rules.
PROOFS = (
    "none", "before_week", "eligible", "ineligible", "uncertain", "unchanged",
    "unchanged_seen_later", "before_raised_boundary", "processing", "processed_unrecognized",
    "after_instant", "not_found",
)
HISTORIES = ("not_applicable", "answered", "gone", "provider_error", "waiting")
ATTACHED = ("none", "answered", "provider_error")


def _case(connection: psycopg.Connection[Any], index: int, case: tuple[Any, ...]) -> None:
    active, recheck, waiting, history, proof, attached = case
    # Fewer than 500 ordinary checks wait, so the pass also adds new ones.
    status = ("pending", "waiting_retry")[index % 2] if waiting else "complete"
    raised = proof == "before_raised_boundary"
    player = connection.execute(
        """
        INSERT INTO players (normalized_tag, active, eligibility_state, eligibility_due_at,
                             eligibility_answer_after, eligibility_attempts)
        VALUES (%s, %s, 'unknown', %s, %s, %s) RETURNING id, normalized_tag
        """,
        (f"#E{index}", active,
         INSTANT - timedelta(minutes=1) if raised or index % 3 == 0 else None,
         WEEK + timedelta(days=1, hours=12) if raised else None,
         1 if raised or index % 3 == 0 else 0),
    ).fetchone()
    key = f"case-{index}"
    in_week = WEEK + timedelta(days=1)
    if proof in ("eligible", "ineligible", "uncertain", "before_raised_boundary"):
        observation = _observation(connection, player, key, in_week)
        _profile(connection, player, observation, in_week,
                 "eligible" if proof == "before_raised_boundary" else proof)
    elif proof in ("before_week", "unchanged", "unchanged_seen_later"):
        observation = _observation(connection, player, key, WEEK - timedelta(days=1))
        _profile(connection, player, observation, WEEK - timedelta(days=1), "eligible")
        if proof != "before_week":
            _response_state(
                connection, player, observation,
                in_week + (timedelta(hours=1) if proof == "unchanged_seen_later" else timedelta()),
                in_week)
    elif proof in ("processing", "processed_unrecognized"):
        observation = _observation(connection, player, key, in_week)
        _response_state(connection, player, observation, in_week, in_week)
        connection.execute(
            "INSERT INTO python_processing_jobs (observation_id, status, deduplication_key)"
            " VALUES (%s, %s, %s)",
            (observation, "pending" if proof == "processing" else "complete", key),
        )
    elif proof == "after_instant":
        observation = _observation(connection, player, key, INSTANT + timedelta(hours=1))
        _profile(connection, player, observation, INSTANT + timedelta(hours=1), "eligible")
    elif proof == "not_found":
        observation = _observation(connection, player, key, in_week, status=404)
        _response_state(connection, player, observation, in_week, None, in_week)
    history_observation = None
    if history in ("answered", "gone", "provider_error"):
        history_observation = _observation(
            connection, player, key + "-history", WEEK - timedelta(days=2),
            {"answered": 200, "gone": 404, "provider_error": 503}[history], "league_history")
    profile_observation = None
    if attached != "none":
        profile_observation = _observation(
            connection, player, key + "-attached", WEEK - timedelta(days=2),
            200 if attached == "answered" else 503)
    connection.execute(
        """
        INSERT INTO collector_work (
            kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key, status,
            profile_status, battle_log_status, league_history_status,
            league_history_observation_id, profile_observation_id, eligibility_recheck,
            completed_at)
        VALUES ('discovery_profile', 'ordinary', 'player', %s, %s, %s, %s, %s, 'pending',
                'not_applicable', %s, %s, %s, %s, %s)
        """,
        (player[0], player[1], INSTANT - timedelta(minutes=30), key, status,
         {"not_applicable": "not_applicable", "answered": "observed", "gone": "observed",
          "provider_error": "pending", "waiting": "pending"}[history],
         history_observation, profile_observation, recheck,
         INSTANT - timedelta(minutes=5) if status == "complete" else None),
    )


def _outcome(connection: psycopg.Connection[Any]) -> tuple[list[tuple], list[tuple]]:
    work = connection.execute(
        """
        SELECT player_id, kind, coalescing_key, status, profile_status, profile_observation_id,
               league_history_status, league_history_observation_id, eligibility_recheck,
               due_at, completed_at
        FROM collector_work ORDER BY player_id, coalescing_key
        """
    ).fetchall()
    players = connection.execute(
        "SELECT id, eligibility_due_at, eligibility_answer_after, eligibility_attempts"
        " FROM players ORDER BY id"
    ).fetchall()
    return work, players


def _pass(connection_info: str, function: str) -> tuple[list[tuple], list[tuple]]:
    with psycopg.connect(connection_info) as connection:
        connection.execute(f"SELECT {function}(%s)", (INSTANT,))
        outcome = _outcome(connection)
        connection.rollback()
    return outcome


def test_waiting_checks_end_as_they_did_under_0084(database_url: str) -> None:
    cases = list(product((False, True), (False, True), (True, False), HISTORIES, PROOFS,
                         ATTACHED))
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            _install_0084(connection)
            for index, case in enumerate(cases):
                _case(connection, index, case)
            before = _outcome(connection)
        old = _pass(connection_info, "clashlens_admit_discovery_profiles_0084")
        new = _pass(connection_info, "clashlens_admit_discovery_profiles")

    assert new == old
    # The cases reach every outcome of the pass, so agreeing means something.
    started = {row[2]: (row[3], row[4]) for row in before[0]}
    assert {
        (row[3], row[4]) for row in new[0]
        if row[2] in started and started[row[2]] != (row[3], row[4])
    } == {
        ("cancelled", "pending"),  # proof found, check cancelled
        ("cancelled", "observed"),  # this week's profile attached, check ended
        ("pending", "observed"),  # attached, still waiting for league history
        ("waiting_retry", "observed"),
    }
    assert len(new[0]) > len(before[0])  # due players without a check got one
    assert new[1] != before[1]  # and their next try moved


def _seed_reset(connection: psycopg.Connection[Any]) -> int:
    """40,000 players at the Reset of 25 November 2026, 18,000 of them tracked
    and still being swept, 10,000 former members with 20 saved profiles each,
    12,000 found once, and 20 discovery checks waiting. As on production,
    where players rows are rewritten all day (49,523 old row versions beside
    36,516 live on 25 November), the table holds an old version of each row
    and its rows sit in no order; a fresh, ordered table makes looking up
    players one by one seem cheaper than production finds it."""
    connection.execute("SET session_replication_role = replica")  # bulk seed only
    connection.execute(
        """
        INSERT INTO players (id, normalized_tag, active, eligibility_state)
        OVERRIDING SYSTEM VALUE
        SELECT n, '#L' || n, n <= 18000, 'eligible'
        FROM generate_series(1, 40000) AS n ORDER BY md5(n::text);
        SELECT setval(pg_get_serial_sequence('players', 'id'), 40000);
        UPDATE players SET updated_at = clock_timestamp();

        INSERT INTO collector_observations (
            occurrence_key, player_id, normalized_tag, endpoint, request_started_at,
            response_completed_at, http_status, response_hash, collector_version, key_label,
            evidence_headers, request_method, request_path, request_query,
            paging_envelope_state, source_adapter_version)
        SELECT 'load:' || player.id || ':' || k, player.id, player.normalized_tag, 'profile',
               at, at, 200, md5(player.id || ':' || k) || md5(k || ':' || player.id), 'load',
               'regular', '{}', 'GET', '/v1/players/%23' || substr(player.normalized_tag, 2),
               '', 'not_applicable', 'player-profile-v1'
        FROM players AS player
        CROSS JOIN LATERAL generate_series(
            1, CASE WHEN player.id <= 18000 THEN 2 WHEN player.id <= 28000 THEN 20 ELSE 1 END
        ) AS k
        CROSS JOIN LATERAL (SELECT timestamptz '2026-10-05 05:00Z' + k * interval '1 day'
                                 + (player.id % 3600) * interval '1 second' AS at) AS reading;

        INSERT INTO player_profile_versions (
            player_id, observation_id, normalized_tag, endpoint_version, schema_version,
            parser_version, observed_at, source_http_status, name, trophies, league_tier_id,
            league_tier_name, eligibility_state, profile_json)
        SELECT player_id, id, normalized_tag, 'profile-v1', 'profile-schema-v1', 'v4',
               response_completed_at, 200, 'load', 5000, 105000036, 'Legend I',
               CASE WHEN id % 5 = 0 THEN 'uncertain' ELSE 'eligible' END, '{}'
        FROM collector_observations;
        INSERT INTO player_profile_effects (
            profile_version_id, observation_id, effect_kind, observed_at, source_http_status,
            endpoint_version, schema_version, parser_version)
        SELECT id, observation_id, 'current_profile', observed_at, 200, endpoint_version,
               schema_version, parser_version
        FROM player_profile_versions;
        INSERT INTO collector_response_state (
            scope, identity_key, endpoint, player_id, normalized_tag, last_response_hash,
            last_content_fingerprint, last_occurrence_key, last_seen_at, last_observation_id,
            last_success_at, last_applied_occurrence_key)
        SELECT DISTINCT ON (player_id) 'player', normalized_tag, 'profile', player_id,
               normalized_tag, response_hash, response_hash, occurrence_key,
               response_completed_at, id, response_completed_at, occurrence_key
        FROM collector_observations ORDER BY player_id, response_completed_at DESC;

        INSERT INTO collector_reset_sweeps (boundary_at)
        VALUES ('2026-11-23 05:00Z'), ('2026-11-24 05:00Z'), ('2026-11-25 05:00Z');
        INSERT INTO collector_work (
            kind, lane, scope, player_id, normalized_tag, sweep_id, due_at, coalescing_key,
            status, profile_status, battle_log_status, league_history_status, completed_at)
        SELECT 'reset_baseline', 'reset', 'player', player.id, player.normalized_tag,
               sweep.id, sweep.boundary_at, 'reset:' || sweep.id || ':' || player.id,
               CASE WHEN sweep.boundary_at < '2026-11-25 05:00Z' THEN 'complete'
                    ELSE 'pending' END,
               'pending', 'pending', 'not_applicable',
               CASE WHEN sweep.boundary_at < '2026-11-25 05:00Z' THEN sweep.boundary_at END
        FROM players AS player CROSS JOIN collector_reset_sweeps AS sweep
        WHERE player.active;
        INSERT INTO collector_work (
            kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key, status,
            profile_status, battle_log_status, league_history_status, completed_at)
        SELECT 'discovery_profile', 'ordinary', 'player', player.id, player.normalized_tag,
               week, 'discovery-profile:' || player.id || ':' || week, 'complete',
               'observed', 'not_applicable', 'not_applicable', week
        FROM players AS player
        CROSS JOIN unnest(ARRAY[timestamptz '2026-11-02 05:00Z', '2026-11-09 05:00Z',
                                '2026-11-16 05:00Z']) AS week
        WHERE NOT player.active;
        -- Five new players' league history is in, five still wait for it.
        INSERT INTO collector_observations (
            occurrence_key, player_id, normalized_tag, endpoint, request_started_at,
            response_completed_at, http_status, response_hash, collector_version, key_label,
            evidence_headers, request_method, request_path, request_query,
            paging_envelope_state, source_adapter_version)
        SELECT 'history:' || id, id, normalized_tag, 'league_history', '2026-11-25 07:31Z',
               '2026-11-25 07:31Z', 200, md5('history:' || id) || md5(id::text), 'load',
               'regular', '{}', 'GET',
               '/v1/players/%23' || substr(normalized_tag, 2) || '/leaguehistory', '',
               'not_applicable', 'league-history-v1'
        FROM players WHERE id > 39995;
        INSERT INTO collector_work (
            kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key, status,
            profile_status, battle_log_status, league_history_status,
            league_history_observation_id, eligibility_recheck)
        SELECT 'discovery_profile', 'ordinary', 'player', player.id, player.normalized_tag,
               CASE WHEN player.id > 39990 THEN timestamptz '2026-11-25 07:30Z'
                    ELSE '2026-11-25 04:52Z' END,
               'waiting:' || player.id, 'pending', 'pending', 'not_applicable',
               CASE WHEN history.id IS NOT NULL THEN 'observed'
                    WHEN player.id > 39990 THEN 'pending' ELSE 'not_applicable' END,
               history.id, player.id <= 39990
        FROM players AS player
        LEFT JOIN collector_observations AS history
          ON history.player_id = player.id AND history.endpoint = 'league_history'
        WHERE player.id BETWEEN 27991 AND 28000 OR player.id > 39990;
        """
    )
    connection.execute("RESET session_replication_role")
    connection.commit()
    connection.execute("ANALYZE")
    return 20


def _timed_pass(
    connection: psycopg.Connection[Any], function: str, role: str | None
) -> tuple[float, int]:
    """One rolled-back pass: seconds taken and profile proofs asked."""
    proofs = (
        "SELECT coalesce(pg_stat_get_xact_function_calls("
        "'clashlens_eligibility_checked_since'::regproc), 0)"
    )
    with connection.transaction(force_rollback=True):
        before = connection.execute(proofs).fetchone()[0]
        if role:
            connection.execute(f"SET LOCAL ROLE {role}")
        started = perf_counter()
        connection.execute(f"SELECT {function}(%s)", (INSTANT,))
        seconds = perf_counter() - started
        connection.execute("RESET ROLE")
        return seconds, connection.execute(proofs).fetchone()[0] - before


def test_a_reset_admission_pass_asks_for_proof_only_for_waiting_checks(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            _install_0084(connection)
            waiting = _seed_reset(connection)
            connection.execute("SET track_functions = 'all'")
            connection.execute("SET statement_timeout = '10s'")
            runs: dict[str, list[tuple[float, int]]] = {}
            for name, function, role in (
                ("0084", "clashlens_admit_discovery_profiles_0084", None),
                ("current", "clashlens_admit_discovery_profiles", "clashlens_collector"),
            ):
                runs[name] = [_timed_pass(connection, function, role) for _ in range(5)]

    print(json.dumps({
        name: {"median_ms": round(statistics.median(s for s, _ in rows) * 1000, 1),
               "profile_proofs": rows[0][1]}
        for name, rows in runs.items()
    }, sort_keys=True))
    # At most once for each waiting check in each of the two housekeeping updates.
    assert all(proofs <= 2 * waiting for _seconds, proofs in runs["current"])
    assert statistics.median(seconds for seconds, _ in runs["current"]) < 1
