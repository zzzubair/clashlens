from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clashlens_prototype.domain import (
    SEASON_ANCHOR_RULE_VERSION,
    TROPHY_ALLOCATION_RULE_VERSION,
    DomainRuleError,
    allocate_trophies,
    ranked_day_for,
    validate_season_anchor,
)

ALLOCATION_TABLE = (
    Path(__file__).parents[2] / "docs" / "data" / "legend-trophy-allocation-v1.csv"
)


def test_trophy_allocation_matches_every_version_1_table_boundary() -> None:
    with ALLOCATION_TABLE.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))

    for row in rows:
        destruction = int(row["minimum_destruction_percentage"])
        for stars, column in enumerate(("0_stars", "1_star", "2_stars", "3_stars")):
            expected = row[column]
            if expected == "--":
                continue

            allocation = allocate_trophies(stars, destruction)

            assert allocation.attacker_gain == int(expected)
            assert allocation.defender_loss == (0 if stars == 0 else int(expected))
            assert allocation.rule_version == TROPHY_ALLOCATION_RULE_VERSION


def test_trophy_allocation_uses_last_boundary_not_greater_than_destruction() -> None:
    assert allocate_trophies(0, 9).attacker_gain == 0
    assert allocate_trophies(0, 10).attacker_gain == 1
    assert allocate_trophies(1, 99).attacker_gain == 15
    assert allocate_trophies(2, 99).attacker_gain == 32
    assert allocate_trophies(3, 100).attacker_gain == 40


@pytest.mark.parametrize(
    ("stars", "destruction"),
    [(-1, 50), (4, 50), (0, -1), (1, 0), (2, 49), (3, 99), (3, 101)],
)
def test_trophy_allocation_rejects_impossible_values(stars: int, destruction: int) -> None:
    with pytest.raises(DomainRuleError, match="impossible_trophy_allocation"):
        allocate_trophies(stars, destruction)


def test_ranked_day_uses_half_open_0500_utc_boundaries_and_anchor() -> None:
    before = ranked_day_for(datetime(2026, 7, 13, 4, 59, 59, tzinfo=UTC))
    first = ranked_day_for(datetime(2026, 7, 13, 5, 0, tzinfo=UTC))
    last = ranked_day_for(datetime(2026, 8, 10, 4, 59, 59, tzinfo=UTC))
    next_season = ranked_day_for(datetime(2026, 8, 10, 5, 0, tzinfo=UTC))

    assert before.season_start == datetime(2026, 6, 15, 5, 0, tzinfo=UTC)
    assert before.day_number == 28
    assert first.start == datetime(2026, 7, 13, 5, 0, tzinfo=UTC)
    assert first.end == datetime(2026, 7, 14, 5, 0, tzinfo=UTC)
    assert first.day_number == 1
    assert first.official_season_id == "1783918800"
    assert first.anchor_rule_version == SEASON_ANCHOR_RULE_VERSION
    assert last.day_number == 28
    assert next_season.day_number == 1
    assert next_season.official_season_id == "1786338000"


def test_season_anchor_accepts_only_adjacent_monday_0500_values() -> None:
    anchor = validate_season_anchor("1783918800", "1781499600")

    assert anchor.current_start == datetime(2026, 7, 13, 5, 0, tzinfo=UTC)
    assert anchor.previous_start == datetime(2026, 6, 15, 5, 0, tzinfo=UTC)

    for current, previous in (
        ("not-a-number", "1781499600"),
        ("1783918801", "1781499601"),
        ("1783918800", "1781499601"),
    ):
        with pytest.raises(DomainRuleError, match="invalid_season_anchor"):
            validate_season_anchor(current, previous)
