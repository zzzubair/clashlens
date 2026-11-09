"""Public player, battle and Season presentation shared by API features."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from .db_rows import text_value
from .domain import (
    BATTLE_DAY_GRACE,
    RANKED_DAY_DURATION,
    SEASON_DAY_COUNT,
    SEASON_DURATION,
    SEASON_START_TROPHIES,
    DomainRuleError,
    ranked_day_for,
    validate_legend_season_start,
)
from .profile import normalize_player_tag
from .reconciliation import (
    COVERAGE_GAP_REASONS,
    MAX_DAILY_ATTACKS,
    MAX_DAILY_DEFENSES,
    all_battles_recorded,
    day_totals_supported,
)

PLAYER_SCREEN_READY_VERSION = "api-player-daily-log-v4"

BATTLE_DOUBT_CODES = frozenset({
    "perspective_disagreement", "duplicate_contribution_disagreement",
    "trophy_equation_mismatch", "ranked_version_mismatch",
    "attack_star_total_mismatch", "defense_star_total_mismatch", "truncated_reasons",
    "attack_count_exceeds_eight", "defense_count_exceeds_eight",
})
PRESENTATION_GAP_CODES = frozenset({
    "missing_start_battle_log_baseline", "missing_end_battle_log_baseline",
    "battle_log_row_gap", "battle_log_overlap_gap", "battle_log_stale_window",
    "battle_log_row_count_exceeds_fifty", "unclassified_rows",
})
_LIVE_WAIT_CODES = frozenset({
    "missing_end_battle_log_baseline", "missing_end_baseline",
    "automatic_defense_basis_unavailable",
})


def heatmap_kind(attacks: int | None, defenses: int | None, net: int | None) -> str:
    """The existing website heat-map cell, independent of worker shield evidence."""
    return "shield" if attacks == 0 and defenses == 0 and net == 0 else "missing" if net is None else "played"


def day_net(day: Mapping[str, Any]) -> int | None:
    """The saved net, or with every battle known, gain less loss."""
    gain, loss = day["attack_gain"], day["defense_loss"]
    if day["net_trophy_change"] is not None:
        return day["net_trophy_change"]
    return gain - loss if day["battles_complete"] and gain is not None and loss is not None else None


def day_presentation(day: Mapping[str, Any], *, is_current: bool) -> dict[str, Any]:
    """Website evidence labels and the bot's deliberately different prefilters.

    Historical summaries have counts rather than event arrays. Their old
    website classifier infers completeness only with all eight on both sides.
    """
    historical = "net_change" in day
    codes = list(day.get("flags" if historical else "uncertainty_reasons", []))
    attack = day.get("attack_count")
    defense = day.get("defense_count")
    events = [*day.get("offense_events", []), *day.get("defense_events", [])]
    has_battles = bool(events) if not historical else bool(attack or defense)
    counts = (attack, defense) if historical else (
        len(day.get("offense_events", [])), len(day.get("defense_events", []))
    )
    complete = day.get("battles_complete", (
        counts == (MAX_DAILY_ATTACKS, MAX_DAILY_DEFENSES)
        and not BATTLE_DOUBT_CODES.intersection(codes)
    ))
    coverage = day.get("coverage")
    if coverage not in ("complete", "partial") and not historical:
        coverage = day["completeness"]["state"]
    doubtful = bool(BATTLE_DOUBT_CODES.intersection(codes)) or (
        not complete and (coverage != "complete" or bool(PRESENTATION_GAP_CODES.intersection(codes)))
    )
    net = day.get("net_change" if historical else "net_trophy_change")
    status = (
        "uncertain" if net is None or doubtful else
        "verified" if day["state"] == "Complete" and day.get("confidence") == "exact" and not codes
        else "calculated"
    )
    gain, loss = day.get("attack_gain"), day.get("defense_loss")
    battle_net = None if gain is None or loss is None else gain - loss
    caution = [code for code in codes if code not in _LIVE_WAIT_CODES]
    return {
        "status": "in_progress" if is_current else "not_enrolled" if "not_enrolled" in codes else status,
        # The bot suppresses battle-free not-eligible days too, but does not
        # call a day with battles Not enrolled merely for that saved flag.
        "day_summary_status": status,
        "heatmap_kind": heatmap_kind(attack, defense, net),
        "battle_net": None if not is_current and "not_enrolled" in codes else battle_net,
        "battle_net_complete": battle_net if complete else None,
        "is_current": is_current,
        "show_in_player_log": (
            "player_not_eligible" not in codes or "not_enrolled" in codes or has_battles
        ),
        "show_in_day_summary": has_battles or not {"not_enrolled", "player_not_eligible"}.intersection(codes),
        "live_routine": day["state"] == "Live" and coverage == "partial" and not caution,
        "reason_codes": codes,
        "caution_codes": caution,
    }


def display_starts(days: list[dict[str, Any]], profile: Mapping[str, Any]) -> None:
    """Port calculateStartingTrophies over one consumer reference group."""
    observed = screen_event_timestamp(profile.get("observed_at"))
    try:
        season_start = datetime.fromtimestamp(int(profile.get("current_league_season_id")), UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        season_start = None
    following = None
    for day in sorted(days, key=lambda item: item["ranked_day_start"], reverse=True):
        next_day, following = following, day
        number = day["season_day_number"]
        saved = day.get("start_trophies")
        if number == 1 and saved != SEASON_START_TROPHIES:
            saved = None
        source = day.get("start_trophies_source")
        day["display_start"] = {
            "trophies": saved,
            "source": "unknown" if saved is None else "season_rule" if source == "season_rule" else "calculated" if source == "previous_day_end" else "saved",
            "calculation": None,
        }
        start = screen_event_timestamp(day["ranked_day_start"])
        end = screen_event_timestamp(day["ranked_day_end"])
        if saved is not None or start is None or end is None:
            continue
        offense, defense = day["offense_events"], day["defense_events"]
        gain, loss = day["attack_gain"], day["defense_loss"]
        events = [*offense, *defense]
        if (gain is None or loss is None
            or day["attack_count"] != len(offense) or day["defense_count"] != len(defense)
            or sum(event["trophy_change"] for event in offense) != gain
            or -sum(event["trophy_change"] for event in defense) != loss
            or any(event.get("perspective_disagreement")
                   or not start + BATTLE_DAY_GRACE <= datetime.fromisoformat(event["battle_timestamp"]) < end + BATTLE_DAY_GRACE
                   for event in events)):
            continue
        net = day["net_trophy_change"]
        if net is None:
            net = gain - loss
        total = None
        eligible = day["completeness"]["state"] == "complete" or (
            day["attack_count"] == MAX_DAILY_ATTACKS and day["defense_count"] == MAX_DAILY_DEFENSES)
        if (not (profile.get("season_reset_pending") and number == 1)
            and season_start is not None and season_start <= start < season_start + SEASON_DURATION
            and observed is not None and start <= observed < end and eligible
            and all(datetime.fromisoformat(event["battle_timestamp"]) <= observed for event in events)):
            total = profile["trophies"]
        elif (next_day is not None and next_day["display_start"]["trophies"] is not None
              and screen_event_timestamp(next_day["ranked_day_start"]) == end
              and number is not None and next_day["season_day_number"] == number + 1 and eligible):
            total = next_day["display_start"]["trophies"]
        if (total is None or not isinstance(total - net, int) or abs(total - net) > 2**53 - 1
            or total - net < 0 or number == 1 and total - net != SEASON_START_TROPHIES):
            continue
        day["display_start"] = {
            "trophies": total - net, "source": "calculated",
            "calculation": {"end_trophies": total, "net_change": net},
        }


def display_end(day: Mapping[str, Any], saved: list[dict[str, Any]], weeks: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    start = day["display_start"]["trophies"]
    net = day["net_trophy_change"]
    before = None if start is None or net is None else start + net
    reset = next((item for item in day["adjustments"] if item["type"] in ("weekly_reset", "season_reset")), None)
    amount = 0 if reset is None else reset["amount"]
    after = None if before is None else before + amount
    dashboard_net = day_net(day)
    dashboard = None if start is None or dashboard_net is None else start + dashboard_net + amount
    end = screen_event_timestamp(day["ranked_day_end"])
    following = next((item["display_start"]["trophies"] for item in saved
                      if screen_event_timestamp(item["ranked_day_start"]) == end), None)
    left = any(screen_event_timestamp(week["week_start"]) == end and week["league"] == "below_legend_i" for week in weeks)
    conflict = None
    if before is not None and following is not None and after != following:
        conflict = {
            "battle_end": before, "reset_kind": None if not amount else "weekly" if reset["type"] == "weekly_reset" else "season",
            "reset_amount": amount, "next_start": following,
        }
    return {
        "before_reset": before, "after_reset": after,
        "player_log": following if following is not None else after if end is not None and end <= now and (now - end < RANKED_DAY_DURATION or left) else None,
        "dashboard": following if following is not None else dashboard,
        "conflict": conflict,
        "status": "uncertain" if conflict else day_presentation(day, is_current=False)["status"],
    }


def player_confidence(active: bool, eligibility_state: str) -> str:
    if eligibility_state == "eligible":
        return "high"
    if eligibility_state == "uncertain":
        return "uncertain"
    return "partial" if active else "uncertain"


def public_snapshot_confidence(value: str) -> str:
    if value in {"exact", "confirmed", "high"}:
        return "high"
    if value in {"inferred", "partial"}:
        return "partial"
    return "uncertain"


def screen_daily_log(day: dict[str, Any], profile_confidence: str) -> dict[str, Any]:
    coverage = day["coverage"]
    reasons = [reason for reason in day["partial_reasons"] if isinstance(reason, str)]
    if day["confidence"] == "uncertain":
        completeness = "uncertain"
    elif day["state"] == "Complete" and coverage == "complete" and not reasons:
        completeness = "complete"
    else:
        completeness = "partial"
    default_reason = {
        "complete": "Published ranked-day evidence is complete.",
        "partial": "Published ranked-day evidence is partial.",
        "uncertain": "Published ranked-day evidence has unresolved uncertainty.",
    }[completeness]
    return {
        **day,
        "completeness": {
            "state": completeness,
            "reason": "; ".join(reasons) if reasons else default_reason,
        },
        "public_confidence": public_snapshot_confidence(
            day["confidence"] if day["confidence"] is not None else profile_confidence
        ),
        "uncertainty_reasons": reasons,
    }


def screen_daily_log_with_events(
    day: dict[str, Any], profile_confidence: str, now: datetime
) -> dict[str, Any]:
    screen_day = screen_daily_log(day, profile_confidence)
    # The events carry every battle the website shows. Sending the stored
    # battles too would double the player response.
    offense_events, defense_events = screen_events(screen_day.pop("battles", None))
    screen_day["offense_events"] = offense_events
    screen_day["defense_events"] = defense_events
    end = day["ranked_day_end"]
    in_progress = end is not None and datetime.fromisoformat(end) > now
    # A finished day may have lost battles after its last check unless it
    # holds all 8 of each, and then its trophy readings must agree with them.
    screen_day["battles_complete"] = battles_so_far_complete(
        screen_day, offense_events, defense_events
    ) and (
        in_progress
        or len(offense_events) == MAX_DAILY_ATTACKS
        and len(defense_events) == MAX_DAILY_DEFENSES
        and "trophy_equation_mismatch" not in screen_day["uncertainty_reasons"]
    )
    return screen_day

_BATTLE_SUM_NEUTRAL_REASONS = frozenset(
    {
        "missing_start_baseline",
        "start_baseline_incomplete",
        "missing_end_baseline",
        "end_baseline_incomplete",
        "end_reading_unverified",
        "missing_end_battle_log_baseline",
        "automatic_defense_basis_unavailable",
        "player_not_eligible",
    }
)


def battle_sums_complete(
    attack_count: Any, defense_count: Any, reasons: Iterable[Any]
) -> bool:
    """Whether a day's saved reasons leave no battle of it so far missing. The
    game allows 8 attacks and 8 defenses a day, so 8 of each leave none."""
    reasons = {reason for reason in reasons if isinstance(reason, str)}
    return (
        all_battles_recorded(attack_count, defense_count, reasons)
        or reasons <= _BATTLE_SUM_NEUTRAL_REASONS
    )


def net_so_far(
    attack_count: Any,
    attack_gain: Any,
    defense_count: Any,
    defense_loss: Any,
    reasons: Iterable[Any],
) -> int | None:
    """Trophies a day's saved battles have won or lost so far, from the day's
    settled start: today's net as the dashboard's Legend day card and a
    watchlist (``api_groups._today``) show it, None while a battle may be
    missing. It reads a saved log's totals and reasons, not its battles, so a
    board of 200 rows stays small; ``battles_so_far_complete`` also checks
    those battles add up to the totals."""
    if (
        attack_gain is None
        or defense_loss is None
        or not battle_sums_complete(attack_count, defense_count, reasons)
    ):
        return None
    return int(attack_gain) - int(defense_loss)


def battles_so_far_complete(
    screen_day: dict[str, Any],
    offense: list[dict[str, Any]],
    defense: list[dict[str, Any]],
) -> bool:
    """Whether the listed battles are every battle of the day so far."""
    return (
        battle_sums_complete(len(offense), len(defense), screen_day["uncertainty_reasons"])
        and screen_day["attack_count"] == len(offense) <= MAX_DAILY_ATTACKS
        and screen_day["defense_count"] == len(defense) <= MAX_DAILY_DEFENSES
        and screen_day["attack_gain"] == sum(item["trophy_change"] for item in offense)
        and screen_day["defense_loss"]
        == -sum(item["trophy_change"] for item in defense)
        and not any(item["perspective_disagreement"] for item in [*offense, *defense])
    )


def screen_events(
    battles: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project versioned published battle evidence into the private API shape.

    The publication is expected to contain canonical, included battle events.
    Perspective-disagreement battles stay visible on their row so the website
    can flag them instead of silently dropping accepted evidence. The
    validation here is deliberately defensive: old publications contain
    reconciliation evidence rather than the new event projection, and a
    malformed JSON element must not make the player operation fail.
    """
    if not isinstance(battles, list):
        return [], []

    candidates: list[tuple[str, datetime, tuple[int, Any], int, dict[str, Any]]] = []
    for index, battle in enumerate(battles):
        parsed = screen_event(battle)
        if parsed is None:
            continue
        lens, battle_id, timestamp, event = parsed
        candidates.append(
            (lens, timestamp, battle_id_sort_key(battle_id), index, event)
        )

    # A published version should already contain one canonical row, but keep
    # the API deterministic if an older or malformed payload repeats a battle.
    candidates.sort(key=lambda item: (item[1], item[2], -item[3]), reverse=True)
    seen: set[str] = set()
    offense: list[dict[str, Any]] = []
    defense: list[dict[str, Any]] = []
    for lens, _timestamp, _battle_id, _index, event in candidates:
        identity = event["battle_id"]
        if identity in seen:
            continue
        seen.add(identity)
        (offense if lens == "offense" else defense).append(event)
    # Rows are numbered in play order: the first battle of the Legend day is 1.
    offense.reverse()
    defense.reverse()
    return offense, defense


