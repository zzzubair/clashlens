"""A Reset's manifest frozen for the whole population at once is the one the
per-player freeze built: the same rows and the same digest, so nothing a
published board froze changes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
from domain_test_support import domain_database, store_observation, text

from clashlens import battle_day_repair, boundary
from clashlens.army_decoder import DECODER_VERSION
from clashlens.boundary_manifest import _moved_decode_ids, _moved_side_arrays
from clashlens.catalog import CATALOG_VERSION
from clashlens.db import Database, _text_value
from clashlens.domain import RANKED_DAY_DURATION, ranked_day_for, season_is_current
from clashlens.worker import ObservationProcessor

BOUNDARY = datetime(2026, 8, 5, 5, tzinfo=UTC)
DAY = BOUNDARY - RANKED_DAY_DURATION
# Enough players that every modulus below picks several of them.
PLAYERS = 400


def seed_population(connection_info: str, players: int) -> int:
    """Seed ``players`` members of one Reset generation and their inputs.

    Player ``i`` varies by ``i`` modulo small numbers: statuses, missing,
    mismatched and non-Complete ranked days, newer and older profiles and
    effects, ineligible and old-Season profiles, no accepted profile with
    and without a failed or conflicting response, official ranks, extra and
    junk daily-log events, missing reports, inactive and old decodes, and
    battle sides 0057 moved or left. Returns the generation id.
    """
    season = ranked_day_for(DAY).official_season_id
    unprofiled = [i for i in range(1, players + 1) if i % 17 == 0]
    archive = (None, None, None, SimpleNamespace(objects={}))
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            """
            INSERT INTO players (id, normalized_tag, active)
            OVERRIDING SYSTEM VALUE
            SELECT i, '#P' || i, true FROM generate_series(1, %s) AS i
            """,
            (players,),
        )
        for i in unprofiled:
            if i // 17 % 3 == 0:
                continue
            store_observation(
                connection_info,
                archive,
                occurrence_key=f"profile-{i}",
                endpoint="profile",
                body=f"profile {i}".encode(),
                observed_at=BOUNDARY - timedelta(minutes=10),
                normalized_tag=f"#P{i}",
                existing_connection=connection,
                commit=False,
            )
        # Inputs whose parents this test does not need skip their checks.
        connection.execute("SET LOCAL session_replication_role = replica")
        connection.execute(
            """
            UPDATE python_processing_jobs AS job
            SET status = 'failed', completed_at = clock_timestamp(),
                failure_category = CASE WHEN player.id / 17 % 3 = 1
                    THEN 'malformed_json' ELSE 'archive_timeout' END
            FROM collector_observations AS observation, players AS player
            WHERE job.observation_id = observation.id
              AND observation.player_id = player.id
            """
        )
        sweep = connection.execute(
            """
            INSERT INTO collector_reset_sweeps
                (boundary_at, member_ids, membership_captured_at)
            VALUES (%s, %s, clock_timestamp())
            RETURNING id
            """,
            (BOUNDARY, list(range(1, players + 1))),
        ).fetchone()[0]
        generation_id, _ = boundary._create_boundary_generation(
            None,
            connection,
            boundary_at=BOUNDARY,
            sweep_id=sweep,
            player_ids=list(range(1, players + 1)),
            generation=1,
            supersedes_id=None,
        )
        parameters = {
            "players": players,
            "generation": generation_id,
            "boundary": BOUNDARY,
            "day": DAY,
            "season": season,
        }
        for statement in _SEED:
            connection.execute(statement, parameters)
    return generation_id


_STATUSES = "ARRAY['pending','partial','failed','missing','unavailable','inconsistent','malformed']"
_SEED = [
    # Every 23rd player has no ranked day; every 97th's names another player.
    # Players with no accepted profile (every 17th) cover each status.
    """
    INSERT INTO ranked_day_versions (
        id, player_id, ranked_day_start, ranked_day_end, official_season_id,
        season_day_number, season_anchor_rule_version,
        reconciliation_rule_version, result_hash, version, state, confidence,
        input_hash, evidence_complete, coverage_complete,
        start_baseline_id, end_baseline_id
    ) OVERRIDING SYSTEM VALUE
    SELECT i, CASE WHEN i %% 97 = 0 THEN i + 1 ELSE i END, %(day)s, %(boundary)s,
           %(season)s, 5, 'anchor', 'rules', encode(sha256(i::text::bytea), 'hex'),
           CASE WHEN i %% 97 = 0 THEN 2 ELSE 1 END, state, 'exact',
           repeat('b', 64), true, state = 'Complete',
           CASE WHEN i %% 6 = 0 THEN NULL ELSE i * 10 END,
           CASE WHEN i %% 8 = 0 THEN NULL ELSE i * 10 + 1 END
    FROM generate_series(1, %(players)s) AS i
    CROSS JOIN LATERAL (
        SELECT CASE
            WHEN i %% 17 = 0 AND i / 17 %% 7 = 1 THEN 'Partial'
            WHEN i %% 13 < 4
            THEN (ARRAY['Partial','Inconsistent','Malformed','Live'])[i %% 13 + 1]
            ELSE 'Complete' END
    ) AS chosen (state)
    WHERE i %% 23 <> 0
    """,
    f"""
    UPDATE boundary_publication_generation_members
    SET ranked_day_version_id = CASE WHEN player_id %% 23 = 0 THEN NULL ELSE player_id END,
        ranked_day_input_hash = CASE WHEN player_id %% 23 = 0 THEN NULL
            ELSE encode(sha256(player_id::text::bytea), 'hex') END,
        status = 'terminal',
        snapshot_status = CASE
            WHEN player_id %% 17 = 0 THEN ({_STATUSES})[player_id / 17 %% 7 + 1]
            WHEN player_id %% 10 < 6 THEN 'complete'
            ELSE ({_STATUSES})[player_id %% 7 + 1] END,
        army_status = CASE WHEN player_id %% 11 < 7 THEN 'complete'
            ELSE ({_STATUSES})[player_id / 3 %% 7 + 1] END
    WHERE generation_id = %(generation)s
    """,
    # Accepted profiles; every 7th player also has one after the Reset, every
    # 11th an older one and every 13th a second one seen at the same time.
    """
    INSERT INTO player_profile_versions (
        id, player_id, observation_id, normalized_tag, endpoint_version,
        schema_version, parser_version, observed_at, source_http_status, name,
        trophies, league_tier_id, league_tier_name, eligibility_state,
        profile_json, source_contract_state, current_league_season_id
    ) OVERRIDING SYSTEM VALUE
    SELECT copy * %(players)s + i, i, copy * %(players)s + i, '#P' || i, 'v1',
           'v1', 'parser', %(boundary)s + make_interval(mins => offset_minutes),
           200, 'Player ' || i, 5000 + i, 105000034, 'Legend League',
           CASE WHEN i %% 19 = 0 THEN 'ineligible' ELSE 'eligible' END,
           jsonb_build_object('tag', '#P' || i, 'trophies', 5000 + i,
                              'copy', copy),
           'accepted',
           CASE WHEN i %% 29 = 0 THEN '2026-07' ELSE %(season)s END
    FROM generate_series(1, %(players)s) AS i
    CROSS JOIN LATERAL (
        VALUES (0, -(i %% 50) - 1), (1, 1), (2, -120), (3, -(i %% 50) - 1)
    ) AS version (copy, offset_minutes)
    WHERE i %% 17 <> 0
      AND (copy = 0 OR copy = 1 AND i %% 7 = 0 OR copy = 2 AND i %% 11 = 0
           OR copy = 3 AND i %% 13 = 0)
    """,
    # Effects re-observe a profile: before the Reset for every 5th player,
    # after it on the older profile for every 22nd.
    """
    INSERT INTO player_profile_effects (
        profile_version_id, observation_id, effect_kind, observed_at,
        source_http_status, endpoint_version, schema_version, parser_version
    )
    SELECT i, 10 * %(players)s + i, 'current_profile',
           %(boundary)s - interval '30 seconds', 200, 'v1', 'v1', 'parser'
    FROM generate_series(1, %(players)s) AS i
    WHERE i %% 5 = 0 AND i %% 17 <> 0
    UNION ALL
    SELECT 2 * %(players)s + i, 11 * %(players)s + i, 'current_profile',
           %(boundary)s + interval '1 minute', 200, 'v1', 'v1', 'parser'
    FROM generate_series(1, %(players)s) AS i
    WHERE i %% 22 = 0 AND i %% 17 <> 0
    """,
    # Every 51st player's only profile conflicts with its source.
    """
    INSERT INTO player_profile_versions (
        id, player_id, observation_id, normalized_tag, endpoint_version,
        schema_version, parser_version, observed_at, source_http_status, name,
        trophies, league_tier_id, league_tier_name, eligibility_state,
        profile_json, source_contract_state
    ) OVERRIDING SYSTEM VALUE
    SELECT 3 * %(players)s + i, i, 3 * %(players)s + i, '#P' || i, 'v1', 'v1',
           'parser', %(boundary)s - interval '5 minutes', 200, 'Player ' || i,
           5000, 105000034, 'Legend League', 'eligible', '{}', 'conflict'
    FROM generate_series(1, %(players)s) AS i
    WHERE i %% 51 = 0
    """,
    """
    INSERT INTO official_top200_attempts (id, observation_id, parser_version, outcome, observed_at)
    OVERRIDING SYSTEM VALUE
    VALUES (1, 1, 'parser', 'official_observed', %(boundary)s - interval '30 minutes')
    """,
    """
    INSERT INTO official_top200_versions (id, attempt_id, observation_id, observed_at, parser_version)
    OVERRIDING SYSTEM VALUE
    VALUES (1, 1, 1, %(boundary)s - interval '30 minutes', 'parser')
    """,
    """
    INSERT INTO official_top200_version_entries (
        version_id, source_row_id, rank, player_id, normalized_tag, source_row_index
    )
    SELECT 1, rank, rank, 3 * rank, '#P' || 3 * rank, rank - 1
    FROM generate_series(1, 200) AS rank
    """,
    # Each ranked day's log lists 4-12 battles, the odd ones attacks. Every
    # 31st also lists events that are not battles, or whose ids are text,
    # signed, fractional, written with an exponent or not numbers; every 13th
    # has an older log.
    """
    INSERT INTO api_player_daily_logs (
        id, player_id, ranked_day_start, version, state, coverage, battles,
        official_season_id, ranked_day_version_id
    ) OVERRIDING SYSTEM VALUE
    SELECT copy * %(players)s + version.id, version.player_id, %(day)s,
           CASE WHEN copy = 0 THEN version.version ELSE version.version + 5 END,
           CASE WHEN version.state = 'Complete' THEN 'Complete' ELSE 'Partial' END,
           CASE WHEN version.state = 'Complete' THEN 'complete' ELSE 'partial' END,
           (
               SELECT jsonb_agg(jsonb_build_object(
                   'battle_id', version.id * 100 + k,
                   'lens', CASE WHEN k %% 2 = 1 THEN 'offense' ELSE 'defense' END
               ) ORDER BY k)
               FROM generate_series(1, version.id %% 9 + 4 - copy) AS k
           ) || CASE WHEN version.id %% 31 = 0
               THEN '["junk", 5, null, [1], {"battle_id": "abc"}, {"battle_id": 7},
                      {"battle_id": "0712", "lens": "offense"}, {"battle_id": -3},
                      {"battle_id": 1.5}, {"battle_id": 2.0}, {"battle_id": 1e2},
                      {"battle_id": true}, {"battle_id": null}, {"battle_id": " 9"},
                      {"battle_id": [8]}, {"battle_id": 4101, "lens": null},
                      {"lens": "defense"}]'::jsonb
               ELSE '[]'::jsonb END,
           %(season)s, version.id
    FROM ranked_day_versions AS version
    CROSS JOIN generate_series(0, 1) AS copy
    WHERE copy = 0 OR version.id %% 13 = 0
    """,
    """
    INSERT INTO legend_battles (id, ranked_day_start, attacker_player_id, defender_player_id)
    OVERRIDING SYSTEM VALUE
    SELECT version.id * 100 + k, %(day)s, version.player_id, 1000000 + version.id * 100 + k
    FROM ranked_day_versions AS version, generate_series(1, 12) AS k
    WHERE k <= version.id %% 9 + 4
    UNION ALL
    SELECT 9000000 + id, %(day)s, player_id, 1000000 + 9000000 + id
    FROM ranked_day_versions WHERE id %% 41 = 0
    """,
    """
    INSERT INTO battle_evidence (
        id, battle_id, source_row_id, observation_id, reporting_player_id,
        perspective, battle_timestamp, stars, destruction_percentage,
        attacker_gain, defender_loss, trophy_rule_version, source_observed_at,
        parser_version
    ) OVERRIDING SYSTEM VALUE
    SELECT battle.id * 2 + side.offset_id, battle.id, battle.id * 2 + side.offset_id,
           battle.id * 2 + side.offset_id,
           battle.attacker_player_id, side.perspective, %(day)s, 2, 80, 30, 30,
           'rule', %(day)s, 'parser'
    FROM legend_battles AS battle
    CROSS JOIN (VALUES (0, 'attacker'), (1, 'defender')) AS side (offset_id, perspective)
    """,
    # Every 5th battle has no defender report selected.
    """
    INSERT INTO battle_perspectives (battle_id, perspective, evidence_id, source_observed_at)
    SELECT battle_id, perspective, id, %(day)s
    FROM battle_evidence
    WHERE NOT (perspective = 'defender' AND battle_id %% 5 = 0)
    """,
    # Every 3rd defender decode is inactive, every 4th battle also has an old
    # decoder's decode.
    """
    INSERT INTO battle_army_decodes (
        id, battle_id, evidence_id, decoder_version, catalog_version,
        catalog_hash, status, failure_category, is_active, perspective
    ) OVERRIDING SYSTEM VALUE
    SELECT evidence.id, evidence.battle_id, evidence.id, 'army-decoder-v2',
           'unit-catalog-v2', repeat('c', 64), 'failed', 'fixture',
           NOT (evidence.perspective = 'defender' AND evidence.battle_id %% 3 = 0),
           evidence.perspective
    FROM battle_evidence AS evidence
    UNION ALL
    SELECT 100000000 + evidence.id, evidence.battle_id, evidence.id,
           'army-decoder-v1', 'unit-catalog-v2', repeat('c', 64), 'failed',
           'fixture', true, evidence.perspective
    FROM battle_evidence AS evidence
    WHERE evidence.battle_id %% 4 = 0
    """,
    # 0057 moved both sides of every 41st day's first attack to another
    # battle and dropped their reports there; the day lists only the attack.
    # Every 41st + 1 day's repair left its report in place.
    """
    INSERT INTO battle_day_repairs (
        from_battle_id, to_battle_id, perspective, evidence_id,
        attacker_player_id, defender_player_id, from_day, to_day
    )
    SELECT id * 100 + 1, 9000000 + id - id %% 41, side.perspective,
           (id * 100 + 1) * 2 + side.offset_id, player_id, player_id + 1,
           %(day)s, %(day)s
    FROM ranked_day_versions
    CROSS JOIN (VALUES (0, 'attacker'), (1, 'defender')) AS side (offset_id, perspective)
    WHERE id %% 41 = 0 OR id %% 41 = 1 AND side.perspective = 'attacker'
    """,
    """
    DELETE FROM battle_perspectives
    WHERE battle_id IN (SELECT id * 100 + 1 FROM ranked_day_versions WHERE id %% 41 = 0)
    """,
]


def per_player_inputs(
    connection: Any, *, generation_id: int, artifact_kind: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The rows and Season inputs the per-player freeze built, reading one
    player at a time.

    Kept verbatim from boundary._freeze_boundary_manifest before it read
    the whole population at once.
    """
    generation = connection.execute(
        "SELECT boundary_at, generation FROM boundary_publication_generations WHERE id = %s",
        (generation_id,),
    ).fetchone()
    official_version_id = None
    if artifact_kind == "snapshot":
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
            (generation[0],),
        ).fetchone()
        official_version_id = int(official_version[0]) if official_version else None
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
    manifest_rows: list[dict[str, Any]] = []
    for ordinal, row in enumerate(members, start=1):
        player_id = int(row[0])
        version_id = int(row[1]) if row[1] is not None else None
        input_hash = _text_value(row[2]) if row[2] is not None else None
        status = _text_value(row[3] if artifact_kind == "snapshot" else row[4])
        classification = {
            "complete": "Complete",
            "partial": "Partial",
            "failed": "Failed",
            "missing": "Missing",
            "unavailable": "Unavailable",
            "inconsistent": "Inconsistent",
            "malformed": "Malformed",
        }.get(status, "Pending")
        if (
            status in {"complete", "partial", "inconsistent", "malformed"}
            and version_id is not None
        ):
            if artifact_kind == "snapshot":
                classification = {
                    "complete": "Complete",
                    "partial": "Partial",
                    "missing": "Missing",
                    "unavailable": "Unavailable",
                    "inconsistent": "Inconsistent",
                    "malformed": "Malformed",
                }.get(
                    boundary._boundary_snapshot_status(
                        connection,
                        player_id=player_id,
                        ranked_day_version_id=version_id,
                        boundary_at=generation[0],
                    ),
                    "Missing",
                )
            elif status == "partial":
                classification = "Partial"
            else:
                state_row = connection.execute(
                    "SELECT state FROM ranked_day_versions WHERE id = %s",
                    (version_id,),
                ).fetchone()
                state = (
                    _text_value(state_row[0])
                    if state_row is not None
                    else "Malformed"
                )
                classification = (
                    state
                    if state in {"Complete", "Partial", "Malformed", "Inconsistent"}
                    else "Partial"
                )
        identity = {
            "artifact_kind": artifact_kind,
            "generation": int(generation[1]),
            "player_id": player_id,
            "ranked_day_version_id": version_id,
            "input_hash": input_hash,
            "classification": classification,
        }
        if artifact_kind == "snapshot":
            identity["official_top200_version_id"] = official_version_id
            official_entry = None
            if official_version_id is not None:
                official_entry = connection.execute(
                    """
                    SELECT entry.rank, version.observed_at
                    FROM official_top200_version_entries AS entry
                    JOIN official_top200_versions AS version
                      ON version.id = entry.version_id
                    WHERE entry.version_id = %s AND entry.player_id = %s
                    """,
                    (official_version_id, player_id),
                ).fetchone()
            identity["official_rank"] = (
                int(official_entry[0]) if official_entry else None
            )
            identity["official_rank_observed_at"] = (
                official_entry[1].astimezone(UTC).isoformat()
                if official_entry
                else None
            )
            profile = connection.execute(
                """
                SELECT profile.id,
                       COALESCE(effect.observation_id, profile.observation_id),
                       COALESCE(effect.observed_at, profile.observed_at),
                       profile.profile_json, profile.normalized_tag,
                       profile.name, profile.trophies, profile.eligibility_state,
                       profile.current_league_season_id
                FROM player_profile_versions AS profile
                LEFT JOIN player_profile_effects AS effect
                  ON effect.profile_version_id = profile.id
                WHERE profile.player_id = %s
                  AND COALESCE(effect.observed_at, profile.observed_at) <= %s
                  AND profile.source_contract_state = 'accepted'
                ORDER BY COALESCE(effect.observed_at, profile.observed_at) DESC,
                         COALESCE(effect.id, profile.id) DESC
                LIMIT 1
                """,
                (player_id, generation[0]),
            ).fetchone()
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
                elif season_is_current(
                    _text_value(profile[8]), generation[0] - RANKED_DAY_DURATION
                ):
                    identity["snapshot_quality"] = "eligible"
                else:
                    # Trophies from before this player's Season reset never
                    # stand for the ended day's Season.
                    identity["snapshot_quality"] = "season_reset_pending"
            else:
                identity["profile_version_id"] = None
                identity["profile_input_hash"] = None
                latest = connection.execute(
                    """
                    SELECT (
                        SELECT profile.source_contract_state
                        FROM player_profile_versions AS profile
                        LEFT JOIN player_profile_effects AS effect
                          ON effect.profile_version_id = profile.id
                        WHERE profile.player_id = %s
                          AND COALESCE(effect.observed_at, profile.observed_at) <= %s
                        ORDER BY COALESCE(effect.observed_at, profile.observed_at) DESC,
                                 COALESCE(effect.id, profile.id) DESC
                        LIMIT 1
                    ), (
                        SELECT job.failure_category
                        FROM collector_observations AS observation
                        LEFT JOIN python_processing_jobs_worker AS job
                          ON job.observation_id = observation.id
                        WHERE observation.player_id = %s
                          AND observation.endpoint = 'profile'
                          AND observation.response_completed_at <= %s
                        ORDER BY observation.response_completed_at DESC,
                                 observation.id DESC
                        LIMIT 1
                    )
                    """,
                    (player_id, generation[0], player_id, generation[0]),
                ).fetchone()
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
        if artifact_kind == "army" and version_id is not None:
            # The day's evidence stays in ranked_day_versions and input_hash
            # pins it; copying it here made army rows ~46 KB each.
            ranked_identity = connection.execute(
                "SELECT start_baseline_id, end_baseline_id FROM ranked_day_versions WHERE id = %s",
                (version_id,),
            ).fetchone()
            if ranked_identity is not None:
                identity.update(
                    {
                        "start_baseline_id": ranked_identity[0],
                        "end_baseline_id": ranked_identity[1],
                    }
                )
            daily_log = connection.execute(
                "SELECT id, battles FROM api_player_daily_logs WHERE ranked_day_version_id = %s ORDER BY id DESC LIMIT 1",
                (version_id,),
            ).fetchone()
            identity["daily_log_id"] = int(daily_log[0]) if daily_log else None
            battle_ids, decode_ids, moved = boundary._army_decode_selection(
                connection, daily_log[1] if daily_log is not None else None
            )
            evidence_ids: list[int] = []
            if daily_log is not None and isinstance(daily_log[1], list):
                for event in daily_log[1]:
                    if (
                        not isinstance(event, dict)
                        or not str(event.get("battle_id", "")).isdigit()
                    ):
                        continue
                    perspective = (
                        "attacker" if event.get("lens") == "offense" else "defender"
                    )
                    evidence_row = connection.execute(
                        """
                        SELECT perspective.evidence_id
                        FROM battle_perspectives AS perspective
                        WHERE perspective.battle_id = %s
                          AND perspective.perspective = %s
                        """,
                        (
                            moved.get(
                                (int(event["battle_id"]), event.get("lens")),
                                int(event["battle_id"]),
                            ),
                            perspective,
                        ),
                    ).fetchone()
                    if evidence_row is not None:
                        evidence_ids.append(int(evidence_row[0]))
            identity["battle_ids"] = sorted(set(battle_ids))
            identity["decode_ids"] = decode_ids
            identity["evidence_ids"] = sorted(set(evidence_ids))
        manifest_rows.append(identity)
    season_inputs = None
    if artifact_kind == "army":
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
        if season_row is not None:
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
            season_logs = connection.execute(
                """
                SELECT DISTINCT ON (ranked_day_version_id) id, battles
                FROM api_player_daily_logs
                WHERE ranked_day_version_id = ANY(%s::bigint[])
                  AND state = 'Complete' AND coverage = 'complete'
                ORDER BY ranked_day_version_id, version DESC, id DESC
                """,
                (season_version_ids,),
            ).fetchall()
            season_daily_log_ids = [int(row[0]) for row in season_logs]
            season_sides = {
                (int(event["battle_id"]), event.get("lens"))
                for row in season_logs
                for event in (row[1] if isinstance(row[1], list) else [])
                if isinstance(event, dict)
                and str(event.get("battle_id", "")).isdigit()
            }
            season_battle_ids = sorted(
                {battle_id for battle_id, _lens in season_sides}
            )
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
            season_moved = {
                side: to_id
                for side, to_id in battle_day_repair.merged_battles(
                    connection, season_battle_ids, season_evidence_ids
                )[0].items()
                if side in season_sides
            }
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
            season_inputs = {
                "ranked_version_ids": season_version_ids,
                "daily_log_ids": season_daily_log_ids,
                "battle_ids": season_battle_ids,
                "decode_ids": season_decode_ids,
                "evidence_ids": season_evidence_ids,
            }
    return manifest_rows, season_inputs


