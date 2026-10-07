"""The Legend II and III list a Monday re-check reads (migration 0076)."""

from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep

import psycopg
import pytest
from domain_test_support import apply_migration, domain_database, store_observation
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_domain_processing_postgres import _processor

from clashlens import promotion_candidates

PROFILE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
MIGRATION = Path(__file__).parents[2] / "deploy" / "migrations" / "0076_promotion_candidates.sql"
OBSERVED_AT = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)
LEGEND_II = {"id": 105000035, "name": "Legend II"}
LEGEND_I = {"id": 105000036, "name": "Legend I"}


def _collector_url(connection_info: str) -> str:
    options = conninfo_to_dict(connection_info).get("options", "")
    return make_conninfo(connection_info, options=f"{options} -c role=clashlens_collector")


def _rows(connection_info: str) -> list[tuple]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT normalized_tag, league_tier_id, trophies, checked_at"
            " FROM promotion_candidates ORDER BY normalized_tag"
        ).fetchall()


def test_lab_list_loads_once_keeps_newer_checks_and_skips_tracked_players(
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
                "INSERT INTO players (normalized_tag, active, eligibility_state, current_observed_at)"
                " VALUES ('#0PP', false, 'ineligible', %s)",
                (datetime.fromisoformat(newer),),
            )
        url = _collector_url(connection_info)

        def load(text: str) -> int:
            monkeypatch.setattr("sys.stdin", io.StringIO(text))
            return promotion_candidates.run_command(url)

        header = "tag,league_tier_id,trophies,checked_at\n"
        assert load(
            header
            + f"#2PP,105000035,5100,{newer}\n"  # tracked: left out
            + f"#8QQ,105000035,4900,{older}\n"
            + f"#8QQ,105000035,4950,{newer}\n"  # same tag: newest kept
            + f"#9QQ,105000034,,{older}\n"
            + f"#0PP,105000035,4800,{older}\n"  # saved profile checked later: left out
        ) == 0
        assert json.loads(capsys.readouterr().out) == {"added_or_updated": 2, "read": 4}
        assert _rows(connection_info) == [
            ("#8QQ", 105000035, 4950, datetime.fromisoformat(newer)),
            ("#9QQ", 105000034, None, datetime.fromisoformat(older)),
        ]

        # Loading an older check again changes nothing; a newer one replaces it.
        assert load(header + f"#8QQ,105000034,4000,{older}\n#9QQ,105000035,4800,{newer}\n") == 0
        assert json.loads(capsys.readouterr().out)["added_or_updated"] == 1
        assert [row[:3] for row in _rows(connection_info)] == [
            ("#8QQ", 105000035, 4950),
            ("#9QQ", 105000035, 4800),
        ]

        # One bad line refuses the whole file, including a tag without '#'.
        assert load(header + f"#2QQ,105000035,4800,{newer}\n#0QQ,105000036,5000,{newer}\n") == 1
        assert "line 3" in capsys.readouterr().err
        assert load(header + f"#2QQ,105000035,4800,{newer}\n0QQ,105000035,5000,{newer}\n") == 1
        assert "line 3" in capsys.readouterr().err
        assert [row[0] for row in _rows(connection_info)] == ["#8QQ", "#9QQ"]


def _profile(tier: dict, trophies: int) -> bytes:
    payload = json.loads(PROFILE.read_bytes())
    payload.update(leagueTier=tier, trophies=trophies)
    return json.dumps(payload).encode()


def _store(connection_info: str, archive_server, key: str, body: bytes, at: datetime) -> None:
    store_observation(
        connection_info, archive_server, occurrence_key=key,
        endpoint="profile", body=body, observed_at=at, normalized_tag="#2PP",
    )


