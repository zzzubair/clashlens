from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any

from .domain import RankedDay

# Version 2 records the complete formula and evidence boundary. Version 1 used
# an incomplete automatic-defense average and could infer a shield from counts.
RECONCILIATION_RULE_VERSION = "legend-ranked-day-reconciliation-v2"
MAX_DAILY_ATTACKS = 8
MAX_DAILY_DEFENSES = 8
BATTLE_LOG_MAX_ROWS = 50
# Trophy values are integer source values. There is no rounding allowance.
TROPHY_RECONCILIATION_TOLERANCE = 0


@dataclass(frozen=True, slots=True)
class CoverageObservation:
    observed_at: datetime
    row_count: int
    battle_identities: tuple[str, ...]
    has_row_gap: bool
    observation_id: int | None = None
    valid: bool = True
    stale_window: bool = False
    unclassified_row_count: int = 0
    malformed_row_count: int = 0
    response_hash: str | None = None
    parser_version: str | None = None
    processing_version: str | None = None
    source_row_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class BattleContribution:
    battle_identity: str
    lens: str
    trophy_amount: int
    # Some source adapters expose the signed award/change directly. When it is
    # present, it is the value used by reconciliation. Stars and destruction
    # are never used here to replace a source-provided value.
    source_trophy_change: int | None = None
    valid: bool = True
    failure_reason: str | None = None
    source_rule_version: str | None = None
    disagreement: bool = False
    source_observation_id: int | None = None
    source_evidence_id: int | None = None
    source_row_id: int | None = None
    source_observed_at: datetime | None = None
    battle_timestamp: datetime | None = None
    stars: int | None = None
    destruction_percentage: int | None = None
    army_share_code: str | None = None
    attacker_gain: int | None = None
    defender_loss: int | None = None

    def __post_init__(self) -> None:
        if self.lens not in {"offense", "defense"}:
            raise ValueError("battle contribution lens must be offense or defense")

    @property
    def amount(self) -> int | None:
        value = (
            self.source_trophy_change
            if self.source_trophy_change is not None
            else self.trophy_amount
        )
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value


@dataclass(frozen=True, slots=True)
class PreviousRankedDay:
    complete: bool
    observed_defense_count: int
    observed_defense_loss: int
    shield_run_length: int
    coverage_complete: bool = True
    shield_state: str | None = None
    version_id: int | None = None
    ranked_day_start: datetime | None = None
    state: str | None = None
    confidence: str | None = None
    input_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationInput:
    ranked_day: RankedDay
    now: datetime
    start_baseline_id: int | None
    end_baseline_id: int | None
    start_trophies: int | None
    next_start_trophies: int | None
    coverage_observations: tuple[CoverageObservation, ...]
    contributions: tuple[BattleContribution, ...]
    previous_day: PreviousRankedDay | None
    boundary_kind: str | None
    season_anchor_valid: bool
    # The battle-log responses that the start and end reset-baseline sweeps
    # selected. A sweep requests its endpoints after the 05:00 UTC boundary,
    # so these observations are normally observed after the ranked-day
    # boundary timestamps; the continuity chain is bound to these exact
    # observation identities, not to boundary-time comparisons.
    start_baseline_battle_log_observation_id: int | None = None
    end_baseline_battle_log_observation_id: int | None = None
    # None preserves the original seam's meaning: an existing baseline ID is
    # accepted. False is explicit incomplete reset evidence.
    start_baseline_complete: bool | None = None
    end_baseline_complete: bool | None = None
    player_eligible: bool = True
    perspective_disagreement: bool = False
    malformed_evidence: bool = False
    unclassified_evidence: bool = False
    start_baseline_evidence: dict[str, Any] = field(default_factory=dict)
    end_baseline_evidence: dict[str, Any] = field(default_factory=dict)
    parser_version: str | None = None
    processing_version: str | None = None
    domain_rule_version: str | None = None
    season_anchor_rule_version: str | None = None
    trophy_allocation_rule_versions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.boundary_kind not in {None, "weekly", "season"}:
            raise ValueError("boundary kind must be weekly, season, or None")


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    state: str
    confidence: str
    attack_count: int
    defense_count: int
    attack_trophy_gain: int
    observed_defense_loss: int
    automatic_defense_loss: int | None
    automatic_defense_evidence_state: str
    boundary_adjustment: int
    boundary_adjustment_type: str | None
    final_trophies_before_reset: int | None
    shield_state: str
    shield_duration_days: int | None
    coverage_complete: bool
    failure_reasons: tuple[str, ...]
    reconciliation_rule_version: str = RECONCILIATION_RULE_VERSION
    net_trophy_change: int | None = None
    observed_trophy_change: int | None = None
    observed_boundary_adjustment: int | None = None
    expected_next_start_trophies: int | None = None
    unexplained_residual: int | None = None
    formula_components: dict[str, Any] = field(default_factory=dict)
    shield_evidence: dict[str, Any] = field(default_factory=dict)
    input_evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def attack_gain(self) -> int:
        return self.attack_trophy_gain

    @property
    def defense_loss(self) -> int:
        return self.observed_defense_loss


