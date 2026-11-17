"""Freeze a Reset generation's sorted inputs into its manifest.

Each kind of input is read for the whole population in one query, in place of
one or more queries per player, and every manifest row is written in one
insert. A Reset's publication lock is held for the whole freeze, so this is
what shortens how long other work waits for it. The rows and digest are the
same as reading each player's inputs one at a time.

A correction's manifest repeats almost every row of the Reset's earlier ones,
so it stores only the rows that differ from the Reset's newest full manifest
and reads the rest from it; boundary_publication_manifest_entries rebuilds
the complete rows the digest covers.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Collection, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from psycopg.errors import QueryCanceled
from psycopg.types.json import Jsonb

from . import boundary_members, reading_rule, reset_total
from .analytics import latest_trophy_changes, unplayed_since_sql, unplayed_sql
from .army_decoder import CURRENT_DECODES, DECODER_VERSION
from .boundary_battles import moved_decode_ids, moved_side_arrays
from .db import DEFAULT_PARSER_VERSION
from .db_rows import text_value
from .domain import (
    RANKED_DAY_DURATION,
    SEASON_START_TROPHIES,
    ranked_day_for,
    season_is_current,
)
from .job_contracts import (
    ANALYTICS_RULE_VERSION,
    ARMY_ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
)
from .league_week import week_start
from .newest_profiles import ACCEPTED_VERSIONS, ANY_VERSIONS, NEWEST_PROFILES_SQL
from .publication_states import ManifestStage, day_snapshot_status
from .reconciliation import DISPUTED_BATTLE_REASONS

_STATUS_CLASSIFICATIONS = {
    "complete": "Complete",
    "partial": "Partial",
    "failed": "Failed",
    "missing": "Missing",
    "unavailable": "Unavailable",
    "inconsistent": "Inconsistent",
    "malformed": "Malformed",
}
# A failed freeze's build, saved by freeze_boundary_manifest.
FREEZE_TIMED_OUT = "freeze_timed_out"


def retry_wait(failures: str) -> str:
    """SQL for how long Reset work waits after ``failures`` failed tries: a
    minute, then twice as long after each further one, at most an hour."""
    return f"least(interval '1 hour', interval '1 minute' * 2 ^ ({failures} - 1))"


def freeze_boundary_manifest(
    connection: Any,
    *,
    generation_id: int,
    artifact_kind: str,
) -> tuple[int, str] | None:
    """Freeze one sorted coordinator input before creating its job (``ManifestStage``).

    The row and digest are persisted together. Once inserted, the manifest
    trigger makes both the population and its selected inputs immutable;
    retrying callers therefore reuse the same identity instead of
    reconstructing inputs from live eligibility tables.
    """
    existing = connection.execute(
        """
        SELECT id, digest
        FROM boundary_publication_manifests
        WHERE generation_id = %s AND artifact_kind = %s
        FOR UPDATE
        """,
        (generation_id, artifact_kind),
    ).fetchone()
    if existing is not None:  # ManifestStage.REUSED
        return int(existing[0]), text_value(existing[1])
    generation = connection.execute(
        """
        SELECT boundary_at, generation, ordering_rule_version, freshness_rule_version,
               snapshot_rule_version, army_rule_version
        FROM boundary_publication_generations
        WHERE id = %s
        FOR UPDATE
        """,
        (generation_id,),
    ).fetchone()
    if generation is None:
        return None
    # On a freeze past the worker's 15-minute query limit, every background
    # pass tried again: 15 minutes each, and four in one pass outlasted the
    # worker's 60-minute health limit. The failure is saved as its build's,
    # which the alert check reports, and the next try waits as a failed
    # build's rerun does (board_deadline.retry_failed_build).
    work_type = "build_snapshot" if artifact_kind == "snapshot" else "build_army_analytics"
    failed = (work_type, Jsonb({
        "boundary_at": f"{generation[0].astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}",
        "generation": int(generation[1])}))
    timeouts, waits = connection.execute(
        f"""
        SELECT count(*), max(updated_at) + {retry_wait("count(*)")} > clock_timestamp()
        FROM python_processing_jobs_worker
        WHERE state = 'failed' AND failure_category = '{FREEZE_TIMED_OUT}'
          AND work_type = %s AND input_json = %s
        """,
        failed,
    ).fetchone()
    if waits:  # ManifestStage.BACKING_OFF
        return None
    # How long each stage took, saved with the freeze's event, and which
    # stage ran out of time when one does.
    timing = {"generation": int(generation[1]), "earlier_timeouts": int(timeouts),
              "stages": [("members", time.monotonic())]}
    try:
        with connection.transaction():
            return _freeze(connection, generation_id, artifact_kind, generation, timing)
    except QueryCanceled:
        print(json.dumps({"event": "boundary_manifest_frozen", "outcome": ManifestStage.TIMED_OUT.value,
                          "artifact_kind": artifact_kind, **_timing(timing),
                          "stage": timing["stages"][-1][0]}), flush=True)
        connection.execute(
            f"""
            INSERT INTO python_processing_jobs_worker (
                work_type, input_json, deduplication_key, state, due_at,
                parser_version, processing_version, domain_rule_version,
                analytics_rule_version, outcome, failure_category, failure_detail,
                completed_at)
            VALUES (%s, %s, %s || clock_timestamp(), 'failed', clock_timestamp(),
                    %s, %s, %s, %s, 'durable_failure', '{FREEZE_TIMED_OUT}',
                    'freezing its inputs ran past the 15-minute query limit',
                    clock_timestamp())
            """,
            (*failed, f"{work_type}:freeze:{generation_id}:",
             DEFAULT_PARSER_VERSION, PROCESSING_VERSION, DOMAIN_RULE_VERSION,
             ANALYTICS_RULE_VERSION if artifact_kind == "snapshot"
             else ARMY_ANALYTICS_RULE_VERSION),
        )
        return None


def _timing(timing: dict[str, Any]) -> dict[str, Any]:
    """The freeze's generation, earlier timeouts and seconds per stage so far."""
    stages = [*timing["stages"], ("", time.monotonic())]
    return {**{key: value for key, value in timing.items() if key != "stages"},
            "seconds": {name: round(stages[index + 1][1] - start, 3)
                        for index, (name, start) in enumerate(stages[:-1])}}


