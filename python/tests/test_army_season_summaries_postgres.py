"""Shared whole-season army summaries (issue #82, army slice).

An army_season_summaries row per (season, lens, category) is projected
from the current versioned battle facts with the shared builder.
Historical reads use the summary alone, survive detail removal, refresh
atomically on late corrections, and leave live seasons untouched.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database
from fastapi.testclient import TestClient
from test_private_api import NOW, NOW_SECONDS, TS_CURRENT, signed_headers

import clashlens.army_season_summaries as army_summaries_module
import clashlens.db as db_module
from clashlens.api import create_app
from clashlens.api_db import ApiDatabase
from clashlens.army_analytics import ArmyAnalyticsSelection, build_army_result
from clashlens.army_season_summaries import (
    PROJECTION_VERSION,
    materialize_army_season,
    materialize_completed_army_season,
)
from clashlens.db import Database

SEASON = "1785714000"
DAY0 = datetime(2026, 5, 1, 5, 0, tzinfo=UTC)


def _fact_hash(lens: str, index: int) -> str:
    return hashlib.sha256(f"{lens}:{index}".encode()).hexdigest()


def _seed(
    connection,
    *,
    player_tag="#2PP",
    offense: list[dict] | None = None,
    defense: list[dict] | None = None,
    completed_days: int = 28,
    with_day28_log: bool = True,
):
    """Seed facts directly; parents are bypassed test-only via replica role."""
    connection.execute("SET LOCAL session_replication_role = replica")
    player_id = connection.execute(
        """
        INSERT INTO players (normalized_tag, active, eligibility_state)
        VALUES (%s, true, 'eligible')
        RETURNING id
        """,
        (player_tag,),
    ).fetchone()[0]
    if with_day28_log:
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, version, state, coverage,
                ranked_day_end, official_season_id, season_day_number
            ) VALUES (%s, %s, 1, 'Complete', 'complete', %s, %s, 28)
            """,
            (player_id, DAY0 + timedelta(days=27), DAY0 + timedelta(days=28), SEASON),
        )
    for index, (lens, specs) in enumerate(
        (("offense", offense or []), ("defense", defense or []))
    ):
        for position, spec in enumerate(specs):
            connection.execute(
                """
                INSERT INTO army_analytics_battle_facts (
                    battle_id, evidence_id, source_ranked_day_version_id,
                    ranked_day_start, official_season_id, season_day_number,
                    lens, population_player_id, stars, destruction_percentage,
                    army_state, home_troops, spells, siege, cc_troops, heroes,
                    unresolved_components, perspective_disagreement,
                    battle_time_trophies, input_hash, version
                ) VALUES (
                    %s, %s, 4242, %s, %s, 1, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s, %s, 1
                )
                """,
                (
                    7000 + index * 10 + position,
                    8000 + index * 10 + position,
                    DAY0,
                    SEASON,
                    lens,
                    player_id,
                    spec["stars"],
                    spec.get("destruction", 100),
                    spec.get("army_state", "decoded"),
                    json.dumps(spec.get("home_troops", [])),
                    json.dumps(spec.get("spells", [])),
                    json.dumps(spec.get("siege", [])),
                    json.dumps(spec.get("cc_troops", [])),
                    json.dumps(spec.get("heroes", [])),
                    json.dumps(spec.get("unresolved", [])),
                    spec.get("disagreement", False),
                    spec.get("trophies", 6000),
                    _fact_hash(lens, index * 10 + position),
                ),
            )
    for day in range(1, completed_days + 1):
        connection.execute(
            """
            INSERT INTO army_analytics_completed_days (
                ranked_day_start, official_season_id, season_day_number,
                fact_input_hash
            ) VALUES (%s, %s, %s, %s)
            """,
            (
                DAY0 + timedelta(days=day - 1),
                SEASON,
                day,
                hashlib.sha256(f"day:{day}".encode()).hexdigest(),
            ),
        )
    return player_id


