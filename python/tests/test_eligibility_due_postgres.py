"""Each player keeps one durable eligibility due state (migration 0082)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_collector_db_postgres import _handoff, _hash
from test_domain_processing_postgres import _processor

from clashlens import population, promotion_candidates
from clashlens.collector_db import CollectorDatabase
from clashlens.db import Database, enqueue_discovered_players

PROFILE = json.loads(
    (Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json").read_bytes()
)
SEASON_ID = str(PROFILE["currentLeagueSeasonId"])
LEGEND_II = {"id": 105000035, "name": "Legend II"}


def _as(connection_info: str, role: str) -> str:
    options = conninfo_to_dict(connection_info).get("options", "")
    return make_conninfo(connection_info, options=f"{options} -c role={role}")


def _players(connection_info: str, count: int, prefix: str = "#Q") -> list[int]:
    with psycopg.connect(connection_info) as connection:
        return [
            row[0]
            for row in connection.execute(
                "INSERT INTO players (normalized_tag, active, eligibility_state)"
                " SELECT %s || n, false, 'unknown' FROM generate_series(1, %s) AS n"
                " ORDER BY n RETURNING id",
                (prefix, count),
            )
        ]


def _admit(connection_info: str, at: datetime) -> None:
    """One collector pass: admission, which also turns due players into checks."""
    with psycopg.connect(_as(connection_info, "clashlens_collector")) as connection:
        connection.execute("SELECT clashlens_admit_discovery_profiles(%s)", (at,))


def _checks(connection_info: str, player_id: int) -> list[tuple[str, str]]:
    with psycopg.connect(connection_info) as connection:
        return [
            # A week's key ends in its Reset time; a retry adds ":<attempt>".
            (key.rsplit(":", 1)[-1] if key.count(":") > 4 else "week", status)
            for key, status in connection.execute(
                "SELECT coalescing_key, status FROM collector_work"
                " WHERE player_id = %s AND kind = 'discovery_profile' ORDER BY id",
                (player_id,),
            )
        ]


def _due(connection_info: str, player_id: int) -> tuple[datetime | None, int]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT eligibility_due_at, eligibility_attempts FROM players WHERE id = %s",
            (player_id,),
        ).fetchone()


def _waiting(connection_info: str) -> int:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT count(*) FROM collector_work WHERE kind = 'discovery_profile'"
            " AND status IN ('pending', 'waiting_retry')"
        ).fetchone()[0]


def _report(connection_info: str) -> dict:
    with psycopg.connect(_as(connection_info, "clashlens_collector")) as connection:
        return connection.execute(
            "SELECT clashlens_population_report(now(), %s)", (SEASON_ID,)
        ).fetchone()[0]


def _discover(connection_info: str, player_ids: list[int]) -> None:
    """A battle log or ranking job naming these players, as the worker."""
    database = Database(_as(connection_info, "clashlens_python_worker"))
    try:
        with database.pool.connection() as connection, connection.transaction():
            enqueue_discovered_players(
                connection, database, SimpleNamespace(work_type="process_observation"), player_ids
            )
    finally:
        database.close()


def test_a_full_queue_keeps_named_players_due_until_there_is_room(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        ids = _players(connection_info, 503)
        _discover(connection_info, ids[:250])
        _discover(connection_info, ids[250:])
        # Naming a player saves it as due; the collector makes the checks.
        assert _waiting(connection_info) == 0
        now = datetime.now(UTC)
        for _ in range(4):  # at most 200 due players a pass
            _admit(connection_info, now)
        assert _waiting(connection_info) == 500
        left = [player for player in ids if not _checks(connection_info, player)]
        assert len(left) == 3
        assert all(_due(connection_info, player)[1] == 0 for player in left)
        _admit(connection_info, now)
        assert _waiting(connection_info) == 500

        # Three checks finish; the next pass takes the three left, oldest due first.
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_work SET status = 'cancelled'"
                " WHERE player_id = ANY(%s::bigint[])",
                (ids[:3],),
            )
        _admit(connection_info, now + timedelta(seconds=1))
        assert all(_checks(connection_info, player) == [("week", "pending")] for player in left)
        assert _waiting(connection_info) == 500


def test_a_failed_check_is_retried_with_backoff_until_answered(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        (player,) = _players(connection_info, 1)
        _discover(connection_info, [player])
        now = datetime.now(UTC)
        _admit(connection_info, now)
        assert _checks(connection_info, player) == [("week", "pending")]
        assert _due(connection_info, player) == (now + timedelta(minutes=5), 1)

        def fail() -> None:
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE collector_work SET status = 'failed', failure_category = 'provider_failure'"
                    " WHERE player_id = %s AND status = 'pending'",
                    (player,),
                )

        fail()
        # Nothing names the player again; the saved due time brings the retry.
        _admit(connection_info, now + timedelta(minutes=4))
        assert _checks(connection_info, player) == [("week", "failed")]
        _discover(connection_info, [player])
        assert _due(connection_info, player) == (now + timedelta(minutes=5), 1)
        retry_at = now + timedelta(minutes=6)
        _admit(connection_info, retry_at)
        assert _checks(connection_info, player) == [("week", "failed"), ("2", "pending")]
        assert _due(connection_info, player) == (retry_at + timedelta(minutes=10), 2)

        # A waiting check moves the next try instead of adding another.
        _admit(connection_info, retry_at + timedelta(minutes=11))
        assert _due(connection_info, player) == (retry_at + timedelta(minutes=21), 2)
        assert len(_checks(connection_info, player)) == 2

        # A not-found answer is final for the week.
        fail()
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO collector_response_state (
                    scope, identity_key, endpoint, player_id, normalized_tag,
                    last_response_hash, last_content_fingerprint, last_occurrence_key,
                    last_applied_occurrence_key, last_seen_at, last_not_found_at
                ) SELECT 'player', normalized_tag, 'profile', id, normalized_tag,
                         repeat('a', 64), repeat('a', 64), 'gone', 'gone', %s, %s
                  FROM players WHERE id = %s
                """,
                (retry_at, retry_at, player),
            )
        _admit(connection_info, retry_at + timedelta(minutes=30))
        assert _due(connection_info, player) == (None, 0)
        assert len(_checks(connection_info, player)) == 2


