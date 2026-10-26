from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta

import pytest
from domain_test_support import as_api_role, domain_database, store_observation, text
from test_army_analytics_publication_postgres import (
    DAY_START,
    DEFENDER_CODE,
    FIXTURE_CODE,
    PARTIAL_CODE,
    _army_job,
    _event,
    _processor,
    _publish_day,
    _publish_day_correction,
    _row,
    _seed_frozen_snapshot_at,
    _selection,
)

from clashlens import api_analytics, army_rank_bands, army_types
from clashlens.api_db import ApiDatabase
from clashlens.army_analytics import (
    CATEGORIES,
    TOP_PRESETS,
    ArmyAnalyticsUnavailable,
)

POPULATIONS = (
    "top-5",
    "top-10",
    "top-200",
    "top-2000",
    "top-10000",
    "band-1-100",
    "band-6-10",
    "band-101-200",
    "band-1001-2000",
    "band-5001-10000",
)

# The Top N views and rank ranges the Armies page offers.
OFFERED = (
    *(f"top-{count}" for count in sorted(TOP_PRESETS)),
    *(f"band-{first}-{first + 99}" for first in range(1, 1000, 100)),
    "band-1001-2000",
    "band-2001-5000",
    "band-5001-10000",
)
# What a view shows, without the evidence fields that name its source.
SHOWN = (
    "rows",
    "total_attacks",
    "usable_army_sample",
    "army_states",
    "unknown_affected_attacks",
    "unknown_component_occurrences",
    "perspective_disagreement_count",
)


