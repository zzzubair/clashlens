"""Each rule the owner has not decided is one switch in ``clashlens.domain``,
set to today's behaviour. Each switch changes only its own decision, so a
decided rule is that switch plus the Season repair."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from test_reconciliation import _input

from clashlens import api_leaderboard, boundary_manifest, domain, reconciliation
from clashlens.analytics import deterministic_tag_hash
from clashlens.boundary_manifest import _reset_total
from clashlens.reconciliation import PreviousRankedDay, reconcile_ranked_day
from clashlens.reset_settlement import DayEnd


def test_switches_default_to_todays_behaviour() -> None:
    assert (
        domain.LATE_RESET_READING, domain.PREVIOUS_DAY_DEFENSES,
        domain.DAILY_BOARD_VALUE, domain.TIE_ORDER, domain.UNSIGNED_UP_PLAYERS,
        domain.MAX_INFERRED_SHIELD_DAYS,
    ) == ("reject", "complete_day", "before_automatic_loss", "per_board", "hidden", 2)


def test_a_partial_day_before_gives_its_defenses_only_by_the_covered_day_rule(
    monkeypatch,
) -> None:
    """A day before missing only a Reset reading still proves its defenses
    when its battle logs are continuous; one with a gap, or disputed and so
    Inconsistent, never does."""

    def day(previous: PreviousRankedDay) -> tuple[str, int | None]:
        result = reconcile_ranked_day(_input(previous_day=previous))
        return result.state, result.automatic_defense_loss

    partial = PreviousRankedDay(False, 2, 20, 0, state="Partial")
    gap = PreviousRankedDay(False, 2, 20, 0, coverage_complete=False, state="Partial")
    disputed = PreviousRankedDay(False, 2, 20, 0, state="Inconsistent")
    assert [day(previous) for previous in (partial, gap, disputed)] == [
        ("Partial", None)
    ] * 3
    monkeypatch.setattr(reconciliation, "PREVIOUS_DAY_DEFENSES", "covered_day")
    assert [day(previous) for previous in (partial, gap, disputed)] == [
        ("Complete", 70), ("Partial", None), ("Partial", None),
    ]


def test_a_longer_quiet_run_is_a_shield_only_up_to_the_switch(monkeypatch) -> None:
    def third_quiet_day() -> tuple[str, int | None]:
        result = reconcile_ranked_day(_input(
            next_start_trophies=6000, contributions=(),
            previous_day=PreviousRankedDay(True, 0, 0, 2),
        ))
        return result.shield_state, result.shield_duration_days

    assert third_quiet_day() == ("uncertain_sequence", None)
    monkeypatch.setattr(reconciliation, "MAX_INFERRED_SHIELD_DAYS", 3)
    assert third_quiet_day() == ("inferred_shielded", 3)


def test_the_daily_board_ranks_the_eod_by_the_eod_rule(monkeypatch) -> None:
    """A Complete day ending on 4,900 after a 31-trophy automatic loss shows
    4,931 before the loss, or 4,900 by the ``eod`` rule; a total whose loss
    is unknown is never proven there."""

    def day(state: str, final: int | None, loss: int | None, loss_state: str):
        return DayEnd(
            state=state, final=final, automatic_loss=loss, automatic_state=loss_state,
            boundary_kind=None, start=4880, end_reading=None, next_start=final,
            official_end=False, settled=None,
            boundary_at=datetime(2026, 10, 7, 5, tzinfo=UTC),
        )

    complete = day("Complete", 4900, 31, "confirmed")
    unknown = day("Partial", None, None, "unknown")
    # A reading of 4,880 plus 51 since, all battles proven.
    battles = (True, 51, 51)
    assert [_reset_total(4880, d, battles) for d in (complete, unknown)] == [
        (4931, True), (4931, True),
    ]
    monkeypatch.setattr(boundary_manifest, "DAILY_BOARD_VALUE", "eod")
    assert [_reset_total(4880, d, battles) for d in (complete, unknown)] == [
        (4900, True), (4931, False),
    ]


def test_the_shared_tie_rule_orders_equal_live_trophies_as_the_daily_board(
    database_url: str,
) -> None:
    """Live breaks ties by MD5 of the tag and the Daily board by SHA-256, so
    the same players and trophies rank differently; the ``tag_hash`` rule
    uses SHA-256 for both."""
    tags = [f"#{suffix}" for suffix in ("2PP", "8LQ", "9UV", "CRY", "GJ0", "LY2", "QR8", "YV9")]
    daily = sorted(tags, key=deterministic_tag_hash)
    with psycopg.connect(database_url) as connection:

        def live(order: str) -> list[str]:
            return [
                row[0] for row in connection.execute(
                    f"SELECT normalized_tag FROM unnest(%s::text[]) AS normalized_tag"
                    f" ORDER BY {order}, normalized_tag",
                    (tags,),
                ).fetchall()
            ]

        assert live("md5(normalized_tag)") != daily
        assert live(api_leaderboard.TAG_HASH_ORDER_SQL) == daily
