from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import boundary, reset_baselines
from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Claim,
    Database,
    _text_value,
)
from .domain import SEASON_ANCHOR_RULE_VERSION, DomainRuleError, ranked_day_for
from .profile import normalize_player_tag
from .reconciliation import (
    RECONCILIATION_RULE_VERSION,
    BattleContribution,
    CoverageObservation,
    PreviousRankedDay,
    ReconciliationInput,
    ReconciliationResult,
    reconcile_ranked_day,
    serialize_ranked_day_battles,
)
from .season_summaries import acquire_player_season_lock, materialize_player_season


def complete_reconciliation(database: Database, claim: Claim) -> None:
    player_id = int(claim.input_json["player_id"])
    day_start = datetime.fromisoformat(str(claim.input_json["ranked_day_start"]))
    ranked_day = ranked_day_for(day_start)
    content_dedup = getattr(database, "_supports_content_dedup", False)
    source_rows_relation = (
        "battle_log_observation_source_rows" if content_dedup else "battle_source_rows"
    )
    source_row_id_column = "source_row_id" if content_dedup else "id"
    evidence_join = (
        "be.id = sr.evidence_id"
        if getattr(database, "_supports_compact_battles", False) else
        "(sr.observation_row_id IS NOT NULL AND be.observation_row_id = sr.observation_row_id)"
        " OR (sr.observation_row_id IS NULL AND be.source_row_id = sr.source_row_id)"
        if content_dedup
        else "be.source_row_id = sr.id"
    )
    with database.pool.connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            # Different source changes can enqueue distinct jobs for one
            # player-day. Serialize their version/publication writes while
            # allowing unrelated player-days to reconcile concurrently.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"ranked-day:{player_id}:{ranked_day.start.isoformat()}",),
            )
            now_row = connection.execute("SELECT clock_timestamp()").fetchone()
            assert now_row is not None
            now = now_row[0]
            from .season_retirement import (
                SEASON_DETAIL_RETIRED,
                acquire_season_lock_shared,
                is_detail_retired_for_day,
                is_season_detail_retired,
            )

            season_row = connection.execute(
                """
                SELECT official_season_id FROM ranked_day_versions
                WHERE player_id = %s AND ranked_day_start = %s
                ORDER BY id DESC LIMIT 1
                """,
                (player_id, ranked_day.start),
            ).fetchone()
            season_id = _text_value(season_row[0]) if season_row else None
            if season_id is None:
                anchor_row = connection.execute(
                    """
                    SELECT current_league_season_id, previous_league_season_id,
                           current_start, previous_start
                    FROM legend_season_anchors
                    WHERE state = 'confirmed' AND anchor_rule_version = %s
                    ORDER BY current_start DESC LIMIT 1
                    """,
                    (SEASON_ANCHOR_RULE_VERSION,),
                ).fetchone()
                if anchor_row is not None:
                    season_id = _text_value(
                        anchor_row[0]
                        if ranked_day.start >= anchor_row[2]
                        else anchor_row[1]
                    )
            if season_id is not None and season_id != "unknown":
                acquire_season_lock_shared(connection, season_id)
            if is_detail_retired_for_day(connection, ranked_day.start) or (
                season_id is not None and is_season_detail_retired(connection, season_id)
            ):
                raise DomainRuleError(
                    SEASON_DETAIL_RETIRED,
                    f"ranked day {ranked_day.start.isoformat()} is retired",
                )
            player = connection.execute(
                "SELECT id, normalized_tag FROM players WHERE id = %s",
                (player_id,),
            ).fetchone()
            if player is None:
                raise ValueError(f"unknown reconciliation player id {player_id}")

            start_baseline = reset_baselines._load_reset_baseline(database, 
                connection,
                player_id,
                ranked_day.start,
                claim.parser_version,
                claim.processing_version,
            )
            end_baseline = reset_baselines._load_reset_baseline(database, 
                connection,
                player_id,
                ranked_day.end,
                claim.parser_version,
                claim.processing_version,
            )
            start_battle_log_observation_id = (
                int(start_baseline["evidence"]["battle_log_observation_id"])
                if start_baseline is not None
                and start_baseline["evidence"]["battle_log_observation_id"]
                is not None
                else None
            )
            end_battle_log_observation_id = (
                int(end_baseline["evidence"]["battle_log_observation_id"])
                if end_baseline is not None
                and end_baseline["evidence"]["battle_log_observation_id"]
                is not None
                else None
            )
            coverage_rows = connection.execute(
                f"""
                SELECT
                    blo.observation_id,
                    blo.observed_at,
                    blo.row_count,
                    blo.has_row_gap,
                    COALESCE(evidence.battle_identities, ARRAY[]::text[]),
                    COALESCE(evidence.source_row_ids, ARRAY[]::bigint[]),
                    COALESCE(row_flags.malformed_count, 0),
                    COALESCE(row_flags.unclassified_count, 0),
                    COALESCE(processing.outcome = 'processed', false),
                    observed.response_hash,
                    blo.parser_version,
                    processing.processing_version
                FROM battle_log_observations AS blo
                JOIN collector_observations AS observed
                  ON observed.id = blo.observation_id
                LEFT JOIN observation_processing_outcomes AS processing
                  ON processing.observation_id = blo.observation_id
                 AND processing.parser_version = blo.parser_version
                LEFT JOIN LATERAL (
                    SELECT
                        array_agg(be.battle_id::text ORDER BY be.id)
                            FILTER (WHERE be.battle_id IS NOT NULL)
                            AS battle_identities,
                        array_agg(sr.{source_row_id_column} ORDER BY sr.{source_row_id_column})
                            FILTER (WHERE sr.{source_row_id_column} IS NOT NULL)
                            AS source_row_ids
                    FROM {source_rows_relation} AS sr
                    LEFT JOIN battle_evidence AS be
                      ON {evidence_join}
                    WHERE sr.battle_log_observation_id = blo.id
                ) AS evidence ON true
                LEFT JOIN LATERAL (
                    SELECT
                        count(*) FILTER (
                            WHERE sr.outcome = 'malformed_legend_row'
                               OR sr.failure_category LIKE 'malformed%%'
                               OR sr.failure_category LIKE 'unsupported%%'
                               OR sr.failure_category LIKE 'identity%%'
                        ) AS malformed_count,
                        count(*) FILTER (
                            WHERE sr.failure_category LIKE 'unclassified%%'
                        ) AS unclassified_count
                    FROM {source_rows_relation} AS sr
                    WHERE sr.battle_log_observation_id = blo.id
                ) AS row_flags ON true
                WHERE blo.player_id = %s
                  AND blo.observed_at >= COALESCE(
                      (SELECT start_blo.observed_at
                         FROM battle_log_observations AS start_blo
                        WHERE start_blo.observation_id = %s),
                      %s
                  )
                  AND blo.observed_at <= COALESCE(
                      (SELECT end_blo.observed_at
                         FROM battle_log_observations AS end_blo
                        WHERE end_blo.observation_id = %s),
                      %s
                  )
                ORDER BY blo.observed_at, blo.id
                """,
                (
                    player_id,
                    start_battle_log_observation_id,
                    ranked_day.start,
                    end_battle_log_observation_id,
                    ranked_day.end,
                ),
            ).fetchall()
            if start_battle_log_observation_id is not None:
                start_index = next(
                    (
                        index
                        for index, row in enumerate(coverage_rows)
                        if int(row[0]) == start_battle_log_observation_id
                    ),
                    None,
                )
                if start_index is not None:
                    coverage_rows = coverage_rows[start_index:]
            if end_battle_log_observation_id is not None:
                end_index = next(
                    (
                        index
                        for index, row in enumerate(coverage_rows)
                        if int(row[0]) == end_battle_log_observation_id
                    ),
                    None,
                )
                if end_index is not None:
                    coverage_rows = coverage_rows[: end_index + 1]
            if getattr(database, "_supports_compact_battles", False):
                # Keep both ends of identical runs. Interior duplicate polls
                # add no overlap/quality evidence, and their later expiry
                # must not manufacture a new ranked-day publication.
                coverage_rows = [
                    row for index, row in enumerate(coverage_rows)
                    if index in (0, len(coverage_rows) - 1)
                    or row[2:] != coverage_rows[index - 1][2:]
                    or row[2:] != coverage_rows[index + 1][2:]
                ]
            coverage = tuple(
                CoverageObservation(
                    observation_id=int(row[0]),
                    observed_at=row[1],
                    row_count=int(row[2]),
                    has_row_gap=bool(row[3]),
                    battle_identities=tuple(str(value) for value in row[4]),
                    source_row_ids=tuple(int(value) for value in row[5]),
                    malformed_row_count=int(row[6]),
                    unclassified_row_count=int(row[7]),
                    valid=bool(row[8]),
                    response_hash=_text_value(row[9]),
                    parser_version=_text_value(row[10]),
                    processing_version=(
                        _text_value(row[11]) if row[11] is not None else None
                    ),
                )
                for row in coverage_rows
            )
            contribution_rows = connection.execute(
                """
                SELECT
                    b.id,
                    p.perspective,
                    e.id,
                    e.source_row_id,
                    e.observation_id,
                    e.source_observed_at,
                    e.battle_timestamp,
                    e.stars,
                    e.destruction_percentage,
                    e.army_share_code,
                    e.attacker_gain,
                    e.defender_loss,
                    e.trophy_rule_version,
                    b.disagreement_state,
                    source_row.outcome,
                    source_row.failure_category,
                    CASE e.parser_version
                        WHEN 'supercell-source-parser-v2'
                            THEN source_row.source_json ->> 'opponentPlayerTag'
                        ELSE source_row.source_json -> 'opponent' ->> 'tag'
                    END,
                    CASE e.parser_version
                        WHEN 'supercell-source-parser-v2'
                            THEN source_row.source_json ->> 'opponentName'
                        ELSE source_row.source_json -> 'opponent' ->> 'name'
                    END
                FROM legend_battles AS b
                JOIN battle_perspectives AS p ON p.battle_id = b.id
                JOIN battle_evidence AS e ON e.id = p.evidence_id
                JOIN battle_source_rows AS source_row
                  ON source_row.id = e.source_row_id
                WHERE e.battle_timestamp >= %s
                  AND e.battle_timestamp < %s
                  AND (
                      (p.perspective = 'attacker' AND b.attacker_player_id = %s)
                      OR
                      (p.perspective = 'defender' AND b.defender_player_id = %s)
                  )
                ORDER BY b.id, p.perspective
                """,
                (ranked_day.start, ranked_day.end, player_id, player_id),
            ).fetchall()
            contributions = tuple(
                BattleContribution(
                    battle_identity=str(row[0]),
                    lens=(
                        "offense"
                        if _text_value(row[1]) == "attacker"
                        else "defense"
                    ),
                    trophy_amount=int(
                        row[10] if _text_value(row[1]) == "attacker" else row[11]
                    ),
                    source_rule_version=_text_value(row[12]),
                    valid=_text_value(row[14]) == "valid_legend",
                    failure_reason=(
                        _text_value(row[15]) if row[15] is not None else None
                    ),
                    disagreement=_text_value(row[13]) == "disagreement",
                    source_observation_id=int(row[4]),
                    source_evidence_id=int(row[2]),
                    source_row_id=int(row[3]),
                    source_observed_at=row[5],
                    battle_timestamp=row[6],
                    stars=int(row[7]),
                    destruction_percentage=int(row[8]),
                    army_share_code=_text_value(row[9]),
                    attacker_gain=int(row[10]),
                    defender_loss=int(row[11]),
                    opponent_tag=(
                        _text_value(row[16]) if row[16] is not None else None
                    ),
                    opponent_name=(
                        _text_value(row[17]) if row[17] is not None else None
                    ),
                )
                for row in contribution_rows
            )
            previous_row = connection.execute(
                """
                SELECT
                    id,
                    state,
                    confidence,
                    defense_count,
                    observed_defense_loss,
                    coverage_complete,
                    shield_state,
                    shield_duration_days,
                    input_hash
                FROM ranked_day_versions
                WHERE player_id = %s AND ranked_day_start = %s
                  AND reconciliation_rule_version = %s
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (
                    player_id,
                    ranked_day.start - timedelta(days=1),
                    RECONCILIATION_RULE_VERSION,
                ),
            ).fetchone()
            previous = (
                PreviousRankedDay(
                    complete=(
                        _text_value(previous_row[1]) == "Complete"
                        and bool(previous_row[5])
                    ),
                    observed_defense_count=int(previous_row[3]),
                    observed_defense_loss=int(previous_row[4]),
                    shield_run_length=(
                        int(previous_row[7] or 0)
                        if _text_value(previous_row[6]) == "inferred_shielded"
                        else 0
                    ),
                    coverage_complete=bool(previous_row[5]),
                    shield_state=_text_value(previous_row[6]),
                    version_id=int(previous_row[0]),
                    ranked_day_start=ranked_day.start - timedelta(days=1),
                    state=_text_value(previous_row[1]),
                    confidence=_text_value(previous_row[2]),
                    input_hash=(
                        _text_value(previous_row[8])
                        if previous_row[8] is not None
                        else None
                    ),
                )
                if previous_row is not None
                else None
            )
            anchor = connection.execute(
                """
                SELECT current_league_season_id, previous_league_season_id,
                       current_start, previous_start
                FROM legend_season_anchors
                WHERE state = 'confirmed' AND anchor_rule_version = %s
                """,
                (SEASON_ANCHOR_RULE_VERSION,),
            ).fetchone()
            anchor_valid = anchor is not None and ranked_day.start >= anchor[3]
            if anchor is None:
                official_season_id = "unknown"
                season_start = ranked_day.start
            elif ranked_day.start >= anchor[2]:
                official_season_id = _text_value(anchor[0])
                season_start = anchor[2]
            else:
                official_season_id = _text_value(anchor[1])
                season_start = anchor[3]
            season_day_number = (ranked_day.start - season_start).days + 1
            boundary_kind = None
            if anchor is not None and ranked_day.end == anchor[2]:
                boundary_kind = "season"
            elif ranked_day.end.weekday() == 0:
                boundary_kind = "weekly"

            trophy_rule_versions = tuple(
                sorted(
                    {
                        contribution.source_rule_version
                        for contribution in contributions
                        if contribution.source_rule_version is not None
                    }
                )
            )
            baseline_eligibility = tuple(
                value
                for value in (
                    start_baseline.get("eligibility_state")
                    if start_baseline is not None
                    else None,
                    end_baseline.get("eligibility_state")
                    if end_baseline is not None
                    else None,
                )
                if value is not None
            )
            player_eligible = bool(baseline_eligibility) and all(
                value == "eligible" for value in baseline_eligibility
            )
            malformed_evidence = any(
                observation.malformed_row_count > 0 for observation in coverage
            )
            unclassified_evidence = any(
                observation.unclassified_row_count > 0 for observation in coverage
            )
            perspective_disagreement = any(
                contribution.disagreement for contribution in contributions
            )
            result = reconcile_ranked_day(
                ReconciliationInput(
                    ranked_day=ranked_day,
                    now=now,
                    start_baseline_id=(
                        int(start_baseline["id"])
                        if start_baseline is not None
                        else None
                    ),
                    end_baseline_id=(
                        int(end_baseline["id"])
                        if end_baseline is not None
                        else None
                    ),
                    start_trophies=(
                        int(start_baseline["trophies"])
                        if start_baseline is not None
                        and start_baseline["trophies"] is not None
                        else None
                    ),
                    next_start_trophies=(
                        int(end_baseline["trophies"])
                        if end_baseline is not None
                        and end_baseline["trophies"] is not None
                        else None
                    ),
                    start_baseline_battle_log_observation_id=(
                        start_battle_log_observation_id
                    ),
                    end_baseline_battle_log_observation_id=(
                        end_battle_log_observation_id
                    ),
                    coverage_observations=coverage,
                    contributions=contributions,
                    previous_day=previous,
                    boundary_kind=boundary_kind,
                    season_anchor_valid=anchor_valid,
                    start_baseline_complete=(
                        bool(start_baseline["complete"])
                        if start_baseline is not None
                        else False
                    ),
                    end_baseline_complete=(
                        bool(end_baseline["complete"])
                        if end_baseline is not None
                        else False
                    ),
                    player_eligible=player_eligible,
                    perspective_disagreement=perspective_disagreement,
                    malformed_evidence=malformed_evidence,
                    unclassified_evidence=unclassified_evidence,
                    start_baseline_evidence=(
                        start_baseline["evidence"]
                        if start_baseline is not None
                        else {}
                    ),
                    end_baseline_evidence=(
                        end_baseline["evidence"] if end_baseline is not None else {}
                    ),
                    parser_version=claim.parser_version,
                    processing_version=claim.processing_version,
                    domain_rule_version=claim.domain_rule_version,
                    season_anchor_rule_version=SEASON_ANCHOR_RULE_VERSION,
                    trophy_allocation_rule_versions=trophy_rule_versions,
                )
            )
            result_data = {
                "state": result.state,
                "confidence": result.confidence,
                "failure_reasons": list(result.failure_reasons),
                "start_trophies": (
                    int(start_baseline["trophies"])
                    if start_baseline is not None
                    and start_baseline["trophies"] is not None
                    else None
                ),
                "next_start_trophies": (
                    int(end_baseline["trophies"])
                    if end_baseline is not None
                    and end_baseline["trophies"] is not None
                    else None
                ),
                "attack_count": result.attack_count,
                "defense_count": result.defense_count,
                "attack_gain": result.attack_trophy_gain,
                "observed_defense_loss": result.observed_defense_loss,
                "automatic_defense_loss": result.automatic_defense_loss,
                "automatic_defense_evidence_state": (
                    result.automatic_defense_evidence_state
                ),
                "net_trophy_change": result.net_trophy_change,
                "observed_trophy_change": result.observed_trophy_change,
                "final_trophies_before_reset": result.final_trophies_before_reset,
                "boundary_adjustment": result.boundary_adjustment,
                "boundary_adjustment_type": result.boundary_adjustment_type,
                "observed_boundary_adjustment": result.observed_boundary_adjustment,
                "expected_next_start_trophies": (
                    result.expected_next_start_trophies
                ),
                "unexplained_residual": result.unexplained_residual,
                "shield_state": result.shield_state,
                "shield_duration_days": result.shield_duration_days,
                "coverage_complete": result.coverage_complete,
                "formula_components": result.formula_components,
                "input_evidence": result.input_evidence,
                "shield_evidence": result.shield_evidence,
            }
            rule_versions = {
                "parser_version": claim.parser_version,
                "processing_version": claim.processing_version,
                "domain_rule_version": claim.domain_rule_version,
                "season_anchor_rule_version": SEASON_ANCHOR_RULE_VERSION,
                "reconciliation_rule_version": RECONCILIATION_RULE_VERSION,
                "trophy_allocation_rule_versions": list(trophy_rule_versions),
            }
            input_payload = {
                "player_id": player_id,
                "ranked_day_start": ranked_day.start.isoformat(),
                "ranked_day_end": ranked_day.end.isoformat(),
                "official_season_id": official_season_id,
                "season_day_number": season_day_number,
                "boundary_kind": boundary_kind,
                "rule_versions": rule_versions,
                "input_evidence": result.input_evidence,
            }
            input_hash = hashlib.sha256(
                json.dumps(
                    input_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            result_hash = hashlib.sha256(
                json.dumps(
                    {
                        "input_hash": input_hash,
                        "result": result_data,
                        "rule_versions": rule_versions,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            input_evidence = result.input_evidence
            coverage_evidence = input_evidence.get("coverage_observations", [])
            contribution_evidence = input_evidence.get("contributions", [])
            existing = connection.execute(
                """
                SELECT id, version FROM ranked_day_versions
                WHERE player_id = %s AND ranked_day_start = %s
                  AND reconciliation_rule_version = %s AND result_hash = %s
                """,
                (
                    player_id,
                    ranked_day.start,
                    RECONCILIATION_RULE_VERSION,
                    result_hash,
                ),
            ).fetchone()
            if existing is None:
                previous_version = connection.execute(
                    """
                    SELECT id, version FROM ranked_day_versions
                    WHERE player_id = %s AND ranked_day_start = %s
                      AND reconciliation_rule_version = %s
                    ORDER BY version DESC LIMIT 1
                    FOR UPDATE
                    """,
                    (player_id, ranked_day.start, RECONCILIATION_RULE_VERSION),
                ).fetchone()
                previous_publication = connection.execute(
                    """
                    SELECT max(version)
                    FROM api_player_daily_logs
                    WHERE player_id = %s AND ranked_day_start = %s
                    """,
                    (player_id, ranked_day.start),
                ).fetchone()
                next_ranked_day_version = (
                    int(previous_version[1]) + 1
                    if previous_version is not None
                    else 1
                )
                next_publication_version = (
                    int(previous_publication[0]) + 1
                    if previous_publication is not None
                    and previous_publication[0] is not None
                    else 1
                )
                # ``api_player_daily_logs.version`` predates the
                # reconciliation-rule version and has a global per-day
                # uniqueness constraint. Continue above any v2 publication
                # when the first v3 republication is written, while
                # retaining idempotence for the same v3 result.
                version_number = max(
                    next_ranked_day_version, next_publication_version
                )
                evidence_complete = bool(
                    result.coverage_complete
                    and start_baseline is not None
                    and start_baseline["complete"]
                    and end_baseline is not None
                    and end_baseline["complete"]
                )
                version = connection.execute(
                    """
                    INSERT INTO ranked_day_versions (
                        player_id, ranked_day_start, ranked_day_end,
                        official_season_id, season_day_number,
                        season_anchor_rule_version, reconciliation_rule_version,
                        result_hash, input_hash,
                        parser_version, processing_version, domain_rule_version,
                        analytics_rule_version, trophy_allocation_rule_versions,
                        version, replaces_version_id, state, confidence,
                        failure_reasons, start_trophies,
                        final_trophies_before_reset, next_start_trophies,
                        expected_next_start_trophies,
                        attack_count, defense_count, attack_gain,
                        observed_defense_loss, automatic_defense_loss,
                        automatic_defense_evidence_state, net_trophy_change,
                        observed_trophy_change, boundary_adjustment,
                        boundary_adjustment_type, observed_boundary_adjustment,
                        unexplained_residual, formula_components,
                        input_evidence, coverage_evidence,
                        contribution_evidence, shield_evidence,
                        evidence_complete, coverage_complete, reconciled,
                        shield_state, shield_duration_days,
                        start_baseline_id, end_baseline_id
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) RETURNING id
                    """,
                    (
                        player_id,
                        ranked_day.start,
                        ranked_day.end,
                        official_season_id,
                        season_day_number,
                        SEASON_ANCHOR_RULE_VERSION,
                        RECONCILIATION_RULE_VERSION,
                        result_hash,
                        input_hash,
                        claim.parser_version,
                        claim.processing_version,
                        claim.domain_rule_version,
                        claim.analytics_rule_version,
                        Jsonb(trophy_rule_versions),
                        version_number,
                        previous_version[0]
                        if previous_version is not None
                        else None,
                        result.state,
                        result.confidence,
                        Jsonb(list(result.failure_reasons)),
                        result_data["start_trophies"],
                        result.final_trophies_before_reset,
                        result_data["next_start_trophies"],
                        result.expected_next_start_trophies,
                        result.attack_count,
                        result.defense_count,
                        result.attack_trophy_gain,
                        result.observed_defense_loss,
                        result.automatic_defense_loss,
                        result.automatic_defense_evidence_state,
                        result.net_trophy_change,
                        result.observed_trophy_change,
                        result.boundary_adjustment,
                        result.boundary_adjustment_type,
                        result.observed_boundary_adjustment,
                        result.unexplained_residual,
                        Jsonb(result.formula_components),
                        Jsonb(input_evidence),
                        Jsonb(coverage_evidence),
                        Jsonb(contribution_evidence),
                        Jsonb(result.shield_evidence),
                        evidence_complete,
                        result.coverage_complete,
                        result.state == "Complete",
                        result.shield_state,
                        result.shield_duration_days,
                        (
                            start_baseline["id"]
                            if start_baseline is not None
                            else None
                        ),
                        (end_baseline["id"] if end_baseline is not None else None),
                    ),
                ).fetchone()
                assert version is not None
                version_id = int(version[0])
                _store_ranked_day_adjustments(connection, version_id, result)
            else:
                version_id = int(existing[0])
                version_number = int(existing[1])
            _publish_player_daily_log(database, 
                connection,
                player_id=player_id,
                ranked_day_start=ranked_day.start,
                ranked_day_end=ranked_day.end,
                official_season_id=official_season_id,
                season_day_number=season_day_number,
                version_number=version_number,
                ranked_day_version_id=version_id,
                result=result,
                contribution_evidence=contribution_evidence,
            )
            if existing is None:
                # A reset sweep is the sole source of expected population.
                # No population-wide job is created for an uncoordinated
                # legacy fixture or a late/discovered player.
                boundary._record_boundary_generation(database, 
                    connection,
                    boundary_at=ranked_day.end,
                    player_id=player_id,
                    ranked_day_version_id=version_id,
                    ranked_day_input_hash=input_hash,
                )
            database._finish_claim(
                connection, claim, job, state="complete", outcome="processed"
            )


def _store_ranked_day_adjustments(
    connection: Any,
    ranked_day_version_id: int,
    result: ReconciliationResult,
) -> None:
    if result.automatic_defense_loss is not None:
        connection.execute(
            """
            INSERT INTO ranked_day_adjustments (
                ranked_day_version_id, adjustment_type, amount,
                evidence_state, rule_version, evidence_json
            ) VALUES (%s, 'automatic_defense', %s, %s, %s, %s)
            """,
            (
                ranked_day_version_id,
                -result.automatic_defense_loss,
                result.automatic_defense_evidence_state,
                RECONCILIATION_RULE_VERSION,
                Jsonb(
                    {
                        "defense_count": result.defense_count,
                        "observed_defense_loss": result.observed_defense_loss,
                    }
                ),
            ),
        )
    if result.boundary_adjustment_type is not None:
        connection.execute(
            """
            INSERT INTO ranked_day_adjustments (
                ranked_day_version_id, adjustment_type, amount,
                evidence_state, rule_version, evidence_json
            ) VALUES (%s, %s, %s, 'official_rule', %s, '{}'::jsonb)
            """,
            (
                ranked_day_version_id,
                result.boundary_adjustment_type,
                result.boundary_adjustment,
                RECONCILIATION_RULE_VERSION,
            ),
        )


def _publish_player_daily_log(
    database: Database,
    connection: Any,
    *,
    player_id: int,
    ranked_day_start: datetime,
    ranked_day_end: datetime,
    official_season_id: str,
    season_day_number: int,
    version_number: int,
    ranked_day_version_id: int,
    result: ReconciliationResult,
    contribution_evidence: list[dict[str, Any]],
) -> None:
    # Targeted corrections for a retired season are rejected before any
    # domain mutation; detailed reconstruction ends at finalization.
    if official_season_id != "unknown":
        from .season_retirement import (
            SEASON_DETAIL_RETIRED,
            acquire_season_lock_shared,
            is_season_detail_retired,
        )

        acquire_season_lock_shared(connection, official_season_id)
        if is_season_detail_retired(connection, official_season_id):
            raise DomainRuleError(
                SEASON_DETAIL_RETIRED,
                f"season {official_season_id} detail is retired",
            )
    adjustment_rows = connection.execute(
        """
        SELECT adjustment_type, amount, evidence_state, rule_version,
               evidence_json
        FROM ranked_day_adjustments
        WHERE ranked_day_version_id = %s
        ORDER BY id
        """,
        (ranked_day_version_id,),
    ).fetchall()
    adjustments = [
        {
            "type": _text_value(row[0]),
            "amount": int(row[1]),
            "evidence_state": _text_value(row[2]),
            "rule_version": _text_value(row[3]),
            "evidence": row[4],
        }
        for row in adjustment_rows
    ]
    canonical_events = serialize_ranked_day_battles(contribution_evidence)
    events_by_battle_id = {
        str(event["battle_id"]): event for event in canonical_events
    }
    battles: list[dict[str, Any]] = []
    for item in contribution_evidence:
        if item.get("included") is not True:
            continue
        battle_id = item.get("battle_identity")
        # Keep the frozen reconciliation evidence (source and decision
        # fields) and add the canonical screen-event projection alongside
        # it. Evidence that cannot produce a screen event is still kept
        # here for the existing private/audit contract; the API mapper
        # excludes it from offense/defense event arrays.
        event = (
            events_by_battle_id.get(str(battle_id))
            if battle_id is not None
            else None
        )
        battles.append({**item, **event} if event is not None else dict(item))
    attack_three_star_count = sum(
        item.get("lens") == "offense" and item.get("stars") == 3 for item in battles
    )
    defense_three_star_count = sum(
        item.get("lens") == "defense" and item.get("stars") == 3 for item in battles
    )
    # Automatic reset loss is published in adjustments, not attributed to
    # an opponent battle. Keep this aggregate equal to the defense events.
    defense_loss = result.observed_defense_loss
    public_state = (
        result.state
        if result.state in {"Live", "Complete", "Partial"}
        else "Partial"
    )
    partial_reasons = list(result.failure_reasons)
    if public_state != result.state:
        partial_reasons.append(f"ranked_day_state:{result.state}")
    offense_events = [
        item
        for item in battles
        if item.get("lens") == "offense" and "battle_id" in item
    ]
    defense_events = [
        item
        for item in battles
        if item.get("lens") == "defense" and "battle_id" in item
    ]
    projection_consistent = (
        len(offense_events) == result.attack_count
        and len(defense_events) == result.defense_count
        and sum(item.get("stars") == 3 for item in offense_events)
        == attack_three_star_count
        and sum(item.get("stars") == 3 for item in defense_events)
        == defense_three_star_count
        and sum(int(item["trophy_change"]) for item in offense_events)
        == result.attack_trophy_gain
        and abs(sum(int(item["trophy_change"]) for item in defense_events))
        == result.observed_defense_loss
    )
    if not projection_consistent:
        if public_state == "Complete":
            public_state = "Partial"
        partial_reasons.append("battle_event_projection_incomplete")
    daily_log_values = (
        player_id,
        ranked_day_start,
        version_number,
        public_state,
        "complete" if result.coverage_complete else "partial",
        Jsonb(adjustments),
        Jsonb(battles),
        Jsonb(partial_reasons),
        ranked_day_end,
        official_season_id,
        season_day_number,
        result.confidence,
        result.attack_count,
        attack_three_star_count,
        result.attack_trophy_gain,
        result.defense_count,
        defense_three_star_count,
        defense_loss,
        result.net_trophy_change,
    )
    if getattr(database, "_supports_coordinator_contract", False):
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, ranked_day_version_id, version, state, coverage,
                adjustments, battles, partial_reasons, ranked_day_end,
                official_season_id, season_day_number, confidence,
                attack_count, attack_three_star_count, attack_gain,
                defense_count, defense_three_star_count, defense_loss,
                net_trophy_change
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (player_id, ranked_day_start, version) DO NOTHING
            """,
            (
                player_id,
                ranked_day_start,
                ranked_day_version_id,
                *daily_log_values[2:],
            ),
        )
    else:
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, version, state, coverage,
                adjustments, battles, partial_reasons, ranked_day_end,
                official_season_id, season_day_number, confidence,
                attack_count, attack_three_star_count, attack_gain,
                defense_count, defense_three_star_count, defense_loss,
                net_trophy_change
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (player_id, ranked_day_start, version) DO NOTHING
            """,
            daily_log_values,
        )
    # Refresh the compact historical summary in the same transaction:
    # a late correction updates an already-summarized season, and a
    # completed day-28 publication establishes one. Active seasons stay
    # on explicit backfill, and unchanged input is a no-op, so routine
    # live publication is unaffected.
    if public_state != "Live" and getattr(
        database, "_supports_season_summaries", False
    ) and official_season_id != "unknown":
        refresh = season_day_number == 28 and public_state == "Complete"
        if not refresh:
            # Probe under the per-player-season lock so a racing first
            # backfill commits or is fenced out before the check, the
            # same serialization the retired exclusive season lock
            # used to give this read for free.
            acquire_player_season_lock(
                connection, player_id, official_season_id
            )
            refresh = (
                connection.execute(
                    """
                    SELECT 1 FROM player_season_summaries
                    WHERE player_id = %s AND official_season_id = %s
                    """,
                    (player_id, official_season_id),
                ).fetchone()
                is not None
            )
        if refresh:
            materialize_player_season(
                connection,
                player_id=player_id,
                season_id=official_season_id,
            )


def _enqueue_live_reconciliation(
    connection: Any,
    *,
    player_id: int,
    ranked_day_start: datetime,
    source_quality: dict[str, Any] | None,
) -> None:
    ranked_day = ranked_day_for(ranked_day_start)
    ranked_day_start = ranked_day.start.astimezone(UTC)
    live = connection.execute(
        "SELECT clock_timestamp() >= %s AND clock_timestamp() < %s",
        (ranked_day.start, ranked_day.end),
    ).fetchone()
    assert live is not None
    if not bool(live[0]):
        return
    ranked_day_start_text = ranked_day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = connection.execute(
        """
        SELECT
            battle.id,
            p.perspective,
            evidence.battle_timestamp,
            evidence.stars,
            evidence.destruction_percentage,
            evidence.army_share_code,
            evidence.reporter_trophies,
            evidence.opponent_trophies,
            evidence.attacker_gain,
            evidence.defender_loss,
            evidence.trophy_rule_version,
            battle.disagreement_state,
            source_row.outcome,
            source_row.failure_category,
            jsonb_build_object(
                'tag', CASE evidence.parser_version
                    WHEN 'supercell-source-parser-v2'
                        THEN source_row.source_json -> 'opponentPlayerTag'
                    ELSE source_row.source_json -> 'opponent' -> 'tag'
                END,
                'name', CASE evidence.parser_version
                    WHEN 'supercell-source-parser-v2'
                        THEN source_row.source_json -> 'opponentName'
                    ELSE source_row.source_json -> 'opponent' -> 'name'
                END
            )
        FROM legend_battles AS battle
        JOIN battle_perspectives AS p ON p.battle_id = battle.id
        JOIN battle_evidence AS evidence ON evidence.id = p.evidence_id
        JOIN battle_source_rows AS source_row
          ON source_row.id = evidence.source_row_id
        WHERE battle.ranked_day_start = %s
          AND evidence.battle_timestamp >= %s
          AND evidence.battle_timestamp < %s
          AND (
              (p.perspective = 'attacker'
               AND battle.attacker_player_id = %s)
              OR
              (p.perspective = 'defender'
               AND battle.defender_player_id = %s)
          )
        ORDER BY battle.id, p.perspective
        """,
        (
            ranked_day.start,
            ranked_day.start,
            ranked_day.end,
            player_id,
            player_id,
        ),
    ).fetchall()
    projection_rows = [
        {
            "battle_id": int(row[0]),
            "perspective": _text_value(row[1]),
            "battle_timestamp": row[2].astimezone(UTC).isoformat(),
            "stars": int(row[3]),
            "destruction_percentage": int(row[4]),
            "army_share_code": _text_value(row[5]),
            "reporter_trophies": (None if row[6] is None else int(row[6])),
            "opponent_trophies": (None if row[7] is None else int(row[7])),
            "attacker_gain": int(row[8]),
            "defender_loss": int(row[9]),
            "trophy_rule_version": _text_value(row[10]),
            "disagreement_state": _text_value(row[11]),
            "source_outcome": _text_value(row[12]),
            "failure_category": (None if row[13] is None else _text_value(row[13])),
            "opponent": row[14],
        }
        for row in rows
    ]
    projection = {
        "events": projection_rows,
        "source_quality": source_quality,
    }
    projection_hash = hashlib.sha256(
        json.dumps(
            projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    deduplication_key = (
        f"reconcile:live:{player_id}:{ranked_day_start_text}:"
        f"{RECONCILIATION_RULE_VERSION}:{projection_hash}"
    )
    connection.execute(
        """
        INSERT INTO python_processing_jobs_worker (
            observation_id, work_type, deduplication_key, input_json,
            state, due_at, parser_version, processing_version,
            domain_rule_version, analytics_rule_version
        ) VALUES (
            NULL, 'reconcile_ranked_day', %s, %s, 'pending', clock_timestamp(),
            %s, %s, %s, %s
        )
        ON CONFLICT (deduplication_key) DO NOTHING
        """,
        (
            deduplication_key,
            Jsonb(
                {
                    "player_id": int(player_id),
                    "ranked_day_start": ranked_day_start_text,
                    "trigger": "live_battle_projection",
                    "projection_hash": projection_hash,
                }
            ),
            DEFAULT_PARSER_VERSION,
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            ANALYTICS_RULE_VERSION,
        ),
    )


def enqueue_reconciliation(
    database: Database,
    *,
    player_tag: str,
    day_start: datetime,
    now: datetime,
    request_key: str,
) -> int:
    del now  # Production jobs use the worker's current time at claim.
    normalized_tag = normalize_player_tag(player_tag)
    ranked_day = ranked_day_for(day_start)
    ranked_day_start = ranked_day.start.astimezone(UTC)
    ranked_day_start_text = ranked_day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    deduplication_key = (
        f"reconcile:{normalized_tag}:{ranked_day_start_text}:{request_key}"
    )
    with database.pool.connection() as connection:
        player = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s",
            (normalized_tag,),
        ).fetchone()
        if player is None:
            raise ValueError(f"unknown reconciliation player {normalized_tag}")
        player_id = int(player[0])
        row = connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                observation_id, work_type, deduplication_key, input_json,
                state, due_at, parser_version, processing_version,
                domain_rule_version, analytics_rule_version
            ) VALUES (
                NULL, 'reconcile_ranked_day', %s, %s,
                'pending', clock_timestamp(), %s, %s, %s, %s
            )
            ON CONFLICT (deduplication_key) DO UPDATE SET
                deduplication_key = EXCLUDED.deduplication_key
            RETURNING id
            """,
            (
                deduplication_key,
                Jsonb(
                    {
                        "player_id": player_id,
                        "ranked_day_start": ranked_day_start_text,
                    }
                ),
                DEFAULT_PARSER_VERSION,
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ANALYTICS_RULE_VERSION,
            ),
        ).fetchone()
        connection.commit()
        assert row is not None
        return int(row[0])


