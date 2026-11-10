"""The confirmed Season anchor: which Season the game says is current,
taken from Legend I profiles (``legend_season_anchors``), and each saved
profile's verdict on it (``season_anchor_evidence``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .db_rows import text_value
from .domain import (
    SEASON_ANCHOR_RULE_VERSION,
    DomainRuleError,
    SeasonAnchor,
    validate_season_anchor,
)
from .profile import PROFILE_PARSER_VERSION, ParsedProfile


class AnchorOutcome(StrEnum):
    """One saved profile's verdict, saved as ``season_anchor_evidence.outcome``.
    NOT_APPLICABLE: a profile not showing Legend I says nothing. ACCEPTED:
    its Seasons are valid and agree with, or advance, the confirmed anchor.
    CONFLICT: its Seasons are invalid or contradict the anchor; the profile
    is then saved as in conflict too."""

    NOT_APPLICABLE = "not_applicable"
    ACCEPTED = "accepted"
    CONFLICT = "conflict"


class AnchorStep(StrEnum):
    """What a valid profile does to the confirmed anchor, of which there is
    at most one per rule version (state ``confirmed``; replaced ones are
    ``superseded``).

    ESTABLISH: there is none yet, so the profile's Seasons become it. KEEP:
    it names the same Seasons, or the profile is no newer than the one that
    confirmed it. ADVANCE: a newer profile names a later Season, so the old
    anchor is superseded and the new one confirmed. REJECT: a newer profile
    names an earlier or different Season of the same start; the anchor
    stays and the profile is a conflict.

    Owner: ``anchor_step``, read again under the anchor row's lock before
    any write (``record``). The writes are in the profile's processing
    transaction, so a failed or retried job leaves the anchor as it was and
    a retry decides again from the same rows. Two profiles establishing at
    once: the second insert finds the first and decides again against it.
    """

    ESTABLISH = "establish"
    KEEP = "keep"
    ADVANCE = "advance"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ConfirmedAnchor:
    id: int
    current_id: str
    previous_id: str
    current_start: datetime
    # When the profile that confirmed it was read.
    observed_at: datetime


def anchor_step(
    confirmed: ConfirmedAnchor | None, anchor: SeasonAnchor, observed_at: datetime,
) -> AnchorStep:
    """What a valid profile read at ``observed_at`` naming ``anchor`` does
    to the ``confirmed`` anchor."""
    if confirmed is None:
        return AnchorStep.ESTABLISH
    if (
        confirmed.current_id == anchor.current_id and confirmed.previous_id == anchor.previous_id
    ) or observed_at <= confirmed.observed_at:
        return AnchorStep.KEEP
    if anchor.current_start > confirmed.current_start:
        return AnchorStep.ADVANCE
    return AnchorStep.REJECT


def _confirmed(connection: Any, *, for_update: bool) -> ConfirmedAnchor | None:
    lock_clause = "FOR UPDATE OF a" if for_update else ""
    row = connection.execute(
        f"""
        SELECT a.id, a.current_league_season_id,
               a.previous_league_season_id, a.current_start,
               v.observed_at
        FROM legend_season_anchors AS a
        JOIN player_profile_versions AS v
          ON v.id = a.source_profile_version_id
        WHERE a.state = 'confirmed'
          AND a.anchor_rule_version = %s
        {lock_clause}
        """,
        (SEASON_ANCHOR_RULE_VERSION,),
    ).fetchone()
    if row is None:
        return None
    return ConfirmedAnchor(
        int(row[0]), text_value(row[1]), text_value(row[2]), row[3], row[4])


def _profile_anchor(profile: ParsedProfile) -> SeasonAnchor:
    if profile.parser_version == PROFILE_PARSER_VERSION:
        if profile.season_anchor_current_id is None:
            raise DomainRuleError(
                "invalid_season_anchor", "profile current season is missing"
            )
        return validate_season_anchor(
            profile.season_anchor_current_id,
            profile.season_anchor_previous_id or "",
        )
    if (
        profile.current_league_season_id is None
        or profile.previous_league_season_id is None
    ):
        raise DomainRuleError(
            "invalid_season_anchor", "profile season values are missing"
        )
    return validate_season_anchor(
        profile.current_league_season_id,
        profile.previous_league_season_id,
    )


def record(
    connection: Any,
    profile_version_id: int,
    profile: ParsedProfile,
) -> AnchorOutcome:
    """Save a new profile's verdict on the anchor, moving the anchor when
    it establishes or advances it."""
    outcome = AnchorOutcome.NOT_APPLICABLE
    failure_reason: str | None = None
    anchor = None
    if profile.eligibility_state == "eligible":
        try:
            anchor = _profile_anchor(profile)
            outcome = AnchorOutcome.ACCEPTED
        except DomainRuleError as error:
            outcome = AnchorOutcome.CONFLICT
            failure_reason = error.category

    if anchor is not None:
        confirmed = _confirmed(connection, for_update=False)
        if anchor_step(confirmed, anchor, profile.observed_at) is not AnchorStep.KEEP:
            # A possible transition must re-read under the row lock. The
            # unlocked read is only an optimization for the common no-op
            # path and is never used to advance confirmed state.
            confirmed = _confirmed(connection, for_update=True)
        step = anchor_step(confirmed, anchor, profile.observed_at)
        if step is AnchorStep.ESTABLISH:
            inserted = connection.execute(
                """
                INSERT INTO legend_season_anchors (
                    current_league_season_id, previous_league_season_id,
                    current_start, previous_start, anchor_rule_version,
                    source_profile_version_id, state
                ) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed')
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    anchor.current_id,
                    anchor.previous_id,
                    anchor.current_start,
                    anchor.previous_start,
                    SEASON_ANCHOR_RULE_VERSION,
                    profile_version_id,
                ),
            ).fetchone()
            if inserted is None:
                # Another profile established it first: decide against that.
                confirmed = _confirmed(connection, for_update=True)
                assert confirmed is not None
                step = anchor_step(confirmed, anchor, profile.observed_at)
        if step is AnchorStep.ADVANCE:
            assert confirmed is not None
            connection.execute(
                "UPDATE legend_season_anchors SET state = 'superseded' WHERE id = %s",
                (confirmed.id,),
            )
            connection.execute(
                """
                INSERT INTO legend_season_anchors (
                    current_league_season_id, previous_league_season_id,
                    current_start, previous_start, anchor_rule_version,
                    source_profile_version_id, state
                ) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed')
                ON CONFLICT (current_league_season_id, anchor_rule_version)
                DO UPDATE SET
                    source_profile_version_id = EXCLUDED.source_profile_version_id,
                    state = 'confirmed',
                    confirmed_at = clock_timestamp()
                """,
                (
                    anchor.current_id,
                    anchor.previous_id,
                    anchor.current_start,
                    anchor.previous_start,
                    SEASON_ANCHOR_RULE_VERSION,
                    profile_version_id,
                ),
            )
        elif step is AnchorStep.REJECT:
            outcome = AnchorOutcome.CONFLICT
            failure_reason = "season_anchor_disagreement"

    connection.execute(
        """
        INSERT INTO season_anchor_evidence (
            profile_version_id, current_league_season_id,
            previous_league_season_id, current_start, previous_start,
            anchor_rule_version, outcome, failure_reason
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (profile_version_id) DO UPDATE SET
            outcome = EXCLUDED.outcome,
            failure_reason = EXCLUDED.failure_reason
        """,
        (
            profile_version_id,
            profile.current_league_season_id,
            profile.previous_league_season_id,
            anchor.current_start if anchor is not None else None,
            anchor.previous_start if anchor is not None else None,
            SEASON_ANCHOR_RULE_VERSION,
            outcome.value,
            failure_reason,
        ),
    )
    if outcome is AnchorOutcome.CONFLICT:
        connection.execute(
            """
            UPDATE player_profile_versions
            SET source_contract_state = 'conflict', season_anchor_state = 'conflict'
            WHERE id = %s
            """,
            (profile_version_id,),
        )
    return outcome