def screen_event(
    battle: Any,
) -> tuple[str, str, datetime, dict[str, Any]] | None:
    if not isinstance(battle, Mapping):
        return None
    if battle.get("included") is False or battle.get("valid") is False:
        return None

    lens = battle.get("lens")
    if lens not in {"offense", "defense"}:
        return None
    battle_id_value = battle.get("battle_id", battle.get("battle_identity"))
    if not isinstance(battle_id_value, (str, int)) or isinstance(battle_id_value, bool):
        return None
    battle_id = str(battle_id_value).strip()
    if not battle_id:
        return None

    timestamp = screen_event_timestamp(battle.get("battle_timestamp"))
    if timestamp is None:
        return None

    opponent_payload = battle.get("opponent")
    if not isinstance(opponent_payload, Mapping):
        return None
    opponent_tag_value = opponent_payload.get("tag")
    opponent_name = opponent_payload.get("name")
    if not isinstance(opponent_tag_value, str):
        return None
    try:
        opponent_tag = normalize_player_tag(opponent_tag_value)
    except (TypeError, ValueError):
        return None
    if opponent_name is not None and not isinstance(opponent_name, str):
        return None

    stars = screen_event_int(battle.get("stars"), lower=0, upper=3)
    destruction = screen_event_int(
        battle.get("destruction_percentage"), lower=0, upper=100
    )
    if stars is None or destruction is None:
        return None
    trophy_value = screen_event_trophy_value(battle, lens)
    if trophy_value is None:
        return None
    trophy_change = abs(trophy_value)
    if lens == "defense":
        trophy_change = -trophy_change
    event = {
        "battle_id": battle_id,
        "battle_timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "opponent": {"tag": opponent_tag, "name": opponent_name},
        "destruction_percentage": destruction,
        "stars": stars,
        "trophy_change": trophy_change,
        "perspective_disagreement": battle.get("disagreement") is True,
    }
    if isinstance(battle.get("army_share_code"), str):
        event["army_share_code"] = battle["army_share_code"]
    return lens, battle_id, timestamp, event


