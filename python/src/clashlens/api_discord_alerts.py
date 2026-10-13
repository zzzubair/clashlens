"""Discord alert switches, saved from the website's Discord alerts page.

Two switches per verified player. Attack alerts are off until turned on; the
Discord bot sends a confirmation and then the alerts, and logs each one; the
website reads that log only to show a DM Discord would not deliver. The
Legend day summary is off until turned on too; each player turned on has a
discord_day_summary_on row, deleted when the player leaves the account, so
moving a player away and back leaves it off.
"""

from __future__ import annotations

from typing import Any

from . import api_db
from .api_accounts import _verified_players
from .api_db import ApiDatabase, OperationResult, RequestBinding, _text


def get_attack_alerts(database: ApiDatabase, account_id: int) -> dict[str, Any]:
    """Each verified player with its switch, and the newest DM that could not
    be delivered when nothing has been delivered since and no confirmation
    has been asked for since: a switch going on or Discord connected again."""
    with database.pool.connection() as connection:
        players = _verified_players(connection, account_id, limit=None)
        summary_on = {
            _text(row[0])
            for row in connection.execute(
                """
                SELECT player.normalized_tag
                FROM discord_day_summary_on AS summary_on
                JOIN players AS player ON player.id = summary_on.player_id
                WHERE summary_on.account_id = %s
                """,
                (account_id,),
            ).fetchall()
        }
        on = {
            _text(row[0])
            for row in connection.execute(
                """
                SELECT player.normalized_tag
                FROM discord_attack_alerts AS alert
                JOIN verified_player_links AS link
                    ON link.player_id = alert.player_id AND link.account_id = alert.account_id
                JOIN players AS player ON player.id = alert.player_id
                WHERE alert.account_id = %s
                """,
                (account_id,),
            ).fetchall()
        }
        newest = connection.execute(
            """
            SELECT delivery.state, delivery.reason, delivery.finished_at,
                   player.normalized_tag
            FROM discord_attack_alert_deliveries AS delivery
            JOIN players AS player ON player.id = delivery.player_id
            WHERE delivery.account_id = %s AND delivery.state <> 'sending'
              AND delivery.created_at >= (
                  SELECT max(confirm_requested_at) FROM discord_attack_alerts
                  WHERE account_id = %s
              )
            ORDER BY delivery.finished_at DESC, delivery.id DESC
            LIMIT 1
            """,
            (account_id, account_id),
        ).fetchone()
    undelivered = None
    if newest is not None and newest[0] in ("refused", "failed"):
        undelivered = {
            "tag": _text(newest[3]),
            "reason": "unreachable" if newest[0] == "failed" else _text(newest[1]),
            "at": newest[2].isoformat(),
        }
    return {
        "kind": "discord-attack-alerts",
        "players": [
            {**player, "on": player["tag"] in on, "summary_on": player["tag"] in summary_on}
            for player in players
        ],
        "undelivered": undelivered,
    }


def set_attack_alert(
    database: ApiDatabase, binding: RequestBinding, *, normalized_tag: str, on: bool
) -> OperationResult:
    """Turn one verified player's attack alerts on or off. Turning them on
    again starts from now, so only battles saved after that are sent."""
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            # Locking the ownership row holds off a transfer until this commits,
            # so the migration 0108 trigger then forgets the switch.
            link = connection.execute(
                """
                SELECT link.player_id, link.account_id
                FROM verified_player_links AS link
                JOIN players AS player ON player.id = link.player_id
                WHERE player.normalized_tag = %s
                FOR SHARE OF link
                """,
                (normalized_tag,),
            ).fetchone()
            if link is None or int(link[1]) != binding.account_id:
                result = OperationResult(404, {"error": "player_not_verified"})
            else:
                if on:
                    connection.execute(
                        """
                        INSERT INTO discord_attack_alerts (account_id, player_id)
                        VALUES (%s, %s)
                        ON CONFLICT (account_id, player_id) DO NOTHING
                        """,
                        (binding.account_id, link[0]),
                    )
                else:
                    connection.execute(
                        "DELETE FROM discord_attack_alerts WHERE account_id = %s AND player_id = %s",
                        (binding.account_id, link[0]),
                    )
                result = OperationResult(200, {"tag": normalized_tag, "on": on})
            api_db._complete_request(connection, binding.request_id, result)
            return result


def set_day_summary(
    database: ApiDatabase, binding: RequestBinding, *, normalized_tag: str, on: bool
) -> OperationResult:
    """Turn one verified player's Legend day summary on or off."""
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            # Locking the ownership row holds off a transfer until this commits,
            # so the migration 0112 trigger then forgets the switch.
            link = connection.execute(
                """
                SELECT link.player_id, link.account_id
                FROM verified_player_links AS link
                JOIN players AS player ON player.id = link.player_id
                WHERE player.normalized_tag = %s
                FOR SHARE OF link
                """,
                (normalized_tag,),
            ).fetchone()
            if link is None or int(link[1]) != binding.account_id:
                result = OperationResult(404, {"error": "player_not_verified"})
            else:
                if on:
                    connection.execute(
                        """
                        INSERT INTO discord_day_summary_on (account_id, player_id)
                        VALUES (%s, %s)
                        ON CONFLICT (account_id, player_id) DO NOTHING
                        """,
                        (binding.account_id, link[0]),
                    )
                else:
                    connection.execute(
                        "DELETE FROM discord_day_summary_on"
                        " WHERE account_id = %s AND player_id = %s",
                        (binding.account_id, link[0]),
                    )
                result = OperationResult(200, {"tag": normalized_tag, "summary_on": on})
            api_db._complete_request(connection, binding.request_id, result)
            return result