def reconcile_ranked_day(data: ReconciliationInput) -> ReconciliationResult:
    failures: list[str] = []
    malformed_evidence = data.malformed_evidence
    if data.malformed_evidence:
        failures.append("malformed_evidence")
    if data.unclassified_evidence:
        failures.append("unclassified_rows")
    inconsistent_evidence = data.perspective_disagreement
    if data.perspective_disagreement:
        failures.append("perspective_disagreement")

    coverage_complete, coverage_evidence, coverage_malformed = _coverage_is_continuous(
        data, failures
    )
    malformed_evidence = malformed_evidence or coverage_malformed

    contributions, contribution_evidence, contribution_reasons, contribution_malformed, contribution_inconsistent = (
        _deduplicate_contributions(data.contributions)
    )
    failures.extend(contribution_reasons)
    malformed_evidence = malformed_evidence or contribution_malformed
    inconsistent_evidence = inconsistent_evidence or contribution_inconsistent

    offenses = tuple(item for item in contributions if item.lens == "offense")
    defenses = tuple(item for item in contributions if item.lens == "defense")
    attack_count = len(offenses)
    defense_count = len(defenses)
    attack_gain = sum(item.amount or 0 for item in offenses)
    defense_loss = sum(item.amount or 0 for item in defenses)

    if attack_count > MAX_DAILY_ATTACKS:
        failures.append("attack_count_exceeds_eight")
    if defense_count > MAX_DAILY_DEFENSES:
        failures.append("defense_count_exceeds_eight")

    automatic_loss, automatic_state = _automatic_defense_adjustment(
        data,
        coverage_complete=coverage_complete,
        defense_count=defense_count,
        observed_defense_loss=defense_loss,
        failures=failures,
    )

    ended = data.now >= data.ranked_day.end
    start_available = _baseline_available(
        data.start_baseline_id,
        data.start_trophies,
        data.start_baseline_complete,
        "start",
        failures,
    )
    end_available = _baseline_available(
        data.end_baseline_id,
        data.next_start_trophies,
        data.end_baseline_complete,
        "end",
        failures,
    )
    if not data.season_anchor_valid:
        failures.append("season_anchor_conflict")
    if not data.player_eligible:
        failures.append("player_not_eligible")

    final_trophies: int | None = None
    net_trophy_change: int | None = None
    observed_trophy_change: int | None = None
    boundary_adjustment = 0
    boundary_type: str | None = None
    observed_boundary_adjustment: int | None = None
    expected_next: int | None = None
    residual: int | None = None

    automatic_value_known = not (1 <= defense_count <= 7) or automatic_loss is not None
    if start_available and automatic_value_known:
        assert data.start_trophies is not None
        final_trophies = (
            data.start_trophies
            + attack_gain
            - defense_loss
            - (automatic_loss or 0)
        )
        net_trophy_change = final_trophies - data.start_trophies
        if data.next_start_trophies is not None:
            observed_trophy_change = data.next_start_trophies - data.start_trophies

        if data.boundary_kind == "season" or (
            data.boundary_kind == "weekly" and final_trophies < 5000
        ):
            boundary_adjustment = 5000 - final_trophies
            boundary_type = f"{data.boundary_kind}_reset"
        expected_next = final_trophies + boundary_adjustment

        if end_available and data.next_start_trophies is not None:
            observed_boundary_adjustment = data.next_start_trophies - final_trophies
            residual = data.next_start_trophies - expected_next
            if abs(residual) > TROPHY_RECONCILIATION_TOLERANCE:
                failures.append("trophy_equation_mismatch")
            elif automatic_loss is not None and automatic_state == "calculated":
                # The paired baselines isolate the calculated settlement loss.
                automatic_state = "confirmed"

    if not ended:
        shield_state, shield_duration, shield_evidence = _shield_state(
            data,
            ended=False,
            attack_count=attack_count,
            defense_count=defense_count,
            coverage_complete=coverage_complete,
            final_trophies=final_trophies,
            automatic_state=automatic_state,
            start_available=start_available,
            end_available=end_available,
            previous=data.previous_day,
        )
        return _result(
            state="Live",
            confidence="partial",
            attack_count=attack_count,
            defense_count=defense_count,
            attack_gain=attack_gain,
            defense_loss=defense_loss,
            automatic_loss=automatic_loss,
            automatic_state=automatic_state,
            boundary_adjustment=0,
            boundary_type=None,
            final_trophies=None,
            shield_state=shield_state,
            shield_duration=shield_duration,
            coverage_complete=coverage_complete,
            failures=failures,
            net_trophy_change=None,
            observed_trophy_change=None,
            observed_boundary_adjustment=None,
            expected_next=None,
            residual=None,
            formula_components=_formula_components(
                data,
                attack_gain=attack_gain,
                defense_loss=defense_loss,
                automatic_loss=automatic_loss,
                final_trophies=None,
                boundary_adjustment=0,
                expected_next=None,
                residual=None,
            ),
            shield_evidence=shield_evidence,
            input_evidence=_input_evidence(
                data,
                coverage_evidence=coverage_evidence,
                contribution_evidence=contribution_evidence,
            ),
        )

    shield_state, shield_duration, shield_evidence = _shield_state(
        data,
        ended=True,
        attack_count=attack_count,
        defense_count=defense_count,
        coverage_complete=coverage_complete,
        final_trophies=final_trophies,
        automatic_state=automatic_state,
        start_available=start_available,
        end_available=end_available,
        previous=data.previous_day,
    )
    if shield_state == "uncertain_sequence":
        failures.append("shield_sequence_longer_than_two_days")
    if shield_state == "unknown" and coverage_complete is False:
        # The coverage reasons already carry the precise cause. This marker is
        # useful to consumers that only inspect the shield evidence state.
        shield_evidence.setdefault("unknown_reason", "coverage_incomplete")

    unique_failures = tuple(dict.fromkeys(failures))
    state = "Partial"
    if malformed_evidence:
        state = "Malformed"
    elif inconsistent_evidence or "trophy_equation_mismatch" in unique_failures:
        state = "Inconsistent"
    elif not unique_failures and coverage_complete and start_available and end_available:
        state = "Complete"

    confidence = "exact" if state == "Complete" else "partial"
    if state in {"Inconsistent", "Malformed"} or shield_state in {
        "unknown",
        "uncertain_sequence",
    }:
        confidence = "uncertain"
    if state == "Complete" and shield_state == "inferred_shielded":
        confidence = "inferred"

    return _result(
        state=state,
        confidence=confidence,
        attack_count=attack_count,
        defense_count=defense_count,
        attack_gain=attack_gain,
        defense_loss=defense_loss,
        automatic_loss=automatic_loss,
        automatic_state=automatic_state,
        boundary_adjustment=boundary_adjustment,
        boundary_type=boundary_type,
        final_trophies=final_trophies,
        shield_state=shield_state,
        shield_duration=shield_duration,
        coverage_complete=coverage_complete,
        failures=unique_failures,
        net_trophy_change=net_trophy_change,
        observed_trophy_change=observed_trophy_change,
        observed_boundary_adjustment=observed_boundary_adjustment,
        expected_next=expected_next,
        residual=residual,
        formula_components=_formula_components(
            data,
            attack_gain=attack_gain,
            defense_loss=defense_loss,
            automatic_loss=automatic_loss,
            final_trophies=final_trophies,
            boundary_adjustment=boundary_adjustment,
            expected_next=expected_next,
            residual=residual,
        ),
        shield_evidence=shield_evidence,
        input_evidence=_input_evidence(
            data,
            coverage_evidence=coverage_evidence,
            contribution_evidence=contribution_evidence,
        ),
    )