def test_a_player_whose_waiting_check_fails_is_still_retried(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with domain_database(database_url) as connection_info:
        imported, repaired = _players(connection_info, 2)
        with psycopg.connect(connection_info) as connection:
            # This week's checks, added before the players were saved as due.
            connection.execute(
                """
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key,
                    profile_status, battle_log_status, league_history_status
                ) SELECT 'discovery_profile', 'ordinary', 'player', id, normalized_tag, now(),
                         'discovery-profile:' || id || ':' || to_char(
                             clashlens_eligibility_week(now()) AT TIME ZONE 'UTC',
                             'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                         'pending', 'not_applicable', 'pending'
                  FROM players WHERE id = ANY(%s::bigint[])
                """,
                ([imported, repaired],),
            )
            # A list import names one player; the repair finds the other.
            assert connection.execute(
                "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])", ([imported],)
            ).fetchone()[0] == 0
        assert population.run_command(_as(connection_info, "clashlens_collector"), repair=True) == 0
        assert json.loads(capsys.readouterr().out)["repair"]["marked_due"] == 1
        now = datetime.now(UTC)
        _admit(connection_info, now)
        assert _waiting(connection_info) == 2
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_work SET status = 'failed', failure_category = 'provider_failure'"
            )
        _admit(connection_info, now + timedelta(minutes=6))
        for player in (imported, repaired):
            assert _checks(connection_info, player) == [("week", "failed"), ("1", "pending")]