def _republish(database, boundary, positions: dict[int, int]) -> int:
    """Publish the next version of a board, moving players to new positions."""
    with database.pool.connection() as connection:
        old = connection.execute(
            """
            SELECT id FROM leaderboard_snapshots
            WHERE snapshot_kind = 'frozen' AND state = 'published'
              AND boundary_at = %s
            ORDER BY version DESC LIMIT 1
            """,
            (boundary,),
        ).fetchone()[0]
        new = connection.execute(
            """
            INSERT INTO leaderboard_snapshots (
                snapshot_kind, boundary_at, version, ordering_rule_version,
                freshness_rule_version, state, measured_coverage, stale_entry_count
            )
            SELECT snapshot_kind, boundary_at, version + 1, ordering_rule_version,
                   freshness_rule_version, state, measured_coverage, stale_entry_count
            FROM leaderboard_snapshots WHERE id = %s
            RETURNING id
            """,
            (old,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO leaderboard_snapshot_entries (
                snapshot_id, position, player_id, trophies, trophy_observation_id,
                trophy_observed_at, observation_age_seconds, freshness,
                confidence, tie_hash
            )
            SELECT %s, COALESCE(moved.position, entry.position), entry.player_id,
                   trophies, trophy_observation_id, trophy_observed_at,
                   observation_age_seconds, freshness, confidence, tie_hash
            FROM leaderboard_snapshot_entries AS entry
            LEFT JOIN unnest(%s::bigint[], %s::integer[])
                AS moved(player_id, position) USING (player_id)
            WHERE entry.snapshot_id = %s
            """,
            (new, list(positions), list(positions.values()), old),
        )
        return int(new)


def _results(
    api: ApiDatabase, days: tuple[tuple[int, int], ...] = ((23, 24), (24, 24))
) -> dict[tuple[str, str, str, int, int], dict]:
    results = {}
    for lens in ("offense", "defense"):
        for category in sorted(CATEGORIES):
            for population in POPULATIONS:
                for start_day, end_day in days:
                    result = api_analytics.get_army_analytics(
                        api,
                        _selection(
                            lens=lens,
                            category=category,
                            population=population,
                            start_day=start_day,
                            end_day=end_day,
                        ),
                    )
                    assert result is not None
                    key = (lens, category, population, start_day, end_day)
                    results[key] = result
    return results


def _shown(result: dict) -> dict:
    return {field: result[field] for field in SHOWN}


def test_each_day_counts_once_by_its_own_board(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url) as ci:
        day2_start = DAY_START + timedelta(days=1)
        ts1 = DAY_START + timedelta(hours=1)
        ts2 = DAY_START + timedelta(hours=3)
        ts3 = day2_start + timedelta(hours=1)
        reports = (
            ("band-a1", "#2PP", _row(True, "#8PP", FIXTURE_CODE, ts1, 3, 100), ts1),
            ("band-a2", "#2PP", _row(True, "#9PP", PARTIAL_CODE, ts2, 1, 50), ts2),
            ("band-d1", "#8PP", _row(False, "#2PP", DEFENDER_CODE, ts1, 3, 100), ts1),
            ("band-a3", "#2PP", _row(True, "#9PP", FIXTURE_CODE, ts3, 2, 60), ts3),
        )
        jobs = [
            store_observation(
                ci, archive_server, occurrence_key=key, endpoint="battle_log",
                body=json.dumps({"items": [row]}).encode(),
                observed_at=ts + timedelta(minutes=1), normalized_tag=tag,
            )[1]
            for key, tag, row, ts in reports
        ]
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(as_api_role(ci), army_cache_capacity=0)
        cached_api = ApiDatabase(as_api_role(ci))
        fact_queries: list[str] = []
        original_connection = api.pool.connection

        @contextmanager
        def traced_connection(*args, **kwargs):
            with original_connection(*args, **kwargs) as connection:
                class TracedConnection:
                    def execute(self, query, *args, **kwargs):
                        if "army_analytics_battle_facts" in str(query):
                            fact_queries.append(str(query))
                        return connection.execute(query, *args, **kwargs)

                    def __getattr__(self, name):
                        return getattr(connection, name)

                yield TracedConnection()

        def view(population: str, start_day: int = 23, **changes) -> dict:
            result = api_analytics.get_army_analytics(
                api,
                _selection(
                    population=population, start_day=start_day, end_day=24, **changes
                ),
            )
            assert result is not None
            return result

        counted: list[tuple[int, int]] = []
        count = army_rank_bands._count

        def counting(connection, snapshot_id, season_id, day, *rest):
            counted.append((snapshot_id, day))
            count(connection, snapshot_id, season_id, day, *rest)

        monkeypatch.setattr(army_rank_bands, "_count", counting)
        api.pool.connection = traced_connection
        try:
            for index, job in enumerate(jobs):
                assert processor.process_job(job, owner=f"ingest-{index}").outcome in (
                    "processed",
                    "processed_with_gaps",
                )
            with database.pool.connection() as connection:
                battles = {
                    (text(a), text(d), day): int(b)
                    for a, d, day, b in connection.execute(
                        """
                        SELECT atk.normalized_tag, def.normalized_tag,
                               b.ranked_day_start, b.id
                        FROM legend_battles b
                        JOIN players atk ON atk.id = b.attacker_player_id
                        JOIN players def ON def.id = b.defender_player_id
                        """
                    ).fetchall()
                }
                ids = {
                    text(tag): int(player_id)
                    for tag, player_id in connection.execute(
                        "SELECT normalized_tag, id FROM players"
                    ).fetchall()
                }
            first = battles[("#2PP", "#8PP", DAY_START)]
            second = battles[("#2PP", "#9PP", DAY_START)]
            third = battles[("#2PP", "#9PP", day2_start)]
            day_one = [
                _event(first, "offense", ts1, 3, 100, 35),
                _event(second, "offense", ts2, 1, 50, 12),
            ]
            _publish_day(database, "#2PP", day_one)
            _publish_day(database, "#8PP", [_event(first, "defense", ts1, 3, 100, -35)])
            assert processor.process_job(_army_job(database), owner="army-1").outcome == "processed"
            _publish_day(
                database, "#2PP", [_event(third, "offense", ts3, 2, 60, 20)],
                day_start=day2_start, day_number=24,
            )
            assert processor.process_job(_army_job(database), owner="army-2").outcome == "processed"
            # #2PP leads day 23's board and drops to rank 7 on day 24's.
            board_23, board_24 = day2_start, day2_start + timedelta(days=1)
            day_23 = _seed_frozen_snapshot_at(
                database,
                board_23,
                [
                    (ids["#2PP"], 1, "fresh", "confirmed"),
                    (ids["#8PP"], 1500, "fresh", "confirmed"),
                    (ids["#9PP"], 150, "stale", "uncertain"),
                ],
            )
            day_24 = _seed_frozen_snapshot_at(
                database,
                board_24,
                [
                    (ids["#2PP"], 7, "fresh", "confirmed"),
                    (ids["#8PP"], 1500, "fresh", "confirmed"),
                    (ids["#9PP"], 150, "fresh", "confirmed"),
                ],
            )

            # A day the worker has not counted is unavailable, never slow.
            with pytest.raises(ArmyAnalyticsUnavailable) as uncounted:
                view("top-5000")
            assert uncounted.value.affected_days == [23, 24]

            # A count never runs alongside an army day build.
            with database.pool.connection() as build, build.transaction():
                build.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("army-facts",),
                )
                army_rank_bands.refresh_rank_band_totals(database)
            assert counted == []

            # One day a run, newest first, then nothing more to count.
            for _run in range(3):
                army_rank_bands.refresh_rank_band_totals(database)
            assert counted == [(day_24, 24), (day_23, 23)]

            fact_queries.clear()
            saved = _results(api)
            assert fact_queries == []
            # Each day counts by its own board: Top 5 holds #2PP's two day 23
            # attacks but not its day 24 one.
            top5 = saved[("offense", "troops", "top-5", 23, 24)]
            assert top5["total_attacks"] == 2
            assert top5["reproducibility"]["snapshot_versions"] == [1, 1]
            assert saved[("offense", "troops", "top-10", 23, 24)]["total_attacks"] == 3
            assert saved[("offense", "troops", "top-5", 24, 24)]["total_attacks"] == 0
            assert saved[("defense", "troops", "band-1001-2000", 23, 24)][
                "total_attacks"
            ] == 1
            # The players in ranks 101-200 on either day, one of them with an
            # old trophy count at a Reset.
            assert saved[("offense", "troops", "band-101-200", 23, 24)][
                "cohort_evidence"
            ]["cohort_players"] == 1
            assert saved[("offense", "troops", "band-101-200", 23, 24)][
                "cohort_evidence"
            ]["stale_or_uncertain_cohort_members"] == 1
            # The top 10,000 holds every player each day, so it shows what
            # counting every attack shows.
            for lens in ("offense", "defense"):
                for category in sorted(CATEGORIES):
                    every_attack = view(
                        "trophies-0-99999", lens=lens, category=category
                    )
                    assert every_attack["missing_trophy_membership_evidence"] == 0
                    assert _shown(
                        saved[(lens, category, "top-10000", 23, 24)]
                    ) == _shown(every_attack)
            # Every Top N and rank range the page offers loads.
            for population in OFFERED:
                fact_queries.clear()
                view(population)
                assert fact_queries == []
            # Army types count the same players, once the worker has labelled
            # the armies and counted both days.
            for _ in range(3):
                army_types.refresh_army_types(database, now=DAY_START.replace(hour=12))
            assert view("top-5", category="army-types")["total_attacks"] == 2
            assert view("top-10", category="army-types")["total_attacks"] == 3

            # A correction moves #2PP back into day 24's top 5. The counted
            # day keeps its board, and nothing is counted again.
            republished = _republish(database, board_24, {ids["#2PP"]: 1})
            army_rank_bands.refresh_rank_band_totals(database)
            assert len(counted) == 2
            assert view("top-5") == top5
            assert view("top-5", category="army-types")["total_attacks"] == 2

            # Day 23 rebuilt without its partial attack: the page keeps
            # serving the saved totals until the worker counts day 23 again.
            _publish_day_correction(database, "#2PP", day_one[:1])
            assert processor.process_job(_army_job(database), owner="army-3").outcome == "processed"
            fact_queries.clear()
            assert _shown(view("top-5")) == _shown(top5)
            assert fact_queries == []
            top5_days = _selection(population="top-5", start_day=23, end_day=24)
            cached = api_analytics.get_army_analytics(cached_api, top5_days)
            assert cached is not None and cached["total_attacks"] == 2
            army_rank_bands.refresh_rank_band_totals(database)
            assert counted[2:] == [(day_23, 23)]
            assert view("top-5")["total_attacks"] == 1
            # The recount is never answered from what was cached before it.
            recounted = api_analytics.get_army_analytics(cached_api, top5_days)
            assert recounted is not None and recounted["total_attacks"] == 1

            # Totals saved before unnamed ids counted, or counted from another
            # Reset's board before each day used its own, are counted again.
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE army_analytics_rank_band_totals
                    SET totals = totals || '{"cc_unknown": 0, "hero_unknown": {},
                        "unknown": {}, "unknown_present": {}, "rows": {}}'::jsonb
                    WHERE season_day_number = 24
                    """
                )
                connection.execute(
                    """
                    UPDATE army_analytics_rank_band_totals SET snapshot_id = %s
                    WHERE season_day_number = 23
                    """,
                    (day_24,),
                )
            with pytest.raises(ArmyAnalyticsUnavailable):
                view("top-5")
            army_rank_bands.refresh_rank_band_totals(database)
            army_rank_bands.refresh_rank_band_totals(database)
            # A count uses the board version published when it runs, which
            # now has #2PP in day 24's top 5.
            assert counted[3:] == [(republished, 24), (day_23, 23)]
            assert view("top-5")["total_attacks"] == 2

            # A day 24 build moves day 23's defense to day 24. Day 23 is
            # unavailable until counted again, so the battle never counts on
            # both days.
            _publish_day(
                database, "#8PP", [_event(first, "defense", ts1, 3, 100, -35)],
                day_start=day2_start, day_number=24,
            )
            assert processor.process_job(_army_job(database), owner="army-4").outcome == "processed"
            army_rank_bands.refresh_rank_band_totals(database)
            assert counted[5:] == [(republished, 24)]
            with pytest.raises(ArmyAnalyticsUnavailable) as departed:
                view("band-1001-2000", lens="defense")
            assert departed.value.affected_days == [23]
            army_rank_bands.refresh_rank_band_totals(database)
            assert view("band-1001-2000", lens="defense")["total_attacks"] == 1
        finally:
            api.close()
            cached_api.close()
            database.close()
