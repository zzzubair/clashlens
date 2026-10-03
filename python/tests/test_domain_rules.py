from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clashlens.domain import (
    HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION,
    SEASON_ANCHOR_RULE_VERSION,
    TROPHY_ALLOCATION_RULE_VERSION,
    DomainRuleError,
    allocate_trophies,
    anchored_ranked_day,
    ranked_day_for,
    validate_legend_season_start,
    validate_season_anchor,
)

ALLOCATION_TABLES = Path(__file__).parents[2] / "docs" / "data"


# Each table must match the version that reads it, including v1's wrong
# 55% cell, which saved v1 results still depend on.
@pytest.mark.parametrize(
    "rule_version",
    [HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION, TROPHY_ALLOCATION_RULE_VERSION],
)
def test_trophy_allocation_matches_every_table_boundary(rule_version: str) -> None:
    table = ALLOCATION_TABLES / f"{rule_version}.csv"
    with table.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))

    for row in rows:
        destruction = int(row["minimum_destruction_percentage"])
        for stars, column in enumerate(("0_stars", "1_star", "2_stars", "3_stars")):
            expected = row[column]
            if expected == "--":
                continue

            allocation = allocate_trophies(
                stars, destruction, rule_version=rule_version
            )

            assert allocation.attacker_gain == int(expected)
            assert allocation.defender_loss == (0 if stars == 0 else int(expected))
            assert allocation.rule_version == rule_version


def test_current_allocation_matches_published_formula_for_all_possible_results() -> (
    None
):
    # Supercell's June 2019 Legend League formulas, independent of the table.
    formulas = {
        0: (range(50), lambda d: d // 10),
        1: (range(1, 100), lambda d: 5 + (d - 1) // 9),
        2: (range(50, 100), lambda d: 16 + (d - 50) // 3),
        3: (range(100, 101), lambda d: 40),
    }
    cases = 0
    for stars, (destructions, gain) in formulas.items():
        for destruction in destructions:
            allocation = allocate_trophies(stars, destruction)
            assert (allocation.attacker_gain, allocation.defender_loss) == (
                gain(destruction),
                0 if stars == 0 else gain(destruction),
            ), (stars, destruction)
            cases += 1
    assert cases == 200


@pytest.mark.parametrize(
    ("destruction", "trophies"),
    [
        (50, 16),
        (52, 16),
        (53, 17),
        (54, 17),
        (55, 17),
        (56, 18),
        (58, 18),
        (59, 19),
        (98, 32),
        (99, 32),
    ],
)
def test_two_star_threshold_uses_56_not_55(destruction: int, trophies: int) -> None:
    allocation = allocate_trophies(2, destruction)

    assert (allocation.attacker_gain, allocation.defender_loss) == (
        trophies,
        trophies,
    )
    assert allocation.rule_version == "legend-trophy-allocation-v2"


def test_zero_star_defense_exception_is_preserved() -> None:
    for destruction, gain in zip(
        (0, 9, 10, 20, 30, 40, 48, 49), (0, 0, 1, 2, 3, 4, 4, 4), strict=True
    ):
        allocation = allocate_trophies(0, destruction)
        assert (allocation.attacker_gain, allocation.defender_loss) == (gain, 0)
    one_star = allocate_trophies(1, 1)
    assert (one_star.attacker_gain, one_star.defender_loss) == (5, 5)


def test_explicit_v1_keeps_its_historical_55_allocation() -> None:
    # Saved v1 results recorded 18 here. Reading them back as v1 must give
    # the number they were saved with; only a repair moves them to v2.
    old = allocate_trophies(
        2, 55, rule_version=HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION
    )
    assert (old.attacker_gain, old.defender_loss, old.rule_version) == (
        18,
        18,
        "legend-trophy-allocation-v1",
    )
    for destruction in (54, 56):
        assert (
            allocate_trophies(
                2, destruction, rule_version=HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION
            ).attacker_gain
            == allocate_trophies(2, destruction).attacker_gain
        )


def test_unknown_allocation_version_is_rejected() -> None:
    with pytest.raises(DomainRuleError, match="unsupported_trophy_allocation_rule"):
        allocate_trophies(2, 55, rule_version="legend-trophy-allocation-v3")


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
def test_trophy_allocation_rejects_impossible_values(
    stars: int, destruction: int
) -> None:
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


def test_league_history_anchor_accepts_old_legend_seasons_on_the_28_day_phase() -> None:
    observed_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

    assert validate_legend_season_start(
        "1781499600", observed_at=observed_at
    ) == datetime(2026, 6, 15, 5, 0, tzinfo=UTC)
    for invalid in (
        str(int(datetime(2026, 6, 22, 5, 0, tzinfo=UTC).timestamp())),
        str(int(datetime(2026, 8, 10, 5, 0, tzinfo=UTC).timestamp())),
    ):
        with pytest.raises(DomainRuleError, match="invalid_season_anchor"):
            validate_legend_season_start(invalid, observed_at=observed_at)


@pytest.mark.parametrize(
    "anchor",
    [("1788757200", "1786338000"), ("1791176400", "1788757200")],
    ids=["september-anchor", "october-anchor"],
)
def test_october_5_season_end_uses_the_phase_whichever_anchor_is_confirmed(
    anchor: tuple[str, str],
) -> None:
    sunday = anchored_ranked_day(datetime(2026, 10, 4, 12, tzinfo=UTC), *anchor)
    monday = anchored_ranked_day(datetime(2026, 10, 5, 5, tzinfo=UTC), *anchor)

    assert (sunday.official_season_id, sunday.day_number) == ("1788757200", 28)
    assert sunday.season_end == monday.start == datetime(2026, 10, 5, 5, tzinfo=UTC)
    assert (monday.official_season_id, monday.day_number) == ("1791176400", 1)


def test_season_anchor_off_the_28_day_phase_is_refused() -> None:
    # Adjacent Monday 05:00 boundaries, but one week off the Legend phase.
    with pytest.raises(DomainRuleError, match="invalid_season_anchor"):
        anchored_ranked_day(
            datetime(2026, 10, 5, 5, tzinfo=UTC), "1789362000", "1786942800"
        )
