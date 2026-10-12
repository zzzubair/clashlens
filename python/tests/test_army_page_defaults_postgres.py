"""The Armies page's request, from saved battles to the API reply.

The website sends only the season, player group, category and sort; this
checks that the API's defaults then mean attacks over every finished
Legend day of the current season.
"""

from __future__ import annotations

import json
from datetime import timedelta

from domain_test_support import domain_database, store_observation, text
from fastapi.testclient import TestClient
from test_army_analytics_publication_postgres import (
    DAY_START,
    DEFENDER_CODE,
    FIXTURE_CODE,
    PARTIAL_CODE,
    SEASON_ID,
    _army_job,
    _event,
    _insert_confirmed_anchor,
    _processor,
    _publish_day,
    _row,
    _seed_frozen_snapshot_at,
)
from test_private_api import NOW_SECONDS, TS_CURRENT, signed_headers

from clashlens import army_rank_bands
from clashlens.api import create_app
from clashlens.api_db import ApiDatabase

PAGE_QUERY = "season=current&population=top-100&category=troops&sort=usage-rate"


def test_page_request_reads_attacks_from_every_finished_day(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url) as ci:
        day_one = DAY_START + timedelta(hours=1)
        day_two = DAY_START + timedelta(days=1, hours=1)
        jobs = [
            store_observation(
                ci, archive_server, occurrence_key=key, endpoint="battle_log",
                body=json.dumps({"items": [row]}).encode(),
                observed_at=ts + timedelta(minutes=1), normalized_tag=tag,
            )[1]
            for key, row, ts, tag in (
                ("page-a1", _row(True, "#8PP", FIXTURE_CODE, day_one, 3, 100), day_one, "#2PP"),
                ("page-d1", _row(False, "#2PP", DEFENDER_CODE, day_one, 3, 100), day_one, "#8PP"),
                ("page-a2", _row(True, "#9PP", PARTIAL_CODE, day_two, 1, 50), day_two, "#2PP"),
            )
        ]
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(ci)
        try:
            for index, job in enumerate(jobs):
                processor.process_job(job, owner=f"ingest-{index}")
            with database.pool.connection() as connection:
                battles = {
                    (text(row[0]), text(row[1])): int(row[2])
                    for row in connection.execute(
                        """
                        SELECT atk.normalized_tag, def.normalized_tag, b.id
                        FROM legend_battles b
                        JOIN players atk ON atk.id = b.attacker_player_id
                        JOIN players def ON def.id = b.defender_player_id
                        """
                    ).fetchall()
                }
                players = {
                    text(tag): int(player_id)
                    for tag, player_id in connection.execute(
                        "SELECT normalized_tag, id FROM players"
                    ).fetchall()
                }
            first, second = battles[("#2PP", "#8PP")], battles[("#2PP", "#9PP")]
            # Day 1 holds one attack and the same battle as a defense; Day 2
            # holds one more attack.
            _publish_day(database, "#2PP", [_event(first, "offense", day_one, 3, 100, 35)],
                         day_start=DAY_START, day_number=1)
            _publish_day(database, "#8PP", [_event(first, "defense", day_one, 3, 100, -35)],
                         day_start=DAY_START, day_number=1)
            assert processor.process_job(_army_job(database), owner="analytics-1").outcome == "processed"
            _publish_day(database, "#2PP", [_event(second, "offense", day_two, 1, 50, 12)],
                         day_start=DAY_START + timedelta(days=1), day_number=2)
            assert processor.process_job(_army_job(database), owner="analytics-2").outcome == "processed"
            # Each day's own board, counted by the worker one day a run.
            for day in (1, 2):
                _seed_frozen_snapshot_at(
                    database,
                    DAY_START + timedelta(days=day),
                    [(players["#2PP"], 1, "fresh", "confirmed"),
                     (players["#8PP"], 2, "fresh", "confirmed")],
                )
                army_rank_bands.refresh_rank_band_totals(database)
            _insert_confirmed_anchor(database, SEASON_ID, "1781296800")

            app = create_app(
                database=api,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                # Day 3 is under way, so Days 1 and 2 are the finished days.
                now=lambda: DAY_START + timedelta(days=2, hours=1),
            )
            with TestClient(app) as client:
                target = f"/v1/analytics/armies?{PAGE_QUERY}"
                response = client.get(target, headers=signed_headers(target))
                assert response.status_code == 200, response.text
                payload = response.json()
                assert payload["selection"]["lens"] == "offense"
                assert (payload["selection"]["start_day"], payload["selection"]["end_day"]) == (1, 2)
                assert payload["collection_coverage"]["covered_days"] == [1, 2]
                # Both attacks count; the saved defense does not.
                assert payload["total_attacks"] == 2

                # The defense is still saved and readable on its own.
                defense = f"/v1/analytics/armies?{PAGE_QUERY}&lens=defense"
                saved = client.get(defense, headers=signed_headers(defense)).json()
                assert saved["total_attacks"] == 1
        finally:
            api.close()
            database.close()
