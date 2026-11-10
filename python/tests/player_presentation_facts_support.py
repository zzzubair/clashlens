"""Shared setup for player presentation facts tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def at(value):
    return datetime.fromisoformat(value)


def saved_day(start="2026-10-07T05:00:00Z", **changes):
    result = {
        "ranked_day_start": start,
        "ranked_day_end": (at(start) + timedelta(days=1)).isoformat(),
        "season_day_number": 3, "state": "Complete", "confidence": "exact",
        "coverage": "complete", "completeness": {"state": "complete"},
        "uncertainty_reasons": [], "battles_complete": True,
        "attack_count": 8, "defense_count": 8, "attack_gain": 310, "defense_loss": 284,
        "attack_three_star_count": 0, "defense_three_star_count": 0,
        "offense_events": [], "defense_events": [], "adjustments": [],
        "start_trophies": 5000, "start_trophies_source": None, "net_trophy_change": 26,
    }
    result.update(changes)
    return result


def start_day(date, losses, attacks=8):
    start = datetime(2026, 9, date, 5, tzinfo=UTC)

    def event(identity, change):
        return {"battle_id": identity, "battle_timestamp": start.replace(hour=13).isoformat(),
                "stars": 3 if abs(change) == 40 else 1, "trophy_change": change,
                "perspective_disagreement": False}

    return saved_day(
        start.isoformat(), season_day_number=date - 6, state="Live", confidence="partial",
        completeness={"state": "partial"}, start_trophies=None, net_trophy_change=None,
        attack_count=attacks, attack_gain=attacks * 40, defense_count=len(losses),
        defense_loss=sum(losses), offense_events=[event(f"a{n}", 40) for n in range(attacks)],
        defense_events=[event(f"d{n}", -loss) for n, loss in enumerate(losses)],
    )
