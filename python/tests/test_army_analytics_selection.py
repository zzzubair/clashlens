import pytest

from clashlens.army_analytics import ArmyAnalyticsSelection, build_army_result


def test_public_army_selection_accepts_settled_filters() -> None:
    for population in (
        "top-5",
        "top-1000",
        "band-1-5",
        "band-901-1000",
        "streak-top-200",
        "trophies-5000-7123",
    ):
        selection = ArmyAnalyticsSelection.parse(
            lens="defense",
            season="2026-08",
            start_day=1,
            end_day=28,
            population=population,
            category="equipment-for-hero",
            sort="three-star-rate",
        )
        assert selection.population == population


@pytest.mark.parametrize(
    "changes",
    [
        {"lens": "combined"},
        {"start_day": 4, "end_day": 3},
        {"population": "top-25"},
        {"population": "band-200-300"},
        {"population": "streak-band-1-5"},
        {"population": "trophies-4999-6000"},
        {"population": "trophies-6000-5000"},
        {"category": "exact-armies"},
        {"sort": "hit-rate"},
    ],
)
def test_public_army_selection_rejects_unsettled_or_invalid_values(
    changes: dict,
) -> None:
    values = {
        "lens": "offense",
        "season": "2026-08",
        "start_day": 1,
        "end_day": 8,
        "population": "top-100",
        "category": "troops",
        "sort": "usage-rate",
    }
    values.update(changes)
    with pytest.raises(ValueError):
        ArmyAnalyticsSelection.parse(**values)


def _fact(
    fact_id: int,
    *,
    state: str = "decoded",
    stars: int = 3,
    destruction: int = 100,
    troops: list[tuple[str, int]] | None = None,
    cc_troops: list[tuple[str, int]] | None = None,
    heroes: list[dict] | None = None,
    unresolved: list[dict] | None = None,
    failure_reason: str | None = None,
) -> dict:
    return {
        "id": fact_id,
        "army_state": state,
        "failure_reason": failure_reason,
        "stars": stars,
        "destruction_percentage": destruction,
        "home_troops": [[tid, qty, "home"] for tid, qty in (troops or [])],
        "spells": [],
        "siege": [],
        "cc_troops": [[tid, qty, "clan_castle"] for tid, qty in (cc_troops or [])],
        "heroes": heroes or [],
        "unresolved_components": unresolved or [],
        "perspective_disagreement": False,
    }


def _selection(**changes: dict) -> "ArmyAnalyticsSelection":
    values = {
        "lens": "offense",
        "season": "2026-08",
        "start_day": 1,
        "end_day": 1,
        "population": "top-100",
        "category": "troops",
        "sort": "usage-rate",
    }
    values.update(changes)
    return ArmyAnalyticsSelection.parse(**values)


def test_usage_counts_once_per_battle_regardless_of_quantity() -> None:
    facts = [
        _fact(1, troops=[("troop:58", 3)]),
        _fact(2, stars=0, troops=[("troop:58", 1)]),
    ]
    result = build_army_result(facts, _selection())
    row = next(row for row in result["rows"] if row["key"] == "troop:58")
    assert row["usage_count"] == 2
    assert row["usage_denominator"] == 2
    assert row["usage_rate"] == 1.0
    assert row["star_counts"] == [1, 0, 0, 1]
    assert row["three_star_rate"] == 0.5
    assert "hit_rate" not in row


def test_partial_individual_facts_count_with_unknown_totals() -> None:
    facts = [
        _fact(1, troops=[("troop:58", 2)]),
        _fact(
            2,
            state="partial",
            stars=1,
            troops=[("troop:58", 1)],
            unresolved=[
                {"numeric_id": 9999, "quantity": 3, "section": "u", "origin": "home"}
            ],
        ),
        _fact(3, state="missing_army_share_code"),
    ]
    result = build_army_result(facts, _selection())
    row = next(row for row in result["rows"] if row["key"] == "troop:58")
    assert row["usage_count"] == 2
    assert row["usage_denominator"] == 2
    assert result["total_attacks"] == 3
    assert result["usable_army_sample"] == 2
    assert result["army_states"]["fully_decoded"] == 1
    assert result["army_states"]["partial"] == 1
    assert result["army_states"]["missing_code"] == 1
    assert result["army_states_sum_confirmed"] is True
    assert result["unknown_affected_attacks"] == 1
    assert result["unknown_component_occurrences"] == 1


def test_uncertain_relationship_excluded_from_denominator() -> None:
    decoded_hero = {"hero": "hero:0", "pet": "pet:9", "equipment": []}
    facts = [
        _fact(1, heroes=[decoded_hero]),
        _fact(
            2,
            state="partial",
            stars=0,
            heroes=[{"hero": "hero:0", "pet": None, "equipment": []}],
            unresolved=[
                {
                    "numeric_id": 77,
                    "quantity": 1,
                    "section": "h",
                    "origin": "hero:0:pet",
                }
            ],
        ),
    ]
    result = build_army_result(facts, _selection(category="hero-pet"))
    row = next(row for row in result["rows"] if "hero:0" in row["key"])
    # The partial attack cannot prove which pet the hero brought, so it must
    # be excluded from the hero+pet denominator and published as excluded.
    assert row["usage_count"] == 1
    assert row["usage_denominator"] == 1
    assert row["unknown_excluded_attacks"] == 1


def test_equipment_for_hero_denominator_uses_confirmed_hero() -> None:
    facts = [
        _fact(
            1,
            heroes=[
                {"hero": "hero:0", "pet": None, "equipment": ["equipment:14", "equipment:32"]}
            ],
        ),
        _fact(2, heroes=[{"hero": "hero:1", "pet": None, "equipment": []}]),
    ]
    result = build_army_result(facts, _selection(category="equipment-for-hero"))
    row = next(row for row in result["rows"] if "equipment:14" in row["key"])
    assert row["usage_count"] == 1
    assert row["usage_denominator"] == 1


def test_cc_composition_uncertain_partial_excluded() -> None:
    facts = [
        _fact(1, cc_troops=[("troop:0", 2)]),
        _fact(
            2,
            state="partial",
            stars=0,
            cc_troops=[("troop:0", 5)],
            unresolved=[
                {"numeric_id": 88, "quantity": 1, "section": "i", "origin": "clan_castle"}
            ],
        ),
    ]
    result = build_army_result(facts, _selection(category="cc-composition"))
    row = next(row for row in result["rows"] if row["key"].startswith("cc:"))
    assert row["usage_count"] == 1
    assert row["usage_denominator"] == 1


def test_sorting_is_deterministic_across_ties_and_selectable() -> None:
    facts = [
        _fact(1, troops=[("troop:9", 1), ("troop:58", 1)]),
        _fact(2, stars=0, troops=[("troop:9", 1), ("troop:58", 1)]),
    ]
    by_rate = build_army_result(facts, _selection(sort="three-star-rate"))
    assert [row["key"] for row in by_rate["rows"]] == ["troop:58", "troop:9"]
    by_count = build_army_result(facts, _selection(sort="average-destruction"))
    keys = [row["key"] for row in by_count["rows"]]
    assert keys == sorted(keys)
