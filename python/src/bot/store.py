"""Clash Lens data for the bot, read with the private API's own code.

The bot connects as the API's database role and calls the same reads the
website's pages use, so a number in Discord matches the number on the site.
Its only writes are each account's main player. Every read takes the moment
of the command, so one reply never mixes two Legend days.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from clashlens import (
    api_accounts,
    api_groups,
    api_leaderboard,
    api_player_lookup,
    api_players,
)
from clashlens.api_db import (
    AccountContext,
    ApiDatabase,
    _daily_log,
    _screen_daily_log_with_events,
)
from clashlens.domain import SEASON_START_TROPHIES, ranked_day_for

# The website's own freshness limit for a saved player profile.
FRESHNESS_SECONDS = 900


def _linked_players(
    connection: Any, account_id: int
) -> list[tuple[int, str, str | None, str | None, str | None]]:
    """Every verified player's id, tag, and newest saved name, clan and league:
    the website's own read, without its 500-player cutoff, so the bot lists
    them all."""
    rows = connection.execute(
        """
        SELECT player.id, player.normalized_tag, profile.name, profile.clan,
               profile.league
        FROM verified_player_links AS link
        JOIN players AS player ON player.id = link.player_id
        LEFT JOIN LATERAL (
            SELECT version.name, version.profile_json -> 'clan' ->> 'name' AS clan,
                   CASE WHEN version.eligibility_state <> 'uncertain'
                        THEN version.league_tier_name END AS league,
                   version.player_id
            FROM player_profile_versions AS version
            CROSS JOIN LATERAL (
                SELECT max(observed_at) AS observed_at FROM player_profile_effects
                WHERE profile_version_id = version.id
            ) AS effect
            WHERE version.normalized_tag = player.normalized_tag
            ORDER BY COALESCE(effect.observed_at, version.observed_at) DESC,
                     version.id DESC
            LIMIT 1
        ) AS profile ON profile.player_id = player.id
        WHERE link.account_id = %s
        ORDER BY player.normalized_tag
        """,
        (account_id,),
    ).fetchall()
    return [
        (
            int(row[0]),
            str(row[1]),
            None if row[2] is None else str(row[2]),
            None if row[3] is None else str(row[3]),
            None if row[4] is None else str(row[4]),
        )
        for row in rows
    ]


def _today(connection: Any, tag: str, now: datetime) -> dict[str, Any] | None:
    """Today's attacks, defenses and net so far, by player_cards' own rule,
    for a player whose card it left without a day."""
    row = connection.execute(
        """
        SELECT ranked_day_start, ranked_day_end, official_season_id,
               season_day_number, version, state, coverage, confidence,
               attack_count, attack_three_star_count, attack_gain,
               defense_count, defense_three_star_count, defense_loss,
               net_trophy_change, adjustments, battles, partial_reasons, NULL, NULL
        FROM api_player_daily_logs
        WHERE player_id = (SELECT id FROM players WHERE normalized_tag = %s)
          AND ranked_day_start = %s
        ORDER BY version DESC
        LIMIT 1
        """,
        (tag, ranked_day_for(now).start),
    ).fetchone()
    if row is None:
        return None
    day = _screen_daily_log_with_events(_daily_log(row), "high", now)
    return {
        "net": day["attack_gain"] - day["defense_loss"] if day["battles_complete"] else None,
        "attacks": day["attack_count"],
        "defenses": day["defense_count"],
    }


class _OneConnection:
    """The API database with every read on one open connection, so the
    website's own page read shares that connection's snapshot."""

    def __init__(self, database: ApiDatabase, connection: Any) -> None:
        self._database = database
        self._connection = connection
        self.pool = self

    @contextmanager
    def connection(self) -> Iterator[Any]:
        yield self._connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._database, name)


class Store:
    def __init__(self, database: ApiDatabase) -> None:
        self.database = database

    def account(self, discord_id: str) -> AccountContext | None:
        """The Clash Lens account this Discord user connected, if any."""
        return api_accounts.resolve_account(self.database, "discord", discord_id)

    def players(self, account: AccountContext, now: datetime) -> list[dict[str, Any]]:
        """Each verified player's card, read with the website's own code, and
        when its saved profile was last seen, both from one database snapshot
        so the time always belongs to the numbers. A player who has not played
        yet this Season shows the profile that ranks them at 5,000, as their
        page and the Live Leaderboard do."""
        with self.database.pool.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cards = api_players.player_cards(
                connection,
                _linked_players(connection, account.internal_id),
                now=now,
            )
            seen = dict(
                connection.execute(
                    """
                    SELECT normalized_tag,
                           GREATEST(current_observed_at, current_profile_confirmed_at)
                    FROM players
                    WHERE normalized_tag = ANY(%s)
                    """,
                    ([card["tag"] for card in cards],),
                ).fetchall()
            )
            for card in cards:
                if (
                    card["state"] != "tracking"
                    or card["reason"] not in (None, "pending")
                    or card["trophies"] not in (None, SEASON_START_TROPHIES)
                ):
                    continue
                unplayed = connection.execute(
                    api_leaderboard.UNPLAYED_PROFILE_SQL,
                    {**api_leaderboard._season_params(now), "tag": card["tag"]},
                ).fetchone()
                if unplayed is None:
                    continue
                seen[card["tag"]] = unplayed[1]
                if card["trophies"] is None:
                    card.update(
                        reason=None,
                        trophies=SEASON_START_TROPHIES,
                        season_reset_pending=False,
                        today=_today(connection, card["tag"], now),
                    )
        for card in cards:
            card["observed_at"] = seen.get(card["tag"])
        return cards

    def main_tag(self, account: AccountContext) -> str | None:
        """The saved main; one no longer verified to this account is forgotten."""
        with self.database.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    DELETE FROM discord_bot_main_players AS main
                    WHERE main.account_id = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM verified_player_links AS link
                          WHERE link.player_id = main.player_id
                            AND link.account_id = main.account_id
                      )
                    """,
                    (account.internal_id,),
                )
                row = connection.execute(
                    """
                    SELECT player.normalized_tag
                    FROM discord_bot_main_players AS main
                    JOIN players AS player ON player.id = main.player_id
                    WHERE main.account_id = %s
                    """,
                    (account.internal_id,),
                ).fetchone()
        return None if row is None else str(row[0])

    def set_main(self, account: AccountContext, tag: str) -> bool:
        """Save `tag` as the main; False when it is not this account's player."""
        with self.database.pool.connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    INSERT INTO discord_bot_main_players (account_id, player_id)
                    SELECT link.account_id, link.player_id
                    FROM verified_player_links AS link
                    JOIN players AS player ON player.id = link.player_id
                    WHERE link.account_id = %s AND player.normalized_tag = %s
                    ON CONFLICT (account_id) DO UPDATE
                        SET player_id = EXCLUDED.player_id,
                            updated_at = clock_timestamp()
                    RETURNING account_id
                    """,
                    (account.internal_id, tag),
                ).fetchone()
        return row is not None

    def player_page(self, tag: str, now: datetime) -> dict[str, Any] | None:
        """The website's player page with the player's lookup state, reason
        and live board position, all from one read-only database snapshot.
        A known player with no page gets only their lookup state; None when
        Clash Lens has never heard of the tag."""
        with self.database.pool.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            page = api_players.get_player_page(
                _OneConnection(self.database, connection),  # type: ignore[arg-type]
                tag,
                now=now,
                freshness_seconds=FRESHNESS_SECONDS,
            )
            lookup = api_player_lookup._lookup(connection, tag)
            if page is None:
                if lookup["state"] == "unknown":
                    return None
                profile = lookup.get("profile") or {}
                return {
                    "tag": tag,
                    "name": profile.get("name"),
                    "clan": profile.get("clan"),
                    "trophies": None,
                    "season_reset_pending": False,
                    "observed_at": None,
                    "state": lookup["state"],
                    "reason": lookup.get("reason"),
                    "rank": None,
                }
            rank = api_leaderboard.live_positions(connection, [tag], now=now).get(tag)
        return {
            **page,
            "observed_at": datetime.fromisoformat(page["observed_at"]),
            "state": lookup["state"],
            "reason": lookup.get("reason"),
            "rank": rank,
        }

    def search(self, query: str, now: datetime) -> list[dict[str, Any]]:
        """Known players whose name matches, exact name first, as the website's
        search, each named by the profile their page shows: for a player who
        has not played yet this Season, the profile that ranks them at 5,000.
        Those players are found from the worker's saved list of them, as
        finding them all live took 17 s on 11 October 2026."""
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params = {**api_leaderboard._season_params(now), "pattern": f"%{escaped}%"}
        exact = query.casefold()
        website = {
            item["tag"]: item
            for item in api_players.search_known_players(
                self.database, query, now=now, freshness_seconds=FRESHNESS_SECONDS, limit=25
            )
        }
        found: list[dict[str, Any]] = []
        with self.database.pool.connection() as connection:
            saved = connection.execute(
                """
                SELECT player.normalized_tag, saved.name
                FROM live_unplayed_profiles AS saved
                JOIN players AS player ON player.id = saved.player_id
                WHERE saved.official_season_id = %(season_id)s
                  AND saved.name ILIKE %(pattern)s ESCAPE '\\'
                ORDER BY lower(saved.name), player.normalized_tag
                """,
                params,
            ).fetchall()
            candidates = sorted(
                [(item["tag"], item["name"]) for item in website.values()] + saved,
                key=lambda candidate: (candidate[1] or "").casefold() != exact,
            )
            for tag in dict.fromkeys(tag for tag, _name in candidates):
                if len(found) == 25:
                    break
                unplayed = connection.execute(
                    api_leaderboard.UNPLAYED_PROFILE_SQL, {**params, "tag": tag}
                ).fetchone()
                if unplayed is None:
                    if tag in website:
                        found.append(website[tag])
                    continue
                name, clan, matches = connection.execute(
                    """
                    SELECT name, profile_json -> 'clan' ->> 'name',
                           name ILIKE %(pattern)s ESCAPE '\\'
                    FROM player_profile_versions
                    WHERE id = %(id)s
                    """,
                    {**params, "id": unplayed[0]},
                ).fetchone()
                if matches:
                    found.append(
                        {"tag": tag, "name": name, "clan": clan, "trophies": SEASON_START_TROPHIES}
                    )
        found.sort(key=lambda item: (item["name"] or "").casefold() != exact)
        return found

    def board(self, now: datetime, focus_tag: str | None = None) -> dict[str, Any] | None:
        """The Live Leaderboard's top 10, or with `focus_tag` that player and
        the 5 players either side; None when the player is not on the board."""
        if focus_tag is None:
            return api_leaderboard.get_live_leaderboard(self.database, limit=10, now=now)
        return api_leaderboard.get_live_leaderboard(
            self.database, limit=1, now=now, focus_tag=focus_tag
        )

    def groups(self, account: AccountContext, now: datetime) -> list[dict[str, Any]]:
        return api_accounts.list_groups(self.database, account.internal_id, now=now)

    def group(
        self, account: AccountContext, group_id: str, days: int, now: datetime
    ) -> dict[str, Any] | None:
        """One of the account's groups side by side; None when the account has
        no such group. Raises api_groups.GroupTooLarge past 20 members."""
        return api_groups.get_group_comparison(
            self.database,
            account.internal_id,
            group_id,
            days=days,
            now=now,
            freshness_seconds=FRESHNESS_SECONDS,
        )
