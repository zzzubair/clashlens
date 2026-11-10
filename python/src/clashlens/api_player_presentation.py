"""Public player, battle and Season presentation shared by API features."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from .army_decoder import with_unnamed_ids
from .catalog import unit_label
from .db_rows import text_value
from .profile import normalize_player_tag
from .reconciliation import (
    COVERAGE_GAP_REASONS,
    MAX_DAILY_ATTACKS,
    MAX_DAILY_DEFENSES,
    all_battles_recorded,
    day_totals_supported,
)

PLAYER_SCREEN_READY_VERSION = "api-player-daily-log-v4"


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


def public_army(row: Any) -> dict[str, Any]:
    army = with_unnamed_ids(
        {
            "status": text_value(row[2]),
            **dict(zip(("home_troops", "spells", "siege", "cc_troops"), row[4:8])),
            "heroes": row[8] if isinstance(row[8], list) else [],
            "unresolved_components": row[9] if isinstance(row[9], list) else [],
        }
    )

    def facts(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result = []
        for fact in value:
            if not isinstance(fact, (list, tuple)) or len(fact) < 3:
                continue
            typed_id, quantity, origin = str(fact[0]), int(fact[1]), str(fact[2])
            result.append(
                {
                    "typed_id": typed_id,
                    "name": unit_label(typed_id),
                    "quantity": quantity,
                    "origin": origin,
                }
            )
        return result

    components = []
    for field in ("home_troops", "spells", "siege", "cc_troops"):
        components.extend(facts(army[field]))
    for hero in army["heroes"]:
        if not isinstance(hero, Mapping):
            continue
        for typed_id in [hero.get("hero"), hero.get("pet"), *(hero.get("equipment") or [])]:
            if isinstance(typed_id, str):
                components.append(
                    {
                        "typed_id": typed_id,
                        "name": unit_label(typed_id),
                        "quantity": 1,
                        "origin": "hero",
                    }
                )
    return {
        "state": army["status"],
        "failure_reason": text_value(row[3]) if row[3] is not None else None,
        "components": components,
        "decoder_version": text_value(row[10]),
        "catalog_version": text_value(row[11]),
    }


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
        "season_start": _iso(record["season_start"]),
        "season_end": _iso(record["season_end"]),
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
    if 28 in hidden:
        summary["end_trophies"] = None
    return summary
