"""The evidence a day result saved again leaves for the Reset plan's later
steps: a count of results saved again unchanged, by what queued them, and
one ``day_proof_lost`` event for each Complete result saved as worse."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from clashlens import reconciliation_db
from clashlens.domain import ranked_day_for

DAY = ranked_day_for(datetime(2026, 11, 10, 6, tzinfo=UTC))
OLD_BATTLES = [
    {"battle_identity": "a", "amount_used": 40, "included": True},
    {"battle_identity": "b", "amount_used": -31, "included": True},
    {"battle_identity": "c", "amount_used": 30, "included": False},
]


class _Connection:
    """Serves the replaced result's counted battles."""

    def execute(self, _query: str, _parameters: tuple[object, ...]) -> _Connection:
        return self

    def fetchone(self) -> tuple[object]:
        return (OLD_BATTLES,)


def _previous(state: str, confidence: str, end: int) -> tuple[object, ...]:
    # id, version, result_hash, replaces_version_id, state, end, next start, confidence
    return (7, 3, "hash", 6, state, end, end - 5, confidence)


def _result(state: str, confidence: str, end: int, **evidence: object) -> SimpleNamespace:
    return SimpleNamespace(
        state=state, confidence=confidence, final_trophies_before_reset=end,
        failure_reasons=() if state == "Complete" else ("end_reading_unverified",),
        input_evidence=evidence)


def _saved(capsys, previous, result, contributions=OLD_BATTLES, trigger="evidence"):
    """Save ``result`` over ``previous``: what was counted and logged."""
    before = reconciliation_db.SAVED_AGAIN.snapshot().get(trigger, 0)
    capsys.readouterr()
    note = reconciliation_db._saved_again_note(
        _Connection(), trigger, 42, DAY, previous, result, contributions)
    reconciliation_db.report_saved_again([] if note is None else [note])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return reconciliation_db.SAVED_AGAIN.snapshot().get(trigger, 0) - before, lines


def test_a_result_saved_again_unchanged_is_counted_by_its_trigger(capsys) -> None:
    counted, logged = _saved(
        capsys, _previous("Partial", "partial", 4939),
        _result("Partial", "partial", 4939), trigger="recheck")
    assert (counted, logged) == (1, [])


def test_a_complete_day_left_unproven_logs_once_with_the_reading(capsys) -> None:
    counted, logged = _saved(
        capsys, _previous("Complete", "exact", 4939),
        _result("Partial", "partial", 4939, end_reading={
            "read_at": "2026-11-10T05:44:56+00:00", "trophies": 4939,
            "outcome": "unverified"}))
    assert counted == 0
    assert logged == [{
        "event": "day_proof_lost", "player_id": 42,
        "ranked_day_start": DAY.start.isoformat(), "trigger": "evidence",
        "old_state": "Complete", "new_state": "Partial",
        "old_confidence": "exact", "new_confidence": "partial",
        "end_changed": False, "battles_changed": False,
        "failure_reasons": ["end_reading_unverified"],
        "reading_at": "2026-11-10T05:44:56+00:00", "reading_outcome": "unverified",
        "leftover": None,
    }]


def test_losing_exact_logs_a_changed_end_and_battle_list(capsys) -> None:
    """A Complete day still Complete but no longer exact: a battle came in
    late and a reading during the day left 40 over."""
    battles = [*OLD_BATTLES, {"battle_identity": "d", "amount_used": -40, "included": True}]
    _, logged = _saved(
        capsys, _previous("Complete", "exact", 4939),
        _result("Complete", "inferred", 4899, end_reading={"during_day": {
            "read_at": "2026-11-10T03:00:00+00:00", "trophies": 4979, "residual": 40}}),
        battles, trigger="late_battle_sweep")
    assert len(logged) == 1
    assert {key: logged[0][key] for key in (
        "trigger", "new_confidence", "end_changed", "battles_changed", "reading_at",
        "leftover")} == {
        "trigger": "late_battle_sweep", "new_confidence": "inferred", "end_changed": True,
        "battles_changed": True, "reading_at": "2026-11-10T03:00:00+00:00", "leftover": 40}


@pytest.mark.parametrize(("previous", "result"), [
    # Not Complete before: nothing to lose.
    (_previous("Partial", "partial", 4939), _result("Inconsistent", "uncertain", 4899)),
    # Gains its proof.
    (_previous("Partial", "partial", 4939), _result("Complete", "exact", 4939)),
    # Complete and inferred before, still Complete.
    (_previous("Complete", "inferred", 4939), _result("Complete", "inferred", 4899)),
])
def test_other_changes_are_neither_counted_nor_logged(capsys, previous, result) -> None:
    assert _saved(capsys, previous, result) == (0, [])


def test_a_first_result_is_neither_counted_nor_logged(capsys) -> None:
    assert _saved(capsys, None, _result("Complete", "exact", 4939)) == (0, [])