def _result(
    *,
    state: str,
    confidence: str,
    attack_count: int,
    defense_count: int,
    attack_gain: int,
    defense_loss: int,
    automatic_loss: int | None,
    automatic_state: str,
    boundary_adjustment: int,
    boundary_type: str | None,
    final_trophies: int | None,
    shield_state: str,
    shield_duration: int | None,
    coverage_complete: bool,
    failures: list[str] | tuple[str, ...],
    net_trophy_change: int | None,
    observed_trophy_change: int | None,
    observed_boundary_adjustment: int | None,
    expected_next: int | None,
    residual: int | None,
    formula_components: dict[str, Any],
    shield_evidence: dict[str, Any],
    input_evidence: dict[str, Any],
) -> ReconciliationResult:
    return ReconciliationResult(
        state=state,
        confidence=confidence,
        attack_count=attack_count,
        defense_count=defense_count,
        attack_trophy_gain=attack_gain,
        observed_defense_loss=defense_loss,
        automatic_defense_loss=automatic_loss,
        automatic_defense_evidence_state=automatic_state,
        boundary_adjustment=boundary_adjustment,
        boundary_adjustment_type=boundary_type,
        final_trophies_before_reset=final_trophies,
        shield_state=shield_state,
        shield_duration_days=shield_duration,
        coverage_complete=coverage_complete,
        failure_reasons=tuple(dict.fromkeys(failures)),
        net_trophy_change=net_trophy_change,
        observed_trophy_change=observed_trophy_change,
        observed_boundary_adjustment=observed_boundary_adjustment,
        expected_next_start_trophies=expected_next,
        unexplained_residual=residual,
        formula_components=formula_components,
        shield_evidence=shield_evidence,
        input_evidence=input_evidence,
    )