def test_processed_profiles_add_and_remove_promotion_candidates(
    database_url: str, archive_server
) -> None:
    profile = _profile

    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            def process(key: str, body: bytes, at: datetime) -> None:
                _store(connection_info, archive_server, key, body, at)
                assert processor.process_once(owner=key) is not None

            process("legend-ii", profile(LEGEND_II, 4900), OBSERVED_AT)
            assert _rows(connection_info) == [("#2PP", 105000035, 4900, OBSERVED_AT)]

            promoted_at = OBSERVED_AT + timedelta(days=6)
            process("legend-i", profile(LEGEND_I, 5000), promoted_at)
            assert _rows(connection_info) == []

            # A late, older Legend II answer does not put a tracked player back.
            process("late-legend-ii", profile(LEGEND_II, 4950), OBSERVED_AT + timedelta(days=1))
            assert _rows(connection_info) == []
        finally:
            database.close()


def test_an_unchanged_profile_answer_moves_the_listed_check_forward(
    database_url: str, archive_server
) -> None:
    rechecked_at = OBSERVED_AT + timedelta(days=7)
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _store(connection_info, archive_server, "legend-ii", _profile(LEGEND_II, 4900), OBSERVED_AT)
            assert processor.process_once(owner="legend-ii") is not None
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            # The collector saves no job for an answer matching the saved profile.
            connection.execute(
                """
                INSERT INTO collector_response_state (
                    scope, identity_key, endpoint, player_id, normalized_tag,
                    last_response_hash, last_content_fingerprint,
                    last_occurrence_key, last_applied_occurrence_key,
                    last_seen_at, last_observation_id, last_success_at
                )
                SELECT 'player', '#2PP', 'profile', player.id, '#2PP',
                       observation.response_hash, player.current_profile_fingerprint,
                       'unchanged', 'unchanged', %s, observation.id, %s
                FROM players AS player
                JOIN collector_observations AS observation ON observation.player_id = player.id
                WHERE player.normalized_tag = '#2PP'
                """,
                (rechecked_at, rechecked_at),
            )
        assert _rows(connection_info) == [("#2PP", 105000035, 4900, rechecked_at)]


def test_an_older_profile_waits_for_a_newer_job_and_leaves_its_removal(
    database_url: str, archive_server
) -> None:
    newer_at = OBSERVED_AT + timedelta(days=2)
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _store(connection_info, archive_server, "legend-ii", _profile(LEGEND_II, 4900), OBSERVED_AT)
            assert processor.process_once(owner="legend-ii") is not None
            _store(
                connection_info, archive_server, "late-legend-ii",
                _profile(LEGEND_II, 4950), OBSERVED_AT + timedelta(days=1),
            )
            with ThreadPoolExecutor(1) as executor:
                with psycopg.connect(connection_info) as newer, newer.transaction():
                    # A newer Legend I profile's job has the player and has not committed.
                    newer.execute(
                        "SELECT 1 FROM players WHERE normalized_tag = '#2PP' FOR NO KEY UPDATE"
                    )
                    newer.execute(
                        "UPDATE players SET current_observed_at = %s WHERE normalized_tag = '#2PP'",
                        (newer_at,),
                    )
                    newer.execute(
                        "SELECT clashlens_note_promotion_candidate('#2PP', 105000036, 5000, %s)",
                        (newer_at,),
                    )
                    late = executor.submit(processor.process_once, owner="late-legend-ii")
                    sleep(0.3)
                assert late.result(timeout=10) is not None
            assert _rows(connection_info) == []
        finally:
            database.close()


def test_migration_copies_saved_profiles_as_of_their_latest_check(
    database_url: str, archive_server
) -> None:
    confirmed_at = OBSERVED_AT + timedelta(hours=1)
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _store(connection_info, archive_server, "legend-ii", _profile(LEGEND_II, 4900), OBSERVED_AT)
            assert processor.process_once(owner="legend-ii") is not None
        finally:
            database.close()
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(
                "UPDATE players SET current_profile_confirmed_at = %s WHERE normalized_tag = '#2PP'",
                (confirmed_at,),
            )
            connection.execute("DROP TABLE promotion_candidates")
            connection.execute(
                "DROP FUNCTION clashlens_note_promotion_candidate(text,integer,integer,timestamptz)"
            )
            connection.execute("DELETE FROM clash_lens_schema_migrations WHERE version = 76")
            apply_migration(connection, MIGRATION.read_text(encoding="utf-8"))
        assert _rows(connection_info) == [("#2PP", 105000035, 4900, confirmed_at)]