def _digest(generation: int, artifact_kind: str, rule_versions: Any, rows: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "generation": generation,
                "artifact_kind": artifact_kind,
                "rule_versions": rule_versions,
                "rows": rows,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def test_whole_population_freeze_matches_the_per_player_freeze(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = seed_population(connection_info, PLAYERS)
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                for artifact_kind in ("snapshot", "army"):
                    expected, season_inputs = per_player_inputs(
                        connection,
                        generation_id=generation_id,
                        artifact_kind=artifact_kind,
                    )
                    manifest_id, digest = boundary._freeze_boundary_manifest(
                        database,
                        connection,
                        generation_id=generation_id,
                        artifact_kind=artifact_kind,
                    )
                    rule_versions = connection.execute(
                        "SELECT rule_versions FROM boundary_publication_manifests WHERE id = %s",
                        (manifest_id,),
                    ).fetchone()[0]
                    assert rule_versions == {
                        **{
                            key: rule_versions[key]
                            for key in (
                                "ordering_rule_version",
                                "freshness_rule_version",
                                "analytics_rule_version",
                            )
                        },
                        **(
                            {"season_inputs": season_inputs}
                            if season_inputs is not None
                            else {}
                        ),
                    }
                    assert digest == _digest(1, artifact_kind, rule_versions, expected)
                    stored = connection.execute(
                        """
                        SELECT ordinal, player_id, ranked_day_version_id,
                               input_hash, classification, unavailable_reason,
                               input_identity
                        FROM boundary_publication_manifest_rows
                        WHERE manifest_id = %s ORDER BY ordinal
                        """,
                        (manifest_id,),
                    ).fetchall()
                    assert [
                        (
                            row[0], row[1], row[2], text(row[3]), text(row[4]),
                            text(row[5]), _canonical(row[6]),
                        )
                        for row in stored
                    ] == [
                        (
                            ordinal,
                            identity["player_id"],
                            identity["ranked_day_version_id"],
                            identity["input_hash"],
                            identity["classification"],
                            "reset_baseline_failed"
                            if identity["classification"] == "Unavailable"
                            else None,
                            _canonical(identity),
                        )
                        for ordinal, identity in enumerate(expected, start=1)
                    ]
                    _assert_every_case_seeded(artifact_kind, expected)
                    if artifact_kind == "army":
                        # Reports on the battles moved sides are on now.
                        assert season_inputs is not None and any(
                            evidence_id >= 2 * 9000000
                            for evidence_id in season_inputs["evidence_ids"]
                        )
        finally:
            database.close()


def _assert_every_case_seeded(
    artifact_kind: str, rows: list[dict[str, Any]]
) -> None:
    classifications = {row["classification"] for row in rows}
    if artifact_kind == "snapshot":
        assert classifications == {
            "Complete", "Partial", "Failed", "Missing", "Unavailable",
            "Inconsistent", "Malformed", "Pending",
        }
        assert {row["snapshot_quality"] for row in rows} >= {
            "eligible", "invalid", "season_reset_pending", "malformed",
            "conflicting", "missing", "unavailable", "partial",
        }
        assert sum(row["official_rank"] is not None for row in rows) > 50
        return
    assert classifications >= {
        "Complete", "Partial", "Failed", "Missing", "Unavailable",
        "Inconsistent", "Malformed", "Pending",
    }
    assert any(
        (9000000 + row["player_id"]) * 2 in row.get("evidence_ids", []) for row in rows
    )
    assert any(
        (9000000 + row["player_id"]) * 2 in row.get("decode_ids", []) for row in rows
    )


# Day 2 of the Season that began on 5 October 2026, as Astra's report of
# 7 October found it: two players whose profiles went "not found" on 5 October
# ranked first and second on their last trophies, above ZOOS Yatta.
DAY_2_RESET = datetime(2026, 10, 7, 5, tzinfo=UTC)
_ARCHIVE = (None, None, None, SimpleNamespace(objects={}))


def _october(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def _seed_board(connection_info: str, readings: list[tuple[str, int, datetime]]) -> int:
    """One Day 2 generation whose members each have one accepted profile."""
    season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
    with psycopg.connect(connection_info) as connection:
        for index, (tag, trophies, observed_at) in enumerate(readings, start=1):
            connection.execute(
                "INSERT INTO players (id, normalized_tag, active)"
                " OVERRIDING SYSTEM VALUE VALUES (%s, %s, true)",
                (index, tag),
            )
        sweep = connection.execute(
            """
            INSERT INTO collector_reset_sweeps
                (boundary_at, member_ids, membership_captured_at)
            VALUES (%s, %s, clock_timestamp())
            RETURNING id
            """,
            (DAY_2_RESET, list(range(1, len(readings) + 1))),
        ).fetchone()[0]
        generation_id, _ = boundary._create_boundary_generation(
            None,
            connection,
            boundary_at=DAY_2_RESET,
            sweep_id=sweep,
            player_ids=list(range(1, len(readings) + 1)),
            generation=1,
            supersedes_id=None,
        )
        # The profiles' own responses are not needed.
        connection.execute("SET LOCAL session_replication_role = replica")
        for index, (tag, trophies, observed_at) in enumerate(readings, start=1):
            connection.execute(
                """
                INSERT INTO player_profile_versions (
                    player_id, observation_id, normalized_tag, endpoint_version,
                    schema_version, parser_version, observed_at,
                    source_http_status, name, trophies, league_tier_id,
                    league_tier_name, eligibility_state, profile_json,
                    source_contract_state, current_league_season_id
                ) VALUES (%s, %s, %s, 'v1', 'v1', 'parser', %s, 200, %s, %s,
                          105000034, 'Legend League', 'eligible', %s,
                          'accepted', %s)
                """,
                (
                    index, 900000 + index, tag, observed_at, tag, trophies,
                    json.dumps({"tag": tag, "trophies": trophies}), season,
                ),
            )
    return generation_id


def test_board_leaves_out_players_whose_profile_went_missing_before_the_reset(
    database_url: str,
) -> None:
    readings = [
        ("#PJ22PJPQJ", 5280, _october(5, 7, 51)),  # KURDiSTAN, 404 at 08:35
        ("#8LLLG2V99", 5277, _october(6, 6, 19)),  # Eason, 404 later that day
        ("#PPVYC88R", 5274, _october(6, 22, 31)),  # ZOOS Yatta
        ("#RECOVERED", 5250, _october(6, 6)),  # 404, then a success
        ("#AFTERRESET", 5240, _october(6, 8)),  # 404 only after the Reset
        ("#SERVERERROR", 5230, _october(6, 8)),  # a timeout says nothing
        ("#OLDREADING", 5220, _october(6, 4)),  # then only server errors
    ]
    responses = [
        ("#PJ22PJPQJ", 404, _october(5, 8, 35)),
        ("#8LLLG2V99", 404, _october(6, 8, 6)),
        ("#RECOVERED", 404, _october(6, 7)),
        ("#RECOVERED", 200, _october(6, 9)),
        ("#AFTERRESET", 404, _october(7, 6)),
        ("#SERVERERROR", 503, _october(6, 10)),
        ("#OLDREADING", 500, _october(6, 12)),
    ]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        for tag, status, at in responses:
            store_observation(
                connection_info, _ARCHIVE, occurrence_key=f"{tag}-{at.isoformat()}",
                endpoint="profile", body=f"{status} {tag}".encode(),
                observed_at=at, normalized_tag=tag, http_status=status,
            )
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                manifest_id, _ = boundary._freeze_boundary_manifest(
                    database, connection, generation_id=generation_id,
                    artifact_kind="snapshot",
                )
                rows = connection.execute(
                    """
                    SELECT input_identity->'profile_snapshot'->>'tag',
                           input_identity->>'snapshot_quality'
                    FROM boundary_publication_manifest_rows
                    WHERE manifest_id = %s
                    ORDER BY (input_identity->'profile_snapshot'->>'trophies')::int DESC
                    """,
                    (manifest_id,),
                ).fetchall()
        finally:
            database.close()
    assert [(text(tag), text(quality)) for tag, quality in rows] == [
        ("#PJ22PJPQJ", "profile_not_found"),
        ("#8LLLG2V99", "profile_not_found"),
        ("#PPVYC88R", "eligible"),
        ("#RECOVERED", "eligible"),
        ("#AFTERRESET", "eligible"),
        ("#SERVERERROR", "eligible"),
        ("#OLDREADING", "eligible"),
    ]
    # The board ranks only eligible rows, by trophies, so Yatta is first.


def test_board_rebuild_queues_one_correction_per_board_still_ranking_a_missing_player(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(
            connection_info,
            [
                ("#8LLLG2V99", 5277, _october(6, 6, 19)),
                ("#PPVYC88R", 5274, _october(6, 22, 31)),
            ],
        )
        database = Database(connection_info)
        try:
            # Frozen before its "not found" response was saved, as boards
            # built before this rule were.
            with database.pool.connection() as connection:
                boundary._freeze_boundary_manifest(
                    database, connection, generation_id=generation_id,
                    artifact_kind="snapshot",
                )
            store_observation(
                connection_info, _ARCHIVE, occurrence_key="eason-404",
                endpoint="profile", body=b"404", observed_at=_october(6, 8, 6),
                normalized_tag="#8LLLG2V99", http_status=404,
            )
            season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
            board = {
                "boundary_at": DAY_2_RESET.isoformat(),
                "generation": 1,
                "profile_not_found": 1,
                "late_battles": 0,
            }
            reports = [
                boundary.queue_board_rebuilds(database, season, queue=queue)
                for queue in (False, True, True)
            ]
            assert [report["boards"] for report in reports] == [
                [{**board, "correction": "not_queued"}],
                [{**board, "correction": "queued"}],
                [{**board, "correction": "already_queued"}],
            ]
            # The Season before is never read.
            earlier = str(int(season) - 28 * 86400)
            assert boundary.queue_board_rebuilds(database, earlier, queue=True)[
                "boards"
            ] == []
            with database.pool.connection() as connection:
                corrections = connection.execute(
                    """
                    SELECT boundary_at, source_generation_id, affected_artifacts,
                           pending_inputs, state
                    FROM boundary_publication_corrections
                    """
                ).fetchall()
        finally:
            database.close()
    assert [
        (row[0], row[1], sorted(text(value) for value in row[2]), row[3], text(row[4]))
        for row in corrections
    ] == [(DAY_2_RESET, generation_id, ["army", "snapshot"], [], "queued")]


def _seed_days(
    connection_info: str,
    generation_id: int,
    days: dict[int, tuple[bool, list[tuple[str, int, datetime, bool]]]],
) -> None:
    """Each member's Day 2: whether its battle logs are continuous, and its
    battles as (lens, trophies, report time, counted). Each reading gets the
    saved response a board entry points to."""
    season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
    with psycopg.connect(connection_info) as connection:
        observations = {
            player_id: store_observation(
                connection_info, _ARCHIVE, occurrence_key=f"reading-{player_id}",
                endpoint="profile", body=f"reading {player_id}".encode(),
                observed_at=observed_at, normalized_tag=text(tag),
                existing_connection=connection, commit=False,
            )[0]
            for player_id, tag, observed_at in connection.execute(
                "SELECT player_id, normalized_tag, observed_at"
                " FROM player_profile_versions WHERE player_id = ANY(%s)",
                (list(days),),
            ).fetchall()
        }
        connection.execute("SET LOCAL session_replication_role = replica")
        for player_id, (coverage_complete, battles) in days.items():
            connection.execute(
                "UPDATE player_profile_versions SET observation_id = %s"
                " WHERE player_id = %s",
                (observations[player_id], player_id),
            )
            contributions = [
                {
                    "battle_identity": str(player_id * 100 + index),
                    "lens": lens,
                    "amount_used": trophies,
                    "battle_timestamp": at.isoformat(),
                    "included": counted,
                    "valid": True,
                }
                for index, (lens, trophies, at, counted) in enumerate(battles)
            ]
            connection.execute(
                """
                INSERT INTO ranked_day_versions (
                    id, player_id, ranked_day_start, ranked_day_end,
                    official_season_id, season_day_number,
                    season_anchor_rule_version, reconciliation_rule_version,
                    result_hash, version, state, confidence, input_hash,
                    coverage_complete, input_evidence
                ) OVERRIDING SYSTEM VALUE
                VALUES (%s, %s, %s, %s, %s, 2, 'anchor', 'rules', repeat('a', 64),
                        1, 'Complete', 'exact', repeat('b', 64), %s, %s)
                """,
                (
                    player_id, player_id, DAY_2_RESET - RANKED_DAY_DURATION,
                    DAY_2_RESET, season, coverage_complete,
                    json.dumps({"contributions": contributions}),
                ),
            )
            connection.execute(
                """
                UPDATE boundary_publication_generation_members
                SET ranked_day_version_id = %s, ranked_day_input_hash = repeat('b', 64)
                WHERE generation_id = %s AND player_id = %s
                """,
                (player_id, generation_id, player_id),
            )


def _build_board(connection_info: str, database: Database, generation_id: int) -> list:
    """Freeze the generation's input, build its board, and return each
    entry's tag, trophies and confidence in rank order."""
    with database.pool.connection() as connection:
        manifest_id, digest = boundary._freeze_boundary_manifest(
            database, connection, generation_id=generation_id, artifact_kind="snapshot"
        )
        connection.execute(
            "UPDATE boundary_publication_generations SET snapshot_state = 'ready'"
            " WHERE id = %s",
            (generation_id,),
        )
        job_id = connection.execute(
            """
            INSERT INTO python_processing_jobs (
                observation_id, work_type, deduplication_key, input_json,
                status, due_at, max_attempts
            ) VALUES (NULL, 'build_snapshot', 'build_snapshot:day-2', %s,
                      'pending', clock_timestamp(), 10)
            RETURNING id
            """,
            (
                json.dumps(
                    {
                        "boundary_at": DAY_2_RESET.isoformat(),
                        "generation": 1,
                        "manifest_id": manifest_id,
                        "manifest_digest": digest,
                    }
                ),
            ),
        ).fetchone()[0]
    result = ObservationProcessor(database, None).process_job(job_id, owner="board")
    assert result is not None and result.outcome == "processed", database.scalar(
        "SELECT failure_detail FROM python_processing_jobs"
    )
    with database.pool.connection() as connection:
        return [
            (text(tag), trophies, text(confidence))
            for tag, trophies, confidence in connection.execute(
                """
                SELECT player.normalized_tag, entry.trophies, entry.confidence
                FROM leaderboard_snapshot_entries AS entry
                JOIN leaderboard_snapshots AS snapshot ON snapshot.id = entry.snapshot_id
                JOIN players AS player ON player.id = entry.player_id
                WHERE snapshot.snapshot_kind = 'frozen'
                ORDER BY entry.position
                """
            ).fetchall()
        ]


def test_board_adds_the_battles_after_each_reading(database_url: str) -> None:
    """Clash Spot's Day 2 board of 7 October 2026 shows trophies at the Reset
    before the automatic defense loss. Ours missed battles after each player's
    last reading: RAIN showed 5,088, not 5,168, and SnowBBcreaM 5,160, not
    5,128."""
    readings = [
        ("#29QUQC8QL", 5088, _october(7, 4, 40)),  # RAIN
        ("#YYPUCVJUP", 5160, _october(6, 22, 22)),  # SnowBBcreaM
        ("#QVCU9PJCR", 5155, _october(7, 4, 50)),  # SUPRA
        ("#2222222", 5150, _october(7, 4, 40)),  # battle logs have a gap
        ("#8888888", 5140, _october(6, 4, 50)),  # read before Day 2 began
    ]
    days = {
        1: (True, [
            ("offense", 40, _october(7, 4, 33), True),
            ("offense", 40, _october(7, 4, 37), True),  # already in the reading
            ("offense", 40, _october(7, 4, 53), True),
            ("offense", 40, _october(7, 4, 57), True),
            ("offense", 40, _october(7, 4, 57), False),  # a second report of it
        ]),
        2: (True, [("defense", 32, _october(7, 4, 57), True)]),
        # Stamped after the Reset, but it finished on Day 2.
        3: (True, [("defense", 40, datetime(2026, 10, 7, 5, 0, 2, tzinfo=UTC), True)]),
        4: (False, [("offense", 40, _october(7, 4, 50), True)]),
        5: (True, [("offense", 40, _october(7, 4, 50), True)]),
    }
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(connection_info, generation_id, days)
        database = Database(connection_info)
        try:
            assert _build_board(connection_info, database, generation_id) == [
                ("#29QUQC8QL", 5168, "confirmed"),
                ("#2222222", 5150, "uncertain"),
                ("#8888888", 5140, "uncertain"),
                ("#YYPUCVJUP", 5128, "confirmed"),
                ("#QVCU9PJCR", 5115, "confirmed"),
            ]
            season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
            # A board built under this rule is not rebuilt; one frozen before
            # it, ranking RAIN's reading alone, is.
            assert boundary.queue_board_rebuilds(database, season, queue=False)[
                "boards"
            ] == []
            with database.pool.connection() as connection:
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute(
                    "UPDATE leaderboard_snapshot_entries SET trophies = 5088"
                    " WHERE player_id = 1"
                )
            assert boundary.queue_board_rebuilds(database, season, queue=True)[
                "boards"
            ] == [
                {
                    "boundary_at": DAY_2_RESET.isoformat(),
                    "generation": 1,
                    "profile_not_found": 0,
                    "late_battles": 1,
                    "correction": "queued",
                }
            ]
        finally:
            database.close()


def test_board_keeps_a_reading_its_later_battles_cannot_prove(
    database_url: str,
) -> None:
    """A reading answered at 04:54:59 for a request sent a second earlier may
    or may not hold an attack stamped 04:54:58, and a battle whose trophies
    the two players' logs disagree on proves nothing; each such entry keeps
    its reading and is marked uncertain."""
    readings = [
        ("#INFLIGHT", 5088, datetime(2026, 10, 7, 4, 54, 59, tzinfo=UTC)),
        ("#DISPUTED", 5100, _october(7, 4, 40)),
        ("#DISPUTEDDAY", 5090, _october(7, 4, 40)),
    ]
    days = {
        1: (True, [
            ("offense", 40, datetime(2026, 10, 7, 4, 54, 58, tzinfo=UTC), True),
            ("offense", 40, _october(7, 4, 57), True),
        ]),
        2: (True, [("offense", 40, _october(7, 4, 57), True)]),
        3: (True, [("offense", 40, _october(7, 4, 57), True)]),
    }
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(connection_info, generation_id, days)
        with psycopg.connect(connection_info) as connection:
            connection.execute("SET LOCAL session_replication_role = replica")
            # One player's log says +40, the other's +0.
            connection.execute(
                """
                UPDATE ranked_day_versions
                SET input_evidence = jsonb_set(
                    input_evidence, '{contributions,0,disagreement}', 'true'
                )
                WHERE id = 2
                """
            )
            connection.execute(
                "UPDATE ranked_day_versions"
                " SET failure_reasons = '[\"duplicate_contribution_disagreement\"]'"
                " WHERE id = 3"
            )
        database = Database(connection_info)
        try:
            assert _build_board(connection_info, database, generation_id) == [
                ("#DISPUTED", 5100, "uncertain"),
                ("#DISPUTEDDAY", 5090, "uncertain"),
                ("#INFLIGHT", 5088, "uncertain"),
            ]
            season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
            assert boundary.queue_board_rebuilds(database, season, queue=False)[
                "boards"
            ] == []
        finally:
            database.close()


def test_board_keeps_a_reading_that_may_hold_a_defense_or_predate_the_daily_loss(
    database_url: str,
) -> None:
    """A defender's report can come up to 4 minutes before the attack ends, so
    a reading requested at 04:54:59 may or may not hold a defense stamped
    04:52. A reading no later than a Reset reading taken before the previous
    day's automatic defense loss still holds that loss. Each keeps its
    reading and is marked uncertain; an attack stamped 04:52, and a reading
    taken after that Reset reading, still prove their battles."""
    readings = [
        ("#DEFENDED", 5160, _october(7, 4, 55)),
        ("#ATTACKED", 5150, _october(7, 4, 55)),
        ("#RESETREAD", 5200, _october(6, 5, 2)),
        ("#LATERREAD", 5100, _october(7, 4, 40)),
    ]
    days = {
        1: (True, [("defense", 32, _october(7, 4, 52), True)]),
        2: (True, [
            ("offense", 40, _october(7, 4, 52), True),
            ("offense", 40, _october(7, 4, 58), True),
        ]),
        3: (True, [("offense", 40, _october(6, 6), True)]),
        4: (True, [("offense", 40, _october(7, 4, 50), True)]),
    }
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(connection_info, generation_id, days)
        with psycopg.connect(connection_info) as connection:
            reset_readings = {
                3: connection.execute(
                    "SELECT observation_id FROM player_profile_versions"
                    " WHERE player_id = 3"
                ).fetchone()[0],
                4: store_observation(
                    connection_info, _ARCHIVE, occurrence_key="reset-4",
                    endpoint="profile", body=b"reset 4",
                    observed_at=_october(6, 5, 1), normalized_tag="#LATERREAD",
                    existing_connection=connection, commit=False,
                )[0],
            }
            connection.execute("SET LOCAL session_replication_role = replica")
            for player_id, observation_id in reset_readings.items():
                connection.execute(
                    """
                    UPDATE ranked_day_versions
                    SET formula_components = jsonb_build_object(
                            'start_unsettled_automatic_loss', 30
                        ),
                        input_evidence = jsonb_set(
                            input_evidence, '{start_baseline_evidence}',
                            jsonb_build_object('profile_observation_id', %s::bigint)
                        )
                    WHERE id = %s
                    """,
                    (observation_id, player_id),
                )
        database = Database(connection_info)
        try:
            assert _build_board(connection_info, database, generation_id) == [
                ("#RESETREAD", 5200, "uncertain"),
                ("#ATTACKED", 5190, "confirmed"),
                ("#DEFENDED", 5160, "uncertain"),
                ("#LATERREAD", 5140, "confirmed"),
            ]
            season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
            assert boundary.queue_board_rebuilds(database, season, queue=False)[
                "boards"
            ] == []
        finally:
            database.close()