def enqueue_current_season_republication(
    database: Database,
    *,
    max_jobs: int = 100,
) -> list[int]:
    """Queue a bounded batch of published current-season days missing v3.

    This rebuilds derived ranked-day publications from canonical database
    evidence; it does not replay archived source observations. Repeating
    the call advances past already queued targets, so an operator can drain
    a season in measured batches without an unbounded deployment action.
    """

    if isinstance(max_jobs, bool) or not 1 <= max_jobs <= 1000:
        raise ValueError("current-season republication batch must be 1 to 1000")
    with database.pool.connection() as connection:
        with connection.transaction():
            candidates = connection.execute(
                """
                WITH current_anchor AS (
                    SELECT current_league_season_id, current_start
                    FROM legend_season_anchors
                    WHERE state = 'confirmed'
                      AND anchor_rule_version = %s
                    ORDER BY current_start DESC
                    LIMIT 1
                ), published AS (
                    SELECT DISTINCT
                           log.player_id,
                           log.ranked_day_start,
                           anchor.current_league_season_id
                    FROM api_player_daily_logs AS log
                    JOIN current_anchor AS anchor
                      ON log.official_season_id =
                         anchor.current_league_season_id
                    WHERE log.ranked_day_start >= anchor.current_start
                      AND log.ranked_day_start <
                          anchor.current_start + interval '28 days'
                )
                SELECT player_id, ranked_day_start,
                       current_league_season_id
                FROM published
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM ranked_day_versions AS version
                    WHERE version.player_id = published.player_id
                      AND version.ranked_day_start =
                          published.ranked_day_start
                      AND version.reconciliation_rule_version = %s
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM python_processing_jobs_worker AS job
                    WHERE job.work_type = 'reconcile_ranked_day'
                      AND job.state IN (
                          'pending', 'waiting_retry', 'waiting_dependency', 'leased'
                      )
                      AND (job.input_json ->> 'player_id')::bigint =
                          published.player_id
                      AND job.input_json ->> 'ranked_day_start' =
                          to_char(
                              published.ranked_day_start AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                          )
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM python_processing_jobs_worker AS job
                    WHERE job.deduplication_key =
                        'reconcile:current-season:'
                        || published.player_id::text || ':'
                        || to_char(
                            published.ranked_day_start AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                        ) || ':' || %s
                )
                ORDER BY player_id, ranked_day_start
                LIMIT %s
                """,
                (
                    SEASON_ANCHOR_RULE_VERSION,
                    RECONCILIATION_RULE_VERSION,
                    RECONCILIATION_RULE_VERSION,
                    max_jobs,
                ),
            ).fetchall()
            job_ids: list[int] = []
            for player_id, ranked_day_start, official_season_id in candidates:
                ranked_day_start_text = ranked_day_start.astimezone(UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                deduplication_key = (
                    f"reconcile:current-season:{int(player_id)}:"
                    f"{ranked_day_start_text}:{RECONCILIATION_RULE_VERSION}"
                )
                row = connection.execute(
                    """
                    INSERT INTO python_processing_jobs_worker (
                        observation_id, work_type, deduplication_key,
                        input_json, state, due_at, parser_version,
                        processing_version, domain_rule_version,
                        analytics_rule_version
                    ) VALUES (
                        NULL, 'reconcile_ranked_day', %s, %s, 'pending',
                        clock_timestamp(), %s, %s, %s, %s
                    )
                    ON CONFLICT (deduplication_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        deduplication_key,
                        Jsonb(
                            {
                                "player_id": int(player_id),
                                "ranked_day_start": ranked_day_start_text,
                                "official_season_id": _text_value(
                                    official_season_id
                                ),
                                "trigger": "current_season_republication",
                            }
                        ),
                        DEFAULT_PARSER_VERSION,
                        PROCESSING_VERSION,
                        DOMAIN_RULE_VERSION,
                        ANALYTICS_RULE_VERSION,
                    ),
                ).fetchone()
                if row is not None:
                    job_ids.append(int(row[0]))
            return job_ids


