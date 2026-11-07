"""The crew role and invite link state machines on their own: every event
from every role, and every link state from its stored values. The crew
writes that apply them are tested in test_crews_postgres.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clashlens.crew_states import (
    INVITE_REUSE_MIN_LEFT,
    MAX_CREWS_PER_ACCOUNT,
    CrewAction,
    CrewEvent,
    CrewRole,
    Invite,
    InviteState,
    Refused,
    invite_state,
    move,
    require,
    reuse_invite,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
OWNER, ADMIN, MEMBER = CrewRole.OWNER, CrewRole.ADMIN, CrewRole.MEMBER
FIXED = (409, "owner_role_fixed")
HAND_OVER_FIRST = (409, "owner_must_hand_over")

# What each event does from each role: the role after it, or the refusal.
MOVES = {
    CrewEvent.MAKE_ADMIN: {OWNER: FIXED, ADMIN: ADMIN, MEMBER: ADMIN},
    CrewEvent.MAKE_MEMBER: {OWNER: FIXED, ADMIN: MEMBER, MEMBER: MEMBER},
    CrewEvent.TAKE_OVER: {OWNER: FIXED, ADMIN: OWNER, MEMBER: OWNER},
    CrewEvent.LEAVE: {OWNER: HAND_OVER_FIRST, ADMIN: None, MEMBER: None},
    CrewEvent.LAST_PLACE: {OWNER: HAND_OVER_FIRST, ADMIN: None, MEMBER: None},
}


@pytest.mark.parametrize(
    ("event", "role", "after"),
    [(event, role, after) for event, table in MOVES.items() for role, after in table.items()],
)
def test_each_event_from_each_role(event, role, after) -> None:
    if isinstance(after, tuple):
        with pytest.raises(Refused) as refused:
            move(role, event)
        assert (refused.value.result.status_code, refused.value.result.payload) == (
            after[0], {"error": after[1]},
        )
    else:
        assert move(role, event) is after


def test_joining_and_handing_over() -> None:
    assert move(None, CrewEvent.CREATE) is OWNER
    assert move(None, CrewEvent.JOIN) is MEMBER
    assert move(OWNER, CrewEvent.HAND_OVER) is ADMIN
    # Moves the crew writes never ask for are mistakes, not refusals.
    for role, event in (
        (MEMBER, CrewEvent.JOIN),
        (None, CrewEvent.LEAVE),
        (ADMIN, CrewEvent.HAND_OVER),
    ):
        with pytest.raises(ValueError):
            move(role, event)


def test_who_may_do_what() -> None:
    allowed = {
        action: {role for role in CrewRole if _may(role, action)} for action in CrewAction
    }
    assert allowed == {
        CrewAction.EDIT: {OWNER, ADMIN},
        CrewAction.ALL_LINKS: {OWNER, ADMIN},
        CrewAction.SET_ROLES: {OWNER},
        CrewAction.HAND_OVER: {OWNER},
        CrewAction.DELETE: {OWNER},
    }
    kicks = {(actor, holder) for actor in CrewRole for holder in CrewRole if actor.outranks(holder)}
    assert kicks == {(OWNER, ADMIN), (OWNER, MEMBER), (ADMIN, MEMBER)}
    assert sorted(CrewRole, key=lambda role: role.rank) == [OWNER, ADMIN, MEMBER]


def _may(role: CrewRole, action: CrewAction) -> bool:
    try:
        require(role, action)
    except Refused as refused:
        assert refused.result.payload == {"error": "crew_forbidden"}
        return False
    return True


def _invite(*, expires_in=timedelta(hours=30), revoked: bool = False, size=3) -> Invite:
    return Invite(
        crew_row=1,
        crew_id="crew",
        name="Night Owls",
        size=size,
        expires_at=NOW + expires_in,
        revoked_at=NOW - timedelta(hours=1) if revoked else None,
    )


def test_each_invite_state_from_its_stored_values() -> None:
    assert invite_state(None, NOW) is InviteState.UNKNOWN
    assert invite_state(_invite(revoked=True), NOW) is InviteState.ENDED
    # Ends at expires_at exactly.
    assert invite_state(_invite(expires_in=timedelta(0)), NOW) is InviteState.ENDED
    assert invite_state(_invite(expires_in=timedelta(seconds=1)), NOW) is InviteState.USABLE
    assert invite_state(_invite(), NOW, used=3) is InviteState.FULL
    at_limit = {"in_crew": False, "crew_count": MAX_CREWS_PER_ACCOUNT}
    assert invite_state(_invite(), NOW, used=2, **at_limit) is InviteState.AT_LIMIT
    # Full is said before the limit, and the limit never for someone in the crew.
    assert invite_state(_invite(), NOW, used=3, **at_limit) is InviteState.FULL
    assert invite_state(
        _invite(), NOW, used=2, in_crew=True, crew_count=MAX_CREWS_PER_ACCOUNT
    ) is InviteState.USABLE
    # The website reads these exact words.
    assert [state.value for state in InviteState] == ["invalid", "ended", "full", "limit", "ok"]


def test_the_newest_link_is_reused_while_it_has_a_day_left() -> None:
    assert reuse_invite(NOW + INVITE_REUSE_MIN_LEFT, NOW, new=False) is True
    assert reuse_invite(NOW + INVITE_REUSE_MIN_LEFT - timedelta(seconds=1), NOW, new=False) is False
    assert reuse_invite(NOW + timedelta(hours=40), NOW, new=True) is False
    assert reuse_invite(None, NOW, new=False) is False