def test_a_profile_without_a_recognized_league_is_fetched_again_once_processed(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        observed_at = datetime.now(UTC) - timedelta(seconds=30)
        observation_id, _job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="unrecognized-this-week",
            endpoint="profile",
            body=json.dumps(
                {**PROFILE, "tag": "#9QQ", "leagueTier": {"id": 105000099, "name": "New League"}}
            ).encode(),
            observed_at=observed_at,
            normalized_tag="#9QQ",
        )
        with psycopg.connect(connection_info) as connection:
            player = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#9QQ'"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO collector_response_state (
                    scope, identity_key, endpoint, player_id, normalized_tag,
                    last_response_hash, last_content_fingerprint, last_occurrence_key,
                    last_applied_occurrence_key, last_seen_at, last_success_at,
                    last_observation_id
                ) VALUES ('player', '#9QQ', 'profile', %s, '#9QQ', repeat('a', 64),
                          repeat('a', 64), 'unrecognized-this-week', 'unrecognized-this-week',
                          %s, %s, %s)
                """,
                (player, observed_at, observed_at, observation_id),
            )
        _discover(connection_info, [player])
        now = datetime.now(UTC)
        _admit(connection_info, now)
        # While the profile is still being processed, the player waits.
        assert _checks(connection_info, player) == []
        assert _due(connection_info, player) == (now + timedelta(minutes=5), 0)
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_once(owner="unrecognized") is not None
        finally:
            database.close()
        _admit(connection_info, now + timedelta(minutes=6))
        _admit(connection_info, now + timedelta(minutes=7))
        # Processed without a recognized league, it neither settles the player
        # nor stands in for the new check's profile.
        assert _checks(connection_info, player) == [("week", "pending")]
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT profile_status, profile_observation_id FROM collector_work"
                " WHERE player_id = %s",
                (player,),
            ).fetchone() == ("pending", None)


def test_one_check_for_a_player_named_by_several_sources(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        (player,) = _players(connection_info, 1)
        _discover(connection_info, [player])  # battle opponent
        _discover(connection_info, [player])  # ranking
        with psycopg.connect(connection_info) as connection:  # a list import
            assert connection.execute(
                "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])", ([player],)
            ).fetchone()[0] == 1
        with psycopg.connect(_as(connection_info, "clashlens_collector")) as connection:
            # The Monday re-check: already due, so nothing new is added.
            assert connection.execute(
                "SELECT handed, added FROM clashlens_queue_promoted_player('#Q1')"
            ).fetchone() == (True, 0)
        _admit(connection_info, datetime.now(UTC) + timedelta(minutes=1))
        assert _checks(connection_info, player) == [("week", "pending")]
        assert _due(connection_info, player)[1] == 1


def test_a_promotion_answer_needs_a_saved_profile_newer_than_this_weeks_answer(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        # Minutes after the Reset the profile still showed Legend II.
        before = datetime.now(UTC) - timedelta(seconds=30)
        store_observation(
            connection_info,
            archive_server,
            occurrence_key="promotion-early-profile",
            endpoint="profile",
            body=json.dumps({**PROFILE, "tag": "#8QQ", "leagueTier": LEGEND_II}).encode(),
            observed_at=before,
            normalized_tag="#8QQ",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_once(owner="promotion-early") is not None
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            player, state = connection.execute(
                "SELECT id, eligibility_state FROM players WHERE normalized_tag = '#8QQ'"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key,
                    status, profile_status, battle_log_status, league_history_status
                ) VALUES ('discovery_profile', 'ordinary', 'player', %s, '#8QQ', %s,
                          'discovery-profile:' || %s || ':' || to_char(
                              clashlens_eligibility_week(%s) AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                          'complete', 'observed', 'not_applicable', 'observed')
                """,
                (player, before, player, before),
            )
        assert state == "ineligible"
        _discover(connection_info, [player])
        assert _due(connection_info, player) == (None, 0)

        with psycopg.connect(_as(connection_info, "clashlens_collector")) as connection:
            assert connection.execute(
                "SELECT handed, added FROM clashlens_queue_promoted_player('#8QQ')"
            ).fetchone() == (True, 1)
        _admit(connection_info, datetime.now(UTC))
        _admit(connection_info, datetime.now(UTC))
        # The earlier Legend II answer neither settles the player nor stands in.
        assert _checks(connection_info, player) == [("week", "complete"), ("1", "pending")]
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT profile_status, profile_observation_id FROM collector_work"
                " WHERE player_id = %s AND status = 'pending'",
                (player,),
            ).fetchone() == ("pending", None)
        # The report counts the player as waiting for that newer answer.
        assert _report(connection_info)["untracked_this_week"]["other_known"] == {
            "total": 1, "answered": 0, "check_waiting": 1, "due_for_retry": 0, "not_checked": 0,
        }


