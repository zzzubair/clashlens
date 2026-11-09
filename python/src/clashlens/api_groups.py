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
from uuid import uuid4

import psycopg

from . import api_player_lookup, api_requests
from .api_db import ApiDatabase
from .api_player_presentation import (
    battles_so_far_complete,
    calendar_payload,
    screen_daily_log,
    screen_events,
    shown_total,
)
from .api_players import MEMBER_JOINS, group_player, left_legend_i
from .api_requests import OperationResult, RequestBinding
from .db_rows import text_value
from .domain import awaits_season_reset, ranked_day_for, season_opening_reset
from .season_lifecycle import retired_day_ranges
from .season_reset_sql import frozen_trophies_sql, opening_day_battles_sql

# A comparison is for a group of 10-20 players. Saving refuses larger groups;
# older ones saved before that limit are refused here, so one request reads a
# bounded number of players.
MAX_GROUPS = 10
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
        group_name = text_value(rows[0][0])
        members = [(int(row[1]), text_value(row[2])) for row in rows if row[1] is not None]
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
        for player_id, tag in [*members, *((int(r[0]), text_value(r[1])) for r in own)]:
            players.setdefault(player_id, {"tag": tag, "in_group": False})
        for player_id, _tag in members:
            players[player_id]["in_group"] = True
        ids = list(players)
        profiles = {
            int(row[0]): row
            for row in connection.execute(
                f"""
                SELECT player.id, player.active, profile.name, profile.trophies,
                       player.current_observed_at, player.current_profile_confirmed_at,
                       profile.current_league_season_id,
                       {frozen_trophies_sql("player.id", "%s")},
                       {opening_day_battles_sql("player.id", "%s")}
                FROM players AS player
                LEFT JOIN player_profile_versions AS profile
                    ON profile.id = player.current_profile_version_id
                   AND profile.source_contract_state = 'accepted'
                WHERE player.id = ANY(%s)
                """,
                (season_opening_reset(now), season_opening_reset(now), ids),
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
        left = left_legend_i(connection, ids, now)
        results = []
        for player_id, player in players.items():
            profile = profiles.get(player_id)
            active = profile is not None and bool(profile[1])
            status = (
                "tracking"
                if active
                else api_player_lookup.read_lookup(connection, player["tag"])["state"]
            )
            results.append(
                {
                    "tag": player["tag"],
                    "you": player_id in own_ids,
                    "in_group": player["in_group"],
                    "status": status,
                    # This Season's Legend I part, for a player who left it.
                    "left_legend_i": left.get(player_id) if status == "not_in_legend" else None,
                    **_current(profile, now, freshness_seconds),
                    **_window(player_id, logs, day_starts, today_start, retired),
                }
            )
    _add_group_difference(results)
    return {
        "kind": "group-comparison",
        "calendar": calendar_payload(now),
        "limits": group_limits(),
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
            else text_value(profile[2]),
            "trophies": None,
            "season_reset_pending": False,
            "observed_at": None,
            "age_seconds": None,
            "freshness": None,
        }
    observed_at = max(profile[4], profile[5] or profile[4]).astimezone(UTC)
    age_seconds = max(0, int((now - observed_at).total_seconds()))
    pending = awaits_season_reset(
        text_value(profile[6]), int(profile[3]), profile[7], profile[8], now
    )
    return {
        "name": text_value(profile[2]),
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
    attack = {
        "count": 0,
        "stars": 0,
        "destruction": 0,
        "three_stars": 0,
        "trophies": 0,
        "days": 0,
    }
    defense = {
        "count": 0,
        "stars": 0,
        "destruction": 0,
        "trophies": 0,
        "star_counts": {"0": 0, "1": 0, "2": 0, "3": 0},
        "days": 0,
    }
    days = []
    net = 0
    counted_days = 0
    counted_attacks = 0
    played_net = 0
    played_days = 0
    seen: set[str] = set()
    for start in day_starts:
        row = logs.get((player_id, start))
        if row is None:
            state = "retired" if start in retired else "missing"
            days.append({"start": start.isoformat(), "state": state, "net": None})
            continue
        state = screen_daily_log(
            {
                "state": text_value(row[2]),
                "coverage": text_value(row[3]),
                "confidence": None if row[4] is None else text_value(row[4]),
                "partial_reasons": list(row[5]) if isinstance(row[5], list) else [],
            },
            "high",
        )["completeness"]["state"]
        # The day that ended at the last Reset can still gain battles saved
        # late, so it is counted but marked as able to change.
        if state == "complete" and start == day_starts[-1]:
            state = "correcting"
        day_net = shown_total(
            row[6],
            text_value(row[3]),
            row[7],
            row[8],
            list(row[5]) if isinstance(row[5], list) else [],
        )
        days.append({"start": start.isoformat(), "state": state, "net": day_net})
        offense_events, defense_events = (
            _unseen(events, seen) for events in screen_events(row[9])
        )
        if state in {"complete", "correcting"} and day_net is not None:
            net += day_net
            counted_days += 1
            counted_attacks += len(offense_events)
            if offense_events or defense_events:
                played_net += day_net
                played_days += 1
        attack["days"] += bool(offense_events)
        defense["days"] += bool(defense_events)
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
        "net_per_day": round(played_net / played_days, 2) if played_days else None,
        "attack": attack,
        "defense": defense,
    }


def _unseen(events: list[dict[str, Any]], seen: set[str]) -> list[dict[str, Any]]:
    """The events whose battle is not in ``seen``, each once, adding them to it:
    a battle saved on more than one day counts on the first."""
    fresh = []
    for event in events:
        if event["battle_id"] not in seen:
            seen.add(event["battle_id"])
            fresh.append(event)
    return fresh


def _today(row: Any) -> dict[str, Any]:
    # Only a complete battle history proves today's net. Recorded totals
    # remain useful evidence when a gap or disputed battle withholds it.
    gained = None if row[10] is None else int(row[10])
    lost = None if row[11] is None else int(row[11])
    attacks = None if row[7] is None else int(row[7])
    defenses = None if row[8] is None else int(row[8])
    offense, defense = screen_events(row[9])
    complete = battles_so_far_complete(
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


# Saving starts at most the comparison limit of new player checks.
GROUP_LOOKUP_LIMIT = MAX_COMPARED_MEMBERS

def create_group(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    name: str,
    normalized_name: str,
    normalized_tags: list[str],
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_requests.reserve_request(connection, binding)
            if existing is not None:
                return existing
            public_id = uuid4()
            try:
                with connection.transaction():
                    group = connection.execute(
                        """
                        INSERT INTO account_groups (
                            public_id, account_id, name, normalized_name
                        ) VALUES (%s, %s, %s, %s)
                        RETURNING id
                        """,
                        (public_id, binding.account_id, name, normalized_name),
                    ).fetchone()
                    assert group is not None
                    _replace_group_players(connection, int(group[0]), normalized_tags)
            except psycopg.errors.UniqueViolation:
                result = OperationResult(409, {"error": "group_name_conflict"})
                api_requests.complete_request(connection, binding.request_id, result)
                return result
            except psycopg.errors.CheckViolation as error:
                if error.diag.constraint_name != "account_groups_limit":
                    raise
                result = OperationResult(422, {"error": "group_limit_reached"})
                api_requests.complete_request(connection, binding.request_id, result)
                return result
            result = OperationResult(
                201,
                {
                    "group_id": str(public_id),
                    "name": name,
                    "tags": sorted(set(normalized_tags)),
                },
            )
            api_requests.complete_request(connection, binding.request_id, result)
            return result


def update_group(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    group_id: str,
    name: str,
    normalized_name: str,
    normalized_tags: list[str] | None,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_requests.reserve_request(connection, binding)
            if existing is not None:
                return existing
            try:
                with connection.transaction():
                    group = connection.execute(
                        """
                        UPDATE account_groups
                        SET name = %s, normalized_name = %s,
                            updated_at = clock_timestamp()
                        WHERE public_id = %s AND account_id = %s
                        RETURNING id
                        """,
                        (name, normalized_name, group_id, binding.account_id),
                    ).fetchone()
                    if group is not None and normalized_tags is not None:
                        _replace_group_players(connection, int(group[0]), normalized_tags)
            except psycopg.errors.UniqueViolation:
                result = OperationResult(409, {"error": "group_name_conflict"})
                api_requests.complete_request(connection, binding.request_id, result)
                return result
            if group is None:
                result = OperationResult(404, {"error": "group_not_found"})
            else:
                result = OperationResult(
                    200,
                    {
                        "group_id": group_id,
                        "name": name,
                        "tags": sorted(set(normalized_tags))
                        if normalized_tags is not None
                        else [player["tag"] for player in _group_players(
                            connection, int(group[0])
                        )],
                    },
                )
            api_requests.complete_request(connection, binding.request_id, result)
            return result


def delete_group(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    group_id: str,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_requests.reserve_request(connection, binding)
            if existing is not None:
                return existing
            deleted = connection.execute(
                """
                DELETE FROM account_groups
                WHERE public_id = %s AND account_id = %s
                """,
                (group_id, binding.account_id),
            )
            if deleted.rowcount == 1:
                result = OperationResult(
                    200, {"deleted": True, "group_id": group_id}
                )
            else:
                result = OperationResult(404, {"error": "group_not_found"})
            api_requests.complete_request(connection, binding.request_id, result)
            return result


def add_group_player(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    group_id: str,
    normalized_tag: str,
    now: datetime | None = None,
) -> OperationResult:
    """Add one player the game has already confirmed exists."""
    now = now or datetime.now(UTC)
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_requests.reserve_request(connection, binding)
            if existing is not None:
                return existing
            group = _lock_group(connection, binding, group_id)
            if group is None:
                result = OperationResult(404, {"error": "group_not_found"})
            else:
                # Same order as saving a whole group: the group row, then the tag.
                api_player_lookup.lock_tag(connection, normalized_tag)
                members = [
                    player["tag"]
                    for player in _group_players(connection, group, now=now)
                ]
                state = api_player_lookup.read_lookup(connection, normalized_tag)["state"]
                if normalized_tag in members:
                    result = OperationResult(409, {"error": "group_player_exists"})
                elif len(members) >= MAX_COMPARED_MEMBERS:
                    result = OperationResult(422, {"error": "group_full"})
                elif state == api_player_lookup.LookupState.NOT_FOUND:
                    result = OperationResult(422, {"error": "player_not_found"})
                elif state not in api_player_lookup.CONFIRMED:
                    # Unchecked, still checking or a failed check: the caller
                    # starts or waits for the player lookup and tries again.
                    result = OperationResult(
                        409, {"error": "player_not_checked", "state": state}
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO account_group_players (group_id, player_id)
                        SELECT %s, id FROM players WHERE normalized_tag = %s
                        """,
                        (group, normalized_tag),
                    )
                    [player] = [
                        player
                        for player in _group_players(connection, group, now=now)
                        if player["tag"] == normalized_tag
                    ]
                    result = OperationResult(200, {"group_id": group_id, **player})
            api_requests.complete_request(connection, binding.request_id, result)
            return result


def remove_group_player(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    group_id: str,
    normalized_tag: str,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_requests.reserve_request(connection, binding)
            if existing is not None:
                return existing
            group = _lock_group(connection, binding, group_id)
            if group is None:
                result = OperationResult(404, {"error": "group_not_found"})
            else:
                removed = connection.execute(
                    """
                    DELETE FROM account_group_players AS member
                    USING players AS player
                    WHERE member.group_id = %s
                      AND member.player_id = player.id
                      AND player.normalized_tag = %s
                    """,
                    (group, normalized_tag),
                )
                result = OperationResult(
                    200,
                    {
                        "group_id": group_id,
                        "tag": normalized_tag,
                        "removed": removed.rowcount == 1,
                    },
                )
            api_requests.complete_request(connection, binding.request_id, result)
            return result


def list_groups(
    database: ApiDatabase, account_id: int, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    with database.pool.connection() as connection:
        rows = connection.execute(
            f"""
            SELECT group_row.public_id, group_row.name, player.normalized_tag,
                   player.active, COALESCE(accepted.name, latest.name),
                   accepted.trophies, accepted.current_league_season_id,
                   {frozen_trophies_sql("player.id", "%s")},
                   {opening_day_battles_sql("player.id", "%s")},
                   group_row.created_at
            FROM account_groups AS group_row
            LEFT JOIN account_group_players AS member ON member.group_id = group_row.id
            {MEMBER_JOINS}
            WHERE group_row.account_id = %s
            ORDER BY group_row.normalized_name, group_row.public_id, player.normalized_tag
            LIMIT 10000
            """,
            (season_opening_reset(now), season_opening_reset(now), account_id),
        ).fetchall()
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            public_id = str(row[0])
            # The dashboard shows the first group made, so each says when.
            group = groups.setdefault(
                public_id,
                {
                    "group_id": public_id,
                    "name": text_value(row[1]),
                    "created_at": row[-1].astimezone(UTC).isoformat(),
                    "tags": [],
                    "players": [],
                },
            )
            if row[2] is not None:
                player = group_player(connection, row[2:-1], now)
                group["tags"].append(player["tag"])
                group["players"].append(player)
        return list(groups.values())


def _replace_group_players(
    connection: Any,
    group_id: int,
    normalized_tags: list[str],
) -> None:
    tags = sorted(set(normalized_tags))
    # A player lookup takes the tag lock before touching the player row, so
    # take every tag lock first, in one order, before inserting players.
    for normalized_tag in tags:
        api_player_lookup.lock_tag(connection, normalized_tag)
    connection.execute(
        "DELETE FROM account_group_players WHERE group_id = %s",
        (group_id,),
    )
    for normalized_tag in tags:
        player_id = api_player_lookup.ensure_player(connection, normalized_tag)
        connection.execute(
            """
            INSERT INTO account_group_players (group_id, player_id)
            VALUES (%s, %s)
            """,
            (group_id, player_id),
        )
    # A tag Clash Lens has no profile for starts the same check a player page
    # visit starts, so the group shows it as being checked, not inactive.
    unchecked = connection.execute(
        """
        SELECT player.normalized_tag
        FROM account_group_players AS member
        JOIN players AS player ON player.id = member.player_id
        WHERE member.group_id = %s AND NOT player.active
          AND player.current_profile_version_id IS NULL
        ORDER BY player.normalized_tag
        LIMIT %s
        """,
        (group_id, GROUP_LOOKUP_LIMIT),
    ).fetchall()
    for (normalized_tag,) in unchecked:
        api_player_lookup.admit(connection, text_value(normalized_tag))


def _lock_group(connection: Any, binding: RequestBinding, group_id: str) -> int | None:
    # Filtering by the account means another account's group reads as missing.
    row = connection.execute(
        """
        SELECT id FROM account_groups
        WHERE public_id = %s AND account_id = %s
        FOR UPDATE
        """,
        (group_id, binding.account_id),
    ).fetchone()
    return None if row is None else int(row[0])


def _group_players(
    connection: Any, group_id: int, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Each member's tag with the stored in-game name and trophies, when known."""
    now = now or datetime.now(UTC)
    rows = connection.execute(
        f"""
        SELECT player.normalized_tag, player.active,
               COALESCE(accepted.name, latest.name), accepted.trophies,
               accepted.current_league_season_id,
               {frozen_trophies_sql("player.id", "%s")},
               {opening_day_battles_sql("player.id", "%s")}
        FROM account_group_players AS member
        {MEMBER_JOINS}
        WHERE member.group_id = %s
        ORDER BY player.normalized_tag
        LIMIT 101
        """,
        (season_opening_reset(now), season_opening_reset(now), group_id),
    ).fetchall()
    return [group_player(connection, row, now) for row in rows]


def group_limits() -> dict[str, Any]:
    return {"max_groups": MAX_GROUPS, "max_players": MAX_COMPARED_MEMBERS,
            "comparison_days": list(COMPARISON_DAYS)}