def _offense_specs():
    troops = [["troop:58", 2]]
    return [
        {"stars": 3, "home_troops": troops, "spells": [["spell:2", 1]]},
        {"stars": 3, "home_troops": troops, "spells": [["spell:2", 1]]},
        # Missing battle-time trophy evidence is counted, not filtered.
        {"stars": 2, "destruction": 80, "home_troops": troops, "trophies": None},
        {
            "stars": 1,
            "destruction": 50,
            "home_troops": [["troop:58", 1]],
            "army_state": "partial",
            "unresolved": [{"origin": "home:pet", "section": "h"}],
        },
        # Undecodable evidence counts in totals, never in the sample.
        {"stars": 0, "destruction": 0, "army_state": "missing_army_share_code"},
    ]


def _defense_specs():
    return [
        {"stars": 3, "home_troops": [["troop:51", 3]]},
        {"stars": 0, "destruction": 40, "home_troops": [["troop:51", 3]]},
    ]


def _row(connection, lens="offense", category="troops"):
    cursor = connection.execute(
        """
        SELECT * FROM army_season_summaries
        WHERE official_season_id = %s AND lens = %s AND category = %s
        """,
        (SEASON, lens, category),
    )
    row = cursor.fetchone()
    assert row is not None
    return dict(zip([d.name for d in cursor.description], row))


