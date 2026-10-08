"""Load Legend II and III players who may be promoted into Legend I.

Production keeps one ``promotion_candidates`` row per such player: the tier
and trophies last seen and when (migration 0076). Profile processing keeps
the rows current. The ``load-promotion-candidates`` command adds the lab's
list once, from a CSV on standard input with the header
``tag,league_tier_id,trophies,checked_at`` and each tag written as ``#TAG``. A tracked player, or one whose
saved profile was checked later than the line, is left out, and a tag already
listed keeps whichever check is newer. Any invalid line refuses the whole file.
It prints what it read (lines, duplicate tags, players by tier), what it left
out or kept, what it added or updated, and the list's size by tier afterwards.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from .profile import normalize_player_tag

CANDIDATE_TIER_IDS = frozenset({105000034, 105000035})  # Legend III, Legend II
COLUMNS = ["tag", "league_tier_id", "trophies", "checked_at"]
_CHUNK = 10_000


def read_candidates(
    lines: Iterable[str],
) -> tuple[list[tuple[str, int, int | None, datetime]], int]:
    """Parse the CSV, keeping each tag's newest check; also returns the lines read."""
    reader = csv.DictReader(lines)
    if reader.fieldnames != COLUMNS:
        raise ValueError(f"header must be {','.join(COLUMNS)}")
    rows: dict[str, tuple[str, int, int | None, datetime]] = {}
    count = 0
    for number, row in enumerate(reader, start=2):
        count += 1
        try:
            tag = normalize_player_tag(row["tag"] or "")
            tier = int(row["league_tier_id"])
            trophies = int(row["trophies"]) if row["trophies"] else None
            checked_at = datetime.fromisoformat(row["checked_at"])
            if (
                tier not in CANDIDATE_TIER_IDS
                or (trophies is not None and trophies < 0)
                or checked_at.utcoffset() is None
            ):
                raise ValueError("tier, trophies or time out of range")
        except (TypeError, ValueError) as error:
            raise ValueError(f"line {number}: {error}") from error
        if tag not in rows or rows[tag][3] < checked_at:
            rows[tag] = (tag, tier, trophies, checked_at)
    return list(rows.values()), count


def load_candidates(
    connection: Any, candidates: list[tuple[str, int, int | None, datetime]]
) -> dict[str, int]:
    """Add or refresh rows; returns how many were left out, added and updated."""
    totals = dict.fromkeys(("skipped_tracked", "skipped_newer_profile", "added", "updated"), 0)
    for start in range(0, len(candidates), _CHUNK):
        tags, tiers, trophies, checked = zip(*candidates[start : start + _CHUNK], strict=True)
        row = connection.execute(
            """
            WITH input AS MATERIALIZED (
                SELECT input.*,
                       EXISTS (SELECT 1 FROM players WHERE players.normalized_tag = input.tag
                                 AND players.active) AS tracked,
                       EXISTS (SELECT 1 FROM players WHERE players.normalized_tag = input.tag
                                 AND GREATEST(players.current_observed_at,
                                              players.current_profile_confirmed_at)
                                     > input.checked) AS newer
                FROM unnest(%s::text[], %s::integer[], %s::integer[], %s::timestamptz[])
                    AS input(tag, tier, trophies, checked)
            ), written AS (
                INSERT INTO promotion_candidates (normalized_tag, league_tier_id, trophies, checked_at)
                SELECT tag, tier, trophies, checked FROM input WHERE NOT tracked AND NOT newer
                ON CONFLICT (normalized_tag) DO UPDATE SET
                    league_tier_id = EXCLUDED.league_tier_id,
                    trophies = EXCLUDED.trophies,
                    checked_at = EXCLUDED.checked_at
                WHERE promotion_candidates.checked_at < EXCLUDED.checked_at
                RETURNING xmax = 0 AS inserted
            )
            SELECT (SELECT count(*) FROM input WHERE tracked),
                   (SELECT count(*) FROM input WHERE newer AND NOT tracked),
                   (SELECT count(*) FROM written WHERE inserted),
                   (SELECT count(*) FROM written WHERE NOT inserted)
            """,
            (list(tags), list(tiers), list(trophies), list(checked)),
        ).fetchone()
        for key, value in zip(totals, row, strict=True):
            totals[key] += int(value)
    return totals


def add_command(
    subparsers: Any, database_argument: Callable[[argparse.ArgumentParser], None]
) -> None:
    """Add the ``load-promotion-candidates`` command to the CLI."""
    command = subparsers.add_parser(
        "load-promotion-candidates",
        help="add Legend II and III players from a CSV on standard input to the Monday promotion list (collector database role)",
    )
    database_argument(command)


def run_command(database_url: str) -> int:
    import psycopg

    try:
        candidates, lines = read_candidates(sys.stdin)
    except ValueError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    with psycopg.connect(database_url) as connection:
        loaded = load_candidates(connection, candidates)
        listed = connection.execute(
            "SELECT count(*) FILTER (WHERE league_tier_id = 105000035),"
            " count(*) FILTER (WHERE league_tier_id = 105000034) FROM promotion_candidates"
        ).fetchone()
    print(
        json.dumps(
            {
                "lines": lines,
                "read": len(candidates),
                "duplicates": lines - len(candidates),
                "read_legend_ii": sum(row[1] == 105000035 for row in candidates),
                "read_legend_iii": sum(row[1] == 105000034 for row in candidates),
                **loaded,
                "added_or_updated": loaded["added"] + loaded["updated"],
                "kept_newer_listed": len(candidates) - sum(loaded.values()),
                "listed_legend_ii": listed[0],
                "listed_legend_iii": listed[1],
            },
            sort_keys=True,
        )
    )
    return 0
