"""A Season's reset to 5,000 can land after the day's first readings.

On 5 October 2026, 112 of 14,877 Day 1 profiles read from 05:15, before the
player's first battle and already naming the new Season, still showed the
previous Season's total. Such a reading must not make Day 1 Uncertain."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from test_reconciliation import DAY, _input

from clashlens.reading_rule import Reading, contradiction_during_day
from clashlens.reconciliation import (
    BattleContribution,
    PreviousRankedDay,
    reconcile_ranked_day,
)


def _during(*readings: Reading):
    return contradiction_during_day(
        readings, day_start=DAY.start, reset_at=DAY.end, start=5125, pending_loss=0,
        day_effects=(), floor=5000, reset=True,
    )[0]


def test_a_reading_may_show_the_old_total_until_the_reset_lands() -> None:
    early, later = DAY.start + timedelta(minutes=20), DAY.start + timedelta(minutes=40)

    assert _during(Reading(early, 5125)) is None
    assert _during(Reading(early, 5000)) is None
    assert _during(Reading(early, 5125), Reading(later, 5000)) is None
    # Once a reading shows the reset landed, the old total cannot come back.
    back = _during(Reading(early, 5000), Reading(later, 5125))
    assert back is not None and back.reading is not None and back.reading.read_at == later


@pytest.mark.parametrize(("noon", "state"), [(5125, "Complete"), (5126, "Inconsistent")])
def test_day_1_read_before_its_reset_landed_stays_complete(noon: int, state: str) -> None:
    # The previous Season ended at 5,125; Day 1 starts at 5,000 by the Season
    # rule and wins 20, then loses 10, after a 05:20 reading.
    day = _input(
        start_trophies=5000, next_start_trophies=5010, season_first_day=True,
        previous_day=PreviousRankedDay(True, 8, 320, 0, final_trophies=5125),
        contributions=(
            BattleContribution("attack-1", "offense", 20, battle_timestamp=DAY.start + timedelta(hours=3)),
            BattleContribution("defense-1", "defense", 10, battle_timestamp=DAY.start + timedelta(hours=4)),
        ),
    )
    result = reconcile_ranked_day(
        replace(day, readings=(Reading(DAY.start + timedelta(minutes=20), noon),))
    )

    assert result.state == state
