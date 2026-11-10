"""The named states of one Legend day's result: how good its evidence is
(``DayState``, ``Confidence``, decided by ``assess``) and whether it
continues an inferred shield run (``ShieldState``, decided by
``infer_shield``). ``reconciliation._reconcile`` works out the numbers and
asks these for the states; nothing else decides them."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple

from .loss_rule import LossEvidence

if TYPE_CHECKING:
    from .reconciliation import PreviousRankedDay, ReconciliationInput

# Trophy values are integer source values. There is no rounding allowance.
TROPHY_RECONCILIATION_TOLERANCE = 0


class DayState(StrEnum):
    """How far a Legend day's evidence goes, saved as each version's
    ``state``. Its value is the saved text.

    LIVE: the day has not ended. COMPLETE: continuous battle logs, a usable
    start and end total (from Reset readings, the Season rule or the
    calculated totals) and no failure reason. MALFORMED: evidence that
    cannot be read. INCONSISTENT: readable evidence that disagrees, such as a
    trophy equation mismatch or a disputed battle. PARTIAL: anything else
    ended, such as a missing reading or a gap in the logs.

    Owner: ``assess``, from the evidence ``_reconcile`` gathered. Each new
    piece of evidence (a battle log, a reading, the next day's start) queues
    the day to be calculated again from all its evidence, and a changed result
    is saved as a new version; nothing moves a day from one state to the next
    in place, so a later version can be better or worse. A failed or retried
    calculation saves nothing, and a restart calculates again.
    """

    LIVE = "Live"
    COMPLETE = "Complete"
    PARTIAL = "Partial"
    INCONSISTENT = "Inconsistent"
    MALFORMED = "Malformed"


class Confidence(StrEnum):
    """How sure a Legend day's numbers are, saved as each version's
    ``confidence``. EXACT: a Complete day the readings prove. INFERRED: a
    Complete day whose end or start is calculated rather than read (hidden
    by a reset, started from the day before's end, read before its automatic
    loss, or a reading in a battle's landing span). PARTIAL: a Live or
    Partial day. UNCERTAIN: an Inconsistent or Malformed day, or one whose
    shield cannot be told. Decided with the state by ``assess``."""

    EXACT = "exact"
    INFERRED = "inferred"
    PARTIAL = "partial"
    UNCERTAIN = "uncertain"


def assess(
    ended: bool,
    *,
    malformed: bool = False,
    inconsistent: bool = False,
    failures: Iterable[str] = (),
    coverage_complete: bool = False,
    start_available: bool = False,
    end_available: bool = False,
    shield: ShieldState | None = None,
    calculated: bool = False,
) -> tuple[DayState, Confidence]:
    """The day's state and confidence. ``calculated`` says a Complete day's
    end or start was calculated rather than read; it lowers EXACT, and an
    unknown shield's UNCERTAIN, to INFERRED."""
    if not ended:
        return DayState.LIVE, Confidence.PARTIAL
    failures = tuple(failures)
    if malformed:
        state = DayState.MALFORMED
    elif inconsistent or "trophy_equation_mismatch" in failures:
        state = DayState.INCONSISTENT
    elif not failures and coverage_complete and start_available and end_available:
        state = DayState.COMPLETE
    else:
        state = DayState.PARTIAL
    confidence = Confidence.EXACT if state is DayState.COMPLETE else Confidence.PARTIAL
    if state in {DayState.INCONSISTENT, DayState.MALFORMED} or shield is ShieldState.UNKNOWN:
        confidence = Confidence.UNCERTAIN
    if state is DayState.COMPLETE and calculated:
        confidence = Confidence.INFERRED
    return state, confidence


class ShieldState(StrEnum):
    """Whether a Legend day is inferred to sit inside a shield, saved as each
    version's ``shield_state``. It is an inference from a day that stood
    still, never proof that a Clasher bought a shield.

    NOT_INFERRED: the day is live, or had a battle, a confirmed automatic
    loss or a trophy change. UNKNOWN: the evidence cannot tell (an ineligible
    player, a gap in the logs, a missing Reset reading, a disputed battle, an
    automatic loss not known to be none, or a next Reset that disagrees).
    INFERRED_SHIELDED: an ended day with no battle, no automatic loss and the
    same trophies at both Resets. Historical ``not_shielded`` and
    ``uncertain_sequence`` values remain allowed by the database.

    The run: a shielded day after a shielded day continues its run, one day
    longer (``shield_duration_days``); any other day ends it, so the next
    shielded day starts a new run of 1 (``shield_run_length``). Owner:
    ``infer_shield``. Like the day's state, it is recalculated from the
    evidence each time the day is, and the day after reads the newest saved
    version of the day before, so a changed day before changes the run only
    once the day after is calculated again.
    """

    NOT_INFERRED = "not_inferred"
    UNKNOWN = "unknown"
    INFERRED_SHIELDED = "inferred_shielded"


class Shield(NamedTuple):
    state: ShieldState
    duration_days: int | None
    evidence: dict[str, Any]


def shield_run_length(state: str | None, duration_days: int | None) -> int:
    """How long a run a saved day leaves for the day after to continue:
    its run when it was shielded, else none."""
    return int(duration_days or 0) if state == ShieldState.INFERRED_SHIELDED else 0


def infer_shield(
    data: ReconciliationInput,
    *,
    ended: bool,
    attack_count: int,
    defense_count: int,
    coverage_complete: bool,
    start_trophies: int | None,
    final_trophies: int | None,
    residual: int | None,
    automatic_state: LossEvidence,
    start_available: bool,
    end_available: bool,
    previous: PreviousRankedDay | None,
) -> Shield:
    evidence: dict[str, Any] = {
        "ended": ended,
        "eligible": data.player_eligible,
        "coverage_complete": coverage_complete,
        "start_baseline_complete": start_available,
        "end_baseline_complete": end_available,
        "attack_count_zero": attack_count == 0,
        "defense_count_zero": defense_count == 0,
        "automatic_defense_not_applied": automatic_state == LossEvidence.NOT_APPLICABLE,
        "trophies_unchanged": (
            final_trophies is not None
            and start_trophies is not None
            and final_trophies == start_trophies
        ),
        "perspective_disagreement": data.perspective_disagreement,
    }
    if not ended:
        return Shield(ShieldState.NOT_INFERRED, None, evidence)
    if not data.player_eligible or not coverage_complete:
        return Shield(ShieldState.UNKNOWN, None, evidence)
    if not start_available or not end_available:
        return Shield(ShieldState.UNKNOWN, None, evidence)
    if data.perspective_disagreement:
        return Shield(ShieldState.UNKNOWN, None, evidence)
    if attack_count or defense_count or automatic_state == LossEvidence.CONFIRMED:
        # A confirmed automatic loss is a trophy change: not a shield.
        return Shield(ShieldState.NOT_INFERRED, None, evidence)
    if automatic_state != LossEvidence.NOT_APPLICABLE:
        return Shield(ShieldState.UNKNOWN, None, evidence)
    # With no events the calculated trophies cannot change, so only the
    # observed next-Reset trophies can show that the day did not stand still.
    if residual is None or abs(residual) > TROPHY_RECONCILIATION_TOLERANCE:
        return Shield(ShieldState.UNKNOWN, None, evidence)
    if not evidence["trophies_unchanged"]:
        return Shield(ShieldState.NOT_INFERRED, None, evidence)

    # Shields stack, so a run of such days is shielded however long it lasts.
    prior_run = previous.shield_run_length if previous is not None else 0
    return Shield(ShieldState.INFERRED_SHIELDED, prior_run + 1, evidence)
