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
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from .analytics import season_attack_tallies
from .army_decoder import DECODER_VERSION
from .catalog import CATALOG_VERSION
from .db import Database, _text_value
from .domain import RANKED_DAY_DURATION, ranked_day_for, season_is_current
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
_RANKED_SNAPSHOT_STATUSES = {
    "Complete": "complete",
    "Partial": "partial",
    "Inconsistent": "inconsistent",
    "Malformed": "malformed",
}


def freeze_boundary_manifest(
    database: Database,
    connection: Any,
    *,
    generation_id: int,
    artifact_kind: str,
) -> tuple[int, str] | None:
    """Freeze one sorted coordinator input before creating its job.

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
    if existing is not None:
        return int(existing[0]), _text_value(existing[1])
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
    if artifact_kind == "snapshot":
        manifest_rows = _snapshot_rows(connection, members, generation)
    else:
        manifest_rows = _army_rows(connection, members, generation)
    season_inputs = (
        _season_inputs(connection, members) if artifact_kind == "army" else None
    )
    rule_versions = {
        "ordering_rule_version": _text_value(generation[2]),
        "freshness_rule_version": _text_value(generation[3]),
        "analytics_rule_version": (
            _text_value(generation[5])
            if artifact_kind == "army"
            else _text_value(generation[4])
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
                "manifest_id": manifest_id,
                "artifact_kind": artifact_kind,
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
    return _text_value(row[0]), {
        "generation": int(row[1]),
        "artifact_kind": _text_value(row[2]),
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
        _text_value(row[2]) if row[2] is not None else None,
        _text_value(row[3] if artifact_kind == "snapshot" else row[4]),
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
    # The ended day's Season attacks up to the Reset, which order equal
    # trophies (analytics.tie_order_key).
    season_attacks = season_attack_tallies(
        connection,
        season_start=ranked_day_for(boundary_at - RANKED_DAY_DURATION).season_start,
        cutoff=boundary_at,
        player_ids=player_ids,
    )
    # Each player's newest accepted profile at the Reset, chosen exactly as
    # for one player, with the population in one query.
    profiles = {
        int(row[0]): row[1:]
        for row in connection.execute(
            """
            SELECT member.player_id, profile.*
            FROM unnest(%s::bigint[]) AS member (player_id)
            CROSS JOIN LATERAL (
                SELECT profile.id,
                       COALESCE(effect.observation_id, profile.observation_id),
                       COALESCE(effect.observed_at, profile.observed_at),
                       profile.profile_json, profile.normalized_tag,
                       profile.name, profile.trophies, profile.eligibility_state,
                       profile.current_league_season_id
                FROM player_profile_versions AS profile
                LEFT JOIN player_profile_effects AS effect
                  ON effect.profile_version_id = profile.id
                WHERE profile.player_id = member.player_id
                  AND COALESCE(effect.observed_at, profile.observed_at) <= %s
                  AND profile.source_contract_state = 'accepted'
                ORDER BY COALESCE(effect.observed_at, profile.observed_at) DESC,
                         COALESCE(effect.id, profile.id) DESC
                LIMIT 1
            ) AS profile
            """,
            (player_ids, boundary_at),
        ).fetchall()
    }
    # Why a player with no accepted profile has none: their newest profile's
    # source state and their newest profile response's failure.
    unprofiled = [player_id for player_id in player_ids if player_id not in profiles]
    not_found = profiles_not_found(
        connection, boundary_at, {player_id: row[2] for player_id, row in profiles.items()}
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
        (int(row[0]), int(row[1])): _text_value(row[2])
        for row in connection.execute(
            "SELECT id, player_id, state FROM ranked_day_versions WHERE id = ANY(%s::bigint[])",
            ([row[1] for row in members if row[1] is not None],),
        ).fetchall()
    }
    manifest_rows: list[dict[str, Any]] = []
    for row in members:
        player_id, version_id, input_hash, status = _member(row, "snapshot")
        classification = _STATUS_CLASSIFICATIONS.get(status, "Pending")
        if _rechecks(status, version_id):
            # The member's status, checked again against its ranked day and
            # accepted profile now, as boundary._boundary_snapshot_status.
            snapshot_status = _RANKED_SNAPSHOT_STATUSES.get(
                ranked_states.get((version_id, player_id), ""), "pending"
            )
            if snapshot_status == "complete" and player_id not in profiles:
                snapshot_status = "missing"
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
        tally = season_attacks.get(player_id)
        identity["season_attacks"] = (
            {"attacks": tally[0], "destruction": tally[1]} if tally else None
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
                "tag": _text_value(profile[4]),
                "name": profile[5],
                "trophies": int(profile[6]),
                "observed_at": profile[2].astimezone(UTC).isoformat(),
                "eligibility_state": _text_value(profile[7]),
                "profile_json": profile[3],
            }
            if _text_value(profile[7]) != "eligible":
                identity["snapshot_quality"] = "invalid"
            elif not season_is_current(
                _text_value(profile[8]), generation[0] - RANKED_DAY_DURATION
            ):
                # Trophies from before this player's Season reset never
                # stand for the ended day's Season.
                identity["snapshot_quality"] = "season_reset_pending"
            elif player_id in not_found:
                # The player went missing after this reading, so its
                # trophies no longer stand for them at the Reset.
                identity["snapshot_quality"] = "profile_not_found"
            else:
                identity["snapshot_quality"] = "eligible"
        else:
            identity["profile_version_id"] = None
            identity["profile_input_hash"] = None
            latest = failures.get(player_id)
            source_state = _text_value(latest[0]) if latest and latest[0] else None
            failure = _text_value(latest[1]) if latest and latest[1] else None
            if failure in {
                "malformed_json",
                "unsupported_profile_schema",
                "source_identity_mismatch",
                "invalid_player_tag",
            }:
                snapshot_quality = "malformed"
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
        manifest_rows.append(identity)
    return manifest_rows


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
    loss, and whether they are proven.

    ``readings`` maps a player to the version of their day ending at
    ``boundary_at``, their reading's saved response, its time and its
    trophies. A Complete day already proves the total: its Reset readings at
    both ends and every battle between agree, so its end plus its automatic
    loss is the total whatever the reading shows. An attacker's profile can
    show an attack minutes after its report time: on 7 October 2026
    #2QCYU8C2G read 4,703 at 04:37:05 without its attack stamped 04:34:08,
    and the board showed 4,902, not 4,931. A Reset that resets trophies
    proves only the start, so there the reading must agree too.

    Otherwise the total is the reading plus the day's battles stamped after
    it, which needs the day's continuous battle logs with no trophy
    mismatch, a reading taken at least 15 minutes into that day, after the
    previous day's last reports and automatic defense loss, a time on every
    battle the day counts, none stamped between the reading's request and its
    response, no defense stamped in the 4 minutes before that request, since
    its attack can end up to 4 minutes after the defender's report, and no
    battle amount the two players' logs disagree on. A reading proves no
    battle stamped before it, so that total is proven only when the day's
    start reading plus all its battles, or without a start its end Reset
    reading, with or without its known automatic loss, comes to it too.
    Without the battles' proof the total is the reading alone; any total not
    proven is marked uncertain.
    """
    if not readings:
        return {}
    days = {
        int(row[0]): row[1:]
        for row in connection.execute(
            """
            SELECT reading.player_id, ranked.state,
                   ranked.final_trophies_before_reset,
                   ranked.automatic_defense_loss,
                   ranked.automatic_defense_evidence_state,
                   ranked.input_evidence->>'boundary_kind',
                   CASE WHEN NOT ranked.failure_reasons
                                 ?| ARRAY['missing_start_baseline',
                                          'start_baseline_incomplete']
                        THEN ranked.start_trophies
                   END,
                   CASE WHEN NOT ranked.failure_reasons
                                 ?| ARRAY['missing_end_baseline',
                                          'end_baseline_incomplete']
                        THEN (ranked.input_evidence->>'next_start_trophies')::integer
                   END,
                   ranked.coverage_complete
                   AND reading.observed_at
                       >= ranked.ranked_day_start + interval '15 minutes'
                   AND ranked.state <> 'Inconsistent'
                   AND NOT ranked.failure_reasons ?| %s::text[]
                   AND battles.every_battle_proven IS NOT FALSE,
                   battles.after_reading, battles.whole_day
            FROM unnest(
                %s::bigint[], %s::bigint[], %s::bigint[], %s::timestamptz[]
            ) AS reading (player_id, version_id, observation_id, observed_at)
            JOIN ranked_day_versions AS ranked
              ON ranked.id = reading.version_id
             AND ranked.player_id = reading.player_id
             AND ranked.ranked_day_end = %s
            JOIN collector_observations AS observation
              ON observation.id = reading.observation_id
            CROSS JOIN LATERAL (
                SELECT COALESCE(sum(battle.change) FILTER (
                           WHERE battle.stamped_at > reading.observed_at
                       ), 0),
                       COALESCE(sum(battle.change), 0),
                       bool_and(
                           battle.stamped_at IS NOT NULL
                           AND battle.stamped_at
                               NOT BETWEEN observation.request_started_at
                                           - CASE WHEN battle.lens = 'defense'
                                                  THEN interval '4 minutes'
                                                  ELSE interval '0' END
                                       AND reading.observed_at
                           AND battle.disputed IS DISTINCT FROM 'true'
                       )
                FROM jsonb_array_elements(ranked.input_evidence->'contributions')
                    AS contribution
                CROSS JOIN LATERAL (
                    SELECT CASE WHEN contribution.value->>'lens' = 'offense'
                                THEN 1 ELSE -1 END
                           * (contribution.value->>'amount_used')::integer,
                           (contribution.value->>'battle_timestamp')::timestamptz,
                           contribution.value->>'lens',
                           contribution.value->>'disagreement'
                ) AS battle (change, stamped_at, lens, disputed)
                WHERE contribution.value->>'included' = 'true'
            ) AS battles (after_reading, whole_day, every_battle_proven)
            """,
            (
                sorted(DISPUTED_BATTLE_REASONS),
                list(readings),
                [reading[0] for reading in readings.values()],
                [reading[1] for reading in readings.values()],
                [reading[2] for reading in readings.values()],
                boundary_at,
            ),
        ).fetchall()
    }
    return {
        player_id: _reset_total(reading[3], days.get(player_id))
        for player_id, reading in readings.items()
    }