def _deduplicate_contributions(
    contributions: tuple[BattleContribution, ...],
) -> tuple[
    tuple[BattleContribution, ...],
    list[dict[str, Any]],
    list[str],
    bool,
    bool,
]:
    by_key: dict[tuple[str, str], BattleContribution] = {}
    evidence: list[dict[str, Any]] = []
    reasons: list[str] = []
    malformed = False
    inconsistent = False
    for contribution in contributions:
        amount = contribution.amount
        if not contribution.valid or not contribution.battle_identity or amount is None:
            malformed = True
            reasons.append(contribution.failure_reason or "malformed_contribution")
            evidence.append(_contribution_evidence(contribution, included=False))
            continue
        key = (contribution.battle_identity, contribution.lens)
        previous = by_key.get(key)
        if previous is not None:
            if previous.amount != amount or contribution.disagreement:
                inconsistent = True
                reasons.append("duplicate_contribution_disagreement")
            evidence.append(
                _contribution_evidence(
                    contribution,
                    included=False,
                    duplicate_of=previous.battle_identity,
                )
            )
            continue
        by_key[key] = contribution
        evidence.append(_contribution_evidence(contribution, included=True))
        if contribution.disagreement:
            inconsistent = True
            reasons.append("perspective_disagreement")
    return (
        tuple(by_key[key] for key in sorted(by_key)),
        evidence,
        list(dict.fromkeys(reasons)),
        malformed,
        inconsistent,
    )


def _contribution_evidence(
    contribution: BattleContribution,
    *,
    included: bool,
    duplicate_of: str | None = None,
) -> dict[str, Any]:
    return {
        "battle_identity": contribution.battle_identity,
        "lens": contribution.lens,
        "source_trophy_change": contribution.source_trophy_change,
        "trophy_amount": contribution.trophy_amount,
        "amount_used": contribution.amount,
        "source_rule_version": contribution.source_rule_version,
        "included": included,
        "duplicate_of": duplicate_of,
        "valid": contribution.valid,
        "failure_reason": contribution.failure_reason,
        "disagreement": contribution.disagreement,
        "source_observation_id": contribution.source_observation_id,
        "source_evidence_id": contribution.source_evidence_id,
        "source_row_id": contribution.source_row_id,
        "source_observed_at": (
            contribution.source_observed_at.isoformat()
            if contribution.source_observed_at is not None
            else None
        ),
        "battle_timestamp": (
            contribution.battle_timestamp.isoformat()
            if contribution.battle_timestamp is not None
            else None
        ),
        "stars": contribution.stars,
        "destruction_percentage": contribution.destruction_percentage,
        "army_share_code": contribution.army_share_code,
        "attacker_gain": contribution.attacker_gain,
        "defender_loss": contribution.defender_loss,
    }