def _freeze(
    connection: Any, generation_id: int, artifact_kind: str, generation: Any,
    timing: dict[str, Any],
) -> tuple[int, str]:
    members = connection.execute(
        """
        SELECT player_id, ranked_day_version_id, ranked_day_input_hash,
               snapshot_status, army_status
        FROM boundary_publication_generation_members
        WHERE generation_id = %s
        ORDER BY player_id, ranked_day_version_id NULLS FIRST, ranked_day_input_hash NULLS FIRST
        """,
        (generation_id,),
    ).fetchall()
    timing["stages"].append(("rows", time.monotonic()))
    if artifact_kind == "snapshot":
        manifest_rows = _snapshot_rows(connection, members, generation)
    else:
        manifest_rows = _army_rows(connection, members, generation)
    timing["stages"].append(("season_inputs", time.monotonic()))
    season_inputs = (
        _season_inputs(connection, members) if artifact_kind == "army" else None
    )
    timing["stages"].append(("store", time.monotonic()))
    rule_versions = {
        "ordering_rule_version": text_value(generation[2]),
        "freshness_rule_version": text_value(generation[3]),
        "analytics_rule_version": (
            text_value(generation[5])
            if artifact_kind == "army"
            else text_value(generation[4])
        ),
        **({"season_inputs": season_inputs} if season_inputs is not None else {}),
    }
    digest = manifest_digest(
        {
            "generation": int(generation[1]),
            "artifact_kind": artifact_kind,
            "rule_versions": rule_versions,
            "rows": manifest_rows,
        }
    )
    columns = _row_columns(manifest_rows)
    base_id, sources = _reuse_plan(connection, generation[0], artifact_kind, columns)
    stored_rule_versions = rule_versions
    season_changes = (
        _season_input_changes(connection, base_id, season_inputs)
        if base_id is not None and season_inputs is not None
        else None
    )
    if season_changes is not None:
        stored_rule_versions = {
            **{key: value for key, value in rule_versions.items() if key != "season_inputs"},
            "season_input_changes": season_changes,
        }
    manifest = connection.execute(
        """
        INSERT INTO boundary_publication_manifests
            (generation_id, artifact_kind, rule_versions, digest,
             base_manifest_id, newest_ranked_day_version_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            generation_id,
            artifact_kind,
            Jsonb(stored_rule_versions),
            digest,
            base_id,
            max((value for value in columns[2] if value is not None), default=None),
        ),
    ).fetchone()
    assert manifest is not None
    manifest_id = int(manifest[0])
    # With a base, only rows that differ from it are stored; a row whose
    # identity an earlier manifest stores names that manifest, not a copy.
    stored = [
        index
        for index in range(len(manifest_rows))
        if base_id is None or sources[index] != base_id
    ]
    connection.execute(
        """
        INSERT INTO boundary_publication_manifest_rows
            (manifest_id, ordinal, player_id, ranked_day_version_id,
             input_hash, classification, unavailable_reason, input_identity,
             identity_manifest_id)
        SELECT %s, manifest_row.*
        FROM unnest(
            %s::integer[], %s::bigint[], %s::bigint[], %s::text[],
            %s::text[], %s::text[], %s::jsonb[], %s::bigint[]
        ) AS manifest_row
        """,
        (
            manifest_id,
            *([column[index] for index in stored] for column in columns[:6]),
            [
                None if base_id is not None and sources[index] else columns[6][index]
                for index in stored
            ],
            [sources[index] if base_id is not None else None for index in stored],
        ),
    )
    identities = [
        len(json.dumps(identity, separators=(",", ":"))) for identity in manifest_rows
    ]
    print(
        json.dumps(
            {
                "event": "boundary_manifest_frozen",
                "outcome": ManifestStage.FROZEN.value,
                "manifest_id": manifest_id,
                "artifact_kind": artifact_kind,
                **_timing(timing),
                "base_manifest_id": base_id,
                "rows": len(manifest_rows),
                "rows_stored": len(stored),
                "identities_stored": sum(
                    base_id is None or not sources[index] for index in stored
                ),
                "identity_bytes": sum(identities),
                "identity_bytes_stored": sum(
                    identities[index]
                    for index in stored
                    if base_id is None or not sources[index]
                ),
                "season_inputs_reused": season_changes is not None,
            }
        ),
        flush=True,
    )
    connection.execute(
        """
        UPDATE boundary_publication_manifests
        SET rows_sealed = true, frozen_at = clock_timestamp(),
            enqueued_at = clock_timestamp()
        WHERE id = %s AND NOT rows_sealed
        """,
        (manifest_id,),
    )
    column = (
        "snapshot_manifest_id"
        if artifact_kind == "snapshot"
        else "army_manifest_id"
    )
    connection.execute(
        f"UPDATE boundary_publication_generations SET {column} = %s, updated_at = clock_timestamp() WHERE id = %s",
        (manifest_id, generation_id),
    )
    return manifest_id, digest


def manifest_digest(contents: Mapping[str, Any]) -> str:
    """The digest of a manifest's generation, kind, rule versions and rows."""
    return hashlib.sha256(
        json.dumps(contents, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def manifest_contents(connection: Any, manifest_id: int) -> tuple[str, dict[str, Any]]:
    """A manifest's stored digest and the contents it covers, rebuilt from
    whatever it and its base store, for checking one against the other."""
    row = connection.execute(
        """
        SELECT manifest.digest, generation.generation, manifest.artifact_kind,
               manifest.rule_versions, base.rule_versions -> 'season_inputs'
        FROM boundary_publication_manifests AS manifest
        JOIN boundary_publication_generations AS generation
          ON generation.id = manifest.generation_id
        LEFT JOIN boundary_publication_manifests AS base
          ON base.id = manifest.base_manifest_id
        WHERE manifest.id = %s
        """,
        (manifest_id,),
    ).fetchone()
    rule_versions = dict(row[3])
    changes = rule_versions.pop("season_input_changes", None)
    if changes is not None:
        rule_versions["season_inputs"] = {
            key: _apply_list_changes(row[4][key], change)
            for key, change in changes.items()
        }
    rows = connection.execute(
        """
        SELECT input_identity FROM boundary_publication_manifest_entries(%s)
        ORDER BY ordinal
        """,
        (manifest_id,),
    ).fetchall()
    return text_value(row[0]), {
        "generation": int(row[1]),
        "artifact_kind": text_value(row[2]),
        "rule_versions": rule_versions,
        "rows": [identity for (identity,) in rows],
    }


def _row_columns(manifest_rows: list[dict[str, Any]]) -> list[list[Any]]:
    """The stored columns of each row, in order."""
    return [
        list(range(1, len(manifest_rows) + 1)),
        [identity["player_id"] for identity in manifest_rows],
        [identity["ranked_day_version_id"] for identity in manifest_rows],
        [identity["input_hash"] for identity in manifest_rows],
        [identity["classification"] for identity in manifest_rows],
        [
            "reset_baseline_failed"
            if identity["classification"] == "Unavailable"
            else None
            for identity in manifest_rows
        ],
        [Jsonb(identity) for identity in manifest_rows],
    ]


def _same_row(stored: str) -> str:
    """Whether ``stored`` holds the ``new`` row, generation aside. Identities
    compare as stored text, so a reused one reads back exactly as sent."""
    return f"""
        {stored}.ranked_day_version_id IS NOT DISTINCT FROM new.ranked_day_version_id
        AND {stored}.input_hash IS NOT DISTINCT FROM new.input_hash
        AND {stored}.classification = new.classification
        AND {stored}.unavailable_reason IS NOT DISTINCT FROM new.unavailable_reason
        AND ({stored}.input_identity - 'generation')::text
            = (new.input_identity - 'generation')::text
    """


def _reuse_plan(
    connection: Any, boundary_at: datetime, artifact_kind: str, columns: list[list[Any]]
) -> tuple[int | None, list[int | None]]:
    """The full manifest a new one can build on, and for each row the
    manifest whose identical row it reuses, or None to store it in full.

    The base is the newest full manifest of this Reset and kind, so no
    manifest is ever more than one step from its rows. A row equal to the
    base's is the base's; a row equal to the newest manifest's names the
    manifest storing that identity. A changed membership freezes a new full
    manifest instead.
    """
    previous = connection.execute(
        """
        SELECT manifest.id, COALESCE(manifest.base_manifest_id, manifest.id)
        FROM boundary_publication_manifests AS manifest
        JOIN boundary_publication_generations AS generation
          ON generation.id = manifest.generation_id
        WHERE generation.boundary_at = %s AND manifest.artifact_kind = %s
          AND manifest.rows_sealed
        ORDER BY manifest.id DESC
        LIMIT 1
        """,
        (boundary_at, artifact_kind),
    ).fetchone()
    if previous is None:
        return None, []
    previous_id, base_id = int(previous[0]), int(previous[1])
    members = connection.execute(
        "SELECT count(*) FROM boundary_publication_manifest_rows WHERE manifest_id = %s",
        (base_id,),
    ).fetchone()[0]
    if int(members) != len(columns[0]):
        return None, []
    plan = connection.execute(
        f"""
        SELECT base.player_id IS NOT NULL, COALESCE({_same_row("base")}, false),
               CASE WHEN {_same_row("previous")}
                    THEN previous.identity_manifest_id END
        FROM unnest(
            %s::integer[], %s::bigint[], %s::bigint[], %s::text[],
            %s::text[], %s::text[], %s::jsonb[]
        ) AS new (ordinal, player_id, ranked_day_version_id, input_hash,
                  classification, unavailable_reason, input_identity)
        LEFT JOIN boundary_publication_manifest_rows AS base
          ON base.manifest_id = %s AND base.ordinal = new.ordinal
         AND base.player_id = new.player_id
        LEFT JOIN boundary_publication_manifest_entries(%s) AS previous
          ON previous.ordinal = new.ordinal AND previous.player_id = new.player_id
        ORDER BY new.ordinal
        """,
        (*columns, base_id, previous_id),
    ).fetchall()
    if not all(member for member, _, _ in plan):
        return None, []
    sources = [
        base_id if same else int(holder) if holder is not None else None
        for _, same, holder in plan
    ]
    return base_id, sources


def _season_input_changes(
    connection: Any, base_id: int, season_inputs: Mapping[str, list[int]]
) -> dict[str, dict[str, list[int]]] | None:
    """Each Season input list's changes from the base manifest's, or None when
    they change more than half the IDs and the full lists are stored."""
    base = connection.execute(
        "SELECT rule_versions -> 'season_inputs' FROM boundary_publication_manifests WHERE id = %s",
        (base_id,),
    ).fetchone()[0]
    if not isinstance(base, dict) or set(base) != set(season_inputs):
        return None
    changes = {}
    for key, values in season_inputs.items():
        change = _list_changes(base[key], values)
        if change is None:
            return None
        changes[key] = change
    changed = sum(len(change["removed"]) + len(change["added"]) for change in changes.values())
    if 2 * changed > sum(len(values) for values in season_inputs.values()):
        return None
    return changes


def _list_changes(base: list[int], values: list[int]) -> dict[str, list[int]] | None:
    """The IDs ``values`` drops from ``base`` and the ones it adds, with
    where each lands, or None when applying them would not give ``values``
    back exactly."""
    kept, present = set(values), set(base)
    added = [(index, value) for index, value in enumerate(values) if value not in present]
    change = {
        "removed": [value for value in base if value not in kept],
        "at": [index for index, _ in added],
        "added": [value for _, value in added],
    }
    return change if _apply_list_changes(base, change) == values else None


def _apply_list_changes(base: list[int], change: Mapping[str, list[int]]) -> list[int] | None:
    removed = set(change["removed"])
    kept = iter(value for value in base if value not in removed)
    added = dict(zip(change["at"], change["added"], strict=True))
    length = len(base) - len(removed) + len(added)
    values = [added[index] if index in added else next(kept, None) for index in range(length)]
    return values if None not in values and next(kept, None) is None else None


def _member(row: Any, artifact_kind: str) -> tuple[int, int | None, str | None, str]:
    """A member's player, ranked day, input hash and this artifact's status."""
    return (
        int(row[0]),
        int(row[1]) if row[1] is not None else None,
        text_value(row[2]) if row[2] is not None else None,
        text_value(row[3] if artifact_kind == "snapshot" else row[4]),
    )


def _rechecks(status: str, version_id: int | None) -> bool:
    """Whether a member's status is checked again against its ranked day."""
    return (
        status in {"complete", "partial", "inconsistent", "malformed"}
        and version_id is not None
    )


def _snapshot_rows(
    connection: Any, members: list[Any], generation: Any
) -> list[dict[str, Any]]:
    boundary_at = generation[0]
    player_ids = [int(row[0]) for row in members]
    official_version = connection.execute(
        """
        SELECT v.id
        FROM official_top200_versions AS v
        JOIN official_top200_attempts AS a ON a.id = v.attempt_id
        JOIN official_top200_version_entries AS e ON e.version_id = v.id
        WHERE a.outcome = 'official_observed' AND v.observed_at <= %s
        GROUP BY v.id, v.observed_at
        HAVING count(*) = 200 AND count(DISTINCT e.rank) = 200
           AND min(e.rank) = 1 AND max(e.rank) = 200
        ORDER BY v.observed_at DESC, v.id DESC
        LIMIT 1
        """,
        (boundary_at,),
    ).fetchone()
    official_version_id = int(official_version[0]) if official_version else None
    official_entries = (
        {
            int(row[0]): (row[1], row[2])
            for row in connection.execute(
                """
                SELECT entry.player_id, entry.rank, version.observed_at
                FROM official_top200_version_entries AS entry
                JOIN official_top200_versions AS version
                  ON version.id = entry.version_id
                WHERE entry.version_id = %s
                  AND entry.player_id = ANY(%s::bigint[])
                """,
                (official_version_id, player_ids),
            ).fetchall()
        }
        if official_version_id is not None
        else {}
    )
    # Each player's latest trophy change in the ended day before the Reset,
    # which orders equal trophies (analytics.latest_change_order_key).
    latest_changes = latest_trophy_changes(
        connection,
        day_start=boundary_at - RANKED_DAY_DURATION,
        cutoff=boundary_at,
        player_ids=player_ids,
    )
    profiles = {
        int(row[0]): row[1:]
        for row in connection.execute(
            NEWEST_PROFILES_SQL.replace("{versions}", ACCEPTED_VERSIONS),
            {"players": player_ids, "boundary": boundary_at},
        ).fetchall()
    }
    # Every member whose accepted profile would leave them off, as
    # boundary.queue_board_rebuilds asks of a frozen board: a player promoted
    # at a Monday Reset who has not played since reads Legend I at 5,000
    # naming Season 0, never accepted, after their last accepted profile in
    # the lower league (135 such players left off the 15 October 2026 board).
    unplayed = unplayed_profiles(
        connection, boundary_at,
        [
            player_id for player_id in player_ids
            if player_id not in profiles
            or text_value(profiles[player_id][7]) != "eligible"
            or profiles[player_id][6] == SEASON_START_TROPHIES
            or not season_is_current(
                text_value(profiles[player_id][8]),
                boundary_at - RANKED_DAY_DURATION,
            )
        ],
    )
    profiles.update(unplayed)
    # A member not read in the ended day's week before the Reset ranks by
    # their Complete day, named by their first Legend I reading since.
    week = week_start(boundary_at - RANKED_DAY_DURATION)
    late = boundary_members.first_readings(connection, boundary_at, {
        int(row[0]): int(row[1]) for row in members
        if row[1] is not None and (int(row[0]) not in profiles or profiles[int(row[0])][2] < week)
    })
    profiles.update(late)
    # Why a player with no accepted profile has none: their newest profile's
    # source state and their newest profile response's failure.
    unprofiled = [player_id for player_id in player_ids if player_id not in profiles]
    # One with none is not found when their newest answer before the Reset
    # says so.
    not_found = profiles_not_found(
        connection, boundary_at,
        {player_id: row[2] for player_id, row in profiles.items()}
        | dict.fromkeys(unprofiled, datetime.min.replace(tzinfo=UTC)),
    )
    failures = {
        int(row[0]): row[1:]
        for row in connection.execute(
            """
            SELECT member.player_id, (
                SELECT profile.source_contract_state
                FROM player_profile_versions AS profile
                LEFT JOIN player_profile_effects AS effect
                  ON effect.profile_version_id = profile.id
                WHERE profile.player_id = member.player_id
                  AND COALESCE(effect.observed_at, profile.observed_at) <= %s
                ORDER BY COALESCE(effect.observed_at, profile.observed_at) DESC,
                         COALESCE(effect.id, profile.id) DESC
                LIMIT 1
            ), (
                SELECT job.failure_category
                FROM collector_observations AS observation
                LEFT JOIN python_processing_jobs_worker AS job
                  ON job.observation_id = observation.id
                WHERE observation.player_id = member.player_id
                  AND observation.endpoint = 'profile'
                  AND observation.response_completed_at <= %s
                ORDER BY observation.response_completed_at DESC,
                         observation.id DESC
                LIMIT 1
            )
            FROM unnest(%s::bigint[]) AS member (player_id)
            """,
            (boundary_at, boundary_at, unprofiled),
        ).fetchall()
    } if unprofiled else {}
    ranked_states = {
        (int(row[0]), int(row[1])): text_value(row[2])
        for row in connection.execute(
            "SELECT id, player_id, state FROM ranked_day_versions WHERE id = ANY(%s::bigint[])",
            ([row[1] for row in members if row[1] is not None],),
        ).fetchall()
    }
    leagues = boundary_members.leagues(connection, boundary_at, player_ids)
    manifest_rows: list[dict[str, Any]] = []
    for row in members:
        player_id, version_id, input_hash, status = _member(row, "snapshot")
        classification = _STATUS_CLASSIFICATIONS.get(status, "Pending")
        # A member ranked at 5,000 before their first Legend battle was saved
        # missing, as boundary_members.snapshot_status finds no accepted
        # profile.
        if _rechecks(status, version_id) or (
            status == "missing" and (player_id in unplayed or player_id in late)
            and version_id is not None
        ):
            # The member's status, checked again against its ranked day and
            # accepted profile now, as boundary_members.snapshot_status.
            snapshot_status = day_snapshot_status(
                ranked_states.get((version_id, player_id), ""), has_profile=player_id in profiles
            )
            classification = _STATUS_CLASSIFICATIONS.get(snapshot_status, "Missing")
        identity: dict[str, Any] = {
            "artifact_kind": "snapshot",
            "generation": int(generation[1]),
            "player_id": player_id,
            "ranked_day_version_id": version_id,
            "input_hash": input_hash,
            "classification": classification,
        }
        identity["official_top200_version_id"] = official_version_id
        official_entry = official_entries.get(player_id)
        identity["official_rank"] = (
            int(official_entry[0]) if official_entry else None
        )
        identity["official_rank_observed_at"] = (
            official_entry[1].astimezone(UTC).isoformat()
            if official_entry
            else None
        )
        latest_change = latest_changes.get(player_id)
        identity["latest_change_at"] = (
            latest_change.astimezone(UTC).isoformat() if latest_change else None
        )
        profile = profiles.get(player_id)
        if profile is not None:
            identity["profile_version_id"] = int(profile[0])
            identity["profile_input_hash"] = hashlib.sha256(
                json.dumps(
                    profile[3], sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            identity["profile_snapshot"] = {
                "observation_id": int(profile[1]),
                "tag": text_value(profile[4]),
                "name": profile[5],
                "trophies": int(profile[6]),
                "observed_at": profile[2].astimezone(UTC).isoformat(),
                "eligibility_state": text_value(profile[7]),
                "profile_json": profile[3],
            }
            if player_id in late:
                identity["profile_snapshot"]["read_after_reset"] = True
            # Whether the player was in Legend I is the board's membership
            # (boundary_members); a lower-league profile's trophies still
            # rank on no Legend I board.
            if text_value(profile[7]) != "eligible":
                identity["snapshot_quality"] = "invalid"
            elif player_id not in unplayed and player_id not in late and not season_is_current(
                text_value(profile[8]), generation[0] - RANKED_DAY_DURATION
            ):
                # Trophies from before this player's Season reset never
                # stand for the ended day's Season.
                identity["snapshot_quality"] = "season_reset_pending"
            elif player_id in not_found:
                # The player went missing after this reading, so its
                # trophies no longer stand for them at the Reset.
                identity["snapshot_quality"] = "profile_not_found"
            elif classification == "Unavailable":
                # A member left out unfinished, as at the board deadline,
                # waits for their day: an older reading never ranks them
                # (on 4 November 2026 it ranked 7 such members).
                identity["snapshot_quality"] = "unavailable"
            else:
                identity["snapshot_quality"] = "eligible"
        else:
            identity["profile_version_id"] = None
            identity["profile_input_hash"] = None
            latest = failures.get(player_id)
            source_state = text_value(latest[0]) if latest and latest[0] else None
            failure = text_value(latest[1]) if latest and latest[1] else None
            if failure in {
                "malformed_json",
                "unsupported_profile_schema",
                "source_identity_mismatch",
                "invalid_player_tag",
            }:
                snapshot_quality = "malformed"
            elif player_id in not_found:
                snapshot_quality = "profile_not_found"
            elif source_state == "conflict":
                snapshot_quality = "conflicting"
            else:
                snapshot_quality = {
                    "Unavailable": "unavailable",
                    "Failed": "unavailable",
                    "Partial": "partial",
                    "Inconsistent": "inconsistent",
                    "Malformed": "malformed",
                }.get(classification, "missing")
            identity["snapshot_quality"] = snapshot_quality
        # A member whose week left Legend I after the members were taken. One
        # the game no longer finds has no league to confirm.
        league = leagues.get(player_id, "legend_i")
        if league == "below_legend_i":
            identity["snapshot_quality"] = "below_legend_i"
        elif league != "legend_i" and identity["snapshot_quality"] != "profile_not_found":
            identity["snapshot_quality"] = "league_not_confirmed"
        manifest_rows.append(identity)
    return manifest_rows


def unplayed_profiles(
    connection: Any, boundary_at: datetime, player_ids: list[int]
) -> dict[int, tuple[Any, ...]]:
    """The players among ``player_ids`` the board at ``boundary_at`` ranks at
    5,000 with their newest profile at the Reset, as the population freeze
    reads a profile: a Legend I one at 5,000 when their saved days hold no
    battle since the Season began or, if later, since the Monday Reset of
    their newest week up to the ended day the weekly league record says they
    entered Legend I. They have not played yet, whatever Season it names. On
    11 October 2026, 1,374 such players' profiles named Season 0."""
    if not player_ids:
        return {}
    day = boundary_at - RANKED_DAY_DURATION
    return {
        int(row[0]): row[1:]
        for row in connection.execute(
            f"""
            SELECT newest.* FROM ({NEWEST_PROFILES_SQL.replace("{versions}", ANY_VERSIONS)})
                AS newest (player_id, id, observation_id, observed_at,
                           profile_json, normalized_tag, name, trophies,
                           eligibility_state, current_league_season_id)
            JOIN player_profile_versions AS version ON version.id = newest.id
            WHERE {unplayed_sql("newest.player_id", "version", unplayed_since_sql(
                "newest.player_id", "%(season_start)s", "%(day)s"), "%(boundary)s")}
            """,
            {"players": player_ids, "boundary": boundary_at, "day": day,
             "season_start": ranked_day_for(day).season_start},
        ).fetchall()
    }


def profiles_not_found(
    connection: Any, boundary_at: datetime, readings: Mapping[int, datetime]
) -> set[int]:
    """Players whose profile answered "player not found" after their reading.

    The newest successful or not-found profile response after each player's
    reading, up to the Reset, decides, as the Live Leaderboard's latest
    response does: a later success brings the player back, and a timeout or
    server error changes nothing. A response that changes the answer is
    always saved, so saved responses show when the player went missing.
    """
    if not readings:
        return set()
    return {
        int(row[0])
        for row in connection.execute(
            """
            SELECT reading.player_id
            FROM unnest(%s::bigint[], %s::timestamptz[])
                AS reading (player_id, observed_at)
            CROSS JOIN LATERAL (
                SELECT observation.http_status
                FROM collector_observations AS observation
                WHERE observation.player_id = reading.player_id
                  AND observation.endpoint = 'profile'
                  AND observation.response_completed_at > reading.observed_at
                  AND observation.response_completed_at <= %s
                  AND (observation.http_status = 404
                       OR observation.http_status BETWEEN 200 AND 299)
                ORDER BY observation.response_completed_at DESC,
                         observation.id DESC
                LIMIT 1
            ) AS latest
            WHERE latest.http_status = 404
            """,
            (list(readings), list(readings.values()), boundary_at),
        ).fetchall()
    }


def reset_trophies(
    connection: Any,
    boundary_at: datetime,
    readings: Mapping[int, tuple[int, int, datetime, int]],
) -> dict[int, tuple[int, bool]]:
    """Each player's trophies at the Reset before the automatic defense
    loss, and whether they are proven: the Reset total stored with the day
    result the board pins (``reset_total``), so the board and the player's
    day show one answer, worked out once.

    ``readings`` maps a player to the version of their day ending at
    ``boundary_at``, their reading's saved response, its time and its
    trophies. A player without that day, or whose day stores no total, as
    one with no reading of its own, ranks by their reading, uncertain. A day
    result saved before results stored the total (15 November 2026) gets one
    from the same rule, over its saved battles and the board's reading
    (``_saved_before``)."""
    if not readings:
        return {}
    days = dict(connection.execute(
        """
        SELECT ranked.player_id, ranked.formula_components -> 'reset_total'
        FROM unnest(%s::bigint[], %s::bigint[]) AS reading (player_id, version_id)
        JOIN ranked_day_versions AS ranked
          ON ranked.id = reading.version_id AND ranked.player_id = reading.player_id
         AND ranked.ranked_day_end = %s
        """,
        (list(readings), [reading[0] for reading in readings.values()], boundary_at),
    ).fetchall())
    stored = {player_id: answer for player_id, answer in days.items() if answer is not None}
    stored.update(_saved_before(connection, boundary_at, {
        player_id: readings[player_id] for player_id in days.keys() - stored.keys()}))
    return {player_id: shown_total(stored.get(player_id), reading[3])
            for player_id, reading in readings.items()}


def shown_total(answer: Mapping[str, Any] | None, reading: int | None) -> tuple[int | None, bool]:
    """The trophies and mark a board entry shows from a day result's stored
    Reset total ``answer``: its value and whether it is proven, or the
    entry's ``reading``, uncertain, when it stores no value."""
    if answer is None or answer["value"] is None:
        return reading, False
    return int(answer["value"]), bool(answer["proven"])


def _saved_before(
    connection: Any, boundary_at: datetime,
    readings: Mapping[int, tuple[int, int, datetime, int]],
) -> dict[int, dict[str, Any]]:
    """The Reset total of each day result saved before results stored it,
    from the rule that now stores it (``reset_total``), over the result's
    saved battles, with the board's reading as its old reading. The
    readings from the Reset on are not judged again: a Complete day's end is
    proven when its own result says a reading proved it, exactly or
    cleanly, and no battle may have ended after the Reset; any other total
    is unproven. A Reset that resets trophies, a Season's end or a weekly
    raise its end reading was not taken before, proves only the start, so
    there the board's reading plus the battles after it must agree too."""
    if not readings:
        return {}
    rows = connection.execute(
        """
        SELECT ranked.player_id, ranked.state, ranked.confidence = 'exact',
               ranked.input_evidence -> 'end_reading' ->> 'clean' = 'true',
               ranked.failure_reasons, ranked.final_trophies_before_reset,
               ranked.automatic_defense_loss, ranked.ranked_day_start,
               ranked.coverage_complete, ranked.input_evidence -> 'contributions',
               ranked.input_evidence ->> 'boundary_kind',
               (ranked.input_evidence -> 'end_baseline_evidence'
                ->> 'official_final_trophies')::integer,
               observation.request_started_at,
               (SELECT jsonb_object_agg(side.battle_id, evidence.battle_timestamp)
                FROM battle_perspectives AS side
                JOIN battle_evidence AS evidence ON evidence.id = side.evidence_id
                WHERE side.perspective = 'attacker' AND side.battle_id IN (
                    SELECT (contribution ->> 'battle_identity')::bigint
                    FROM jsonb_array_elements(ranked.input_evidence -> 'contributions')
                        AS contribution
                    WHERE contribution ->> 'lens' = 'defense'
                      AND (contribution ->> 'battle_timestamp')::timestamptz
                          >= ranked.ranked_day_end - %s))
        FROM unnest(%s::bigint[], %s::bigint[]) AS reading (version_id, observation_id)
        JOIN ranked_day_versions AS ranked ON ranked.id = reading.version_id
        JOIN collector_observations AS observation ON observation.id = reading.observation_id
        """,
        (reset_total.ATTACK_ENDS_WITHIN, [reading[0] for reading in readings.values()],
         [reading[1] for reading in readings.values()]),
    ).fetchall()
    answers = {}
    for (player_id, state, exact, clean, failures, final, loss, day_start, coverage,
         contributions, kind, official, requested_at, ends) in rows:
        battles = [reset_total.Battle(
            (1 if item["lens"] == "offense" else -1) * int(item.get("amount_used") or 0),
            None if item.get("battle_timestamp") is None
            else datetime.fromisoformat(item["battle_timestamp"]),
            item["lens"] == "defense", item.get("disagreement") is True,
            None if (ended := (ends or {}).get(str(item["battle_identity"]))) is None
            else datetime.fromisoformat(ended),
        ) for item in contributions or () if item.get("included") is True]
        _, _, read_at, trophies = readings[player_id]
        answer = reset_total.reset_total(
            reset_at=boundary_at, day_start=day_start, state=text_value(state),
            failure_reasons=failures, end_before_loss=None if final is None else final + (loss or 0),
            eod=final, season_end=text_value(kind) == "season" or official is not None,
            official_final=official, battles=battles,
            battles_usable=bool(coverage) and not DISPUTED_BATTLE_REASONS & set(failures) and all(
                battle.stamped_at is not None and not battle.disputed for battle in battles),
            old_reading=reading_rule.Reading(read_at, trophies, requested_at=requested_at),
            end_proof=None, reading_proof=None)
        end_reset = text_value(kind) == "season" or (
            text_value(kind) == "weekly" and final is not None and final <= SEASON_START_TROPHIES
            and not clean)
        shown = trophies + sum(battle.change for battle in battles
                               if battle.stamped_at is not None and battle.stamped_at > read_at)
        if answer.basis == reset_total.CALCULATED_END and text_value(state) == "Complete" and (
                (exact or clean) and not any(reset_total.may_end_after(battle, boundary_at)
                                             for battle in battles)
                and (not end_reset or shown == final + (loss or 0))):
            answer = replace(answer, proven=True)
        answers[int(player_id)] = answer.evidence()
    return answers


def _army_rows(
    connection: Any, members: list[Any], generation: Any
) -> list[dict[str, Any]]:
    version_ids = sorted({int(row[1]) for row in members if row[1] is not None})
    versions = {
        int(row[0]): row[1:]
        for row in connection.execute(
            """
            SELECT id, state, start_baseline_id, end_baseline_id
            FROM ranked_day_versions
            WHERE id = ANY(%s::bigint[])
            """,
            (version_ids,),
        ).fetchall()
    }
    daily_logs = {
        int(row[0]): row[1:]
        for row in connection.execute(
            """
            SELECT DISTINCT ON (ranked_day_version_id)
                   ranked_day_version_id, id, battles
            FROM api_player_daily_logs
            WHERE ranked_day_version_id = ANY(%s::bigint[])
            ORDER BY ranked_day_version_id, id DESC
            """,
            (version_ids,),
        ).fetchall()
    }
    selections = _army_selections(
        connection,
        {version_id: log[1] for version_id, log in daily_logs.items()},
    )
    manifest_rows: list[dict[str, Any]] = []
    for row in members:
        player_id, version_id, input_hash, status = _member(row, "army")
        classification = _STATUS_CLASSIFICATIONS.get(status, "Pending")
        if _rechecks(status, version_id):
            if status == "partial":
                classification = "Partial"
            else:
                version = versions.get(version_id)
                state = (
                    text_value(version[0]) if version is not None else "Malformed"
                )
                classification = (
                    state
                    if state in {"Complete", "Partial", "Malformed", "Inconsistent"}
                    else "Partial"
                )
        identity: dict[str, Any] = {
            "artifact_kind": "army",
            "generation": int(generation[1]),
            "player_id": player_id,
            "ranked_day_version_id": version_id,
            "input_hash": input_hash,
            "classification": classification,
        }
        if version_id is not None:
            # The day's evidence stays in ranked_day_versions and input_hash
            # pins it; copying it here made army rows ~46 KB each.
            version = versions.get(version_id)
            if version is not None:
                identity.update(
                    {"start_baseline_id": version[1], "end_baseline_id": version[2]}
                )
            daily_log = daily_logs.get(version_id)
            identity["daily_log_id"] = int(daily_log[0]) if daily_log else None
            battle_ids, decode_ids, evidence_ids = selections.get(
                version_id, ([], [], [])
            )
            identity["battle_ids"] = sorted(set(battle_ids))
            identity["decode_ids"] = decode_ids
            identity["evidence_ids"] = sorted(set(evidence_ids))
        manifest_rows.append(identity)
    return manifest_rows


def _sides(battles: Any) -> list[tuple[int, Any]]:
    """A daily log's listed (battle, lens) sides, in listed order."""
    return [
        (int(event["battle_id"]), event.get("lens"))
        for event in (battles if isinstance(battles, list) else [])
        if isinstance(event, dict) and str(event.get("battle_id", "")).isdigit()
    ]


def _perspective(lens: Any) -> str:
    return "attacker" if lens == "offense" else "defender"


def _army_selections(
    connection: Any, battles_by_day: Mapping[int, Any]
) -> dict[int, tuple[list[int], list[int], list[int]]]:
    """Each daily log's listed battles, frozen decodes and listed reports.

    The same as boundary_battles.army_decode_selection plus each listed side's
    report on the battle it is on now, per log, read for all logs at once.
    """
    sides_by_day = {day: _sides(battles) for day, battles in battles_by_day.items()}
    listed = sorted({battle_id for sides in sides_by_day.values() for battle_id, _ in sides})
    if not listed:
        return {}
    reports = _reports(connection, listed)
    repairs = _repairs(connection, listed)
    moved_by_day = {
        day: _moved(
            {
                battle_id: repairs[battle_id]
                for battle_id, _ in sides
                if battle_id in repairs
            },
            set(sides),
            {
                reports[(battle_id, perspective)]
                for battle_id, _ in sides
                for perspective in ("attacker", "defender")
                if (battle_id, perspective) in reports
            },
        )
        for day, sides in sides_by_day.items()
    }
    moved_sides = sorted(
        {
            (to_id, _perspective(lens))
            for moved in moved_by_day.values()
            for (_battle_id, lens), to_id in moved.items()
        }
    )
    reports.update(
        _reports(
            connection,
            sorted({to_id for to_id, _ in moved_sides} - set(listed)),
        )
    )
    decodes: dict[int, list[int]] = {}
    for battle_id, decode_id in connection.execute(
        f"SELECT battle_id, id FROM {CURRENT_DECODES} AS decode",
        (DECODER_VERSION, listed),
    ).fetchall():
        decodes.setdefault(int(battle_id), []).append(int(decode_id))
    moved_decodes: dict[tuple[int, str], list[int]] = {}
    for battle_id, perspective, decode_id in connection.execute(
        f"""
        SELECT decode.battle_id, decode.perspective, decode.id
        FROM {CURRENT_DECODES} AS decode
        JOIN unnest(%s::bigint[], %s::text[]) AS side (battle_id, perspective)
          USING (battle_id, perspective)
        """,
        (
            DECODER_VERSION,
            [to_id for to_id, _ in moved_sides],
            [to_id for to_id, _ in moved_sides],
            [perspective for _, perspective in moved_sides],
        ),
    ).fetchall():
        moved_decodes.setdefault(
            (int(battle_id), text_value(perspective)), []
        ).append(int(decode_id))
    selections: dict[int, tuple[list[int], list[int], list[int]]] = {}
    for day, sides in sides_by_day.items():
        moved = moved_by_day[day]
        battle_ids = [battle_id for battle_id, _ in sides]
        decode_ids = sorted(
            {
                *(
                    decode_id
                    for battle_id in battle_ids
                    for decode_id in decodes.get(battle_id, [])
                ),
                *(
                    decode_id
                    for (_battle_id, lens), to_id in moved.items()
                    for decode_id in moved_decodes.get(
                        (to_id, _perspective(lens)), []
                    )
                ),
            }
        )
        evidence_ids = [
            reports[key]
            for key in (
                (moved.get((battle_id, lens), battle_id), _perspective(lens))
                for battle_id, lens in sides
            )
            if key in reports
        ]
        selections[day] = (battle_ids, decode_ids, evidence_ids)
    return selections


def _repairs(
    connection: Any, battle_ids: list[int]
) -> dict[int, list[tuple[str, int, set[int]]]]:
    """The 0057 repairs of these battles, oldest first: each side's lens, the
    battle it moved to and the reports still saved on its old battle side."""
    repairs: dict[int, list[tuple[str, int, set[int]]]] = {}
    if not battle_ids:
        return repairs
    for from_id, perspective, to_id, side_reports in connection.execute(
        """
        SELECT repair.from_battle_id, repair.perspective, repair.to_battle_id,
               ARRAY(
                   SELECT evidence.id FROM battle_evidence AS evidence
                   WHERE evidence.battle_id = repair.from_battle_id
                     AND evidence.perspective = repair.perspective
               )
        FROM battle_day_repairs AS repair
        WHERE repair.from_battle_id = ANY(%s::bigint[])
        ORDER BY repair.id
        """,
        (battle_ids,),
    ).fetchall():
        repairs.setdefault(int(from_id), []).append(
            (
                "offense" if text_value(perspective) == "attacker" else "defense",
                int(to_id),
                {int(report) for report in side_reports},
            )
        )
    return repairs


def _moved(
    repairs: Mapping[int, list[tuple[str, int, set[int]]]],
    listed_sides: Collection[tuple[int, Any]],
    listed_reports: set[int],
) -> dict[tuple[int, str], int]:
    """battle_day_repair.merged_battles for these repairs of listed battles:
    each listed side 0057 moved, unless a listed report is still on its old
    battle side, mapped to the battle it is on now. Only the listed reports
    on repaired sides matter."""
    moved: dict[tuple[int, str], int] = {}
    for battle_id in sorted(repairs):
        for lens, to_id, side_reports in repairs[battle_id]:
            if (battle_id, lens) in listed_sides and not side_reports & listed_reports:
                moved[(battle_id, lens)] = to_id
    return moved


def _reports(connection: Any, battle_ids: list[int]) -> dict[tuple[int, str], int]:
    """The selected report of each side of these battles."""
    if not battle_ids:
        return {}
    return {
        (int(row[0]), text_value(row[1])): int(row[2])
        for row in connection.execute(
            """
            SELECT battle_id, perspective, evidence_id
            FROM battle_perspectives
            WHERE battle_id = ANY(%s::bigint[])
            """,
            (battle_ids,),
        ).fetchall()
    }


def _season_inputs(connection: Any, members: list[Any]) -> dict[str, Any] | None:
    """Every input of the Season the population's ranked days belong to."""
    season_row = connection.execute(
        """
        SELECT official_season_id
        FROM ranked_day_versions
        WHERE id = ANY(%s::bigint[])
        ORDER BY id
        LIMIT 1
        """,
        ([row[1] for row in members if row[1] is not None],),
    ).fetchone()
    if season_row is None:
        return None
    season_versions = connection.execute(
        """
        SELECT id
        FROM ranked_day_versions
        WHERE official_season_id = %s
          AND state = 'Complete' AND coverage_complete
        ORDER BY id
        """,
        (season_row[0],),
    ).fetchall()
    season_version_ids = [int(row[0]) for row in season_versions]
    # The logs' battle lists, about 8 KB each and one per player per Season
    # day, stay in the database. One statement returns the logs it picks,
    # each listed battle once in id order, and the sides listed for battles
    # 0057 repaired: only those sides can have moved.
    log_ids, battle_ids, repaired_ids, repaired_lenses = connection.execute(
        """
        WITH log AS (
            SELECT DISTINCT ON (ranked_day_version_id)
                   ranked_day_version_id, id, battles
            FROM api_player_daily_logs
            WHERE ranked_day_version_id = ANY(%s::bigint[])
              AND state = 'Complete' AND coverage = 'complete'
            ORDER BY ranked_day_version_id, version DESC, id DESC
        ), side AS (
            SELECT (event ->> 'battle_id')::bigint AS battle_id,
                   event ->> 'lens' AS lens
            FROM log
            CROSS JOIN LATERAL jsonb_array_elements(log.battles) AS event
            WHERE event ->> 'battle_id' ~ '^[0-9]+$'
        )
        SELECT logs.ids, battles.ids, repaired.ids, repaired.lenses
        FROM (SELECT array_agg(id ORDER BY ranked_day_version_id) AS ids
              FROM log) AS logs,
             (SELECT array_agg(DISTINCT battle_id ORDER BY battle_id) AS ids
              FROM side) AS battles,
             (SELECT array_agg(battle_id) AS ids, array_agg(lens) AS lenses
              FROM side
              WHERE battle_id IN (SELECT from_battle_id FROM battle_day_repairs)
             ) AS repaired
        """,
        (season_version_ids,),
    ).fetchone()
    season_daily_log_ids = [int(log_id) for log_id in log_ids or ()]
    season_battle_ids = [int(battle_id) for battle_id in battle_ids or ()]
    season_sides = {
        (int(battle_id), lens)
        for battle_id, lens in zip(repaired_ids or (), repaired_lenses or ())
    }
    season_evidence_ids = [
        int(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT evidence_id
            FROM battle_perspectives
            WHERE battle_id = ANY(%s::bigint[])
            ORDER BY evidence_id
            """,
            (season_battle_ids,),
        ).fetchall()
    ]
    season_repairs = _repairs(connection, season_battle_ids)
    repaired_reports = {
        report
        for repairs in season_repairs.values()
        for _lens, _to_id, side_reports in repairs
        for report in side_reports
    }
    season_moved = _moved(
        season_repairs,
        season_sides,
        {report for report in season_evidence_ids if report in repaired_reports},
    )
    season_decode_ids = sorted(
        {
            *(
                int(row[0])
                for row in connection.execute(
                    f"SELECT id FROM {CURRENT_DECODES} AS decode",
                    (DECODER_VERSION, season_battle_ids),
                ).fetchall()
            ),
            *moved_decode_ids(connection, season_moved),
        }
    )
    season_evidence_ids = sorted(
        {
            *season_evidence_ids,
            *(
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT perspective.evidence_id
                    FROM battle_perspectives AS perspective
                    JOIN unnest(%s::bigint[], %s::text[])
                      AS side (battle_id, perspective)
                      USING (battle_id, perspective)
                    """,
                    moved_side_arrays(season_moved),
                ).fetchall()
            ),
        }
    )
    return {
        "ranked_version_ids": season_version_ids,
        "daily_log_ids": season_daily_log_ids,
        "battle_ids": season_battle_ids,
        "decode_ids": season_decode_ids,
        "evidence_ids": season_evidence_ids,
    }
