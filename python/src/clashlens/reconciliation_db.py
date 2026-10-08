from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import (
    battle_day_repair,
    boundary,
    domain,
    first_battle_log,
    ranked_day_inputs,
    reset_baselines,
)
from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    PYTHON_BACKFILL_PRIORITY,
    RESET_LOCK_WAIT,
    Claim,
    Database,
    _text_value,
    ended_day_priority,
)
from .domain import (
    SEASON_ANCHOR_RULE_VERSION,
    DomainRuleError,
    RankedDay,
    ranked_day_for,
)
from .profile import normalize_player_tag
from .reconciliation import (
    RECONCILIATION_RULE_VERSION,
    ReconciliationInput,
    ReconciliationResult,
    reads_later_reading,
    reconcile_ranked_day,
    serialize_ranked_day_battles,
)
from .season_summaries import acquire_player_season_lock, materialize_player_season

# The 2026-10-05 and 2026-10-06 Resets saved every reading within 45 minutes.
DAY_END_RECALCULATION_DELAY = timedelta(hours=2)


def limit_lock_waits(connection: Any) -> None:
    """Make each lock the rest of this transaction waits for give up after
    RESET_LOCK_WAIT.

    A daily calculation that cannot take a lock in time rolls back and is
    retried later, rather than holding its player's days, and any Resets it
    already took, while it waits.
    """
    connection.execute(
        "SELECT set_config('lock_timeout', %s, true)", (RESET_LOCK_WAIT,)
    )


def complete_reconciliation(database: Database, claim: Claim) -> None:
    with database.pool.connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            limit_lock_waits(connection)
            player_id = int(claim.input_json["player_id"])
            day_start = datetime.fromisoformat(str(claim.input_json["ranked_day_start"]))
            day_starts = {day_start}
            if "recalculate_season" in claim.input_json:
                day_starts.add(
                    datetime.fromisoformat(
                        str(claim.input_json["last_ranked_day_start"])
                    )
                )
                saved_days = connection.execute(
                    """
                    SELECT DISTINCT ranked_day_start
                    FROM api_player_daily_logs
                    WHERE player_id = %s AND ranked_day_start >= %s
                      AND official_season_id = %s
                    ORDER BY ranked_day_start
                    """,
                    (player_id, day_start, claim.input_json["recalculate_season"]),
                ).fetchall()
                day_starts.update(row[0] for row in saved_days)
            if claim.input_json.get("trigger") == "day_end":
                # Its Reset reading usually finished the day already. A day a
                # reading since may settle runs again.
                latest = connection.execute(
                    f"""
                    SELECT state = 'Live'
                           OR {ranked_day_inputs.LATER_READING_DAY_SQL}
                    FROM ranked_day_versions
                    WHERE player_id = %s AND ranked_day_start = %s
                      AND reconciliation_rule_version = %s
                    ORDER BY version DESC LIMIT 1
                    """,
                    (player_id, day_start, RECONCILIATION_RULE_VERSION),
                ).fetchone()
                if latest is None or not latest[0]:
                    day_starts = set()
            pending = sorted(day_starts)
            while pending:
                day_start = pending.pop(0)
                following = day_start + timedelta(days=1)
                # A changed ended result changes the following saved day of
                # its Season too, until a day's result stays the same.
                if recalculate_ranked_day(
                    database,
                    connection,
                    player_id=player_id,
                    day_start=day_start,
                    parser_version=claim.parser_version,
                    processing_version=claim.processing_version,
                    domain_rule_version=claim.domain_rule_version,
                    analytics_rule_version=claim.analytics_rule_version,
                ) and following not in day_starts and (
                    ranked_day_for(following).official_season_id
                    == ranked_day_for(day_start).official_season_id
                ) and connection.execute(
                    """
                    SELECT 1 FROM ranked_day_versions
                    WHERE player_id = %s AND ranked_day_start = %s
                      AND reconciliation_rule_version = %s
                    LIMIT 1
                    """,
                    (player_id, following, RECONCILIATION_RULE_VERSION),
                ).fetchone() is not None:
                    day_starts.add(following)
                    pending.insert(0, following)
            database._finish_claim(
                connection, claim, job, state="complete", outcome="processed"
            )