def screen_event_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        if value.endswith("Z") and "-" not in value:
            timestamp = datetime.strptime(value, "%Y%m%dT%H%M%S.%fZ").replace(
                tzinfo=UTC
            )
        else:
            timestamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return None
    return timestamp.astimezone(UTC)


def screen_event_int(value: Any, *, lower: int, upper: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if lower <= value <= upper else None


def screen_event_trophy_value(battle: Mapping[str, Any], lens: str) -> int | None:
    value = battle.get("trophy_change")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if (lens == "offense" and value < 0) or (lens == "defense" and value > 0):
        return None
    return value


def battle_id_sort_key(value: str) -> tuple[int, Any]:
    try:
        return 0, int(value)
    except ValueError:
        return 1, value


def daily_log(day: Any) -> dict[str, Any]:
    return {
        "ranked_day_start": day[0].astimezone(UTC).isoformat(),
        "ranked_day_end": None
        if day[1] is None
        else day[1].astimezone(UTC).isoformat(),
        "official_season_id": None if day[2] is None else text_value(day[2]),
        "season_day_number": None if day[3] is None else int(day[3]),
        "version": int(day[4]),
        "state": text_value(day[5]),
        "coverage": text_value(day[6]),
        "confidence": None if day[7] is None else text_value(day[7]),
        "attack_count": None if day[8] is None else int(day[8]),
        "attack_three_star_count": None if day[9] is None else int(day[9]),
        "attack_gain": None if day[10] is None else int(day[10]),
        "defense_count": None if day[11] is None else int(day[11]),
        "defense_three_star_count": None if day[12] is None else int(day[12]),
        "defense_loss": None if day[13] is None else int(day[13]),
        "net_trophy_change": shown_total(
            day[14], text_value(day[6]), day[8], day[11], json_array(day[17])
        ),
        "adjustments": json_array(day[15]),
        "battles": json_array(day[16]),
        "partial_reasons": json_array(day[17]),
        "start_trophies": None if day[18] is None else int(day[18]),
        # "season_rule" when Day 1, or the day a player signed up, starts at
        # the Season rule's 5,000.
        "start_trophies_source": None if day[19] is None else text_value(day[19]),
    }


def json_array(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def shown_total(
    value: Any,
    coverage: Any,
    attack_count: Any,
    defense_count: Any,
    reasons: list[Any],
) -> int | None:
    """A saved EOD or net change, unless the day's battles were not all
    captured. Rows saved before that rule still carry the numbers."""
    if value is None or not saved_totals_supported(
        coverage, attack_count, defense_count, reasons
    ):
        return None
    return int(value)


def saved_totals_supported(
    coverage: Any, attack_count: Any, defense_count: Any, reasons: list[Any]
) -> bool:
    """A saved row without the coverage or battle counts needed to decide is
    withheld only when its stored reasons name a battle log gap."""
    if day_totals_supported(
        coverage == "complete", attack_count, defense_count, reasons
    ):
        return True
    if coverage == "partial" and attack_count is not None and defense_count is not None:
        return False
    return not any(
        isinstance(reason, str) and reason in COVERAGE_GAP_REASONS
        for reason in reasons
    )


def withhold_unsupported_entries(entries: list[Any]) -> set[Any]:
    """Apply the same rule to a saved Season summary's days and return the day
    numbers withheld. A day's EOD change also needs the EOD of the day before."""
    hidden: set[Any] = set()
    for entry in entries:
        if not isinstance(entry, dict) or saved_totals_supported(
            entry.get("coverage"),
            entry.get("attack_count"),
            entry.get("defense_count"),
            json_array(entry.get("flags")),
        ):
            continue
        hidden.add(entry.get("season_day_number"))
        entry.update(end_trophies=None, eod_state=None, net_change=None)
    for entry in entries:
        number = entry.get("season_day_number") if isinstance(entry, dict) else None
        if number in hidden or (number is not None and number - 1 in hidden):
            entry.update(eod_change=None, eod_change_state=None)
    return hidden


def historical_season_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Project a stored compact season summary to the public shape.

    The stored row is already the complete historical record; this only
    normalizes timestamps and preserves numeric values (never rendered
    text) so future comparisons can reuse them.
    """

    def _optional_int(value: Any) -> int | None:
        return None if value is None else int(value)

    def _iso(value: Any) -> str | None:
        return None if value is None else value.astimezone(UTC).isoformat()

    summary = {
        "kind": "player-season-summary",
        "tag": text_value(record["normalized_tag"]),
        "official_season_id": text_value(record["official_season_id"]),
        **summary_bounds_payload(record["season_start"], record["season_end"]),
        "start_trophies": _optional_int(record["start_trophies"]),
        "end_trophies": _optional_int(record["end_trophies"]),
        "final_rank": _optional_int(record["final_rank"]),
        "attack_count": _optional_int(record["attack_count"]),
        "attack_gain": _optional_int(record["attack_gain"]),
        "attack_three_star_count": _optional_int(record["attack_three_star_count"]),
        "defense_count": _optional_int(record["defense_count"]),
        "defense_loss": _optional_int(record["defense_loss"]),
        "defense_three_star_count": _optional_int(record["defense_three_star_count"]),
        "net_trophy_change": _optional_int(record["net_trophy_change"]),
        "attack_stars": {
            str(star): int(record[f"attack_star_{star}"]) for star in range(4)
        },
        "attack_stars_unknown": int(record["attack_star_unknown"]),
        "defense_stars": {
            str(star): int(record[f"defense_star_{star}"]) for star in range(4)
        },
        "defense_stars_unknown": int(record["defense_star_unknown"]),
        "days_observed": int(record["days_observed"]),
        "days_missing": int(record["days_missing"]),
        "missing_days": [int(day) for day in (record["missing_days"] or [])],
        "coverage_state": text_value(record["coverage_state"]),
        "unresolved_flags": [
            text_value(flag) for flag in (record["unresolved_flags"] or [])
        ],
        "daily_entries": json_array(record["daily_entries"]),
        "projection_version": text_value(record["projection_version"]),
        "published_at": _iso(record["published_at"]),
    }
    hidden = withhold_unsupported_entries(summary["daily_entries"])
    if hidden:
        summary["net_trophy_change"] = None
    if SEASON_DAY_COUNT in hidden:
        summary["end_trophies"] = None
    return summary


def calendar_payload(now: datetime) -> dict[str, Any]:
    day = ranked_day_for(now)
    return {
        "as_of": now.astimezone(UTC).isoformat(),
        "day_start": day.start.isoformat(),
        "day_end": day.end.isoformat(),
        "season_id": day.official_season_id,
        "season_start": day.season_start.isoformat(),
        "season_end": day.season_end.isoformat(),
        "day_number": day.day_number,
        "day_count": SEASON_DAY_COUNT,
    }


def season_bounds_payload(season_id: str) -> dict[str, Any] | None:
    try:
        start = validate_legend_season_start(
            season_id, observed_at=datetime.max.replace(tzinfo=UTC)
        )
    except DomainRuleError:
        return None
    return {
        "id": season_id,
        "start": start.isoformat(),
        "end": (start + SEASON_DURATION).isoformat(),
        "last_day_start": (start + SEASON_DURATION - RANKED_DAY_DURATION).isoformat(),
        "day_count": SEASON_DAY_COUNT,
    }


def summary_bounds_payload(start: datetime | None, end: datetime | None) -> dict[str, Any]:
    def _iso(value: datetime | None) -> str | None:
        return None if value is None else value.astimezone(UTC).isoformat()

    return {
        "season_start": _iso(start),
        "season_end": _iso(end),
        "last_day_start": _iso(None if end is None else end - RANKED_DAY_DURATION),
        "day_count": SEASON_DAY_COUNT,
    }
