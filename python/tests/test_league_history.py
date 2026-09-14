from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, store_observation

from clashlens.db import Database
from clashlens.league_history import (
    LEAGUE_HISTORY_ENDPOINT_VERSION,
    LEAGUE_HISTORY_PARSER_VERSION,
    LEAGUE_HISTORY_SCHEMA_VERSION,
    LeagueHistoryParseError,
    complete_league_history,
    parse_league_history,
)

OBSERVED_AT = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)


def _payload(*items: object) -> bytes:
    return json.dumps({"items": list(items)}).encode()


def _entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "leagueSeasonId": "1781499600",
        "leagueTrophies": 5812,
        "leagueTierId": 105000036,
        "placement": 12,
        "attackWins": 6,
        "attackLosses": 2,
        "attackStars": 21,
        "defenseWins": 5,
        "defenseLosses": 3,
        "defenseStars": 18,
        "maxBattles": 8,
    }
    entry.update(overrides)
    return entry


def test_league_history_parser_reads_season_entries() -> None:
    parsed = parse_league_history(
        _payload(_entry(), _entry(leagueSeasonId="1757102400")),
        expected_tag="#2PP",
        observed_at=OBSERVED_AT,
    )

    assert parsed.normalized_tag == "#2PP"
    assert parsed.schema_version == LEAGUE_HISTORY_SCHEMA_VERSION
    assert parsed.endpoint_version == LEAGUE_HISTORY_ENDPOINT_VERSION
    assert parsed.parser_version == LEAGUE_HISTORY_PARSER_VERSION
    assert parsed.row_count == 2
    assert parsed.has_row_gap is False
    assert parsed.outcome == "official_observed"
    first = parsed.entries[0]
    assert first.league_season_id == "1781499600"
    assert first.league_trophies == 5812
    assert first.attack_wins == 6
    assert first.max_battles == 8


def test_league_history_parser_accepts_integer_season_ids() -> None:
    parsed = parse_league_history(
        _payload(_entry(leagueSeasonId=1781499600)),
        expected_tag="#2PP",
        observed_at=OBSERVED_AT,
    )

    assert parsed.entries[0].league_season_id == "1781499600"


@pytest.mark.parametrize(
    "item",
    [
        "not-an-object",
        {"leagueTrophies": 5800},
        {"leagueSeasonId": "not-a-season"},
        {"leagueSeasonId": True},
        _entry(attackWins="six"),
    ],
)
def test_league_history_parser_marks_bad_rows_as_gaps(item: object) -> None:
    parsed = parse_league_history(
        _payload(_entry(), item),
        expected_tag="#2PP",
        observed_at=OBSERVED_AT,
    )

    assert parsed.row_count == 2
    assert len(parsed.entries) == 1
    assert parsed.has_row_gap is True
    assert parsed.outcome == "official_partial"


@pytest.mark.parametrize("body", [b"not-json", b"{}", b"[]", b'{"items": {}}'])
def test_league_history_parser_rejects_unsupported_bodies(body: bytes) -> None:
    expected = (
        "malformed_json" if body == b"not-json" else "unsupported_league_history_schema"
    )
    with pytest.raises(LeagueHistoryParseError, match=expected):
        parse_league_history(
            body, expected_tag="#2PP", observed_at=OBSERVED_AT
        )


def test_complete_league_history_stores_season_rows(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        body = _payload(_entry())
        observation_id, job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="league-history:1",
            endpoint="league_history",
            body=body,
            observed_at=OBSERVED_AT,
            normalized_tag="#2PP",
            parser_version=LEAGUE_HISTORY_PARSER_VERSION,
            processing_version="clashlens-domain-processing-v1",
            domain_rule_version="clashlens-domain-rules-v1",
        )
        database = Database(connection_info)
        try:
            claim = database.claim_job(
                owner="league-history-worker", job_id=job_id
            )
            assert claim is not None
            assert claim.endpoint == "league_history"
            history = parse_league_history(
                body, expected_tag="#2PP", observed_at=OBSERVED_AT
            )
            complete_league_history(database, claim, history)
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            rows = connection.execute(
                """
                SELECT league_season_id, league_trophies, placement,
                       attack_wins, observation_id
                FROM player_league_history_entries
                """
            ).fetchall()
            outcome = connection.execute(
                """
                SELECT outcome FROM observation_processing_outcomes
                WHERE observation_id = %s
                """,
                (observation_id,),
            ).fetchone()
        assert rows == [("1781499600", 5812, 12, 6, observation_id)]
        assert outcome == ("processed",)


def test_complete_league_history_upserts_repeated_seasons(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            for index, trophies in enumerate((5812, 5840)):
                observed = OBSERVED_AT + timedelta(hours=index)
                body = _payload(_entry(leagueTrophies=trophies))
                _observation_id, job_id = store_observation(
                    connection_info,
                    archive_server,
                    occurrence_key=f"league-history:repeat:{index}",
                    endpoint="league_history",
                    body=body,
                    observed_at=observed,
                    normalized_tag="#2PP",
                    parser_version=LEAGUE_HISTORY_PARSER_VERSION,
                    processing_version="clashlens-domain-processing-v1",
                    domain_rule_version="clashlens-domain-rules-v1",
                )
                claim = database.claim_job(
                    owner="league-history-worker", job_id=job_id
                )
                assert claim is not None
                complete_league_history(
                    database,
                    claim,
                    parse_league_history(
                        body, expected_tag="#2PP", observed_at=observed
                    ),
                )
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            rows = connection.execute(
                """
                SELECT league_trophies, count(*) OVER ()
                FROM player_league_history_entries
                """
            ).fetchall()
        assert rows == [(5840, 1)]


def test_complete_league_history_ignores_stale_replays(
    database_url: str, archive_server
) -> None:
    # A delayed or replayed response with an older observed_at must not move
    # the stored season row backwards.
    with domain_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            for index, (trophies, hours) in enumerate(((5840, 2), (5812, 0))):
                observed = OBSERVED_AT + timedelta(hours=hours)
                body = _payload(_entry(leagueTrophies=trophies))
                _observation_id, job_id = store_observation(
                    connection_info,
                    archive_server,
                    occurrence_key=f"league-history:stale:{index}",
                    endpoint="league_history",
                    body=body,
                    observed_at=observed,
                    normalized_tag="#2PP",
                    parser_version=LEAGUE_HISTORY_PARSER_VERSION,
                    processing_version="clashlens-domain-processing-v1",
                    domain_rule_version="clashlens-domain-rules-v1",
                )
                claim = database.claim_job(
                    owner="league-history-worker", job_id=job_id
                )
                assert claim is not None
                complete_league_history(
                    database,
                    claim,
                    parse_league_history(
                        body, expected_tag="#2PP", observed_at=observed
                    ),
                )
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            row = connection.execute(
                "SELECT league_trophies FROM player_league_history_entries"
            ).fetchone()
        assert row == (5840,)
