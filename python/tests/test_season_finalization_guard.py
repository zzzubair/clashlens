"""Seven-day wait before a Season can be finalized or retired."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from clashlens.season_retirement import season_close_block

# September 2026 Season 1788757200: September 7 to October 5 at 05:00 UTC.
START = datetime(2026, 9, 7, 5, tzinfo=UTC)
END = datetime(2026, 10, 5, 5, tzinfo=UTC)
ELIGIBLE = datetime(2026, 10, 12, 5, tzinfo=UTC)


def test_september_season_id_is_its_start() -> None:
    assert datetime.fromtimestamp(1788757200, UTC) == START
    assert START + timedelta(days=28) == END


WINDOW = (START, END)
KNOWN = {"eligible_at": ELIGIBLE.isoformat()}


@pytest.mark.parametrize(
    ("now", "closable"),
    [
        (END, False),
        (ELIGIBLE - timedelta(microseconds=1), False),
        (ELIGIBLE, True),
        (ELIGIBLE + timedelta(microseconds=1), True),
    ],
)
def test_season_close_waits_until_exact_seven_day_instant(now, closable) -> None:
    reason = None if closable else "season_close_wait"
    assert season_close_block(WINDOW, WINDOW, now) == (KNOWN, reason)


def test_wait_uses_utc_instants_and_twenty_eight_day_bounds() -> None:
    plus_two = timezone(timedelta(hours=2))
    shifted = (START.astimezone(plus_two), END.astimezone(plus_two))
    assert season_close_block(shifted, WINDOW, ELIGIBLE) == (KNOWN, None)
    early = (ELIGIBLE - timedelta(seconds=1)).astimezone(plus_two)
    assert season_close_block(WINDOW, shifted, early) == (KNOWN, "season_close_wait")
    with pytest.raises(ValueError, match="timezone-aware"):
        season_close_block(WINDOW, WINDOW, ELIGIBLE.replace(tzinfo=None))


def test_unknown_or_disagreeing_windows_never_close() -> None:
    late = ELIGIBLE + timedelta(days=30)
    assert season_close_block(None, WINDOW, late) == ({}, "unknown_season_boundary")
    for stored in ((None, END), (START, None), (None, None)):
        assert season_close_block(WINDOW, stored, late) == (KNOWN, "unknown_season_boundary")
    for stored in (
        (END, START),
        (START, END - timedelta(days=1)),
        (START + timedelta(days=1), END + timedelta(days=1)),
        (START + timedelta(hours=1), END + timedelta(hours=1)),
        (START.replace(tzinfo=None), END),
    ):
        assert season_close_block(WINDOW, stored, late) == (KNOWN, "conflicting_season_boundary")