def _reset_total(reading: int, day: Any) -> tuple[int, bool]:
    """A player's trophies at the Reset and whether they are proven, from
    their reading and their day as ``reset_trophies`` reads it."""
    if day is None:
        return reading, False
    (
        state, final, automatic_loss, automatic_state, boundary_kind,
        start, end, battles_proven, after_reading, whole_day,
    ) = day
    total = reading + int(after_reading) if battles_proven else None
    # The game resets trophies at a Season's end, and raises a total at or
    # below 5,000 at a weekly one, so that Reset's reading proves nothing.
    end_reset = _text_value(boundary_kind) == "season" or (
        _text_value(boundary_kind) == "weekly"
        and (final if final is not None else end if end is not None else 0) <= 5000
    )
    if _text_value(state) == "Complete" and final is not None:
        settled = int(final) + int(automatic_loss or 0)
        if not end_reset or total == settled:
            return settled, True
    if total is None:
        return reading, False
    if start is not None:
        return total, int(start) + int(whole_day) == total
    known_loss = (
        int(automatic_loss or 0)
        if _text_value(automatic_state) in {"calculated", "confirmed"}
        else 0
    )
    return total, end is not None and not end_reset and int(end) in {
        total, total - known_loss
    }


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
    # Each ranked day's newest daily log.
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
                    _text_value(version[0]) if version is not None else "Malformed"
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

    The same as boundary._army_decode_selection plus each listed side's
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
        """
        SELECT battle_id, id FROM battle_army_decodes
        WHERE battle_id = ANY(%s::bigint[]) AND is_active
          AND decoder_version = %s AND catalog_version = %s
        """,
        (listed, DECODER_VERSION, CATALOG_VERSION),
    ).fetchall():
        decodes.setdefault(int(battle_id), []).append(int(decode_id))
    moved_decodes: dict[tuple[int, str], list[int]] = {}
    for battle_id, perspective, decode_id in connection.execute(
        """
        SELECT decode.battle_id, decode.perspective, decode.id
        FROM battle_army_decodes AS decode
        JOIN unnest(%s::bigint[], %s::text[]) AS side (battle_id, perspective)
          USING (battle_id, perspective)
        WHERE decode.is_active
          AND decode.decoder_version = %s AND decode.catalog_version = %s
        """,
        (
            [to_id for to_id, _ in moved_sides],
            [perspective for _, perspective in moved_sides],
            DECODER_VERSION,
            CATALOG_VERSION,
        ),
    ).fetchall():
        moved_decodes.setdefault(
            (int(battle_id), _text_value(perspective)), []
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
                "offense" if _text_value(perspective) == "attacker" else "defense",
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
        (int(row[0]), _text_value(row[1])): int(row[2])
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
                    """
                    SELECT id
                    FROM battle_army_decodes
                    WHERE battle_id = ANY(%s::bigint[])
                      AND decoder_version = %s AND catalog_version = %s
                      AND is_active
                    """,
                    (season_battle_ids, DECODER_VERSION, CATALOG_VERSION),
                ).fetchall()
            ),
            *_moved_decode_ids(connection, season_moved),
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
                    _moved_side_arrays(season_moved),
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


def _moved_side_arrays(
    moved: dict[tuple[int, str], int],
) -> tuple[list[int], list[str]]:
    return (
        list(moved.values()),
        ["attacker" if lens == "offense" else "defender" for _id, lens in moved],
    )


def _moved_decode_ids(
    connection: Any, moved: dict[tuple[int, str], int]
) -> list[int]:
    """Current decodes of moved sides, read on the battles they are on now."""
    if not moved:
        return []
    return [
        int(row[0])
        for row in connection.execute(
            """
            SELECT decode.id
            FROM battle_army_decodes AS decode
            JOIN unnest(%s::bigint[], %s::text[]) AS side (battle_id, perspective)
              USING (battle_id, perspective)
            WHERE decode.is_active
              AND decode.decoder_version = %s AND decode.catalog_version = %s
            """,
            (*_moved_side_arrays(moved), DECODER_VERSION, CATALOG_VERSION),
        ).fetchall()
    ]
