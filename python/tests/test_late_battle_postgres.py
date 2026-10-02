"""A battle fetched late still lands at its real time.

Battle logs are fetched only when a battle can have happened, so a battle that
moves no trophies can be first fetched after the next Reset.
"""

from __future__ import annotations

import json
from datetime import timedelta

import psycopg
from domain_test_support import domain_database, store_observation, text
from test_domain_processing_postgres import (
    LIVE_BATTLE_PARSER_VERSION,
    _live_battle_row,
    _processor,
    _seed_battle_anchor,
)

from clashlens.domain import ranked_day_for


def test_late_fetched_zero_trophy_defense_lands_on_its_own_day_once(
    database_url: str,
    archive_server,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        today = ranked_day_for(now)
        yesterday = ranked_day_for(today.start - timedelta(hours=1))
        _seed_battle_anchor(connection_info, yesterday.start)
        # #2PP attacks #8PP ten minutes before Reset: 0 stars, 49%. The
        # attacker gains trophies; the defender loses none.
        late_defense_at = today.start - timedelta(minutes=10)
        rows = [
            _live_battle_row(
                attack=True,
                battle_timestamp=today.start + timedelta(minutes=5),
                opponent_tag="#9PP",
                opponent_name="Today's Defender",
            ),
            _live_battle_row(
                attack=False,
                battle_timestamp=late_defense_at,
                opponent_tag="#2PP",
                opponent_name="Zero Star Attacker",
                stars=0,
                destruction_percentage=49,
            ),
            _live_battle_row(
                attack=False,
                battle_timestamp=yesterday.start + timedelta(hours=1),
                opponent_tag="#QPP",
                opponent_name="Earlier Attacker",
            ),
        ]
        attacker_row = _live_battle_row(
            attack=True,
            battle_timestamp=late_defense_at,
            opponent_tag="#8PP",
            opponent_name="Late Defender",
            stars=0,
            destruction_percentage=49,
        )
        # The defender's log is first fetched 20 minutes after Reset; the
        # attacker's copy of the same battle arrives later still.
        _defender_observation, defender_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="late-defender-log",
            endpoint="battle_log",
            body=json.dumps({"items": rows}).encode(),
            observed_at=today.start + timedelta(minutes=20),
            normalized_tag="#8PP",
            parser_version=LIVE_BATTLE_PARSER_VERSION,
        )
        _attacker_observation, attacker_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="late-attacker-log",
            endpoint="battle_log",
            body=json.dumps({"items": [attacker_row]}).encode(),
            observed_at=today.start + timedelta(minutes=40),
            normalized_tag="#2PP",
            parser_version=LIVE_BATTLE_PARSER_VERSION,
        )

        database, processor = _processor(connection_info, archive_server)
        try:
            for job, owner in (
                (defender_job, "late-defender"),
                (attacker_job, "late-attacker"),
            ):
                result = processor.process_job(job, owner=owner)
                assert result is not None and result.outcome == "processed"

            with database.pool.connection() as connection:
                battles = connection.execute(
                    """
                    SELECT battle.ranked_day_start, evidence.battle_timestamp,
                           attacker.normalized_tag,
                           (SELECT count(*) FROM battle_perspectives AS p
                            WHERE p.battle_id = battle.id),
                           evidence.stars, evidence.destruction_percentage,
                           evidence.attacker_gain, evidence.defender_loss
                    FROM legend_battles AS battle
                    JOIN players AS attacker
                      ON attacker.id = battle.attacker_player_id
                    JOIN players AS defender
                      ON defender.id = battle.defender_player_id
                    JOIN battle_perspectives AS perspective
                      ON perspective.battle_id = battle.id
                     AND perspective.perspective = 'defender'
                    JOIN battle_evidence AS evidence
                      ON evidence.id = perspective.evidence_id
                    WHERE defender.normalized_tag = '#8PP'
                    ORDER BY evidence.battle_timestamp
                    """
                ).fetchall()
                total = connection.execute(
                    "SELECT count(*) FROM legend_battles"
                ).fetchone()[0]

            assert [(row[0], row[1], text(row[2])) for row in battles] == [
                (yesterday.start, yesterday.start + timedelta(hours=1), "#QPP"),
                (yesterday.start, late_defense_at, "#2PP"),
            ]
            late = battles[1]
            # Both reports of the late battle share one canonical battle.
            assert late[3] == 2
            assert total == 3
            stars, destruction, attacker_gain, defender_loss = late[4:8]
            assert (stars, destruction, defender_loss) == (0, 49, 0)
            assert attacker_gain > 0
        finally:
            database.close()
