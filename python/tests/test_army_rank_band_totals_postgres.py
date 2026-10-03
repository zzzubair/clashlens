from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta

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

from clashlens import api_analytics, army_rank_bands
from clashlens.api_db import ApiDatabase
from clashlens.army_analytics import CATEGORIES

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


def test_saved_rank_band_totals_serve_the_same_results_without_reading_facts(
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
            # Each player sits in a different rank band of day 24's leaderboard.
            _seed_frozen_snapshot_at(
                database,
                DAY_START + timedelta(days=2),
                [
                    (ids["#2PP"], 1, "fresh", "confirmed"),
                    (ids["#8PP"], 1500, "fresh", "confirmed"),
                    (ids["#9PP"], 150, "fresh", "confirmed"),
                ],
            )

            from_facts = _results(api)
            assert from_facts[("offense", "troops", "top-5", 23, 24)]["total_attacks"] == 3
            assert from_facts[("defense", "troops", "band-1001-2000", 23, 24)][
                "total_attacks"
            ] == 1
            assert from_facts[("defense", "troops", "top-10000", 23, 24)][
                "total_attacks"
            ] == 1
            assert from_facts[("defense", "troops", "top-200", 23, 24)][
                "total_attacks"
            ] == 0

            # A trophy range may start below 5,000 and may hold no attacks.
            wide, empty = (
                api_analytics.get_army_analytics(
                    api, _selection(population=population, start_day=23, end_day=24)
                )
                for population in ("trophies-3000-6100", "trophies-100-200")
            )
            assert wide is not None and wide["total_attacks"] == 3
            assert empty is not None
            assert (empty["total_attacks"], empty["rows"]) == (0, [])

            fact_queries.clear()
            army_rank_bands.refresh_rank_band_totals(database)
            from_totals = _results(api)
            assert fact_queries == []
            assert from_totals == from_facts

            # Totals saved before ranks 1,001-10,000 had bands lack those rows:
            # the next check counts them instead of keeping the old ones.
            with database.pool.connection() as connection:
                connection.execute(
                    "DELETE FROM army_analytics_rank_band_totals"
                    " WHERE first_position > 1000"
                )
            army_rank_bands.refresh_rank_band_totals(database)
            assert _results(api) == from_facts
            assert fact_queries == []

            # Rebuilding day 23 without its partial attack: until the totals are
            # counted again the page reads facts, then the totals take over.
            _publish_day_correction(database, "#2PP", day_one[:1])
            assert processor.process_job(_army_job(database), owner="army-3").outcome == "processed"
            corrected_from_facts = _results(api)
            assert fact_queries
            assert corrected_from_facts[("offense", "troops", "top-5", 23, 24)][
                "total_attacks"
            ] == 2
            fact_queries.clear()
            army_rank_bands.refresh_rank_band_totals(database)
            corrected_from_totals = _results(api)
            assert fact_queries == []
            assert corrected_from_totals == corrected_from_facts
            # A Top N view's evidence hash changes with its own facts.
            key = ("offense", "troops", "top-5", 23, 24)
            assert (
                corrected_from_totals[key]["publication_identity"]
                != from_totals[key]["publication_identity"]
            )

            # Moving a battle to another Legend day also changes the marker of
            # the day it left, so neither cached results nor saved totals for
            # that day are reused. A day 23 leaderboard lets the page show day
            # 23 alone; the second reader keeps the page's result cache.
            _seed_frozen_snapshot_at(
                database,
                day2_start,
                [
                    (ids["#2PP"], 1, "fresh", "confirmed"),
                    (ids["#8PP"], 1500, "fresh", "confirmed"),
                    (ids["#9PP"], 150, "fresh", "confirmed"),
                ],
            )
            single_days = ((23, 23), (24, 24))
            _results(cached_api, single_days)

            # A day 23 correction moves day 24's attack to day 23.
            _publish_day_correction(
                database,
                "#2PP",
                [day_one[0], _event(third, "offense", ts3, 2, 60, 20)],
                version=3,
            )
            assert processor.process_job(_army_job(database), owner="army-4").outcome == "processed"
            moved_back = _results(api, single_days)
            assert moved_back[("offense", "troops", "top-5", 24, 24)][
                "total_attacks"
            ] == 0
            assert moved_back[("offense", "troops", "top-5", 23, 23)][
                "total_attacks"
            ] == 2
            assert _results(cached_api, single_days) == moved_back

            # A day 24 build moves day 23's defense to day 24.
            _publish_day(
                database, "#8PP", [_event(first, "defense", ts1, 3, 100, -35)],
                day_start=day2_start, day_number=24,
            )
            assert processor.process_job(_army_job(database), owner="army-5").outcome == "processed"
            moved_forward = _results(api, single_days)
            assert moved_forward[("defense", "troops", "band-1001-2000", 23, 23)][
                "total_attacks"
            ] == 0
            assert moved_forward[("defense", "troops", "band-1001-2000", 24, 24)][
                "total_attacks"
            ] == 1
            assert _results(cached_api, single_days) == moved_forward

            fact_queries.clear()
            army_rank_bands.refresh_rank_band_totals(database)
            assert _results(api, ((24, 24),)) == {
                key: value for key, value in moved_forward.items() if key[3] == 24
            }
            assert fact_queries == []
        finally:
            api.close()
            cached_api.close()
            database.close()
