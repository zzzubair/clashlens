from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

TROPHY_ALLOCATION_RULE_VERSION = "legend-trophy-allocation-v1"
SEASON_ANCHOR_RULE_VERSION = "legend-season-anchor-v1"
BOOTSTRAP_CURRENT_SEASON_ID = "1783918800"
BOOTSTRAP_PREVIOUS_SEASON_ID = "1781499600"
RANKED_DAY_DURATION = timedelta(days=1)
SEASON_DURATION = timedelta(days=28)

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
        (55, 18),
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


def allocate_trophies(stars: int, destruction: int) -> TrophyAllocation:
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
    return TrophyAllocation(
        attacker_gain=gain,
        defender_loss=0 if stars == 0 else gain,
    )


def validate_season_anchor(current_id: str, previous_id: str) -> SeasonAnchor:
    try:
        if not current_id.isascii() or not previous_id.isascii():
            raise ValueError
        if str(int(current_id)) != current_id or str(int(previous_id)) != previous_id:
            raise ValueError
        current = datetime.fromtimestamp(int(current_id), tz=UTC)
        previous = datetime.fromtimestamp(int(previous_id), tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise DomainRuleError(
            "invalid_season_anchor", "season IDs must be canonical Unix seconds"
        ) from error
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
