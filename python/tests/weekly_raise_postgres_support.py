"""Shared setup for weekly raise postgres tests."""

from __future__ import annotations

from dataclasses import replace

LATE_RAISE, LOSS_TOO, RAISE_UNDONE, LATE_CREDIT, QUIET, DEFENDED = (
    "#2PP", "#8PP", "#9PP", "#PPP", "#LPP", "#YPP")


def _instant_raise(judge):
    """``judge`` as before 13 October 2026, which took the weekly raise as
    landed at the Reset: a value below 5,000 read unraised fit only when the
    automatic loss had taken it there."""
    def judged(reading, **rule):
        result = judge(reading, **rule)
        if result is None or rule.get("floor") is None or rule.get("reset"):
            return result
        ways = frozenset(
            (loss, up, shown) for loss, up, shown in result.ways
            if up or reading.trophies - result.new_day_change + loss >= rule["floor"])
        return replace(result, matched=bool(ways), ways=ways)
    return judged
