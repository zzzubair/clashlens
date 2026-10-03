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
    block = season_close_block(START, END, now)
    if closable:
        assert block is None
    else:
        assert block == {"reason": "season_close_wait", "eligible_at": ELIGIBLE.isoformat()}


def test_wait_uses_utc_instants_and_twenty_eight_day_bounds() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert season_close_block(START.astimezone(plus_two), END.astimezone(plus_two), ELIGIBLE) is None
    early = (ELIGIBLE - timedelta(seconds=1)).astimezone(plus_two)
    assert season_close_block(START, END, early)["eligible_at"] == "2026-10-12T05:00:00+00:00"
    with pytest.raises(ValueError, match="timezone-aware"):
        season_close_block(START, END, ELIGIBLE.replace(tzinfo=None))
    late = ELIGIBLE + timedelta(days=30)
    for start, end in ((None, END), (START, None), (START.replace(tzinfo=None), END)):
        assert season_close_block(start, end, late) == {"reason": "unknown_season_boundary"}
    for start, end in (
        (END, START),
        (START, END - timedelta(days=1)),
        (START, END + timedelta(days=1)),
        (START + timedelta(hours=1), END + timedelta(hours=1)),
    ):
        assert season_close_block(start, end, late) == {"reason": "invalid_season_boundary"}
