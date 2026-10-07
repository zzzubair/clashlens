"""Load Legend II and III players who may be promoted into Legend I.

Production keeps one ``promotion_candidates`` row per such player: the tier
and trophies last seen and when (migration 0076). Profile processing keeps
the rows current. The ``load-promotion-candidates`` command adds the lab's
list once, from a CSV on standard input with the header
``tag,league_tier_id,trophies,checked_at`` and each tag written as ``#TAG``. A tracked player, or one whose
saved profile was checked later than the line, is left out, and a tag already
listed keeps whichever check is newer. Any invalid line refuses the whole file.
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


def read_candidates(lines: Iterable[str]) -> list[tuple[str, int, int | None, datetime]]:
    """Parse the CSV, keeping each tag's newest check."""
    reader = csv.DictReader(lines)
    if reader.fieldnames != COLUMNS:
        raise ValueError(f"header must be {','.join(COLUMNS)}")
    rows: dict[str, tuple[str, int, int | None, datetime]] = {}
    for number, row in enumerate(reader, start=2):
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
    return list(rows.values())


def load_candidates(
    connection: Any, candidates: list[tuple[str, int, int | None, datetime]]
) -> int:
    """Add or refresh rows; returns how many were added or changed."""
    changed = 0
    for start in range(0, len(candidates), _CHUNK):
        tags, tiers, trophies, checked = zip(*candidates[start : start + _CHUNK], strict=True)
        changed += connection.execute(
            """
            INSERT INTO promotion_candidates (normalized_tag, league_tier_id, trophies, checked_at)
            SELECT input.tag, input.tier, input.trophies, input.checked
            FROM unnest(%s::text[], %s::integer[], %s::integer[], %s::timestamptz[])
                AS input(tag, tier, trophies, checked)
            WHERE NOT EXISTS (
                SELECT 1 FROM players
                WHERE players.normalized_tag = input.tag
                  AND (players.active OR GREATEST(players.current_observed_at,
                                                  players.current_profile_confirmed_at) > input.checked)
            )
            ON CONFLICT (normalized_tag) DO UPDATE SET
                league_tier_id = EXCLUDED.league_tier_id,
                trophies = EXCLUDED.trophies,
                checked_at = EXCLUDED.checked_at
            WHERE promotion_candidates.checked_at < EXCLUDED.checked_at
            """,
            (list(tags), list(tiers), list(trophies), list(checked)),
        ).rowcount
    return changed


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
        candidates = read_candidates(sys.stdin)
    except ValueError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    with psycopg.connect(database_url) as connection:
        changed = load_candidates(connection, candidates) if candidates else 0
    print(json.dumps({"read": len(candidates), "added_or_updated": changed}, sort_keys=True))
    return 0
