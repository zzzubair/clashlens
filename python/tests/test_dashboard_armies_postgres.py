"""The dashboard's army cards, from saved battle logs to the private API."""

from __future__ import annotations

import json
from datetime import timedelta

from domain_test_support import as_api_role, domain_database, store_observation, text
from fastapi.testclient import TestClient
from test_api_security import KEY, _signed_headers
from test_api_security import NOW as SIGNED_AT
from test_army_analytics_publication_postgres import (
    DAY_NUMBER,
    DAY_START,
    _army_job,
    _event,
    _processor,
    _publish_day,
    _row,
)

from clashlens import army_types
from clashlens.api import create_app
from clashlens.api_db import ApiDatabase

THROWER = "u9x132-4x7"
HYDRA = "u8x8-6x65"
NEXT_DAY = DAY_START + timedelta(days=1)
# Noon on the day after the two finished days.
NOW = DAY_START + timedelta(days=2, hours=7)


def _read(client: TestClient, tag: str, lens: str, days: int) -> dict:
    target = f"/v1/players/{tag.replace('#', '%23')}/armies?lens={lens}&days={days}"
    response = client.get(target, headers=_signed_headers(target))
    assert response.status_code == 200, response.text
    return response.json()


def _rows(result: dict) -> list[tuple]:
    return [
        (row["name"], row["battles"], row["three_stars"], row["trophies"])
        for row in result["rows"]
    ]


def test_army_cards_from_saved_battles_to_the_api(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url) as ci:
        # Day 23: #8PP triples you with Thrower and #9PP two-stars you with
        # Hydra; you triple #8PP with Thrower; #9PP one-stars #8PP with
        # Thrower. Day 24: #8PP two-stars you with Thrower.
        ts = {
            name: DAY_START + timedelta(hours=hour)
            for name, hour in (("a", 1), ("b", 2), ("c", 3), ("d", 4))
        }
        ts["e"] = NEXT_DAY + timedelta(hours=1)
        battles_seen = [
            ("e1", "#8PP", "#2PP", THROWER, ts["a"], 3, 100),
            ("e2", "#9PP", "#2PP", HYDRA, ts["b"], 2, 80),
            ("e3", "#2PP", "#8PP", THROWER, ts["c"], 3, 100),
            ("e4", "#9PP", "#8PP", THROWER, ts["d"], 1, 50),
            ("e5", "#8PP", "#2PP", THROWER, ts["e"], 2, 90),
        ]
        jobs = []
        for key, attacker, defender, code, at, stars, destruction in battles_seen:
            # Both players' battle logs report each battle.
            for side, tag, opponent in (("a", attacker, defender), ("d", defender, attacker)):
                jobs.append(store_observation(
                    ci, archive_server, occurrence_key=f"{key}{side}",
                    endpoint="battle_log",
                    body=json.dumps({"items": [
                        _row(side == "a", opponent, code, at, stars, destruction)
                    ]}).encode(),
                    observed_at=at + timedelta(minutes=1), normalized_tag=tag,
                )[1])
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(as_api_role(ci))
        try:
            for index, job in enumerate(jobs):
                assert processor.process_job(job, owner=f"ingest-{index}").outcome in (
                    "processed", "processed_with_gaps",
                )
            with database.pool.connection() as connection:
                ids = {
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
            days = {DAY_START: (DAY_NUMBER, []), NEXT_DAY: (DAY_NUMBER + 1, [])}
            for _key, attacker, defender, _code, at, stars, destruction in battles_seen:
                day = DAY_START if at < NEXT_DAY else NEXT_DAY
                battle_id = ids[(attacker, defender, day)]
                days[day][1].extend([
                    (attacker, _event(battle_id, "offense", at, stars, destruction, 0)),
                    (defender, _event(battle_id, "defense", at, stars, destruction, 0)),
                ])
            for day, (number, events) in days.items():
                for tag in ("#2PP", "#8PP", "#9PP"):
                    _publish_day(
                        database, tag, [event for owner, event in events if owner == tag],
                        day_start=day, day_number=number,
                    )
                assert processor.process_job(
                    _army_job(database), owner=f"army-{number}"
                ).outcome == "processed"

            app = create_app(
                api, keys={("typescript-website", "current"): KEY},
                clock=lambda: SIGNED_AT, now=lambda: NOW, dashboard_enabled=True,
            )
            with TestClient(app) as client:
                # Nothing counts until every saved army has its type.
                waiting = _read(client, "#2PP", "defense", 3)
                assert (waiting["covered_days"], waiting["rows"]) == ([], [])

                army_types.refresh_army_types(database, now=NOW)
                # Last 3 days: the day before them has no army records, so 2
                # count. The armies that cost you the most come first:
                # Thrower cost you 40 + 29.
                defense = _read(client, "#2PP", "defense", 3)
                assert defense["covered_days"] == [
                    DAY_START.isoformat(), NEXT_DAY.isoformat()
                ]
                assert _rows(defense) == [("Thrower", 2, 1, 69), ("Hydra", 1, 0, 26)]
                assert defense["total"] == {"battles": 3, "three_stars": 1, "trophies": 95}

                # Last day only.
                assert _rows(_read(client, "#2PP", "defense", 1)) == [("Thrower", 1, 0, 29)]

                # Attacks: your one Thrower triple.
                offense = _read(client, "#2PP", "offense", 3)
                assert _rows(offense) == [("Thrower", 1, 1, 40)]
                assert offense["total"] == {"battles": 1, "three_stars": 1, "trophies": 40}

                target = "/v1/players/%23QQQ/armies?lens=defense&days=3"
                assert client.get(target, headers=_signed_headers(target)).status_code == 404
                for query in ("lens=army&days=3", "lens=defense&days=29", "lens=offense"):
                    target = f"/v1/players/%232PP/armies?{query}"
                    response = client.get(target, headers=_signed_headers(target))
                    assert response.status_code == 422, query
        finally:
            api.close()
            database.close()