def test_repair_marks_never_answered_players_due_and_lists_known_lower_legends(
    database_url: str, archive_server, capsys: pytest.CaptureFixture[str]
) -> None:
    with domain_database(database_url) as connection_info:
        never, uncertain, gone = _players(connection_info, 3)
        for tag, tier, key in (
            ("#8QQ", LEGEND_II, "repair-legend-ii"),
            ("#9QQ", {"id": 105000099, "name": "New League"}, "repair-unknown-tier"),
        ):
            observation_id, _job = store_observation(
                connection_info,
                archive_server,
                occurrence_key=key,
                endpoint="profile",
                body=json.dumps({**PROFILE, "tag": tag, "leagueTier": tier}).encode(),
                observed_at=datetime.now(UTC) - timedelta(days=8),
                normalized_tag=tag,
            )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_once(owner="repair-1") is not None
            assert processor.process_once(owner="repair-2") is not None
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            # Saved before the list existed, as most Legend II players were.
            connection.execute("DELETE FROM promotion_candidates")
            # A player whose profile answer was not found is not checked again here.
            connection.execute(
                """
                INSERT INTO collector_response_state (
                    scope, identity_key, endpoint, player_id, normalized_tag,
                    last_response_hash, last_content_fingerprint, last_occurrence_key,
                    last_applied_occurrence_key, last_seen_at, last_not_found_at
                ) SELECT 'player', normalized_tag, 'profile', id, normalized_tag,
                         repeat('a', 64), repeat('a', 64), 'gone', 'gone', now(), now()
                  FROM players WHERE id = %s
                """,
                (gone,),
            )
            unknown_tier = connection.execute(
                "SELECT id, eligibility_state FROM players WHERE normalized_tag = '#9QQ'"
            ).fetchone()
            # One player was seen as a battle opponent.
            connection.execute(
                "INSERT INTO known_player_discoveries"
                " (player_id, observation_id, source_row_index, source_kind, discovered_at)"
                " VALUES (%s, %s, 0, 'battle_opponent', now())",
                (never, observation_id),
            )
        assert unknown_tier[1] == "unknown"

        assert population.run_command(_as(connection_info, "clashlens_collector"), repair=False) == 0
        before = json.loads(capsys.readouterr().out)
        assert before["repair_candidates"] == 3
        assert before["promotion_list"]["known_but_unlisted"] == 1
        assert before["old_classifications_remaining"] == 1
        assert before["untracked_this_week"] == {
            "battle_opponents": {
                "total": 1, "answered": 0, "check_waiting": 0, "due_for_retry": 0,
                "not_checked": 1,
            },
            "other_known": {
                "total": 4, "answered": 1, "check_waiting": 0, "due_for_retry": 0,
                "not_checked": 3,
            },
        }

        assert population.run_command(_as(connection_info, "clashlens_collector"), repair=True) == 0
        after = json.loads(capsys.readouterr().out)
        assert after["repair"] == {
            "unanswered_found": 3, "marked_due": 3, "promotion_rows_added": 1,
        }
        assert after["repair_candidates"] == 0
        assert after["promotion_list"]["legend_ii"] == 1
        assert after["promotion_list"]["known_but_unlisted"] == 0
        assert after["eligibility_due"]["total"] == 3
        assert after["untracked"]["not_found"] == 1
        assert after["untracked_this_week"]["battle_opponents"]["due_for_retry"] == 1
        assert after["untracked_this_week"]["other_known"]["due_for_retry"] == 2
        _admit(connection_info, datetime.now(UTC))
        for player in (never, uncertain, unknown_tier[0]):
            assert _checks(connection_info, player) == [("week", "pending")]
        assert _checks(connection_info, gone) == []
        # A queued check does not count as a new classification.
        assert _report(connection_info)["old_classifications_remaining"] == 1