def _known_not_enrolled(connection: Any, player_id: int, ranked_day: Any) -> bool:
    """Whether a Legend I profile saved after the day ended still showed no
    Season, and a later one in the same Season showed the player signed up.
    A player cannot leave a Season once signed up, so neither answer alone is
    enough: the first proves no sign-up yet, the second that this Season is
    the one they joined."""
    return bool(
        connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM player_profile_versions AS waiting
                JOIN player_profile_effects AS waiting_seen
                  ON waiting_seen.profile_version_id = waiting.id
                JOIN player_profile_versions AS joined
                  ON joined.player_id = waiting.player_id
                JOIN player_profile_effects AS joined_seen
                  ON joined_seen.profile_version_id = joined.id
                WHERE waiting.player_id = %(player)s
                  AND waiting.eligibility_state = 'eligible'
                  AND waiting.eligibility_reason = 'confirmed_legend_i'
                  AND waiting.current_league_season_id = '0'
                  AND waiting_seen.observed_at >= %(day_end)s
                  AND joined.current_league_season_id = %(season)s
                  AND joined.eligibility_state = 'eligible'
                  AND joined.source_contract_state = 'accepted'
                  AND joined_seen.observed_at > waiting_seen.observed_at
                  AND joined_seen.observed_at < %(season_end)s
            )
            """,
            {
                "player": player_id,
                "day_end": ranked_day.end,
                "season": ranked_day.official_season_id,
                "season_end": ranked_day.season_end,
            },
        ).fetchone()[0]
    )


def recalculate_ranked_day(
    database: Database,
    connection: Any,
    *,
    player_id: int,
    day_start: datetime,
    parser_version: str,
    processing_version: str,
    domain_rule_version: str,
    analytics_rule_version: str,
) -> bool:
    """Recalculate and publish one player-day in the caller's transaction;
    whether the day has ended and this changed the state, end or next start
    of its saved result, or saved its first."""
    ranked_day = ranked_day_for(day_start)
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
        season_day = _anchored_day(connection, ranked_day.start)[1]
        season_id = season_day.official_season_id if season_day else None
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
        processing_version,
    )
    end_baseline = reset_baselines._load_reset_baseline(database, 
        connection,
        player_id,
        ranked_day.end,
        processing_version,
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
    # A player first tracked after the day's start has no Reset battle log;
    # their first saved one can prove the day's battles instead.
    if start_battle_log_observation_id is None and (
        first_log := first_battle_log.coverage_start(
            database, connection, player_id, ranked_day
        )
    ):
        start_battle_log_observation_id, holds_whole_day = first_log
        if holds_whole_day and end_battle_log_observation_id is None:
            end_battle_log_observation_id = start_battle_log_observation_id
    if start_baseline is None:
        start_baseline = first_battle_log.season_rule_start(
            connection, player_id, ranked_day,
            complete=start_battle_log_observation_id is not None,
        )
    coverage = ranked_day_inputs.load_coverage(
        database,
        connection,
        player_id,
        ranked_day,
        start_battle_log_observation_id,
        end_battle_log_observation_id,
    )
    contributions = ranked_day_inputs.load_contributions(
        connection, player_id, ranked_day
    )
    previous = ranked_day_inputs.load_previous_day(connection, player_id, ranked_day)
    zero_result_attacks, zero_result_defenses = ranked_day_inputs.slot_counts(
        ranked_day_inputs.load_zero_result_slots(connection, coverage)
        | ranked_day_inputs.load_late_zero_result_slots(
            database, connection, player_id, ranked_day,
            coverage[-1].observed_at if coverage else ranked_day.start,
        ),
        *domain.battle_window(ranked_day.start),
    )
    anchor, season_day = _anchored_day(connection, ranked_day.start)
    # Days before the anchor's previous Season stay an anchor conflict.
    anchor_valid = season_day is not None and ranked_day.start >= anchor[3]
    official_season_id = season_day.official_season_id if season_day else "unknown"
    season_day_number = season_day.day_number if season_day else 1
    boundary_kind = None
    if season_day is not None and ranked_day.end == season_day.season_end:
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
    data = ReconciliationInput(
        ranked_day=ranked_day,
        now=now,
        start_baseline_id=(
            int(start_baseline["id"])
            if start_baseline is not None and start_baseline["id"] is not None
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
        not_enrolled=not contributions
        and _known_not_enrolled(connection, player_id, ranked_day),
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
        parser_version=parser_version,
        processing_version=processing_version,
        domain_rule_version=domain_rule_version,
        season_anchor_rule_version=SEASON_ANCHOR_RULE_VERSION,
        trophy_allocation_rule_versions=trophy_rule_versions,
        season_first_day=season_day is not None and season_day.day_number == 1,
        zero_result_attack_slots=zero_result_attacks,
        zero_result_defense_slots=zero_result_defenses,
    )
    result = reconcile_ranked_day(data)
    reading_at = (
        end_baseline["evidence"]["profile"]["observed_at"]
        if end_baseline is not None else None
    )
    if reading_at and reads_later_reading(data, result):
        # A later reading can settle an end Reset reading taken before the
        # game finished crediting the day or charging its automatic loss.
        later = ranked_day_inputs.load_later_reading(
            database, connection, player_id, ranked_day,
            datetime.fromisoformat(reading_at),
        )
        if later is not None:
            result = reconcile_ranked_day(
                replace(data, later_next_start_reading=later)
            )
    result_data = {
        "state": result.state,
        "confidence": result.confidence,
        "failure_reasons": list(result.failure_reasons),
        # The Reset reading, less the previous day's automatic defense loss
        # when the reading came before the game applied it.
        "start_trophies": result.start_trophies,
        # The end reading, less this day's automatic defense loss when the
        # reading came before the game applied it: the next day's start.
        "next_start_trophies": result.next_start_trophies,
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
        "parser_version": parser_version,
        "processing_version": processing_version,
        "domain_rule_version": domain_rule_version,
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
    contribution_evidence = input_evidence.get("contributions", [])
    previous_version = connection.execute(
        """
        SELECT id, version, result_hash, replaces_version_id, state,
               final_trophies_before_reset, next_start_trophies
        FROM ranked_day_versions
        WHERE player_id = %s AND ranked_day_start = %s
          AND reconciliation_rule_version = %s
        ORDER BY version DESC LIMIT 1
        FOR NO KEY UPDATE
        """,
        (player_id, ranked_day.start, RECONCILIATION_RULE_VERSION),
    ).fetchone()
    existing = None
    if previous_version is not None and _text_value(previous_version[2]) in (
        result_hash,
        _restored_result_hash(result_hash, previous_version[3]),
    ):
        existing = previous_version
    elif previous_version is not None and connection.execute(
        """
        SELECT 1 FROM ranked_day_versions
        WHERE player_id = %s AND ranked_day_start = %s
          AND reconciliation_rule_version = %s AND result_hash = %s
        """,
        (player_id, ranked_day.start, RECONCILIATION_RULE_VERSION, result_hash),
    ).fetchone() is not None:
        # The inputs returned to an earlier, non-current result. Publish it
        # again as the current version so later days and the leaderboard stop
        # reading the replaced one; the hash names what it replaces.
        result_hash = _restored_result_hash(result_hash, previous_version[0])
    if existing is None:
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
                input_evidence, shield_evidence,
                evidence_complete, coverage_complete, reconciled,
                shield_state, shield_duration_days,
                start_baseline_id, end_baseline_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
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
                parser_version,
                processing_version,
                domain_rule_version,
                analytics_rule_version,
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
                # This table keeps only a net proven by trophy readings; a
                # net from 8 attacks and 8 defenses alone is published below.
                result.net_trophy_change
                if result.final_trophies_before_reset is not None
                else None,
                result.observed_trophy_change,
                result.boundary_adjustment,
                result.boundary_adjustment_type,
                result.observed_boundary_adjustment,
                result.unexplained_residual,
                Jsonb(result.formula_components),
                Jsonb(input_evidence),
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
    if result.state == "Live":
        _enqueue_day_end_reconciliation(connection, player_id, ranked_day)
    if existing is None:
        # A reset sweep is the sole source of expected population.
        # No population-wide job is created for an uncoordinated
        # legacy fixture or a late/discovered player. A busy Reset fails
        # the whole calculation, which rolls back, releasing any earlier
        # Resets it took, and retries later instead of holding them.
        boundary._record_boundary_generation(database, 
            connection,
            boundary_at=ranked_day.end,
            player_id=player_id,
            ranked_day_version_id=version_id,
            ranked_day_input_hash=input_hash,
            reset_lock_wait=RESET_LOCK_WAIT,
        )
    return now >= ranked_day.end and (
        previous_version is None
        or (_text_value(previous_version[4]), *previous_version[5:7]) != (
            result.state,
            result.final_trophies_before_reset,
            result_data["next_start_trophies"],
        )
    )


def _anchored_day(connection: Any, day_start: datetime) -> tuple[Any, RankedDay | None]:
    """The confirmed anchor row and the day's Season identity counted from it
    in 28-day steps; no identity without an anchor or with an off-phase one."""
    anchor = connection.execute(
        """
        SELECT current_league_season_id, previous_league_season_id,
               current_start, previous_start
        FROM legend_season_anchors
        WHERE state = 'confirmed' AND anchor_rule_version = %s
        ORDER BY current_start DESC LIMIT 1
        """,
        (SEASON_ANCHOR_RULE_VERSION,),
    ).fetchone()
    try:
        ids = (_text_value(anchor[0]), _text_value(anchor[1])) if anchor else None
        return anchor, domain.anchored_ranked_day(day_start, *ids) if ids else None
    except DomainRuleError:
        return anchor, None


def _restored_result_hash(result_hash: str, replaces_version_id: Any) -> str:
    """The unique hash of an earlier result published again over another."""
    return hashlib.sha256(
        f"{result_hash}:restores-over:{replaces_version_id}".encode()
    ).hexdigest()


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
                    WHEN 'supercell-source-parser-v1'
                        THEN source_row.source_json -> 'opponent' -> 'tag'
                    ELSE source_row.source_json -> 'opponentPlayerTag'
                END,
                'name', CASE evidence.parser_version
                    WHEN 'supercell-source-parser-v1'
                        THEN source_row.source_json -> 'opponent' -> 'name'
                    ELSE source_row.source_json -> 'opponentName'
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
            *domain.battle_window(ranked_day.start),
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
            domain_rule_version, analytics_rule_version, priority
        ) VALUES (
            NULL, 'reconcile_ranked_day', %s, %s, 'pending', clock_timestamp(),
            %s, %s, %s, %s, %s
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
            ended_day_priority(ranked_day_start),
        ),
    )


def _enqueue_day_end_reconciliation(
    connection: Any, player_id: int, ranked_day: RankedDay
) -> None:
    """Queue one calculation of a day saved Live, due after its Reset.

    The Reset reading normally finishes the day, but a player switched off
    during it, such as one moved out of Legend I when a Season starts, gets
    none and nothing else calculates the day again. On 2026-10-06 that left
    2,037 ended Day 1 results Live. Due DAY_END_RECALCULATION_DELAY after the
    Reset, once its readings have landed, the job runs only when no other
    work waits, and does nothing once the day is finished, unless a reading
    since the Reset may settle it (``ranked_day_inputs.LATER_READING_DAY_SQL``).
    """
    day_text = ranked_day.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    connection.execute(
        """
        INSERT INTO python_processing_jobs_worker (
            observation_id, work_type, deduplication_key, input_json,
            state, due_at, parser_version, processing_version,
            domain_rule_version, analytics_rule_version, priority
        ) VALUES (
            NULL, 'reconcile_ranked_day', %s, %s, 'pending', %s,
            %s, %s, %s, %s, %s
        )
        ON CONFLICT (deduplication_key) DO NOTHING
        """,
        (
            f"reconcile:day-end:{player_id}:{day_text}:{RECONCILIATION_RULE_VERSION}",
            Jsonb(
                {
                    "player_id": int(player_id),
                    "ranked_day_start": day_text,
                    "trigger": "day_end",
                }
            ),
            ranked_day.end + DAY_END_RECALCULATION_DELAY,
            DEFAULT_PARSER_VERSION,
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            ANALYTICS_RULE_VERSION,
            PYTHON_BACKFILL_PRIORITY,
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
) -> dict[str, Any]:
    """Compatibility entry; see ``battle_day_repair``."""
    return battle_day_repair.enqueue_current_season_republication(
        database, max_jobs=max_jobs
    )