def test_migration_is_reentrant(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            migration = (
                Path(__file__).parents[2]
                / "deploy"
                / "migrations"
                / "0020_army_season_summaries.sql"
            )
            connection.execute(migration.read_text(encoding="utf-8"))
            row = connection.execute(
                "SELECT count(*) FROM army_season_summaries"
            ).fetchone()
            assert row[0] == 0


def test_complete_season_materializes_whole_season_aggregates(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _seed(
                    connection,
                    offense=_offense_specs(),
                    defense=_defense_specs(),
                )
                connection.commit()
                report = materialize_completed_army_season(
                    connection, season_id=SEASON, now=DAY0 + timedelta(days=29)
                )
                connection.commit()
            assert report["season_completed"] is True
            assert report["lenses"]["offense"]["materialized"] == 11
            assert report["lenses"]["offense"]["failures"] == []
            with database.pool.connection() as connection:
                summary = _row(connection)
            # Defense sightings stay in the defense lens, never here.
            assert summary["total_attacks"] == 5
            assert summary["usable_army_sample"] == 4
            assert summary["coverage_state"] == "complete"
            assert summary["days_observed"] == 28
            assert summary["days_missing"] == 0
            assert summary["missing_days"] == []
            assert summary["projection_version"] == PROJECTION_VERSION
            assert summary["missing_trophy_membership_evidence"] == 1
            assert summary["unknown_affected_attacks"] == 1
            assert summary["unknown_component_occurrences"] == 1
            states = dict(summary["army_states"])
            assert states["fully_decoded"] == 3
            assert states["partial"] == 1
            assert states["missing_code"] == 1
            rows = {row["key"]: row for row in summary["result_rows"]}
            troops = rows["troop:58"]
            assert troops["usage_count"] == 4
            assert troops["usage_denominator"] == 4
            assert troops["usage_rate"] == 1.0
            assert troops["star_counts"] == [0, 1, 1, 2]
            assert troops["three_star_rate"] == 0.5
            assert troops["average_stars"] == pytest.approx(2.25)
            assert troops["average_destruction"] == pytest.approx(82.5)
            spells = {
                row["key"]: row
                for row in _row(connection, category="spells")["result_rows"]
            }["spell:2"]
            assert spells["usage_count"] == 2
            assert spells["usage_denominator"] == 4
            with database.pool.connection() as connection:
                defense = _row(connection, lens="defense")
            assert defense["total_attacks"] == 2
            assert defense["usable_army_sample"] == 2
            assert {
                row["key"]: row["usage_count"]
                for row in defense["result_rows"]
            } == {"troop:51": 2}
        finally:
            database.close()


def test_historical_read_needs_no_battle_facts(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _seed(
                    connection,
                    offense=_offense_specs(),
                    defense=_defense_specs(),
                )
                connection.commit()
                materialize_army_season(connection, SEASON, "offense")
                connection.commit()
                before = database.get_army_season_summary(
                    SEASON, "offense", "troops", "usage-rate"
                )
                # Retire every detail row the live reads depend on.
                connection.execute(
                    "DELETE FROM army_analytics_battle_facts WHERE official_season_id = %s",
                    (SEASON,),
                )
                connection.execute(
                    "DELETE FROM army_analytics_completed_days WHERE official_season_id = %s",
                    (SEASON,),
                )
                connection.commit()
                after = database.get_army_season_summary(
                    SEASON, "offense", "troops", "usage-rate"
                )
            assert before is not None and after is not None
            assert after == before
            assert after["kind"] == "army-analytics"
            assert after["selection"] == {
                "lens": "offense",
                "season": SEASON,
                "start_day": 1,
                "end_day": 28,
                "population": "all",
                "category": "troops",
                "sort": "usage-rate",
            }
            assert after["collection_coverage"] == {
                "state": "complete",
                "completed_days": 28,
            }
            assert after["reproducibility"]["legend_days"] == [1, 28]
            assert after["reproducibility"]["snapshot_versions"] == []
            assert after["publication_identity"].startswith("army-season-")
            rows = {row["key"]: row for row in after["rows"]}
            assert rows["troop:58"]["star_counts"] == [0, 1, 1, 2]
            assert rows["troop:58"]["three_star_rate"] == 0.5
            assert database.get_army_season_summary(
                SEASON, "offense", "troops", "usage-rate"
            ) is not None
            assert (
                database.get_army_season_summary(
                    "unknown-season", "offense", "troops", "usage-rate"
                )
                is None
            )
        finally:
            database.close()


def test_repeat_is_unchanged_and_correction_refreshes(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _seed(connection, offense=_offense_specs())
                connection.commit()
                first = materialize_army_season(connection, SEASON, "offense")
                published_at = _row(connection)["published_at"]
                connection.commit()
                second = materialize_army_season(connection, SEASON, "offense")
                connection.commit()
            assert first["materialized"] == 11
            assert second == {
                **first,
                "materialized": 0,
                "unchanged": 11,
            }
            with database.pool.connection() as connection:
                assert _row(connection)["published_at"] == published_at
                # A genuine correction changes the stored aggregate.
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute(
                    """
                    UPDATE army_analytics_battle_facts
                    SET stars = 3, destruction_percentage = 100
                    WHERE official_season_id = %s AND lens = 'offense'
                      AND army_state = 'partial'
                    """,
                    (SEASON,),
                )
                connection.commit()
                third = materialize_army_season(connection, SEASON, "offense")
                connection.commit()
            assert third["materialized"] >= 1
            assert third["content_digests"]["troops"] != first["content_digests"]["troops"]
            with database.pool.connection() as connection:
                rows = {
                    row["key"]: row
                    for row in _row(connection)["result_rows"]
                }
            assert rows["troop:58"]["star_counts"] == [0, 0, 1, 3]
            assert rows["troop:58"]["three_star_rate"] == 0.75
        finally:
            database.close()


def test_partial_days_stay_partial_and_live_season_untouched(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _seed(connection, offense=_offense_specs(), completed_days=20)
                connection.commit()
                report = materialize_completed_army_season(
                    connection, season_id=SEASON, now=DAY0 + timedelta(days=29)
                )
                connection.commit()
                summary = _row(connection)
            assert report["season_completed"] is True
            assert summary["coverage_state"] == "partial"
            assert summary["days_observed"] == 20
            assert summary["days_missing"] == 8
            assert summary["missing_days"] == list(range(21, 29))
            read = database.get_army_season_summary(
                SEASON, "offense", "troops", "usage-rate"
            )
            assert read is not None
            assert read["collection_coverage"] == {
                "state": "partial",
                "completed_days": 20,
            }
            with database.pool.connection() as connection:
                live = materialize_completed_army_season(
                    connection,
                    season_id="live-season",
                    now=DAY0 + timedelta(days=29),
                )
                connection.commit()
                count = connection.execute(
                    """
                    SELECT count(*) FROM army_season_summaries
                    WHERE official_season_id = 'live-season'
                    """
                ).fetchone()[0]
            assert live["season_completed"] is False
            assert count == 0
        finally:
            database.close()


def test_historical_season_endpoint_serves_summary_without_facts(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _seed(connection, offense=_offense_specs())
                connection.commit()
                materialize_army_season(connection, SEASON, "offense")
                connection.commit()
            app = create_app(
                database=database,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                now=lambda: NOW,
            )
            target = (
                f"/v1/analytics/armies/seasons/{SEASON}"
                "?lens=offense&category=troops&sort=usage-rate"
            )
            with TestClient(app) as client:
                assert client.get(target).status_code == 401
                response = client.get(target, headers=signed_headers(target))
                assert response.status_code == 200
                payload = response.json()
                assert payload["kind"] == "army-analytics"
                assert payload["selection"]["population"] == "all"
                assert payload["total_attacks"] == 5
                assert payload["usable_army_sample"] == 4
                rows = {row["key"]: row for row in payload["rows"]}
                assert rows["troop:58"]["usage_count"] == 4
                assert rows["troop:58"]["three_star_rate"] == 0.5
                missing = client.get(
                    "/v1/analytics/armies/seasons/unknown-season",
                    headers=signed_headers(
                        "/v1/analytics/armies/seasons/unknown-season"
                    ),
                )
                assert missing.status_code == 404
                bad_lens = (
                    f"/v1/analytics/armies/seasons/{SEASON}?lens=sideways"
                )
                rejected = client.get(
                    bad_lens, headers=signed_headers(bad_lens)
                )
                assert rejected.status_code == 422
        finally:
            database.close()


def test_projected_troops_match_live_builder(database_url: str) -> None:
    """The stored troops aggregate equals the shared live builder output."""
    specs = _offense_specs()
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _seed(connection, offense=specs)
                connection.commit()
                materialize_army_season(connection, SEASON, "offense")
                connection.commit()
                stored = _row(connection)
            facts = [
                {
                    "stars": spec["stars"],
                    "destruction_percentage": spec.get("destruction", 100),
                    "army_state": spec.get("army_state", "decoded"),
                    "home_troops": spec.get("home_troops", []),
                    "spells": spec.get("spells", []),
                    "siege": spec.get("siege", []),
                    "cc_troops": spec.get("cc_troops", []),
                    "heroes": spec.get("heroes", []),
                    "unresolved_components": spec.get("unresolved", []),
                    "perspective_disagreement": spec.get("disagreement", False),
                }
                for spec in specs
            ]
            selection = ArmyAnalyticsSelection(
                lens="offense",
                season=SEASON,
                start_day=1,
                end_day=28,
                population="top-100",
                category="troops",
                sort="usage-rate",
            )
            expected = build_army_result(facts, selection)
            assert stored["result_rows"] == expected["rows"]
            assert stored["total_attacks"] == expected["total_attacks"]
            assert stored["usable_army_sample"] == expected["usable_army_sample"]
            assert dict(stored["army_states"]) == expected["army_states"]
            assert (
                stored["unknown_affected_attacks"]
                == expected["unknown_affected_attacks"]
            )
        finally:
            database.close()


def test_failed_lens_keeps_prior_and_reports_lens_failure(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed lens never publishes partially; the backfill reports it."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _seed(
                    connection,
                    offense=_offense_specs(),
                    defense=_defense_specs(),
                )
                connection.commit()
                published = materialize_completed_army_season(
                    connection, season_id=SEASON, now=DAY0 + timedelta(days=29)
                )
                connection.commit()
            assert published["lenses"]["offense"]["failures"] == []
            with database.pool.connection() as connection:
                prior = _row(connection)
                prior_count = connection.execute(
                    """
                    SELECT count(*) FROM army_season_summaries
                    WHERE official_season_id = %s AND lens = 'offense'
                    """,
                    (SEASON,),
                ).fetchone()[0]
                real_upsert = army_summaries_module._upsert_category

                def _fail_offense(
                    connection, season_id, lens, category, summary, digest
                ):
                    if lens == "offense":
                        raise RuntimeError("projection store unavailable")
                    return real_upsert(
                        connection, season_id, lens, category, summary, digest
                    )

                monkeypatch.setattr(
                    army_summaries_module, "_upsert_category", _fail_offense
                )
                with pytest.raises(RuntimeError, match="projection store"):
                    materialize_army_season(connection, SEASON, "offense")
                connection.rollback()
            with database.pool.connection() as connection:
                # No partial publish: the prior complete lens is intact.
                assert (
                    connection.execute(
                        """
                        SELECT count(*) FROM army_season_summaries
                        WHERE official_season_id = %s AND lens = 'offense'
                        """,
                        (SEASON,),
                    ).fetchone()[0]
                    == prior_count
                )
                current = _row(connection)
                assert current["content_digest"] == prior["content_digest"]
                assert current["published_at"] == prior["published_at"]
                # The backfill reports the bad lens while defense is unaffected.
                report = materialize_completed_army_season(
                    connection, season_id=SEASON, now=DAY0 + timedelta(days=29)
                )
                connection.commit()
            assert report["lenses"]["offense"]["failures"] != []
            assert report["lenses"]["defense"]["failures"] == []
            assert report["lenses"]["defense"]["unchanged"] == 11
            with database.pool.connection() as connection:
                assert _row(connection)["content_digest"] == prior["content_digest"]
        finally:
            database.close()


def test_refresh_failure_warns_and_keeps_day_durable(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late-correction refresh failure cannot roll back day facts/marker."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                _seed(connection, offense=_offense_specs())
                connection.commit()
                materialize_army_season(connection, SEASON, "offense")
                connection.commit()
            database._supports_army_season_summaries = True

            def _boom(connection, season_id, lens):
                raise RuntimeError("projection unavailable")

            monkeypatch.setattr(db_module, "materialize_army_season", _boom)
            with database.pool.connection() as connection:
                # Inside an enclosing day-build-like transaction the refresh
                # warns instead of raising, and the transaction stays healthy.
                with pytest.warns(
                    RuntimeWarning, match="army_season_summary_refresh_failed"
                ):
                    database._refresh_army_season_summaries(connection, SEASON)
                marker = connection.execute(
                    """
                    SELECT fact_input_hash FROM army_analytics_completed_days
                    WHERE official_season_id = %s LIMIT 1
                    """,
                    (SEASON,),
                ).fetchone()
                assert marker is not None
                facts = connection.execute(
                    """
                    SELECT count(*) FROM army_analytics_battle_facts
                    WHERE official_season_id = %s AND is_current
                    """,
                    (SEASON,),
                ).fetchone()[0]
                assert facts == 5
                connection.commit()
            # A working refresh on a summarized season applies corrections.
            monkeypatch.undo()
            with database.pool.connection() as connection:
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute(
                    """
                    UPDATE army_analytics_battle_facts
                    SET stars = 0
                    WHERE official_season_id = %s AND lens = 'offense' AND stars = 3
                    """,
                    (SEASON,),
                )
                database._refresh_army_season_summaries(connection, SEASON)
                connection.commit()
            with database.pool.connection() as connection:
                rows = {row["key"]: row for row in _row(connection)["result_rows"]}
            assert rows["troop:58"]["star_counts"] == [2, 1, 1, 0]
        finally:
            database.close()
