"""The two state machines behind crews: one Clash Lens account's place in one
crew (`CrewRole` and `move`), and one invite link's life (`InviteState` and
`invite_state`). Nothing here touches the database; api_crews.py reads the
rows, asks these for the state or the move, and writes the answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, StrEnum
from typing import Any

from .api_db import OperationResult

MAX_CREWS_PER_ACCOUNT = 5
INVITE_LIFETIME = timedelta(hours=48)
# Opening Invite shows the caller's newest link again while it has this long
# left, so a link shared yesterday keeps working.
INVITE_REUSE_MIN_LEFT = timedelta(hours=24)


class Refused(Exception):
    """A crew write's refusal: its answer, stored like a success."""

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        super().__init__(payload["error"])
        self.result = OperationResult(status_code, payload)


class CrewRole(StrEnum):
    """One Clash Lens account's role in one crew; None means outside it.

    States: outside (no crew_accounts row), MEMBER, ADMIN, OWNER. A crew has
    exactly one OWNER (index crew_accounts_one_owner). Everyone in a crew
    holds at least one place, except an owner whose game accounts all moved
    away by support transfer; that owner stays until handing over.

    Events, and what `move` makes of them:
      CREATE         outside -> OWNER (create_crew, with its first places)
      JOIN           outside -> MEMBER (accept_invite; someone already in
                     only adds places, with no event)
      MAKE_ADMIN     MEMBER or ADMIN -> ADMIN    } the owner's call; an
      MAKE_MEMBER    MEMBER or ADMIN -> MEMBER   } OWNER target is refused
      TAKE_OVER      MEMBER or ADMIN -> OWNER    } (owner_role_fixed)
      HAND_OVER      OWNER -> ADMIN, the giver's side of TAKE_OVER; with no
                     places left the giver is then outside
      LEAVE          MEMBER or ADMIN -> outside, places and all; OWNER is
                     refused (owner_must_hand_over)
      LAST_PLACE     MEMBER or ADMIN -> outside: their last place went,
                     however it went (a kick, removing it, the game account
                     moving); the database trigger
                     crew_players_member_without_places applies it. An OWNER
                     removing their own last place is refused
                     (owner_must_hand_over).
    Deleting the crew (OWNER only) takes everyone outside at once.

    Who may cause each event is `PERMITTED` and `outranks`. Owner: api_crews,
    each write one transaction holding the crew row lock before it reads a
    role, so a role changed while it waited is the one it sees. A refusal or
    crash undoes the whole write; a repeated request gets its first answer
    back. Nothing runs later, so a restart has nothing to resume.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"

    @property
    def rank(self) -> int:
        """0 for the owner, then admins, then members: list order too."""
        return list(CrewRole).index(self)

    def outranks(self, holder: CrewRole) -> bool:
        """Whether this role may kick a place `holder` holds: the owner
        kicks admins and members, an admin members only."""
        return self.rank < holder.rank


class CrewEvent(Enum):
    CREATE = "create"
    JOIN = "join"
    MAKE_ADMIN = "make_admin"
    MAKE_MEMBER = "make_member"
    TAKE_OVER = "take_over"
    HAND_OVER = "hand_over"
    LEAVE = "leave"
    LAST_PLACE = "last_place"


_TO = {
    CrewEvent.MAKE_ADMIN: CrewRole.ADMIN,
    CrewEvent.MAKE_MEMBER: CrewRole.MEMBER,
    CrewEvent.TAKE_OVER: CrewRole.OWNER,
}


def move(role: CrewRole | None, event: CrewEvent) -> CrewRole | None:
    """The role after `event`, None for outside the crew; the only place a
    role changes. Raises Refused for a move the crew's rules forbid."""
    if event in (CrewEvent.CREATE, CrewEvent.JOIN):
        if role is not None:
            raise ValueError(f"{event} for an account already in the crew")
        return CrewRole.OWNER if event is CrewEvent.CREATE else CrewRole.MEMBER
    if role is None:
        raise ValueError(f"{event} for an account outside the crew")
    if event in _TO:
        if role is CrewRole.OWNER:
            raise Refused(409, {"error": "owner_role_fixed"})
        return _TO[event]
    if event is CrewEvent.HAND_OVER:
        if role is not CrewRole.OWNER:
            raise ValueError("only the owner hands over")
        return CrewRole.ADMIN
    # LEAVE and LAST_PLACE: the crew must keep its owner.
    if role is CrewRole.OWNER:
        raise Refused(409, {"error": "owner_must_hand_over"})
    return None


