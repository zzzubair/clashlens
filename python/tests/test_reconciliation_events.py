"""Reconciliation events behavior."""

from __future__ import annotations

from datetime import UTC, datetime

from clashlens.api_player_presentation import screen_events
from clashlens.reconciliation import (
    BATTLE_EVENT_SERIALIZATION_VERSION,
    BattleContribution,
    serialize_ranked_day_battles,
)


def test_ranked_day_battle_events_are_canonical_signed_and_ordered() -> None:
    events = serialize_ranked_day_battles(
        (
            BattleContribution(
                "attack-1",
                "offense",
                40,
                battle_timestamp=datetime(2026, 8, 4, 12, tzinfo=UTC),
                stars=3,
                destruction_percentage=100,
                opponent_tag=" #8pp ",
                opponent_name="Attacked player",
            ),
            # A two-sided/repeated observation is one canonical event. The
            # second row would also sort after the first if it were retained.
            BattleContribution(
                "attack-1",
                "offense",
                40,
                battle_timestamp=datetime(2026, 8, 4, 11, tzinfo=UTC),
                stars=2,
                destruction_percentage=67,
                opponent_tag="#8PP",
                opponent_name="Stale name",
            ),
            BattleContribution(
                "defense-1",
                "defense",
                30,
                battle_timestamp=datetime(2026, 8, 4, 13, tzinfo=UTC),
                stars=1,
                destruction_percentage=50,
                opponent_tag="#9PP",
                opponent_name=None,
            ),
        )
    )

    assert BATTLE_EVENT_SERIALIZATION_VERSION == "legend-ranked-day-battle-events-v1"
    assert [event["battle_id"] for event in events] == ["defense-1", "attack-1"]
    assert events == [
        {
            "battle_id": "defense-1",
            "battle_timestamp": "2026-08-04T13:00:00Z",
            "opponent": {"tag": "#9PP", "name": None},
            "destruction_percentage": 50,
            "stars": 1,
            "trophy_change": -30,
        },
        {
            "battle_id": "attack-1",
            "battle_timestamp": "2026-08-04T12:00:00Z",
            "opponent": {"tag": "#8PP", "name": "Attacked player"},
            "destruction_percentage": 100,
            "stars": 3,
            "trophy_change": 40,
        },
    ]


def test_ranked_day_battle_events_exclude_invalid_and_unselected_rows() -> None:
    valid = BattleContribution(
        "accepted",
        "offense",
        0,
        battle_timestamp=datetime(2026, 8, 4, 12, tzinfo=UTC),
        stars=0,
        destruction_percentage=0,
        opponent_tag="#8PP",
    )
    events = serialize_ranked_day_battles(
        (
            valid,
            BattleContribution(
                "bad-stars",
                "offense",
                40,
                battle_timestamp=datetime(2026, 8, 4, 11, tzinfo=UTC),
                stars=4,
                destruction_percentage=100,
                opponent_tag="#8PP",
            ),
            BattleContribution(
                "disagreement",
                "defense",
                20,
                battle_timestamp=datetime(2026, 8, 4, 10, tzinfo=UTC),
                stars=2,
                destruction_percentage=50,
                opponent_tag="#9PP",
                disagreement=True,
            ),
            {
                "battle_identity": "excluded",
                "lens": "offense",
                "included": False,
                "valid": True,
                "amount_used": 40,
                "battle_timestamp": "2026-08-04T09:00:00Z",
                "stars": 3,
                "destruction_percentage": 100,
                "opponent_tag": "#8PP",
            },
        )
    )

    # A battle the two sides report differently is counted, so it stays shown
    # and flagged for the website's "Result awaiting confirmation" note.
    assert events == [
        {
            "battle_id": "accepted",
            "battle_timestamp": "2026-08-04T12:00:00Z",
            "opponent": {"tag": "#8PP", "name": None},
            "destruction_percentage": 0,
            "stars": 0,
            "trophy_change": 0,
        },
        {
            "battle_id": "disagreement",
            "battle_timestamp": "2026-08-04T10:00:00Z",
            "opponent": {"tag": "#9PP", "name": None},
            "destruction_percentage": 50,
            "stars": 2,
            "trophy_change": -20,
            "disagreement": True,
        },
    ]
    # Publication rows carry each contribution's lens beside its event.
    lenses = {"accepted": "offense", "disagreement": "defense"}
    offense, defense = screen_events(
        [{"lens": lenses[event["battle_id"]], **event} for event in events]
    )
    assert [event["perspective_disagreement"] for event in offense + defense] == [
        False,
        True,
    ]


def test_ranked_day_battle_events_can_be_empty() -> None:
    assert (
        serialize_ranked_day_battles(
            (
                {
                    "battle_identity": "missing-opponent",
                    "lens": "offense",
                    "included": True,
                    "valid": True,
                    "disagreement": False,
                    "amount_used": 40,
                    "battle_timestamp": "2026-08-04T12:00:00Z",
                    "stars": 3,
                    "destruction_percentage": 100,
                },
            )
        )
        == []
    )