def _coverage_is_continuous(
    data: ReconciliationInput,
    failures: list[str],
) -> tuple[bool, list[dict[str, Any]], bool]:
    observations = tuple(
        sorted(
            data.coverage_observations,
            key=lambda item: (item.observed_at, item.observation_id or 0),
        )
    )
    evidence = [
        {
            "observation_id": item.observation_id,
            "observed_at": item.observed_at.isoformat(),
            "row_count": item.row_count,
            "battle_identities": list(item.battle_identities),
            "has_row_gap": item.has_row_gap,
            "valid": item.valid,
            "stale_window": item.stale_window,
            "unclassified_row_count": item.unclassified_row_count,
            "malformed_row_count": item.malformed_row_count,
            "response_hash": item.response_hash,
            "parser_version": item.parser_version,
            "processing_version": item.processing_version,
            "source_row_ids": list(item.source_row_ids),
        }
        for item in observations
    ]
    malformed = False
    if not observations:
        failures.extend(
            ["missing_start_battle_log_baseline", "missing_end_battle_log_baseline"]
        )
        return False, evidence, malformed
    if (
        data.start_baseline_battle_log_observation_id is None
        or observations[0].observation_id
        != data.start_baseline_battle_log_observation_id
    ):
        failures.append("missing_start_battle_log_baseline")
    if (
        data.end_baseline_battle_log_observation_id is None
        or observations[-1].observation_id
        != data.end_baseline_battle_log_observation_id
    ):
        failures.append("missing_end_battle_log_baseline")

    for observation in observations:
        if observation.row_count < 0 or observation.row_count > BATTLE_LOG_MAX_ROWS:
            failures.append("battle_log_row_count_exceeds_fifty")
            malformed = True
        if not observation.valid or observation.has_row_gap or observation.malformed_row_count:
            failures.append("battle_log_row_gap")
            malformed = True
        if observation.unclassified_row_count:
            failures.append("unclassified_rows")
        if observation.stale_window:
            failures.append("battle_log_stale_window")
        if len(set(observation.battle_identities)) != len(observation.battle_identities):
            failures.append("duplicate_battle_identity_in_observation")
            malformed = True

    for previous, current in pairwise(observations):
        if current.row_count >= BATTLE_LOG_MAX_ROWS and not (
            set(previous.battle_identities) & set(current.battle_identities)
        ):
            failures.append("battle_log_overlap_gap")

    hard_coverage_reasons = {
        "missing_start_battle_log_baseline",
        "missing_end_battle_log_baseline",
        "battle_log_row_gap",
        "battle_log_overlap_gap",
        "battle_log_stale_window",
        "battle_log_row_count_exceeds_fifty",
        "unclassified_rows",
    }
    complete = not any(reason in hard_coverage_reasons for reason in failures)
    return complete, evidence, malformed


def _automatic_defense_adjustment(
    data: ReconciliationInput,
    *,
    coverage_complete: bool,
    defense_count: int,
    observed_defense_loss: int,
    failures: list[str],
) -> tuple[int | None, str]:
    if defense_count == 0 or defense_count >= MAX_DAILY_DEFENSES:
        return None, "not_applicable"
    previous = data.previous_day
    if (
        previous is None
        or not previous.complete
        or not previous.coverage_complete
        or not coverage_complete
        or previous.observed_defense_count < 0
    ):
        failures.append("automatic_defense_basis_unavailable")
        return None, "unknown"
    denominator = previous.observed_defense_count + defense_count
    if denominator <= 0:
        failures.append("automatic_defense_basis_unavailable")
        return None, "unknown"
    average_loss = (
        previous.observed_defense_loss + observed_defense_loss
    ) // denominator
    return average_loss * (MAX_DAILY_DEFENSES - defense_count), "calculated"


def _baseline_available(
    baseline_id: int | None,
    trophies: int | None,
    explicit_complete: bool | None,
    label: str,
    failures: list[str],
) -> bool:
    if baseline_id is None or trophies is None:
        failures.append(f"missing_{label}_baseline")
        return False
    if explicit_complete is False:
        failures.append(f"{label}_baseline_incomplete")
        return False
    return True