class CrewAction(Enum):
    EDIT = "edit"  # rename or resize
    ALL_LINKS = "all_links"  # see and turn off every member's invite links
    SET_ROLES = "set_roles"
    HAND_OVER = "hand_over"
    DELETE = "delete"


PERMITTED: dict[CrewAction, frozenset[CrewRole]] = {
    CrewAction.EDIT: frozenset({CrewRole.OWNER, CrewRole.ADMIN}),
    CrewAction.ALL_LINKS: frozenset({CrewRole.OWNER, CrewRole.ADMIN}),
    CrewAction.SET_ROLES: frozenset({CrewRole.OWNER}),
    CrewAction.HAND_OVER: frozenset({CrewRole.OWNER}),
    CrewAction.DELETE: frozenset({CrewRole.OWNER}),
}


def require(role: CrewRole, action: CrewAction) -> None:
    if role not in PERMITTED[action]:
        raise Refused(403, {"error": "crew_forbidden"})


class InviteState(StrEnum):
    """An invite link as the person opening it sees it; the value is
    get_invite's `state`.

    States, from the stored row alone (expires_at, revoked_at) plus the
    crew's places and the opener's crews:
      UNKNOWN    no such code: never made, its crew deleted (the row goes
                 with it), or deleted once run out
      ENDED      turned off, or past expires_at; still names its crew
      FULL       live, but every place is taken
      AT_LIMIT   live, but the opener is in MAX_CREWS_PER_ACCOUNT crews and
                 not this one
      USABLE     live and open to join
    Events: make_invite makes a link live for INVITE_LIFETIME, or hands back
    the caller's newest live one (`reuse_invite`); revoke_invite or the clock
    ends it; make_invite deletes the crew's run-out links (turned-off ones
    are kept until they would have run out); deleting the crew deletes all.
    Places and the opener's crews move a live link between FULL, AT_LIMIT
    and USABLE.

    Owner: api_crews. Accepting derives the state again under the crew lock,
    so a link turned off or a crew deleted while it waited is refused
    (invite_invalid); FULL and AT_LIMIT are refused by the join's own checks.
    A failed write changes nothing, and no step runs later.
    """

    UNKNOWN = "invalid"
    ENDED = "ended"
    FULL = "full"
    AT_LIMIT = "limit"
    USABLE = "ok"


# The live rule of `Invite.live`, for queries that list links; `now` is its
# one parameter and `invite` the crew_invites alias.
LIVE_INVITE_SQL = "invite.revoked_at IS NULL AND invite.expires_at > %s"


@dataclass(frozen=True)
class Invite:
    """One crew_invites row, with its crew."""

    crew_row: int
    crew_id: str
    name: str
    size: int
    expires_at: datetime
    revoked_at: datetime | None

    def live(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now


def invite_state(
    invite: Invite | None,
    now: datetime,
    *,
    used: int = 0,
    in_crew: bool = True,
    crew_count: int = 0,
) -> InviteState:
    """The link's state. Left at their defaults, the places and crew count
    only tell a live link from an ended or unknown one."""
    if invite is None:
        return InviteState.UNKNOWN
    if not invite.live(now):
        return InviteState.ENDED
    if used >= invite.size:
        return InviteState.FULL
    if not in_crew and crew_count >= MAX_CREWS_PER_ACCOUNT:
        return InviteState.AT_LIMIT
    return InviteState.USABLE


def reuse_invite(newest_expires_at: datetime | None, now: datetime, *, new: bool) -> bool:
    """Whether Invite hands back the caller's newest live link instead of a
    new one: unless asked for a new one, while it has a day left."""
    return (
        not new
        and newest_expires_at is not None
        and newest_expires_at - now >= INVITE_REUSE_MIN_LEFT
    )
