"""The named states of a Reset's publication, and the database's own lists."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database

from bot.alerts import Logged
from clashlens.army_decoder import DecodedArmy, DecodeFailure
from clashlens.board_deadline import (
    BOARD_DEADLINE,
    HARD_DEADLINE,
    MAX_UNFINISHED,
    DeadlineStage,
    deadline_stage,
)
from clashlens.crew_states import CrewRole
from clashlens.day_result import Confidence, DayState, ShieldState
from clashlens.domain_repair import CampaignState
from clashlens.loss_rule import LossEvidence
from clashlens.past_reset_pacing import CorrectionPace, correction_pace
from clashlens.publication_states import (
    ArtifactState,
    CorrectionState,
    DecodeStatus,
    DecodeVerdict,
    MemberStatus,
    decode_verdict,
)
from clashlens.reset_acceptance import Milestone, reached, settled
from clashlens.season_anchors import AnchorOutcome

RESET = datetime(2026, 10, 12, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("table", "column", "states"),
    [
        ("boundary_publication_generations", "snapshot_state", ArtifactState),
        ("boundary_publication_generations", "army_state", ArtifactState),
        ("boundary_publication_generation_members", "snapshot_status", MemberStatus),
        ("boundary_publication_generation_members", "army_status", MemberStatus),
        ("boundary_publication_corrections", "state", CorrectionState),
        ("battle_army_decodes", "status", DecodeStatus),
        ("ranked_day_versions", "state", DayState),
        # The day's confidence is listed only where the website reads it.
        ("api_player_daily_logs", "confidence", Confidence),
        # Historical rows retain these values; current writers no longer emit them.
        ("ranked_day_versions", "shield_state",
         [*ShieldState, "not_shielded", "uncertain_sequence"]),
        ("ranked_day_versions", "automatic_defense_evidence_state", LossEvidence),
        ("season_anchor_evidence", "outcome", AnchorOutcome),
        ("domain_repair_campaigns", "state", CampaignState),
        ("crew_accounts", "role", CrewRole),
        ("discord_day_summary_deliveries", "state", Logged),
        # Only a summary can be skipped.
        ("discord_attack_alert_deliveries", "state", set(Logged) - {Logged.SKIPPED}),
    ],
)
def test_each_named_state_list_matches_the_database(
    database_url: str, table: str, column: str, states: Iterable[str]
) -> None:
    with domain_database(database_url) as connection_info, psycopg.connect(
        connection_info
    ) as connection:
        lists = [
            listed
            for (definition,) in connection.execute(
                """
                SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid = %s::regclass AND contype = 'c'
                """,
                (table,),
            )
            # Each list of values the column may hold, alone or inside a
            # larger check; a narrower rule elsewhere repeats some of them.
            for listed in re.findall(rf"\b{column} = ANY \(ARRAY\[([^\]]*)\]", definition)
        ]
    assert lists, table
    allowed = {value for listed in lists for value in re.findall(r"'([A-Za-z_]+)'::text", listed)}
    assert allowed == set(states)


@pytest.mark.parametrize(
    ("after", "published", "waiting", "stage"),
    [
        (timedelta(minutes=10), False, 3, DeadlineStage.WAITING),
        (BOARD_DEADLINE, False, 0, DeadlineStage.WAITING),
        (BOARD_DEADLINE, False, MAX_UNFINISHED, DeadlineStage.CUT_FEW),
        (BOARD_DEADLINE, False, MAX_UNFINISHED + 1, DeadlineStage.HOLDING),
        (HARD_DEADLINE, False, MAX_UNFINISHED + 1, DeadlineStage.CUT_ALL),
        # Once a board published, a late member joins a rebuild instead.
        (HARD_DEADLINE, True, MAX_UNFINISHED + 1, DeadlineStage.PUBLISHED),
    ],
)
def test_board_deadlines_leave_out_few_members_then_all(
    after: timedelta, published: bool, waiting: int, stage: DeadlineStage
) -> None:
    assert deadline_stage(
        now=RESET + after, boundary_at=RESET, published=published, waiting=waiting
    ) is stage


def test_a_correction_waits_by_its_resets_age_and_the_quiet_window(
    database_url: str,
) -> None:
    newest = RESET + timedelta(days=1)
    afternoon = newest + timedelta(hours=10)
    with domain_database(database_url) as connection_info, psycopg.connect(
        connection_info
    ) as connection:
        assert correction_pace(
            connection, RESET, latest_reset=newest, now=afternoon, arrival=False
        ) is CorrectionPace.EVERY_SIX_HOURS
    assert correction_pace(
        None, RESET, latest_reset=newest, now=newest + timedelta(hours=1), arrival=False
    ) is CorrectionPace.QUIET_WINDOW
    assert correction_pace(
        None, newest, latest_reset=newest, now=afternoon, arrival=True
    ) is CorrectionPace.AT_ONCE


def test_a_fresh_decode_is_saved_only_when_it_differs() -> None:
    army = DecodedArmy((), (), (), (), (), (), (), "u1x2", "v1", "abc")
    failure = DecodeFailure(None, "malformed", "bad code", "v1")
    decoded = (7, 11, "u1x2", "abc", "decoded", None)
    assert decode_verdict(None, 11, "u1x2", army) is DecodeVerdict.MISSING
    # Another battle row's evidence, or another code, needs its own decode.
    assert decode_verdict(decoded, 12, "u1x2", army) is DecodeVerdict.MISSING
    assert decode_verdict(decoded, 11, "u9", army) is DecodeVerdict.MISSING
    assert decode_verdict(decoded, 11, "u1x2", army) is DecodeVerdict.CURRENT
    assert decode_verdict(decoded, 11, "u1x2", failure) is DecodeVerdict.OUTDATED
    # A row that kept unnamed ids apart counts for any successful decode.
    partial = (7, 11, "u1x2", "old", "partial", None)
    assert decode_verdict(partial, 11, "u1x2", army) is DecodeVerdict.CURRENT
    failed = (7, 11, "u1x2", None, "failed", "malformed")
    assert decode_verdict(failed, 11, "u1x2", failure) is DecodeVerdict.CURRENT
    assert decode_verdict(failed, 11, "u1x2", army) is DecodeVerdict.OUTDATED


def test_a_resets_milestones_are_independent_and_settle_when_shown_and_processed() -> None:
    record = {milestone.value: None for milestone in Milestone}
    record["published_at"] = RESET + timedelta(minutes=25)
    # Published before its readings were processed: not settled yet.
    assert reached(record) == {Milestone.PUBLISHED}
    assert not settled(record)
    record["readable_at"] = RESET + timedelta(minutes=26)
    record["proof_processed_at"] = RESET + timedelta(hours=3)
    assert settled(record)
