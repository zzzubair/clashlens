"""Discord alert switches, saved from the website's Discord alerts page.

Two switches per verified player. Attack alerts are off until turned on; the
Discord bot sends a confirmation and then the alerts, and logs each one; the
website reads that log only to show a DM Discord would not deliver. The
Legend day summary is off until turned on too; each player turned on has a
discord_day_summary_on row, deleted when the player leaves the account, so
moving a player away and back leaves it off. One more switch, for the whole
account, draws the picture at the bottom of each attack alert dark rather
than light; it is off until turned on, and on while the account has a
discord_alert_dark_picture row.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from . import api_accounts, api_db
from .api_db import ApiDatabase, OperationResult, RequestBinding, _text

# Whether the account `alert` names may get the DMs of the player it names:
# the player is linked to it, or it is a site owner (migrations 0138, 0139),
# whose switches for any player api_view_as saves. The bot checks this before
# each DM, so none goes once the link or the owner record is gone.
MAY_SEND = """(
    EXISTS (
        SELECT 1 FROM verified_player_links AS link
        WHERE link.account_id = {alert}.account_id AND link.player_id = {alert}.player_id
    )
    OR EXISTS (
        SELECT 1 FROM site_owner_accounts AS owner
        WHERE owner.account_id = {alert}.account_id
    )
)"""


def get_attack_alerts(database: ApiDatabase, account_id: int) -> dict[str, Any]:
    """Each verified player with its switch, and the newest DM that could not
    be delivered when nothing has been delivered since and no confirmation
    has been asked for since: a switch going on or Discord connected again."""
    with database.pool.connection() as connection:
        players = api_accounts._verified_players(connection, account_id, limit=None)
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
        dark_picture = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM discord_alert_dark_picture WHERE account_id = %s)",
            (account_id,),
        ).fetchone()[0]
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
        "dark_picture": bool(dark_picture),
    }


class Switch(StrEnum):
    """A per-player Discord switch, named by the table that holds it.

    States: OFF, no row for the account and player; ON, a row. An ATTACK
    row also holds enabled_at, after which saved defenses are sent, and
    confirm_requested_at, the confirmation the bot owes for it.

    Events; `flip` and `ask_to_confirm_again` are the only writers outside
    the database's triggers:
      turn on    OFF -> ON, both times now; ON stays ON with its times kept,
                 so turning on twice sends one confirmation
      turn off   ON -> OFF
      Discord connected again (ATTACK only): ON stays ON with
                 confirm_requested_at now and enabled_at kept, so the bot
                 confirms again and no saved defense is skipped
      the player leaving the account (triggers of migrations 0108 and
                 0112), or the site owner record going (0139): ON -> OFF
    Who turns a switch: the account the player is linked to
    (set_attack_alert, set_day_summary), or a site owner for any player
    (api_view_as.set_follow).

    ON alone sends nothing: the bot sends only while `MAY_SEND` holds and
    Discord is connected as the same user. The confirmation's own progress
    (due, being sent, sent, refused, failed, or never sent once the bot's
    15 minutes pass) is the bot's delivery log row whose `confirms` equals
    confirm_requested_at (bot/alerts.py).

    Failure, retry, restart: each write is one transaction under a
    repeat-safe request record, so a failure changes nothing and a repeated
    request gets its first answer back; nothing is held in memory.
    """

    ATTACK = "discord_attack_alerts"
    DAY_SUMMARY = "discord_day_summary_on"


def flip(connection: Any, switch: Switch, account_id: int, player_id: int, *, on: bool) -> None:
    """Turn `switch` on or off for the account and player."""
    if on:
        connection.execute(
            f"INSERT INTO {switch} (account_id, player_id) VALUES (%s, %s)"
            " ON CONFLICT (account_id, player_id) DO NOTHING",
            (account_id, player_id),
        )
    else:
        connection.execute(
            f"DELETE FROM {switch} WHERE account_id = %s AND player_id = %s",
            (account_id, player_id),
        )


def ask_to_confirm_again(connection: Any, account_id: int) -> None:
    """Discord connected again: the bot confirms each attack alert switch
    that is on once more; when each went on is kept."""
    connection.execute(
        f"UPDATE {Switch.ATTACK} SET confirm_requested_at = clock_timestamp()"
        " WHERE account_id = %s",
        (account_id,),
    )


def set_attack_alert(
    database: ApiDatabase, binding: RequestBinding, *, normalized_tag: str, on: bool
) -> OperationResult:
    """Turn one verified player's attack alerts on or off. Turning them on
    again starts from now, so only battles saved after that are sent."""
    return _set_own_switch(database, binding, Switch.ATTACK, normalized_tag, on, "on")


def set_day_summary(
    database: ApiDatabase, binding: RequestBinding, *, normalized_tag: str, on: bool
) -> OperationResult:
    """Turn one verified player's Legend day summary on or off."""
    return _set_own_switch(
        database, binding, Switch.DAY_SUMMARY, normalized_tag, on, "summary_on"
    )


def _set_own_switch(
    database: ApiDatabase,
    binding: RequestBinding,
    switch: Switch,
    normalized_tag: str,
    on: bool,
    answer: str,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            # Locking the ownership row holds off a transfer until this commits,
            # so the migration 0108 or 0112 trigger then forgets the switch.
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
                flip(connection, switch, int(link[1]), int(link[0]), on=on)
                result = OperationResult(200, {"tag": normalized_tag, answer: on})
            api_db._complete_request(connection, binding.request_id, result)
            return result


def set_dark_picture(database: ApiDatabase, binding: RequestBinding, *, on: bool) -> OperationResult:
    """Turn the account's dark attack alert picture on or off."""
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            if on:
                connection.execute(
                    "INSERT INTO discord_alert_dark_picture (account_id) VALUES (%s)"
                    " ON CONFLICT (account_id) DO NOTHING",
                    (binding.account_id,),
                )
            else:
                connection.execute(
                    "DELETE FROM discord_alert_dark_picture WHERE account_id = %s",
                    (binding.account_id,),
                )
            result = OperationResult(200, {"dark_picture": on})
            api_db._complete_request(connection, binding.request_id, result)
            return result
