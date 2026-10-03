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

POPULATIONS = ("top-5", "top-10", "top-200", "band-6-10", "band-101-200")


def _results(api: ApiDatabase) -> dict[tuple[str, str, str, int], dict]:
    results = {}
    for lens in ("offense", "defense"):
        for category in sorted(CATEGORIES):
            for population in POPULATIONS:
                for start_day in (23, 24):
                    result = api_analytics.get_army_analytics(
                        api,
                        _selection(
                            lens=lens,
                            category=category,
                            population=population,
                            start_day=start_day,
                            end_day=24,
                        ),
                    )
                    assert result is not None
                    results[(lens, category, population, start_day)] = result
    return results


def _numbers(result: dict) -> dict:
    """Everything the page shows; the evidence hash is derived differently."""
    numbers = json.loads(json.dumps(result))
    del numbers["reproducibility"]["source_evidence_hash"]
    del numbers["publication_identity"]
    return numbers


def test_saved_rank_band_totals_serve_the_same_numbers_without_reading_facts(
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
                    (ids["#8PP"], 7, "fresh", "confirmed"),
                    (ids["#9PP"], 150, "fresh", "confirmed"),
                ],
            )

            from_facts = _results(api)
            assert from_facts[("offense", "troops", "top-5", 23)]["total_attacks"] == 3
            assert from_facts[("defense", "troops", "band-6-10", 23)]["total_attacks"] == 1

            fact_queries.clear()
            army_rank_bands.refresh_rank_band_totals(database)
            from_totals = _results(api)
            assert fact_queries == []
            assert {key: _numbers(value) for key, value in from_totals.items()} == {
                key: _numbers(value) for key, value in from_facts.items()
            }

            # Rebuilding day 23 without its partial attack: until the totals are
            # counted again the page reads facts, then the totals take over.
            _publish_day_correction(database, "#2PP", day_one[:1])
            assert processor.process_job(_army_job(database), owner="army-3").outcome == "processed"
            corrected_from_facts = _results(api)
            assert fact_queries
            assert corrected_from_facts[("offense", "troops", "top-5", 23)][
                "total_attacks"
            ] == 2
            fact_queries.clear()
            army_rank_bands.refresh_rank_band_totals(database)
            corrected_from_totals = _results(api)
            assert fact_queries == []
            assert {
                key: _numbers(value) for key, value in corrected_from_totals.items()
            } == {key: _numbers(value) for key, value in corrected_from_facts.items()}
            # A Top N view's evidence hash changes with its own facts.
            key = ("offense", "troops", "top-5", 23)
            assert (
                corrected_from_totals[key]["publication_identity"]
                != from_totals[key]["publication_identity"]
            )
        finally:
            api.close()
            database.close()