def test_tracked_players_are_split_into_available_waiting_and_unavailable(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        for tag, season, key in (
            ("#8QQ", PROFILE["currentLeagueSeasonId"], "available"),
            ("#9QQ", 0, "not-signed-up"),
            ("#CQQ", PROFILE["currentLeagueSeasonId"], "gone"),
        ):
            store_observation(
                connection_info,
                archive_server,
                occurrence_key=key,
                endpoint="profile",
                body=json.dumps({**PROFILE, "tag": tag, "currentLeagueSeasonId": season}).encode(),
                observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
                normalized_tag=tag,
            )
        database, processor = _processor(connection_info, archive_server)
        try:
            for owner in ("split-1", "split-2", "split-3"):
                assert processor.process_once(owner=owner) is not None
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO collector_response_state (
                    scope, identity_key, endpoint, player_id, normalized_tag,
                    last_response_hash, last_content_fingerprint, last_occurrence_key,
                    last_applied_occurrence_key, last_seen_at, last_success_at,
                    last_not_found_at
                ) SELECT 'player', normalized_tag, 'profile', id, normalized_tag,
                         repeat('a', 64), repeat('a', 64), 'gone', 'gone', now(),
                         now() - interval '1 hour', now()
                  FROM players WHERE normalized_tag = '#CQQ'
                """
            )
        with psycopg.connect(_as(connection_info, "clashlens_collector")) as connection:
            report = connection.execute(
                "SELECT clashlens_population_report(now(), %s)", (SEASON_ID,)
            ).fetchone()[0]
        assert report["tracked"] == {
            "total": 3, "available": 1, "waiting_to_sign_up": 1, "unavailable": 1,
            "first_battle_log_pending": 3,
        }


def test_the_delay_from_a_first_check_to_the_first_battle_log_is_reported(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        logged, waiting = _players(connection_info, 2)
        _discover(connection_info, [logged, waiting])
        _admit(connection_info, datetime.now(UTC))
        with psycopg.connect(connection_info) as connection:
            # Profile processing found both in Legend I.
            connection.execute(
                "UPDATE players SET active = true, eligibility_state = 'eligible'"
                " WHERE id = ANY(%s::bigint[])",
                ([logged, waiting],),
            )
            first_at = connection.execute(
                "SELECT created_at FROM collector_work WHERE player_id = %s", (logged,)
            ).fetchone()[0]
        collector = CollectorDatabase(_as(connection_info, "clashlens_collector"))
        try:
            collector.record_response(
                _handoff(
                    occurrence_key="first-battle-log",
                    response_hash=_hash("first-battle-log"),
                    player_id=logged,
                    tag="#Q1",
                    endpoint="battle_log",
                    completed_at=first_at + timedelta(seconds=90),
                )
            )
        finally:
            collector.close()
        assert _report(connection_info)["first_battle_log_delay"] == {
            "players": 2, "with_first_log": 1,
            "median_seconds": 90, "p95_seconds": 90, "max_seconds": 90,
        }


def test_the_lab_list_load_reports_what_it_read_left_out_and_kept(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    older = "2026-10-06T20:00:00+00:00"
    newer = "2026-10-06T21:00:00+00:00"
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "INSERT INTO players (normalized_tag, active, eligibility_state)"
                " VALUES ('#2PP', true, 'eligible')"
            )
            connection.execute(
                "INSERT INTO promotion_candidates (normalized_tag, league_tier_id, checked_at)"
                " VALUES ('#UQQ', 105000034, %s)",
                (datetime.fromisoformat(newer),),
            )
        header = "tag,league_tier_id,trophies,checked_at\n"
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(
                header
                + f"#2PP,105000035,5100,{newer}\n"  # tracked
                + f"#8QQ,105000035,4900,{older}\n"
                + f"#8QQ,105000035,4950,{newer}\n"  # duplicate tag
                + f"#9QQ,105000034,,{older}\n"
                + f"#UQQ,105000035,4800,{older}\n"  # listed with a newer check
            ),
        )
        assert promotion_candidates.run_command(_as(connection_info, "clashlens_collector")) == 0
        assert json.loads(capsys.readouterr().out) == {
            "lines": 5, "read": 4, "duplicates": 1,
            "read_legend_ii": 3, "read_legend_iii": 1,
            "skipped_tracked": 1, "skipped_newer_profile": 0,
            "added": 2, "updated": 0, "added_or_updated": 2, "kept_newer_listed": 1,
            "listed_legend_ii": 1, "listed_legend_iii": 2,
        }


def test_two_collector_passes_at_once_never_pass_the_limit(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        ids = _players(connection_info, 2)
        _discover(connection_info, ids)
        now = datetime.now(UTC)
        with psycopg.connect(_as(connection_info, "clashlens_collector")) as first:
            with first.transaction():
                # The first pass is still adding checks.
                assert first.execute(
                    "SELECT clashlens_admit_due_eligibility(%s)", (now,)
                ).fetchone()[0] == 2
                with psycopg.connect(_as(connection_info, "clashlens_collector")) as second:
                    assert second.execute(
                        "SELECT clashlens_admit_due_eligibility(%s)", (now,)
                    ).fetchone()[0] == 0
        assert _waiting(connection_info) == 2
