"""The Notebook's daily meta table: each counted Legend day's defenses by the
attacker's army type and the defender's Reset trophies, in 10-trophy steps,
for the Tomorrow screen (notebook_tomorrow.py).

It reads the day's defense counts (army_package_defense_day_counts, which
army_types.py counts one day at a time) with each defender's Reset trophies
from their newest published day. Unreadable and older partly read armies
are left out. The worker saves one day per run, after the army counts, for
a day recounted or relabelled since it was saved, and keeps the last
KEEP_DAYS days.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

KEEP_DAYS = 28


def refresh_day(connection: Any, rules_version: str) -> dict[str, Any] | None:
    """Save one counted day due a meta count, newest first, in the caller's
    transaction; None when none is due."""
    newest = connection.execute(
        "SELECT max(ranked_day_start) FROM army_type_defense_counted_days"
    ).fetchone()[0]
    if newest is None:
        return None
    oldest = newest - timedelta(days=KEEP_DAYS - 1)
    connection.execute(
        "DELETE FROM notebook_meta_counts WHERE ranked_day_start < %s", (oldest,))
    connection.execute(
        "DELETE FROM notebook_meta_days WHERE ranked_day_start < %s", (oldest,))
    due = connection.execute(
        """
        SELECT counted.ranked_day_start, counted.fact_input_hash
        FROM army_type_defense_counted_days AS counted
        LEFT JOIN notebook_meta_days AS saved
          ON saved.ranked_day_start = counted.ranked_day_start
        WHERE counted.ranked_day_start >= %s
          AND (saved.ranked_day_start IS NULL
               OR saved.fact_input_hash <> counted.fact_input_hash
               OR saved.rules_version <> %s)
        ORDER BY counted.ranked_day_start DESC LIMIT 1
        """,
        (oldest, rules_version),
    ).fetchone()
    if due is None:
        return None
    day, marker = due
    connection.execute("DELETE FROM notebook_meta_counts WHERE ranked_day_start = %s", (day,))
    saved = connection.execute(
        """
        WITH starts AS (
            SELECT DISTINCT ON (daily.player_id)
                   daily.player_id, ranked_day.start_trophies
            FROM api_player_daily_logs AS daily
            JOIN ranked_day_versions AS ranked_day
              ON ranked_day.id = daily.ranked_day_version_id
            WHERE daily.ranked_day_start = %(day)s
            ORDER BY daily.player_id, daily.version DESC
        )
        INSERT INTO notebook_meta_counts (
            ranked_day_start, type_id, step, defenses, three_star, trophies_lost)
        SELECT %(day)s, label.type_id, starts.start_trophies / 10,
               sum(saved.defenses), sum(saved.three_star), sum(saved.trophies_lost)
        FROM army_package_defense_day_counts AS saved
        JOIN starts ON starts.player_id = saved.population_player_id
        JOIN army_package_types AS label ON label.exact_army_id = saved.exact_army_id
        WHERE saved.ranked_day_start = %(day)s AND starts.start_trophies IS NOT NULL
        GROUP BY 2, 3
        """,
        {"day": day},
    ).rowcount
    connection.execute(
        """
        INSERT INTO notebook_meta_days (ranked_day_start, fact_input_hash, rules_version)
        VALUES (%s, %s, %s)
        ON CONFLICT (ranked_day_start) DO UPDATE
        SET fact_input_hash = EXCLUDED.fact_input_hash,
            rules_version = EXCLUDED.rules_version
        """,
        (day, marker, rules_version),
    )
    return {"status": "notebook_meta", "ranked_day_start": day.isoformat(), "rows": saved}