def _shield_state(
    data: ReconciliationInput,
    *,
    ended: bool,
    attack_count: int,
    defense_count: int,
    coverage_complete: bool,
    final_trophies: int | None,
    automatic_state: str,
    start_available: bool,
    end_available: bool,
    previous: PreviousRankedDay | None,
) -> tuple[str, int | None, dict[str, Any]]:
    evidence: dict[str, Any] = {
        "ended": ended,
        "eligible": data.player_eligible,
        "coverage_complete": coverage_complete,
        "start_baseline_complete": start_available,
        "end_baseline_complete": end_available,
        "attack_count_zero": attack_count == 0,
        "defense_count_zero": defense_count == 0,
        "automatic_defense_not_applied": automatic_state == "not_applicable",
        "trophies_unchanged": (
            final_trophies is not None
            and data.start_trophies is not None
            and final_trophies == data.start_trophies
        ),
        "perspective_disagreement": data.perspective_disagreement,
    }
    if not ended:
        return "not_inferred", None, evidence
    if not data.player_eligible or not coverage_complete:
        return "unknown", None, evidence
    if not start_available or not end_available:
        return "unknown", None, evidence
    if data.perspective_disagreement:
        return "unknown", None, evidence
    if attack_count or defense_count:
        return "not_inferred", None, evidence
    if automatic_state != "not_applicable":
        return "unknown", None, evidence
    if not evidence["trophies_unchanged"]:
        return "not_inferred", None, evidence

    prior_run = previous.shield_run_length if previous is not None else 0
    duration = prior_run + 1
    if duration > 2:
        return "uncertain_sequence", None, evidence
    return "inferred_shielded", duration, evidence


def _formula_components(
    data: ReconciliationInput,
    *,
    attack_gain: int,
    defense_loss: int,
    automatic_loss: int | None,
    final_trophies: int | None,
    boundary_adjustment: int,
    expected_next: int | None,
    residual: int | None,
) -> dict[str, Any]:
    return {
        "start_trophies": data.start_trophies,
        "attack_gain": attack_gain,
        "observed_defense_loss": defense_loss,
        "automatic_defense_loss": automatic_loss,
        "final_trophies_before_reset": final_trophies,
        "boundary_adjustment": boundary_adjustment,
        "expected_next_start_trophies": expected_next,
        "next_start_trophies": data.next_start_trophies,
        "unexplained_residual": residual,
        "tolerance": TROPHY_RECONCILIATION_TOLERANCE,
        "equation": "next_start = start + attack_gain - defense_loss - automatic_defense_loss + boundary_adjustment",
    }


def _input_evidence(
    data: ReconciliationInput,
    *,
    coverage_evidence: list[dict[str, Any]],
    contribution_evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ranked_day_start": data.ranked_day.start.isoformat(),
        "ranked_day_end": data.ranked_day.end.isoformat(),
        "start_baseline_id": data.start_baseline_id,
        "end_baseline_id": data.end_baseline_id,
        "start_baseline_battle_log_observation_id": (
            data.start_baseline_battle_log_observation_id
        ),
        "end_baseline_battle_log_observation_id": (
            data.end_baseline_battle_log_observation_id
        ),
        "start_baseline_complete": data.start_baseline_complete,
        "end_baseline_complete": data.end_baseline_complete,
        "start_trophies": data.start_trophies,
        "next_start_trophies": data.next_start_trophies,
        "boundary_kind": data.boundary_kind,
        "season_anchor_valid": data.season_anchor_valid,
        "player_eligible": data.player_eligible,
        "perspective_disagreement": data.perspective_disagreement,
        "malformed_evidence": data.malformed_evidence,
        "unclassified_evidence": data.unclassified_evidence,
        "start_baseline_evidence": data.start_baseline_evidence,
        "end_baseline_evidence": data.end_baseline_evidence,
        "previous_day": _previous_day_evidence(data.previous_day),
        "rule_versions": {
            "reconciliation": RECONCILIATION_RULE_VERSION,
            "parser": data.parser_version,
            "processing": data.processing_version,
            "domain": data.domain_rule_version,
            "season_anchor": data.season_anchor_rule_version,
            "trophy_allocation": list(data.trophy_allocation_rule_versions),
        },
        "coverage_observations": coverage_evidence,
        "contributions": contribution_evidence,
    }


def _previous_day_evidence(previous: PreviousRankedDay | None) -> dict[str, Any] | None:
    if previous is None:
        return None
    return {
        "complete": previous.complete,
        "observed_defense_count": previous.observed_defense_count,
        "observed_defense_loss": previous.observed_defense_loss,
        "shield_run_length": previous.shield_run_length,
        "coverage_complete": previous.coverage_complete,
        "shield_state": previous.shield_state,
        "version_id": previous.version_id,
        "ranked_day_start": (
            previous.ranked_day_start.isoformat()
            if previous.ranked_day_start is not None
            else None
        ),
        "state": previous.state,
        "confidence": previous.confidence,
        "input_hash": previous.input_hash,
    }
