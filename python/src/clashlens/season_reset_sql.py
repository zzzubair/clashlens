"""Stored-data predicates for a player's first Legend day of the Season."""

from .domain import MAX_BATTLE_TROPHIES, SEASON_START_TROPHIES


def frozen_trophies_sql(player_id: str, reset_at: str) -> str:
    """A player's trophies on the published frozen final board of the day
    that ended at ``reset_at``, or NULL. A NULL ``reset_at`` matches nothing."""
    return f"""(
        SELECT frozen_entry.trophies
        FROM leaderboard_snapshots AS frozen_board
        JOIN leaderboard_snapshot_entries AS frozen_entry
          ON frozen_entry.snapshot_id = frozen_board.id
        WHERE frozen_board.snapshot_kind = 'frozen'
          AND frozen_board.state = 'published'
          AND frozen_board.boundary_at = {reset_at}::timestamptz
          AND frozen_entry.player_id = {player_id}
    )"""


def opening_day_battles_sql(player_id: str, reset_at: str) -> str:
    """A Legend I player's recorded net trophy change and battle count on the
    Legend day starting at ``reset_at``, as a two-item array ([0, 0] with
    none recorded), or NULL for a player outside Legend I."""
    return f"""(
        CASE WHEN (SELECT eligibility_state FROM players
                   WHERE players.id = {player_id}) = 'eligible'
        THEN COALESCE((
            SELECT ARRAY[
                COALESCE(day_log.net_trophy_change,
                         day_log.attack_gain - day_log.defense_loss, 0),
                COALESCE(day_log.attack_count, 0)
                    + COALESCE(day_log.defense_count, 0)
            ]
            FROM api_player_daily_logs AS day_log
            WHERE day_log.player_id = {player_id}
              AND day_log.ranked_day_start = {reset_at}::timestamptz
            ORDER BY day_log.version DESC
            LIMIT 1
        ), ARRAY[0, 0])
        END
    )"""


def season_reset_waiting_sql(player_id: str, trophies: str, reset_at: str) -> str:
    """SQL for the first-Legend-day half of ``awaits_season_reset``: trophies
    other than 5,000 that equal the frozen final board or, for a Legend I
    player, are not explained by that day's recorded battles. A NULL
    ``reset_at`` (any other day) gives false, never NULL."""
    gap = f"({trophies} - {SEASON_START_TROPHIES})"
    return f"""COALESCE((
        SELECT {trophies} <> {SEASON_START_TROPHIES} AND (
            {trophies} = {frozen_trophies_sql(player_id, "opening.reset_at")}
            OR NOT (
                {gap} = day.battles[1]
                OR abs({gap}) <= {MAX_BATTLE_TROPHIES} * day.battles[2]
            )
        )
        FROM (SELECT {reset_at}::timestamptz AS reset_at) AS opening
        CROSS JOIN LATERAL (
            SELECT {opening_day_battles_sql(player_id, "opening.reset_at")}
                AS battles
        ) AS day
        WHERE opening.reset_at IS NOT NULL
    ), false)"""
