from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# v1 gave a 2-star attack at exactly 55% 18 trophies; Supercell's formula
# gives 17. v1 stays for results already saved under it.
HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION = "legend-trophy-allocation-v1"
TROPHY_ALLOCATION_RULE_VERSION = "legend-trophy-allocation-v2"
SEASON_ANCHOR_RULE_VERSION = "legend-season-anchor-v1"
BOOTSTRAP_CURRENT_SEASON_ID = "1783918800"
BOOTSTRAP_PREVIOUS_SEASON_ID = "1781499600"
RANKED_DAY_DURATION = timedelta(days=1)
SEASON_DURATION = timedelta(days=28)
# Every Legend I player starts a Season at exactly this many trophies.
SEASON_START_TROPHIES = 5000
# One attack or defense moves trophies by at most this many.
MAX_BATTLE_TROPHIES = 40
# No new-day attack can start in the first minutes after the Reset, so a
# battle reported in the first five minutes finished a previous-day attack.
# The attacker's report is stamped when the attack ends, often after the
# Reset; the defender's for the same battle usually before it. The Reset
# itself stays at 05:00 UTC.
BATTLE_DAY_GRACE = timedelta(minutes=5)

# Rules the owner has not decided yet. Each is one switch, set to how Clash
# Lens behaves today; the module named beside it applies it. Changing one
# changes saved results, so a release that does reruns the Season repair
# (``ranked_day_repair``).
#
# A Reset profile read after the player's first battle of the new day
# (reset_baselines): "reject" leaves both days it bounds without that
# Reset's trophies. A rule recovering such readings plugs in there.
LATE_RESET_READING = "reject"
# Whose day before can give the automatic defense loss its defense count
# and losses (reconciliation, cl-partial-chain-rule): "complete_day" needs
# that whole day Complete; "covered_day" any day whose battle logs are
# continuous and whose battles undisputed, as Partial days missing only a
# Reset reading are.
PREVIOUS_DAY_DEFENSES = "complete_day"
# What the Daily board ranks (boundary_manifest): "before_automatic_loss",
# the trophies at the Reset before the game's automatic defense loss, or
# "eod", the day's end after it.
DAILY_BOARD_VALUE = "before_automatic_loss"
# Order of equal trophies (snapshots, api_leaderboard): "per_board", the
# Daily board by SHA-256 of the tag and the Live board by MD5 of it, or
# "tag_hash", both by SHA-256 of the tag.
TIE_ORDER = "per_board"
# Players in Legend I who have not signed up for the Season (snapshots'
# manifest, api_leaderboard): "hidden" leaves them off both boards until a
# profile names the Season. A rule showing them plugs in there.
UNSIGNED_UP_PLAYERS = "hidden"
# The longest run of days with no battles and unchanged trophies inferred
# as a shield (reconciliation); a longer run stays uncertain.
MAX_INFERRED_SHIELD_DAYS = 2

_ALLOCATION_THRESHOLDS: dict[int, tuple[tuple[int, int], ...]] = {
    0: ((0, 0), (10, 1), (20, 2), (30, 3), (40, 4)),
    1: (
        (1, 5),
        (10, 6),
        (19, 7),
        (28, 8),
        (37, 9),
        (46, 10),
        (55, 11),
        (64, 12),
        (73, 13),
        (82, 14),
        (91, 15),
    ),
    2: (
        (50, 16),
        (53, 17),
        (56, 18),
        (59, 19),
        (62, 20),
        (65, 21),
        (68, 22),
        (71, 23),
        (74, 24),
        (77, 25),
        (80, 26),
        (83, 27),
        (86, 28),
        (89, 29),
        (92, 30),
        (95, 31),
        (98, 32),
    ),
    3: ((100, 40),),
}


class DomainRuleError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(f"{category}: {message}")
        self.category = category


@dataclass(frozen=True, slots=True)
class TrophyAllocation:
    attacker_gain: int
    defender_loss: int
    rule_version: str = TROPHY_ALLOCATION_RULE_VERSION


@dataclass(frozen=True, slots=True)
class SeasonAnchor:
    current_id: str
    previous_id: str
    current_start: datetime
    previous_start: datetime
    rule_version: str = SEASON_ANCHOR_RULE_VERSION


