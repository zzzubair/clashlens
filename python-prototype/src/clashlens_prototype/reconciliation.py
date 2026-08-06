from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .domain import RankedDay

RECONCILIATION_RULE_VERSION = "legend-ranked-day-reconciliation-v1"


@dataclass(frozen=True, slots=True)
class CoverageObservation:
    observed_at: datetime
    row_count: int
    battle_identities: tuple[str, ...]
    has_row_gap: bool


@dataclass(frozen=True, slots=True)
class BattleContribution:
    battle_identity: str
    lens: str
    trophy_amount: int

    def __post_init__(self) -> None:
        if self.lens not in {"offense", "defense"}:
            raise ValueError("battle contribution lens must be offense or defense")
        if self.trophy_amount < 0:
            raise ValueError("battle contribution amount must not be negative")


@dataclass(frozen=True, slots=True)
class PreviousRankedDay:
    complete: bool
    observed_defense_count: int
    observed_defense_loss: int
    shield_run_length: int


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


def reconcile_ranked_day(data: ReconciliationInput) -> ReconciliationResult:
    failures: list[str] = []
    coverage_complete = _coverage_is_continuous(data, failures)
    contributions = _deduplicate_contributions(data.contributions)
    offenses = [item for item in contributions if item.lens == "offense"]
    defenses = [item for item in contributions if item.lens == "defense"]
    attack_count = len(offenses)
    defense_count = len(defenses)
    attack_gain = sum(item.trophy_amount for item in offenses)
    defense_loss = sum(item.trophy_amount for item in defenses)

    if attack_count > 8:
        failures.append("attack_count_exceeds_eight")
    if defense_count > 8:
        failures.append("defense_count_exceeds_eight")

    automatic_loss: int | None = None
    automatic_state = "not_applicable"
    if 1 <= defense_count <= 7:
        previous = data.previous_day
        if (
            previous is not None
            and previous.complete
            and previous.observed_defense_count > 0
        ):
            average_loss = (
                previous.observed_defense_loss // previous.observed_defense_count
            )
            automatic_loss = average_loss * (8 - defense_count)
            automatic_state = "calculated"
        else:
            failures.append("automatic_defense_basis_unavailable")
            automatic_state = "unknown"

    shield_state, shield_duration = _shield_state(data.previous_day, defense_count)
    if shield_state == "uncertain_sequence":
        failures.append("shield_sequence_longer_than_two_days")

    if data.now < data.ranked_day.end:
        return ReconciliationResult(
            state="Live",
            confidence="partial",
            attack_count=attack_count,
            defense_count=defense_count,
            attack_trophy_gain=attack_gain,
            observed_defense_loss=defense_loss,
            automatic_defense_loss=automatic_loss,
            automatic_defense_evidence_state=automatic_state,
            boundary_adjustment=0,
            boundary_adjustment_type=None,
            final_trophies_before_reset=None,
            shield_state=shield_state,
            shield_duration_days=shield_duration,
            coverage_complete=coverage_complete,
            failure_reasons=tuple(dict.fromkeys(failures)),
        )

    if data.start_baseline_id is None or data.start_trophies is None:
        failures.append("missing_start_baseline")
    if data.end_baseline_id is None or data.next_start_trophies is None:
        failures.append("missing_end_baseline")
    if not data.season_anchor_valid:
        failures.append("season_anchor_conflict")

    final_trophies: int | None = None
    boundary_adjustment = 0
    boundary_type: str | None = None
    automatic_value_known = not (1 <= defense_count <= 7) or automatic_loss is not None
    if data.start_trophies is not None and automatic_value_known:
        final_trophies = (
            data.start_trophies
            + attack_gain
            - defense_loss
            - (automatic_loss or 0)
        )
        if data.boundary_kind is not None:
            boundary_adjustment = 5000 - final_trophies
            boundary_type = f"{data.boundary_kind}_reset"
        expected_next = final_trophies + boundary_adjustment
        if data.next_start_trophies is not None:
            if data.next_start_trophies == expected_next:
                if automatic_loss is not None:
                    automatic_state = "confirmed"
            else:
                failures.append("trophy_equation_mismatch")

    unique_failures = tuple(dict.fromkeys(failures))
    state = "Complete" if not unique_failures else "Partial"
    return ReconciliationResult(
        state=state,
        confidence="exact" if state == "Complete" else "partial",
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
        failure_reasons=unique_failures,
    )


def _deduplicate_contributions(
    contributions: tuple[BattleContribution, ...],
) -> tuple[BattleContribution, ...]:
    by_lens: dict[tuple[str, str], BattleContribution] = {}
    for contribution in contributions:
        by_lens[(contribution.battle_identity, contribution.lens)] = contribution
    return tuple(by_lens[key] for key in sorted(by_lens))


def _coverage_is_continuous(
    data: ReconciliationInput,
    failures: list[str],
) -> bool:
    observations = tuple(
        sorted(data.coverage_observations, key=lambda item: item.observed_at)
    )
    if not observations or observations[0].observed_at > data.ranked_day.start:
        failures.append("missing_start_battle_log_baseline")
    if not observations or observations[-1].observed_at < data.ranked_day.end:
        failures.append("missing_end_battle_log_baseline")
    for observation in observations:
        if observation.has_row_gap:
            failures.append("battle_log_row_gap")
    for previous, current in zip(observations, observations[1:], strict=False):
        if current.row_count >= 50 and not (
            set(previous.battle_identities) & set(current.battle_identities)
        ):
            failures.append("battle_log_overlap_gap")
    return not any(
        reason
        in {
            "missing_start_battle_log_baseline",
            "missing_end_battle_log_baseline",
            "battle_log_row_gap",
            "battle_log_overlap_gap",
        }
        for reason in failures
    )


def _shield_state(
    previous: PreviousRankedDay | None,
    defense_count: int,
) -> tuple[str, int | None]:
    if defense_count >= 8:
        return "not_shielded", None
    prior_run = previous.shield_run_length if previous is not None else 0
    duration = prior_run + 1
    if duration <= 2:
        return "inferred_shielded", duration
    return "uncertain_sequence", None
