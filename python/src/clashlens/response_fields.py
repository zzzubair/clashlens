"""The response fields Clash Lens uses, and the collector's change test.

The collector hashes only the listed fields to decide whether a response
changed. Bytes that differ outside these fields are not stored again. This is
the only thing the collector reads from a response body. Apart from what
decides when a check also fetches the battle log (profile trophies and win
counts, battle times and opponents), it never interprets meaning. When a response counts as changed, the full raw body is
still stored and archived exactly as before. A Reset baseline always counts as changed;
that rule lives in ``collector_db.record_response``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# The profile fields Clash Lens uses, per the standing decision on issue #110.
# Everything else in the body (donations, achievements, troop and equipment
# lists, clan details beyond identity) is ignored for the change test, and so
# is legendStatistics.currentSeason.rank: it moves whenever other players
# battle and nothing reads it.
PROFILE_FIELDS = (
    "tag",
    "name",
    "trophies",
    "leagueTier",
    "currentLeagueSeasonId",
    "previousLeagueSeasonId",
    "expLevel",
    "bestTrophies",
    "legendStatistics",
    "role",
)
PROFILE_CLAN_FIELDS = ("tag", "name")
# Read but not fingerprinted: a battle changes at least one of them unless it
# is a 0-star attack under 10% destruction.
PROFILE_SIGNALS = ("trophies", "attackWins", "defenseWins")

# Only Legend entries count, and only the listed fields on them, per the
# standing decision on issue #110. Other entry types and every other field
# (loot, donations, town-hall levels, trophies) are ignored.
BATTLE_LOG_ENTRY_FIELDS = (
    "attack",
    "stars",
    "destructionPercentage",
    "battleTime",
    "battleTimestamp",
    "armyShareCode",
    "opponentPlayerTag",
    "opponentName",
)

_FIELD_ENDPOINTS = {"profile", "battle_log"}


def content_fingerprint(
    endpoint: str,
    body: bytes,
    *,
    http_status: int,
    response_hash: str,
) -> str:
    """Digest of the fields Clash Lens uses, else the raw body digest.

    Endpoints without a field list, error responses, and unparseable bodies
    fall back to the raw-response digest so their behaviour is unchanged.
    """
    return read_fields(
        endpoint, body, http_status=http_status, response_hash=response_hash
    )[0]


def read_fields(
    endpoint: str,
    body: bytes,
    *,
    http_status: int,
    response_hash: str,
) -> tuple[str, Any]:
    """The content fingerprint and the fields the collector schedules from.

    The second value is the profile's trophies and win counts, or the Legend
    battle-log entries, or None when the response is unreadable.
    """
    if not 200 <= http_status < 300 or endpoint not in _FIELD_ENDPOINTS:
        return response_hash, None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return response_hash, None
    projection = (
        _profile_projection(payload)
        if endpoint == "profile"
        else _battle_log_projection(payload)
    )
    if projection is None:
        return response_hash, None
    canonical = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if endpoint == "profile":
        return fingerprint, {name: payload.get(name) for name in PROFILE_SIGNALS}
    return fingerprint, projection


def _profile_projection(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    clan = payload.get("clan")
    stats = payload.get("legendStatistics")
    season = stats.get("currentSeason") if isinstance(stats, dict) else None
    if isinstance(season, dict) and "rank" in season:
        # Drop only the rank, so profiles without one keep their fingerprint.
        stats = stats | {
            "currentSeason": {k: v for k, v in season.items() if k != "rank"}
        }
    return {
        name: payload.get(name) for name in PROFILE_FIELDS
    } | {
        "legendStatistics": stats,
        "clan": (
            {name: clan.get(name) for name in PROFILE_CLAN_FIELDS}
            if isinstance(clan, dict)
            else clan
        ),
    }


def _battle_log_projection(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items = payload["items"]
    else:
        return None
    return [
        {name: entry.get(name) for name in BATTLE_LOG_ENTRY_FIELDS}
        for entry in items
        if isinstance(entry, dict) and entry.get("battleType") == "legend"
    ]
