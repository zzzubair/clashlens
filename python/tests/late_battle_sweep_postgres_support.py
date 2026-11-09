"""Shared setup for late battle sweep postgres tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import (
    LIVE_BATTLE_PARSER_VERSION,
    _live_battle_row,
    store_observation,
)

ANCHOR = datetime(2026, 8, 3, 5, tzinfo=UTC)


DAY = ANCHOR + timedelta(days=1)


TAG = "#8PP"


OPPONENT = "#2PP"


def _late_defense() -> dict:
    # 0 stars at 49%: the attacker gains trophies, the defender loses none.
    return _live_battle_row(
        attack=False,
        battle_timestamp=DAY + timedelta(days=1, seconds=-10),
        opponent_tag="#2PP",
        opponent_name="Zero Star Attacker",
        stars=0,
        destruction_percentage=49,
    )


def _store_log(connection_info, archive_server, *, key, rows, observed_at):
    return store_observation(
        connection_info,
        archive_server,
        occurrence_key=key,
        endpoint="battle_log",
        body=json.dumps({"items": rows}).encode(),
        observed_at=observed_at,
        normalized_tag=TAG,
        parser_version=LIVE_BATTLE_PARSER_VERSION,
    )[1]


def _process(processor, *job_ids: int) -> None:
    for job_id in job_ids:
        result = processor.process_job(job_id, owner=f"test-{job_id}")
        assert result is not None and result.outcome == "processed"


def _save_log(connection_info, archive_server, processor, *, key, rows, observed_at):
    _process(
        processor,
        _store_log(
            connection_info,
            archive_server,
            key=key,
            rows=rows,
            observed_at=observed_at,
        ),
    )


def _finish_reset_sweep(connection_info: str, boundary: datetime) -> None:
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            "INSERT INTO collector_reset_sweeps (boundary_at) VALUES (%s)",
            (boundary,),
        )
        connection.commit()


def _published(
    connection_info: str, day: datetime, tag: str = TAG
) -> tuple[list, int] | None:
    """The defenses on the player's latest published log, and its version id."""
    with psycopg.connect(connection_info) as connection:
        row = connection.execute(
            """
            SELECT log.battles, log.ranked_day_version_id
            FROM api_player_daily_logs AS log
            JOIN players AS player ON player.id = log.player_id
            WHERE player.normalized_tag = %s AND log.ranked_day_start = %s
            ORDER BY log.version DESC
            LIMIT 1
            """,
            (tag, day),
        ).fetchone()
    if row is None:
        return None
    defenses = sorted(
        (item["opponent_tag"], item["stars"], item["trophy_change"])
        for item in row[0]
        if item["lens"] == "defense"
    )
    return defenses, int(row[1])


def _previous_day_version(connection_info: str, day: datetime, tag: str = TAG) -> int:
    with psycopg.connect(connection_info) as connection:
        return int(
            connection.execute(
                """
                SELECT input_evidence -> 'previous_day' ->> 'version_id'
                FROM ranked_day_versions AS version
                JOIN players AS player ON player.id = version.player_id
                WHERE player.normalized_tag = %s AND version.ranked_day_start = %s
                ORDER BY version.version DESC
                LIMIT 1
                """,
                (tag, day),
            ).fetchone()[0]
        )


def _saved_disagreement(connection_info: str, tag: str) -> list[bool]:
    """The disagreement flag of each battle on the player's latest saved result."""
    with psycopg.connect(connection_info) as connection:
        row = connection.execute(
            """
            SELECT version.input_evidence -> 'contributions'
            FROM api_player_daily_logs AS log
            JOIN players AS player ON player.id = log.player_id
            JOIN ranked_day_versions AS version
              ON version.id = log.ranked_day_version_id
            WHERE player.normalized_tag = %s AND log.ranked_day_start = %s
            ORDER BY log.version DESC
            LIMIT 1
            """,
            (tag, DAY),
        ).fetchone()
    return [item["disagreement"] for item in row[0]]
