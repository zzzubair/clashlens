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
from .api_db import (
    ApiDatabase,
    _battles_so_far_complete,
    _screen_daily_log,
    _screen_events,
    _shown_total,
    _text,
)
from .domain import ranked_day_for, season_is_current
from .season_retirement import retired_day_ranges

# A comparison is for a group of 10-20 players. Saving refuses larger groups;
# older ones saved before that limit are refused here, so one request reads a
# bounded number of players.
MAX_COMPARED_MEMBERS = 20
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
    today = ranked_day_for(now)
    today_start = today.start
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
            """,
            (account_id,),
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
                       player.current_observed_at, player.current_profile_confirmed_at,
                       profile.current_league_season_id
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
                   defense_count, battles, attack_gain, defense_loss
            FROM api_player_daily_logs
            WHERE player_id = ANY(%s)
              AND ranked_day_start >= %s AND ranked_day_start <= %s
            ORDER BY player_id, ranked_day_start, version DESC
            """,
            (ids, day_starts[0], today_start),
        ).fetchall():
            logs[(int(row[0]), row[1].astimezone(UTC))] = row
        # Completed seasons lose their daily detail after cleanup, in batches,
        # so a player's day reads as history no longer kept only when its row
        # is already gone; a row still present is shown as recorded.
        ranges = retired_day_ranges(connection)
        retired = {
            start
            for start in day_starts
            if any(low <= start < high for low, high in ranges)
        }
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
                    **_window(player_id, logs, day_starts, today_start, retired),
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
        "season": today.official_season_id,
        "players": results,
    }


def _current(profile: Any, now: datetime, freshness_seconds: int) -> dict[str, Any]:
    if profile is None or profile[3] is None or profile[4] is None:
        return {
            "name": None
            if profile is None or profile[2] is None
            else _text(profile[2]),
            "trophies": None,
            "season_reset_pending": False,
            "observed_at": None,
            "age_seconds": None,
            "freshness": None,
        }
    observed_at = max(profile[4], profile[5] or profile[4]).astimezone(UTC)
    age_seconds = max(0, int((now - observed_at).total_seconds()))
    pending = not season_is_current(_text(profile[6]), now)
    return {
        "name": _text(profile[2]),
        "trophies": None if pending else int(profile[3]),
        "season_reset_pending": pending,
        "observed_at": observed_at.isoformat(),
        "age_seconds": age_seconds,
        "freshness": "fresh" if age_seconds <= freshness_seconds else "stale",
    }


def _window(
    player_id: int,
    logs: dict[tuple[int, datetime], Any],
    day_starts: list[datetime],
    today_start: datetime,
    retired: set[datetime],
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
    counted_attacks = 0
    for start in day_starts:
        row = logs.get((player_id, start))
        if row is None:
            state = "retired" if start in retired else "missing"
            days.append({"start": start.isoformat(), "state": state, "net": None})
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
        day_net = _shown_total(
            row[6],
            _text(row[3]),
            row[7],
            row[8],
            list(row[5]) if isinstance(row[5], list) else [],
        )
        days.append({"start": start.isoformat(), "state": state, "net": day_net})
        offense_events, defense_events = _screen_events(row[9])
        if state in {"complete", "correcting"} and day_net is not None:
            net += day_net
            counted_days += 1
            counted_attacks += len(offense_events)
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
        "today": None if today is None else _today(today),
        "day_results": days,
        "counted_days": counted_days,
        "counted_attacks": counted_attacks,
        "net": net if counted_days else None,
        "net_per_day": round(net / counted_days, 2) if counted_days else None,
        "attack": attack,
        "defense": defense,
    }


def _today(row: Any) -> dict[str, Any]:
    # Only a complete battle history proves today's net. Recorded totals
    # remain useful evidence when a gap or disputed battle withholds it.
    gained = None if row[10] is None else int(row[10])
    lost = None if row[11] is None else int(row[11])
    attacks = None if row[7] is None else int(row[7])
    defenses = None if row[8] is None else int(row[8])
    offense, defense = _screen_events(row[9])
    complete = _battles_so_far_complete(
        {
            "uncertainty_reasons": list(row[5]) if isinstance(row[5], list) else [],
            "attack_count": attacks,
            "defense_count": defenses,
            "attack_gain": gained,
            "defense_loss": lost,
        },
        offense,
        defense,
    )
    return {
        "net": gained - lost if complete else None,
        "gained": gained,
        "lost": lost,
        "attacks": attacks,
        "defenses": defenses,
    }


def _add_group_difference(players: list[dict[str, Any]]) -> None:
    """Trophies each player gained on every other group member.

    Each pair is compared only on the days both have counted results, then
    averaged across the group members that share at least one such day. The
    account's own players outside the group are never part of the reference.
    """
    counted = [
        {
            day["start"]: day["net"]
            for day in player["day_results"]
            if day["state"] in {"complete", "correcting"} and day["net"] is not None
        }
        for player in players
    ]
    for player, mine in zip(players, counted, strict=True):
        gaps = [
            sum(mine[start] - theirs[start] for start in mine.keys() & theirs.keys())
            for other, theirs in zip(players, counted, strict=True)
            if other is not player and other["in_group"] and mine.keys() & theirs.keys()
        ]
        player["vs_group"] = round(sum(gaps) / len(gaps), 1) if gaps else None
