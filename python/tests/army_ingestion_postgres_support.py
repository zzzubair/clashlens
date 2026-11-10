"""Shared setup for army ingestion postgres tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import _processor, store_observation, text


def _live_row(attack: bool, tag: str, code: str | None, ts: datetime):
    row = {
        "battleType": "legend",
        "attack": attack,
        "battleTime": ts.strftime("%Y%m%dT%H%M%S.000Z"),
        "stars": 3,
        "destructionPercentage": 100,
        "opponentPlayerTag": tag,
        "opponentName": "Opp",
        "opponentTownHallLevel": 17,
    }
    if code is not None:
        row["armyShareCode"] = code
    return row


def _unswept_battles(ci: str, archive_server, players: dict[str, str]):
    """Save one Aug 4 battle per player whose decodes still need saving."""
    ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
    db, proc = _processor(ci, archive_server)
    try:
        for player, code in players.items():
            _, job_id = store_observation(
                ci,
                archive_server,
                occurrence_key=f"unswept-{player}",
                endpoint="battle_log",
                body=json.dumps({"items": [_live_row(True, "#8PP", code, ts)]}).encode(),
                observed_at=ts + timedelta(minutes=1),
                normalized_tag=player,
            )
            assert proc.process_job(job_id, owner="seed").outcome == "processed"
    finally:
        db.close()
    with psycopg.connect(ci) as connection:
        # As if each battle were new, so the next save writes decodes again.
        connection.execute("UPDATE battle_army_decodes SET is_active = false")
        return {
            text(tag): [int(battle_id)]
            for tag, battle_id in connection.execute(
                """
                SELECT player.normalized_tag, battle.id FROM legend_battles AS battle
                JOIN players AS player ON player.id = battle.attacker_player_id
                """
            ).fetchall()
        }
