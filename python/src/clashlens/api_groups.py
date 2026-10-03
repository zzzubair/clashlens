"""Side-by-side comparison of one private group's players.

One read serves the whole group: the group's members plus the signed-in
account's own verified players, over the same ended Legend days for
everyone. Totals keep their sample sizes so the website never has to guess
how many battles or days a number came from, and a day without a result
stays missing instead of counting as zero.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from . import api_player_lookup
from .api_db import ApiDatabase, _screen_daily_log, _screen_events, _text
from .domain import ranked_day_for

# A comparison is for a group of 10-20 players. Larger groups stay valid lists
# but are refused here, so one request reads a bounded number of players.
MAX_COMPARED_MEMBERS = 20
# The account's own verified players shown alongside the group.
MAX_OWN_PLAYERS = 5
COMPARISON_DAYS = (3, 7, 14)


class GroupTooLarge(Exception):
    def __init__(self, member_count: int) -> None:
        super().__init__("group_too_large")
        self.member_count = member_count


def get_group_comparison(
    database: ApiDatabase,
    account_id: int,
    group_id: str,
    *,
    days: int,
    now: datetime,
    freshness_seconds: int,
) -> dict[str, Any] | None:
    if days not in COMPARISON_DAYS:
        raise ValueError("comparison window is not supported")
    now = now.astimezone(UTC)
    today_start = ranked_day_for(now).start
    day_starts = [today_start - timedelta(days=offset) for offset in range(days, 0, -1)]
    with database.pool.connection() as connection:
        # Filtering by the account as well as the group means a guessed group
        # ID of another account reads exactly what a missing one reads.
        rows = connection.execute(
            """
            SELECT group_row.name, player.id, player.normalized_tag
            FROM account_groups AS group_row
            LEFT JOIN account_group_players AS member ON member.group_id = group_row.id
            LEFT JOIN players AS player ON player.id = member.player_id
            WHERE group_row.public_id = %s AND group_row.account_id = %s
            ORDER BY player.normalized_tag
            LIMIT 101
            """,
            (group_id, account_id),
        ).fetchall()
        if not rows:
            return None
        group_name = _text(rows[0][0])
        members = [(int(row[1]), _text(row[2])) for row in rows if row[1] is not None]
        if len(members) > MAX_COMPARED_MEMBERS:
            raise GroupTooLarge(len(members))
        own = connection.execute(
            """
            SELECT player.id, player.normalized_tag
            FROM verified_player_links AS link
            JOIN players AS player ON player.id = link.player_id
            WHERE link.account_id = %s
            ORDER BY player.normalized_tag
            LIMIT %s
            """,
            (account_id, MAX_OWN_PLAYERS),
        ).fetchall()
        own_ids = {int(row[0]) for row in own}
        players: dict[int, dict[str, Any]] = {}
        for player_id, tag in [*members, *((int(r[0]), _text(r[1])) for r in own)]:
            players.setdefault(player_id, {"tag": tag, "in_group": False})
        for player_id, _tag in members:
            players[player_id]["in_group"] = True
        ids = list(players)
        profiles = {
            int(row[0]): row
            for row in connection.execute(
                """
                SELECT player.id, player.active, profile.name, profile.trophies,
                       player.current_observed_at, player.current_profile_confirmed_at
                FROM players AS player
                LEFT JOIN player_profile_versions AS profile
                    ON profile.id = player.current_profile_version_id
                   AND profile.source_contract_state = 'accepted'
                WHERE player.id = ANY(%s)
                """,
                (ids,),
            ).fetchall()
        }
        logs: dict[tuple[int, datetime], Any] = {}
        for row in connection.execute(
            """
            SELECT DISTINCT ON (player_id, ranked_day_start)
                   player_id, ranked_day_start, state, coverage, confidence,
                   partial_reasons, net_trophy_change, attack_count,
                   defense_count, battles
            FROM api_player_daily_logs
            WHERE player_id = ANY(%s)
              AND ranked_day_start >= %s AND ranked_day_start <= %s
            ORDER BY player_id, ranked_day_start, version DESC
            """,
            (ids, day_starts[0], today_start),
        ).fetchall():
            logs[(int(row[0]), row[1].astimezone(UTC))] = row
        results = []
        for player_id, player in players.items():
            profile = profiles.get(player_id)
            active = profile is not None and bool(profile[1])
            status = (
                "tracking"
                if active
                else api_player_lookup._lookup(connection, player["tag"])["state"]
            )
            results.append(
                {
                    "tag": player["tag"],
                    "you": player_id in own_ids,
                    "in_group": player["in_group"],
                    "status": status,
                    **_current(profile, now, freshness_seconds),
                    **_window(player_id, logs, day_starts, today_start),
                }
            )
    _add_group_difference(results)
    return {
        "kind": "group-comparison",
        "group_id": group_id,
        "name": group_name,
        "days": days,
        "day_starts": [start.isoformat() for start in day_starts],
        "today_start": today_start.isoformat(),
        "generated_at": now.isoformat(),
        "players": results,
    }


def _current(profile: Any, now: datetime, freshness_seconds: int) -> dict[str, Any]:
    if profile is None or profile[3] is None or profile[4] is None:
        return {
            "name": None if profile is None or profile[2] is None else _text(profile[2]),
            "trophies": None,
            "observed_at": None,
            "age_seconds": None,
            "freshness": None,
        }
    observed_at = max(profile[4], profile[5] or profile[4]).astimezone(UTC)
    age_seconds = max(0, int((now - observed_at).total_seconds()))
    return {
        "name": _text(profile[2]),
        "trophies": int(profile[3]),
        "observed_at": observed_at.isoformat(),
        "age_seconds": age_seconds,
        "freshness": "fresh" if age_seconds <= freshness_seconds else "stale",
    }


def _window(
    player_id: int,
    logs: dict[tuple[int, datetime], Any],
    day_starts: list[datetime],
    today_start: datetime,
) -> dict[str, Any]:
    attack = {"count": 0, "stars": 0, "destruction": 0, "three_stars": 0, "trophies": 0}
    defense = {
        "count": 0,
        "stars": 0,
        "destruction": 0,
        "trophies": 0,
        "star_counts": {"0": 0, "1": 0, "2": 0, "3": 0},
    }
    days = []
    net = 0
    counted_days = 0
    for start in day_starts:
        row = logs.get((player_id, start))
        if row is None:
            days.append({"start": start.isoformat(), "state": "missing", "net": None})
            continue
        state = _screen_daily_log(
            {
                "state": _text(row[2]),
                "coverage": _text(row[3]),
                "confidence": None if row[4] is None else _text(row[4]),
                "partial_reasons": list(row[5]) if isinstance(row[5], list) else [],
            },
            "high",
        )["completeness"]["state"]
        # The day that ended at the last Reset can still gain battles saved
        # late, so it is counted but marked as able to change.
        if state == "complete" and start == day_starts[-1]:
            state = "correcting"
        day_net = None if row[6] is None else int(row[6])
        days.append({"start": start.isoformat(), "state": state, "net": day_net})
        if state in {"complete", "correcting"} and day_net is not None:
            net += day_net
            counted_days += 1
        offense_events, defense_events = _screen_events(row[9])
        for event in offense_events:
            attack["count"] += 1
            attack["stars"] += event["stars"]
            attack["destruction"] += event["destruction_percentage"]
            attack["three_stars"] += event["stars"] == 3
            attack["trophies"] += event["trophy_change"]
        for event in defense_events:
            defense["count"] += 1
            defense["stars"] += event["stars"]
            defense["destruction"] += event["destruction_percentage"]
            defense["trophies"] += -event["trophy_change"]
            defense["star_counts"][str(event["stars"])] += 1
    today = logs.get((player_id, today_start))
    return {
        "today": None
        if today is None
        else {
            "net": None if today[6] is None else int(today[6]),
            "attacks": None if today[7] is None else int(today[7]),
            "defenses": None if today[8] is None else int(today[8]),
        },
        "day_results": days,
        "counted_days": counted_days,
        "net": net if counted_days else None,
        "net_per_day": round(net / counted_days, 2) if counted_days else None,
        "attack": attack,
        "defense": defense,
    }


def _add_group_difference(players: list[dict[str, Any]]) -> None:
    """Compare each player's trophies per counted day with everyone else's."""
    for player in players:
        others = [
            other["net_per_day"]
            for other in players
            if other is not player and other["net_per_day"] is not None
        ]
        player["vs_group_per_day"] = (
            None
            if player["net_per_day"] is None or not others
            else round(player["net_per_day"] - sum(others) / len(others), 2)
        )
