"""Shared setup for reconciliation postgres tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from domain_test_support import (
    store_observation,
)

from clashlens.archive import S3ArchiveReader
from clashlens.db import Database
from clashlens.worker import ObservationProcessor

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"


BATTLE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"


DAY_START = datetime(2026, 8, 4, 5, tzinfo=UTC)


DAY_END = DAY_START + timedelta(days=1)


def _profile(trophies: int, normalized_tag: str = "#2PP") -> bytes:
    payload = json.loads(PROFILE_FIXTURE.read_bytes())
    payload["tag"] = normalized_tag
    payload["trophies"] = trophies
    return json.dumps(payload).encode()


def _battle_log(*, empty: bool = False) -> bytes:
    payload = json.loads(BATTLE_FIXTURE.read_bytes())
    payload["items"] = [] if empty else payload["items"][:1]
    return json.dumps(payload).encode()


def _processor(
    connection_info: str, archive_server
) -> tuple[Database, ObservationProcessor]:
    database = Database(connection_info)
    return database, ObservationProcessor(
        database,
        S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="fixture-access",
            secret_key="fixture-secret",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )


def _seed_reset_collection_identity(
    connection_info: str,
    *,
    key: str,
    boundary: datetime,
    profile_observation_id: int,
    battle_observation_id: int,
    normalized_tag: str = "#2PP",
    production_admission: bool = False,
) -> None:
    with psycopg.connect(connection_info) as connection:
        player_id = int(
            connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s",
                (normalized_tag,),
            ).fetchone()[0]
        )
        work = connection.execute(
            """
            SELECT work.id, work.sweep_id
            FROM collector_work AS work
            JOIN collector_reset_sweeps AS sweep ON sweep.id = work.sweep_id
            WHERE work.kind = 'reset_baseline'
              AND work.player_id = %s
              AND sweep.boundary_at = %s
            ORDER BY work.id DESC
            LIMIT 1
            """,
            (player_id, boundary),
        ).fetchone()
        if work is None and production_admission:
            raise RuntimeError("production reset admission did not create compact work")
        if work is None:
            sweep_id = int(
                connection.execute(
                    """
                    INSERT INTO collector_reset_sweeps (
                        boundary_at, member_ids, membership_captured_at
                    ) VALUES (%s, %s, clock_timestamp())
                    ON CONFLICT (boundary_at) DO UPDATE
                    SET member_ids = EXCLUDED.member_ids
                    RETURNING id
                    """,
                    (boundary, [player_id]),
                ).fetchone()[0]
            )
            work_id = int(
                connection.execute(
                    """
                    INSERT INTO collector_work (
                        kind, lane, scope, player_id, normalized_tag, sweep_id,
                        due_at, coalescing_key, status
                    ) VALUES (
                        'reset_baseline', 'reset', 'player', %s, %s, %s,
                        %s, %s, 'pending'
                    )
                    RETURNING id
                    """,
                    (
                        player_id,
                        normalized_tag,
                        sweep_id,
                        boundary,
                        f"reset-baseline-{key}",
                    ),
                ).fetchone()[0]
            )
        else:
            work_id = int(work[0])
        connection.execute(
            """
            UPDATE collector_work
            SET profile_status = 'observed',
                battle_log_status = 'observed',
                profile_observation_id = %s,
                battle_log_observation_id = %s,
                status = 'complete',
                completed_at = clock_timestamp(),
                updated_at = clock_timestamp()
            WHERE id = %s
            """,
            (profile_observation_id, battle_observation_id, work_id),
        )
        connection.commit()


def _store_baseline_pair(
    connection_info: str,
    archive_server,
    *,
    key: str,
    boundary: datetime,
    trophies: int,
    empty_battle_log: bool,
    observed_at: datetime | None = None,
    normalized_tag: str = "#2PP",
    production_admission: bool = False,
    profile_parser_version: str | None = None,
) -> tuple[int, int, int, int]:
    # A reset-baseline sweep requests its endpoints after the boundary, so the
    # stored observations may complete after ``boundary`` itself.
    observed_at = boundary if observed_at is None else observed_at
    profile_observation, profile_job = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"{key}-profile",
        endpoint="profile",
        body=_profile(trophies, normalized_tag),
        observed_at=observed_at,
        normalized_tag=normalized_tag,
        parser_version=profile_parser_version,
    )
    battle_observation, battle_job = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"{key}-battle",
        endpoint="battle_log",
        body=_battle_log(empty=empty_battle_log),
        observed_at=observed_at,
        normalized_tag=normalized_tag,
    )
    _seed_reset_collection_identity(
        connection_info,
        key=key,
        boundary=boundary,
        profile_observation_id=profile_observation,
        battle_observation_id=battle_observation,
        normalized_tag=normalized_tag,
        production_admission=production_admission,
    )
    return profile_observation, battle_observation, profile_job, battle_job
