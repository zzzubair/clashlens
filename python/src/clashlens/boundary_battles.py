"""Battle sides and current army decodes used by Reset publications."""

from __future__ import annotations

from typing import Any

from . import battle_day_repair
from .army_decoder import CURRENT_DECODES, DECODER_VERSION


def army_decode_selection(
    connection: Any, battles: Any
) -> tuple[list[int], list[int], dict[tuple[int, str], int]]:
    """A daily log's listed battles, the decodes its army inputs freeze and
    the listed sides 0057 moved.

    The decodes are the newest saved army of both sides of every listed
    battle, plus each moved side's on the battle it is on now.
    """
    sides = [
        (int(event["battle_id"]), event.get("lens"))
        for event in (battles if isinstance(battles, list) else [])
        if isinstance(event, dict) and str(event.get("battle_id", "")).isdigit()
    ]
    battle_ids = [battle_id for battle_id, _lens in sides]
    if not battle_ids:
        return battle_ids, [], {}
    moved = moved_sides(connection, sides)
    decode_ids = sorted(
        {
            *(
                int(row[0])
                for row in connection.execute(
                    f"SELECT id FROM {CURRENT_DECODES} AS decode",
                    (DECODER_VERSION, battle_ids),
                ).fetchall()
            ),
            *moved_decode_ids(connection, moved),
        }
    )
    return battle_ids, decode_ids, moved


def moved_sides(
    connection: Any, sides: list[tuple[int, Any]]
) -> dict[tuple[int, str], int]:
    """Map each listed (battle, lens) 0057 moved to the battle it is on now."""
    listed = sorted({battle_id for battle_id, _lens in sides})
    if not listed:
        return {}
    moved, _ = battle_day_repair.merged_battles(
        connection,
        listed,
        [
            int(row[0])
            for row in connection.execute(
                "SELECT evidence_id FROM battle_perspectives"
                " WHERE battle_id = ANY(%s::bigint[])",
                (listed,),
            ).fetchall()
        ],
    )
    listed_sides = set(sides)
    return {side: to_id for side, to_id in moved.items() if side in listed_sides}


def moved_side_arrays(moved: dict[tuple[int, str], int]) -> tuple[list[int], list[str]]:
    return (
        list(moved.values()),
        ["attacker" if lens == "offense" else "defender" for _id, lens in moved],
    )


def moved_decode_ids(
    connection: Any, moved: dict[tuple[int, str], int]
) -> list[int]:
    """Current decodes of moved sides, read on the battles they are on now."""
    if not moved:
        return []
    return [
        int(row[0])
        for row in connection.execute(
            f"""
            SELECT decode.id
            FROM {CURRENT_DECODES} AS decode
            JOIN unnest(%s::bigint[], %s::text[]) AS side (battle_id, perspective)
              USING (battle_id, perspective)
            """,
            (DECODER_VERSION, list(moved.values()), *moved_side_arrays(moved)),
        ).fetchall()
    ]