@dataclass(frozen=True, slots=True)
class RankedDay:
    start: datetime
    end: datetime
    season_start: datetime
    season_end: datetime
    day_number: int
    official_season_id: str
    anchor_rule_version: str = SEASON_ANCHOR_RULE_VERSION


def allocate_trophies(
    stars: int,
    destruction: int,
    *,
    rule_version: str = TROPHY_ALLOCATION_RULE_VERSION,
) -> TrophyAllocation:
    if rule_version not in {
        TROPHY_ALLOCATION_RULE_VERSION,
        HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION,
    }:
        raise DomainRuleError(
            "unsupported_trophy_allocation_rule",
            "trophy allocation rule version is not installed",
        )
    if stars not in _ALLOCATION_THRESHOLDS or not 0 <= destruction <= 100:
        raise DomainRuleError(
            "impossible_trophy_allocation",
            "stars or destruction is outside the Legend I rule",
        )
    eligible = [
        trophies
        for minimum, trophies in _ALLOCATION_THRESHOLDS[stars]
        if minimum <= destruction
    ]
    if not eligible:
        raise DomainRuleError(
            "impossible_trophy_allocation",
            "stars and destruction do not form a valid Legend I result",
        )
    gain = eligible[-1]
    if (stars, destruction) == (2, 55) and (
        rule_version == HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION
    ):
        gain = 18
    return TrophyAllocation(
        attacker_gain=gain,
        defender_loss=0 if stars == 0 else gain,
        rule_version=rule_version,
    )


def _canonical_season_start(value: str) -> datetime:
    try:
        if not value.isascii() or str(int(value)) != value:
            raise ValueError
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise DomainRuleError(
            "invalid_season_anchor", "season IDs must be canonical Unix seconds"
        ) from error


def validate_season_anchor(current_id: str, previous_id: str) -> SeasonAnchor:
    current = _canonical_season_start(current_id)
    previous = _canonical_season_start(previous_id)
    if (
        current - previous != SEASON_DURATION
        or current.weekday() != 0
        or previous.weekday() != 0
        or current.time().replace(tzinfo=None) != datetime.min.time().replace(hour=5)
        or previous.time().replace(tzinfo=None) != datetime.min.time().replace(hour=5)
    ):
        raise DomainRuleError(
            "invalid_season_anchor",
            "season IDs must be adjacent Monday 05:00 UTC boundaries",
        )
    return SeasonAnchor(
        current_id=current_id,
        previous_id=previous_id,
        current_start=current,
        previous_start=previous,
    )


def validate_profile_season_anchor(
    current_id: str, *, observed_at: datetime
) -> SeasonAnchor:
    """Validate a profile's current tournament and derive its prior boundary."""
    current = _canonical_season_start(current_id)
    bootstrap = _canonical_season_start(BOOTSTRAP_CURRENT_SEASON_ID)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise DomainRuleError(
            "invalid_season_anchor",
            "profile observation time must include a UTC offset",
        )
    if (
        current < bootstrap
        or current > observed_at.astimezone(UTC)
        or current.weekday() != 0
        or current.time().replace(tzinfo=None) != datetime.min.time().replace(hour=5)
        or (current - bootstrap) % SEASON_DURATION
    ):
        raise DomainRuleError(
            "invalid_season_anchor",
            "profile current season is not aligned to the observed 28-day phase",
        )
    previous = current - SEASON_DURATION
    return SeasonAnchor(
        current_id=current_id,
        previous_id=str(int(previous.timestamp())),
        current_start=current,
        previous_start=previous,
    )


def validate_legend_season_start(season_id: str, *, observed_at: datetime) -> datetime:
    """Return a confirmed Legend season start from an official season id."""
    start = _canonical_season_start(season_id)
    bootstrap = _canonical_season_start(BOOTSTRAP_CURRENT_SEASON_ID)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise DomainRuleError(
            "invalid_season_anchor",
            "season observation time must include a UTC offset",
        )
    if (
        start > observed_at.astimezone(UTC)
        or start.weekday() != 0
        or start.time().replace(tzinfo=None) != datetime.min.time().replace(hour=5)
        or (start - bootstrap) % SEASON_DURATION
    ):
        raise DomainRuleError(
            "invalid_season_anchor",
            "season ID is not aligned to the observed 28-day Legend phase",
        )
    return start


