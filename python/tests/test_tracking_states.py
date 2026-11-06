"""The named states of player tracking, the promotion list and the Season anchor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clashlens.domain import validate_season_anchor
from clashlens.league_days import Tracking, tracking
from clashlens.league_week import League, Strength
from clashlens.promotion_recheck import Outcome, Step, step
from clashlens.season_anchors import AnchorStep, ConfirmedAnchor, anchor_step

LEGEND_I, BELOW, UNKNOWN = League.LEGEND_I.value, League.BELOW.value, League.UNKNOWN.value


@pytest.mark.parametrize(
    ("this_week", "strength", "last_week", "newest", "expected", "saved"),
    [
        (LEGEND_I, Strength.ACCEPTED, None, "ineligible", Tracking.LEGEND_I, "eligible"),
        # After a Legend I week, an unknown week or a weak lower answer keeps polling.
        (UNKNOWN, Strength.NONE, LEGEND_I, "ineligible", Tracking.UNSETTLED_AFTER_LEGEND_I, "eligible"),
        (BELOW, Strength.NONE, LEGEND_I, "ineligible", Tracking.UNSETTLED_AFTER_LEGEND_I, "eligible"),
        # An accepted lower answer stops it.
        (BELOW, Strength.ACCEPTED, LEGEND_I, "ineligible", Tracking.BELOW, "ineligible"),
        # A newest Legend I profile tracks a player the record does not yet.
        (BELOW, Strength.ACCEPTED, BELOW, "eligible", Tracking.NEWEST_ELIGIBLE, "eligible"),
        (UNKNOWN, Strength.NONE, None, None, Tracking.UNKNOWN, "unknown"),
    ],
)
def test_tracking_follows_the_weekly_record_then_the_newest_profile(
    this_week: str, strength: int, last_week: str | None, newest: str | None,
    expected: Tracking, saved: str,
) -> None:
    state = tracking(this_week, strength, last_week, newest)
    assert state is expected
    assert state.eligibility_state == saved
    assert state.tracked is (saved == "eligible")


def test_a_promoted_player_stays_due_until_handed_on() -> None:
    assert step(Outcome.PROMOTED, handed=False) is Step.LEAVE_DUE
    assert step(Outcome.PROMOTED, handed=True) is Step.REFRESH
    assert step(Outcome.LISTED, handed=True) is Step.REFRESH
    assert step(Outcome.REMOVED, handed=True) is Step.REMOVE


def test_the_season_anchor_is_established_kept_advanced_or_defended() -> None:
    october = validate_season_anchor("1791176400", "1788757200")
    november = validate_season_anchor("1793595600", "1791176400")
    read_at = datetime(2026, 10, 10, tzinfo=UTC)
    assert anchor_step(None, october, read_at) is AnchorStep.ESTABLISH
    confirmed = ConfirmedAnchor(
        1, october.current_id, october.previous_id, october.current_start, read_at
    )
    later = read_at + timedelta(days=1)
    assert anchor_step(confirmed, october, later) is AnchorStep.KEEP
    # A profile no newer than the confirming one never moves it.
    assert anchor_step(confirmed, november, read_at) is AnchorStep.KEEP
    assert anchor_step(confirmed, november, later) is AnchorStep.ADVANCE
    advanced = ConfirmedAnchor(
        2, november.current_id, november.previous_id, november.current_start, later
    )
    assert anchor_step(advanced, october, later + timedelta(days=1)) is AnchorStep.REJECT