def anchored_ranked_day(
    timestamp: datetime, current_id: str, previous_id: str
) -> RankedDay:
    """The Legend day with its Season counted in 28-day steps from a confirmed
    anchor, so days after an anchor's Season ends get the next Season even
    before a profile reports it. Refuses an anchor off the 28-day phase."""
    day = ranked_day_for(timestamp, anchor=validate_season_anchor(current_id, previous_id))
    if day.season_start != ranked_day_for(timestamp).season_start:
        raise DomainRuleError(
            "invalid_season_anchor",
            "confirmed season anchor is not on the 28-day Legend phase",
        )
    return day


def is_season_boundary(boundary: datetime) -> bool:
    """True when a 05:00 UTC Reset boundary also opens a new season."""
    return ranked_day_for(boundary).season_start == boundary.astimezone(UTC)


def season_is_current(profile_season_id: str | None, at: datetime) -> bool:
    """True when a profile's current league Season is the calendar Season at
    ``at``. Otherwise its trophies come from before that player's Season reset
    and are never a total for the calendar Season."""
    return profile_season_id == ranked_day_for(at).official_season_id


def season_opening_reset(at: datetime) -> datetime | None:
    """The Reset that opened the Season when ``at`` is on its first Legend
    day. Its frozen final board holds each player's pre-Reset trophies."""
    day = ranked_day_for(at)
    return day.start if day.day_number == 1 else None


def opening_day_trophies_explained(trophies: int, net: int, battles: int) -> bool:
    """Whether a Legend I profile's trophies on a Season's first Legend day
    are 5,000 plus what that day's recorded battles moved. When the recorded
    net change does not match, some battles are missing, so the gap from 5,000
    may be at most 40 trophies per recorded attack or defense."""
    gap = trophies - SEASON_START_TROPHIES
    return gap == net or abs(gap) <= MAX_BATTLE_TROPHIES * battles


def awaits_season_reset(
    profile_season_id: str | None,
    trophies: int | None,
    frozen_trophies: int | None,
    day_battles: Sequence[int] | None,
    at: datetime,
) -> bool:
    """True when a profile's trophies come from before that player's Season
    reset: it names an earlier Season, or on the Season's first Legend day
    its trophies are not 5,000 and either still equal the player's frozen
    pre-Reset final trophies or, for a Legend I player, are not explained by
    ``day_battles`` (that day's recorded net change and battle count)."""
    if not season_is_current(profile_season_id, at):
        return True
    if (
        season_opening_reset(at) is None
        or trophies is None
        or trophies == SEASON_START_TROPHIES
    ):
        return False
    return trophies == frozen_trophies or (
        day_battles is not None
        and not opening_day_trophies_explained(trophies, *day_battles)
    )


def battle_day_for(timestamp: datetime) -> RankedDay:
    """The Legend day a battle report belongs to: reports stamped in the first
    ``BATTLE_DAY_GRACE`` after a Reset belong to the day before it."""
    return ranked_day_for(timestamp - BATTLE_DAY_GRACE)


def battle_window(day_start: datetime) -> tuple[datetime, datetime]:
    """Report timestamps ``[start, end)`` of the Legend day starting then."""
    start = day_start + BATTLE_DAY_GRACE
    return start, start + RANKED_DAY_DURATION


def ranked_day_for(
    timestamp: datetime,
    *,
    anchor: SeasonAnchor | None = None,
) -> RankedDay:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DomainRuleError(
            "invalid_event_timestamp", "battle timestamp must include a UTC offset"
        )
    confirmed = anchor or validate_season_anchor(
        BOOTSTRAP_CURRENT_SEASON_ID, BOOTSTRAP_PREVIOUS_SEASON_ID
    )
    observed = timestamp.astimezone(UTC)
    elapsed = observed - confirmed.current_start
    season_offset = elapsed // SEASON_DURATION
    season_start = confirmed.current_start + season_offset * SEASON_DURATION
    day_number = ((observed - season_start) // RANKED_DAY_DURATION) + 1
    start = season_start + (day_number - 1) * RANKED_DAY_DURATION
    return RankedDay(
        start=start,
        end=start + RANKED_DAY_DURATION,
        season_start=season_start,
        season_end=season_start + SEASON_DURATION,
        day_number=day_number,
        official_season_id=str(int(season_start.timestamp())),
    )
