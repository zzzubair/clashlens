#!/usr/bin/env python3
"""Deterministic private-API fixture for the website prototype.

This server is test-only. It uses production-shaped public payloads, in-memory
refresh work, and in-memory Google account state. It does not call Supercell,
PostgreSQL, or the real Python application.

Account rules mirror the production private API (python/src/clashlens/api.py):
- Account operations require a signed google identity in the HMAC proof.
- An unresolved google identity returns 403 account_not_found.
- POST /v1/account creates an account; an existing account returns
  409 account_exists; a taken username returns 409 username_unavailable.
- Saved tags and groups are stored per account with deterministic payloads.
- POST /v1/players/{tag}/verifytoken returns the documented verification
  payloads: linked, already_linked, invalid_token, verification_unavailable,
  support_required, and 202 in_progress for replays.
- Every mutation replays the stored outcome for the same request ID, or
  returns 409 request_id_conflict for a mismatched replay.

The fixture verification token for player tag #2PP is FIXTURE-VERIFY-2PP.
The token FIXTURE-VERIFY-UNAVAILABLE always reports verification_unavailable.
The reset endpoint is loopback-only and clears all account state.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

PROOF_VERSION = "clashlens-hmac-v1"
AUDIENCE = "clashlens-python-private-api"
TAG_PATTERN = re.compile(r"^#[0289PYLQGRJCUV]{3,15}$")
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
FIXTURE_OBSERVED_AT = "2026-08-05T18:00:00Z"
FIXTURE_VERSION = "python-fixture-v1"
ALLOWED_PROVIDERS = {"discord", "google"}
JOB_RETENTION_SECONDS = 300
MAX_JOBS = 1_000
ACCOUNT_USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
RESERVED_USERNAMES = frozenset(
    {
        "account",
        "admin",
        "analytics",
        "api",
        "clashlens",
        "groups",
        "leaderboard",
        "login",
        "logout",
        "players",
        "settings",
        "support",
        "users",
    }
)
MAX_ACCOUNT_NAME_LENGTH = 80
MAX_GROUP_TAGS = 100
MAX_VERIFICATION_TOKEN_LENGTH = 512
MAX_SAVED_TAGS_PER_ACCOUNT = 500
MAX_GROUPS_PER_ACCOUNT = 500
MAX_REPLAY_ENTRIES = 2_000
REPLAY_RETENTION_SECONDS = 600
FIXTURE_VERIFY_PREFIX = "FIXTURE-VERIFY-"
FIXTURE_VERIFY_UNAVAILABLE_TOKEN = "FIXTURE-VERIFY-UNAVAILABLE"

PLAYER_SPECS = [
    ("#2PP", "Nova", "Northwind", 7211, 90, "available"),
    ("#2PQ", "Nova", "Eastwatch", 7197, 5_400, "stale"),
    ("#2P8", "Ember", "Cloudbreak", 7184, 260, "available"),
    ("#2PY", "Rook", "Ironvale", 7168, 1_800, "uncertain"),
    ("#2PL", "Mira", "Northwind", 7152, 310, "available"),
    ("#2PG", "Aster", "Blue Harbor", 7139, 480, "available"),
    ("#2PR", "Kite", "Lanterns", 7122, 720, "available"),
    ("#2PJ", "Vale", "Cloudbreak", 7105, 1_020, "available"),
    ("#2PC", "Orion", "Eastwatch", 7094, 1_440, "available"),
    ("#2PU", "Sable", "Ironvale", 7078, 1_800, "available"),
    ("#2PV", "Lumen", "Northwind", 7061, 2_160, "available"),
    ("#2P9", "Talon", "Blue Harbor", 7048, 2_520, "available"),
    ("#28PP", "Iris", "Lanterns", 7032, 2_880, "available"),
    ("#28PQ", "Pine", "Cloudbreak", 7018, 3_240, "available"),
    ("#28P8", "Cedar", "Eastwatch", 7001, 3_600, "available"),
    ("#28PY", "Juno", "Ironvale", 6988, 3_960, "available"),
    ("#28PL", "Ash", "Northwind", 6975, 4_320, "available"),
    ("#28PG", "Lyra", "Blue Harbor", 6962, 4_680, "available"),
    ("#28PR", "Moss", "Lanterns", 6949, 5_040, "available"),
    ("#28PJ", "Oriel", "Cloudbreak", 6937, 5_400, "available"),
    ("#28PC", "Rowan", "Eastwatch", 6925, 5_760, "available"),
    ("#28PU", "Skye", "Ironvale", 6914, 6_120, "available"),
    ("#28PV", "Vesper", "Northwind", 6902, 6_480, "available"),
    ("#28P9", "Wren", "Blue Harbor", 6889, 6_840, "available"),
    ("#29PP", "Zephyr", "Lanterns", 6878, 7_200, "available"),
    ("#29PQ", "Arden", "Cloudbreak", 6864, 7_560, "available"),
    ("#29P8", "Briar", "Eastwatch", 6851, 7_920, "available"),
    ("#29PY", "Cove", "Ironvale", 6839, 8_280, "available"),
    ("#29PL", "Dawn", "Northwind", 6827, 8_640, "available"),
    ("#29PG", "Fable", "Blue Harbor", 6815, 9_000, "available"),
]
_FIXTURE_TAG_ALPHABET = "0289PYLQGRJCUV"
PLAYER_SPECS.extend(
    (
        "#Q" + _FIXTURE_TAG_ALPHABET[index // 14] + _FIXTURE_TAG_ALPHABET[index % 14],
        f"Player {index + 31}",
        "Fixture Clan",
        6814 - index,
        60,
        "available",
    )
    for index in range(71)
)

STATE = {
    "jobs": {},
    "jobs_by_tag": {},
    "jobs_by_request": {},
    "refresh_counts": {},
    "refreshed": set(),
    "accounts": {},
    "accounts_by_username": {},
    "saved_tags": {},
    "groups": {},
    "verified_players": {},
    "verified_owner": {},
    "replays": {},
    "lock": threading.RLock(),
}


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode_text(value):
    if value == "" or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        if value == "":
            return ""
        raise ValueError("invalid text encoding")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if b64url(decoded) != value:
        raise ValueError("noncanonical text encoding")
    return decoded.decode("utf-8")


def configured_key():
    encoded = os.environ.get(
        "CLASHLENS_FIXTURE_HMAC_SECRET_B64",
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
    )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise ValueError("fixture key is not canonical base64url")
    key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    if len(key) != 32 or b64url(key) != encoded:
        raise ValueError("fixture key must decode to 32 bytes")
    return key


def configured_identity(name, default):
    value = os.environ.get(name, default)
    if not value or len(value) > 128 or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value):
        raise ValueError("fixture identity is invalid")
    return value


def normalize_tag(value):
    if len(value) > 64:
        return None
    normalized = value.strip().upper()
    return normalized if TAG_PATTERN.fullmatch(normalized) else None


def spec_for(tag):
    for spec in PLAYER_SPECS:
        if spec[0] == tag:
            return spec
    return None


def entry_for(spec, position, kind="live"):
    tag, name, clan, trophies, age_seconds, state = spec
    freshness = "stale" if state == "stale" else "fresh"
    confidence = (
        "uncertain"
        if state == "uncertain"
        else "confirmed"
        if kind == "frozen"
        else "eligible"
    )
    public_confidence = "uncertain" if state == "uncertain" else "high"
    return {
        "position": position,
        "tag": tag,
        "name": name,
        "clan": clan,
        "trophies": trophies,
        "observed_at": FIXTURE_OBSERVED_AT,
        "age_seconds": age_seconds,
        "freshness": freshness,
        "confidence": confidence,
        "public_confidence": public_confidence,
        "official_rank": ((position * 17 - 1) % 200) + 1 if position <= 200 else None,
    }


def leaderboard(limit, view, offset=0, season=None, day=None):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    kind = "frozen" if view == "daily" else "live"
    entries = [entry_for(spec, index, kind) for index, spec in enumerate(PLAYER_SPECS, 1)]
    if kind == "live":
        for entry in entries:
            entry["official_rank"] = None
    ordering_rule_version = (
        "fixture-frozen-v1" if kind == "frozen" else "tracked-trophies-md5-v1"
    )
    payload = {
        "kind": kind,
        "ordering_rule_version": ordering_rule_version,
        "generated_at": FIXTURE_OBSERVED_AT,
        "entries": entries[offset : offset + limit],
        "tracked_population": len(PLAYER_SPECS),
        "total_entries": len(PLAYER_SPECS),
        "page": offset // limit + 1,
        "page_size": limit,
        "page_count": (len(PLAYER_SPECS) + limit - 1) // limit,
        "has_previous": offset > 0,
        "has_next": offset + limit < len(PLAYER_SPECS),
        "coverage": {
            "state": "partial",
            "tracked_players": len(PLAYER_SPECS),
            "measured_percent": 63.0,
            "note": "This fixture represents a measured tracked cohort, not the full Legend I population.",
        },
        "provenance": {
            "source": "deterministic-python-fixture",
            "observed_at": FIXTURE_OBSERVED_AT,
            "freshness": "fresh",
            "confidence": "partial",
            "coverage": "partial",
            "version": ordering_rule_version,
        },
        "quality_states": [
            "missing",
            "partial",
            "stale",
            "malformed",
            "unclassified",
            "uncertain",
            "rate-limited",
            "unavailable",
        ],
    }
    if kind == "frozen":
        older = season == "2026-07" and day == 28
        payload.update(
            {
                "snapshot_id": "6ccbbf21-87e2-5b22-8f05-e415c30ca6ac" if older else "c74af723-6da8-54a3-a710-ee8229c9f747",
                "boundary_at": "2026-07-27T05:00:00Z" if older else "2026-08-05T05:00:00Z",
                "reset_at": "2026-07-27T05:00:00Z" if older else "2026-08-05T05:00:00Z",
                "official_season_id": "2026-07" if older else "2026-08",
                "season_day_number": 28 if older else 21,
                "previous_snapshot": None if older else {
                    "official_season_id": "2026-07", "season_day_number": 28
                },
                "next_snapshot": {
                    "official_season_id": "2026-08", "season_day_number": 21
                } if older else None,
                "version": 1,
            }
        )
    return payload


def day_for(tag):
    is_uncertain = tag in {"#2PP", "#2PY"}
    offense_events = []
    defense_events = []
    if tag == "#2PP":
        offense_events = [
            {
                "battle_id": "fixture-attack-1",
                "battle_timestamp": "2026-08-05T16:30:00Z",
                "opponent": {"tag": "#2P8", "name": "Ember"},
                "destruction_percentage": 100,
                "stars": 3,
                "trophy_change": 40,
            },
            {
                "battle_id": "fixture-attack-2",
                "battle_timestamp": "2026-08-05T17:15:00Z",
                "opponent": {"tag": "#2PY", "name": None},
                "destruction_percentage": 49,
                "stars": 1,
                "trophy_change": 0,
            },
        ]
        defense_events = [
            {
                "battle_id": "fixture-defense-1",
                "battle_timestamp": "2026-08-05T17:45:00Z",
                "opponent": {"tag": "#2PL", "name": "Mira"},
                "destruction_percentage": 75,
                "stars": 2,
                "trophy_change": -21,
            }
        ]
    return {
        "ranked_day_start": "2026-08-05T05:00:00Z",
        "ranked_day_end": "2026-08-06T05:00:00Z",
        "official_season_id": "1783918800",
        "season_day_number": 24,
        "version": 1,
        "state": "Live",
        "coverage": "partial",
        "confidence": "uncertain" if is_uncertain else "partial",
        "attack_count": 7,
        "attack_three_star_count": 5,
        "attack_gain": 31,
        "defense_count": 6,
        "defense_three_star_count": 2,
        "defense_loss": 13,
        "net_trophy_change": 18,
        "offense_events": offense_events,
        "defense_events": defense_events,
        "adjustments": [],
        "battles": [],
        "partial_reasons": [
            "The paired battle-log evidence is not complete for this fixture observation."
        ],
        "completeness": {
            "state": "uncertain" if is_uncertain else "partial",
            "reason": "The paired battle-log evidence is not complete for this fixture observation.",
        },
        "public_confidence": "uncertain" if is_uncertain else "partial",
        "uncertainty_reasons": [
            "Current day evidence is still being reconciled.",
            "This screen does not claim complete Legend I coverage.",
        ],
    }


def previous_day():
    return {
        "ranked_day_start": "2026-08-04T05:00:00Z",
        "ranked_day_end": "2026-08-05T05:00:00Z",
        "official_season_id": "1783918800",
        "season_day_number": 23,
        "version": 1,
        "state": "Complete",
        "coverage": "complete",
        "confidence": "exact",
        "attack_count": 8,
        "attack_three_star_count": 6,
        "attack_gain": 38,
        "defense_count": 8,
        "defense_three_star_count": 3,
        "defense_loss": 16,
        "net_trophy_change": 22,
        "offense_events": [],
        "defense_events": [],
        "adjustments": [],
        "battles": [],
        "partial_reasons": [],
        "completeness": {"state": "complete", "reason": "Fixture evidence reconciles this ended day."},
        "public_confidence": "high",
        "uncertainty_reasons": [],
    }


def player_page(tag):
    spec = spec_for(tag)
    if spec is None:
        return None
    _, name, clan, trophies, age_seconds, state = spec
    with STATE["lock"]:
        refreshed = tag in STATE["refreshed"]
        refresh_count = STATE["refresh_counts"].get(tag, 0)
    if refreshed:
        trophies += 4 * refresh_count
        age_seconds = 45
        state = "available"
    current = day_for(tag)
    recent_days = [current, previous_day()]
    daily_logs = [
        {
            key: value
            for key, value in day.items()
            if key not in {"completeness", "public_confidence", "uncertainty_reasons"}
        }
        for day in recent_days
    ]
    public_confidence = "uncertain" if state == "uncertain" else "high"
    observed_at = FIXTURE_OBSERVED_AT if not refreshed else "2026-08-05T18:10:00Z"
    return {
        "tag": tag,
        "name": name,
        "trophies": trophies,
        "eligibility": "uncertain" if state == "uncertain" else "eligible",
        "active": True,
        "freshness": "fresh" if refreshed else "stale" if state == "stale" else "fresh",
        "age_seconds": age_seconds,
        "coverage": "ranked_days",
        "observed_at": observed_at,
        "source_http_status": 200,
        "endpoint_version": "v1",
        "schema_version": "fixture-v1",
        "parser_version": FIXTURE_VERSION,
        "clan": clan,
        "public_confidence": public_confidence,
        "daily_logs": daily_logs,
        "screen_ready": {
            "current_day": current,
            "recent_days": recent_days,
            "season_days": recent_days,
            "season": {
                "id": "1783918800",
                "start": "2026-07-13T05:00:00Z",
                "end": "2026-08-10T05:00:00Z",
                "current_day_number": 24,
            },
            "data_quality": [
                {
                    "code": "partial",
                    "label": "Partial coverage",
                    "detail": "Some paired endpoint evidence is still missing in this saved fixture.",
                },
                {
                    "code": "uncertain",
                    "label": "Uncertain reconciliation",
                    "detail": "The active day is not a complete ranked-day claim.",
                },
            ],
            "provenance": {
                "source": "deterministic-python-fixture",
                "observed_at": observed_at,
                "freshness": "fresh" if refreshed else "stale" if state == "stale" else "fresh",
                "confidence": "uncertain" if state == "uncertain" else "partial",
                "coverage": "partial",
                "version": FIXTURE_VERSION,
            },
        },
    }


def search_players(query, limit=50):
    exact_tag = normalize_tag(query)
    if exact_tag:
        spec = spec_for(exact_tag)
        results = [] if spec is None else [entry_for(spec, PLAYER_SPECS.index(spec) + 1)]
        results = [
            {
                "tag": result["tag"],
                "name": result["name"],
                "clan": result["clan"],
                "trophies": result["trophies"],
                "observed_at": result["observed_at"],
                "age_seconds": result["age_seconds"],
                "freshness": result["freshness"],
                "public_confidence": result["public_confidence"],
            }
            for result in results
        ]
        return {"query": query, "known_only": True, "results": results}
    needle = query.strip().casefold()
    results = []
    if needle:
        for index, spec in enumerate(PLAYER_SPECS, 1):
            if needle in spec[1].casefold():
                result = entry_for(spec, index)
                results.append(
                    {
                        "tag": result["tag"],
                        "name": result["name"],
                        "clan": result["clan"],
                        "trophies": result["trophies"],
                        "observed_at": result["observed_at"],
                        "age_seconds": result["age_seconds"],
                        "freshness": result["freshness"],
                        "public_confidence": result["public_confidence"],
                    }
                )
    return {
        "query": query,
        "results": results[:limit],
        "known_only": True,
    }


def remove_job(work_id):
    STATE["jobs"].pop(work_id, None)
    for tag, tag_work_id in list(STATE["jobs_by_tag"].items()):
        if tag_work_id == work_id:
            del STATE["jobs_by_tag"][tag]
    for request_key, request_work_id in list(STATE["jobs_by_request"].items()):
        if request_work_id == work_id:
            del STATE["jobs_by_request"][request_key]


def cleanup_jobs(now=None):
    now = time.monotonic() if now is None else now
    terminal_states = {"complete", "failed", "unavailable"}
    for work_id, job in list(STATE["jobs"].items()):
        if job["state"] in terminal_states and now - job["updatedAt"] > JOB_RETENTION_SECONDS:
            remove_job(work_id)
    if len(STATE["jobs"]) >= MAX_JOBS:
        oldest_terminal = min(
            (
                (job["updatedAt"], work_id)
                for work_id, job in STATE["jobs"].items()
                if job["state"] in terminal_states
            ),
            default=None,
        )
        if oldest_terminal is not None:
            remove_job(oldest_terminal[1])


def reset_state():
    with STATE["lock"]:
        STATE["jobs"].clear()
        STATE["jobs_by_tag"].clear()
        STATE["jobs_by_request"].clear()
        STATE["refresh_counts"].clear()
        STATE["refreshed"].clear()
        STATE["accounts"].clear()
        STATE["accounts_by_username"].clear()
        STATE["saved_tags"].clear()
        STATE["groups"].clear()
        STATE["verified_players"].clear()
        STATE["verified_owner"].clear()
        STATE["replays"].clear()


def normalize_account_username(value):
    normalized = value.strip().lower()
    if not ACCOUNT_USERNAME_PATTERN.fullmatch(normalized):
        return None
    if normalized in RESERVED_USERNAMES:
        return None
    return normalized


def normalize_account_name(value):
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > MAX_ACCOUNT_NAME_LENGTH:
        return None
    if any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Cn"}
        for char in normalized
    ):
        return None
    return normalized


def normalize_group_tags(values):
    if not isinstance(values, list) or len(values) > MAX_GROUP_TAGS:
        return None
    tags = set()
    for value in values:
        if not isinstance(value, str):
            return None
        tag = normalize_tag(value)
        if tag is None:
            return None
        tags.add(tag)
    return sorted(tags)


def account_payload(account):
    return {
        "username": account["username"],
        "display_name": account["display_name"],
        "preferences": dict(account["preferences"]),
        "providers": list(account["providers"]),
    }


def saved_players_payload(subject):
    players = []
    for tag in STATE["saved_tags"].get(subject, []):
        spec = spec_for(tag)
        players.append({"tag": tag, "name": spec[1] if spec else None})
    return {"players": players}


def groups_payload(subject):
    groups = []
    for group_id, group in sorted(
        STATE["groups"].get(subject, {}).items(),
        key=lambda item: (item[1]["name"].casefold(), item[0]),
    ):
        groups.append(
            {"group_id": group_id, "name": group["name"], "tags": list(group["tags"])}
        )
    return {"groups": groups}


def verified_players_payload(subject):
    players = []
    for tag in sorted(STATE["verified_players"].get(subject, [])):
        spec = spec_for(tag)
        players.append({"tag": tag, "name": spec[1] if spec else None})
    return players


def summary_payload(account, subject):
    return {
        "username": account["username"],
        "display_name": account["display_name"],
        "verified_players": verified_players_payload(subject),
    }


def cleanup_replays():
    now = time.monotonic()
    for request_id, entry in list(STATE["replays"].items()):
        if now - entry["at"] > REPLAY_RETENTION_SECONDS:
            del STATE["replays"][request_id]
    if len(STATE["replays"]) >= MAX_REPLAY_ENTRIES:
        oldest = min(
            STATE["replays"].items(), key=lambda item: item[1]["at"], default=None
        )
        if oldest is not None:
            del STATE["replays"][oldest[0]]


def store_replay(request_id, binding, status, payload):
    with STATE["lock"]:
        cleanup_replays()
        STATE["replays"][request_id] = {
            "binding": binding,
            "status": status,
            "payload": payload,
            "at": time.monotonic(),
        }


def lookup_replay(request_id, binding):
    with STATE["lock"]:
        cleanup_replays()
        entry = STATE["replays"].get(request_id)
        if entry is None:
            return None
        if entry["binding"] != binding:
            return (409, {"error": "request_id_conflict"})
        return (entry["status"], entry["payload"])


def refresh_work(tag, idempotency_key):
    with STATE["lock"]:
        cleanup_jobs()
        request_key = (tag, idempotency_key)
        existing_id = STATE["jobs_by_request"].get(request_key)
        if existing_id is not None:
            return existing_id, STATE["jobs"][existing_id]

        active_id = STATE["jobs_by_tag"].get(tag)
        active_job = STATE["jobs"].get(active_id)
        if active_job is not None and active_job["state"] in {"queued", "running"}:
            work_id = active_id
        else:
            generation = STATE["refresh_counts"].get(tag, 0) + 1
            work_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"clashlens-fixture-refresh:{tag}:{generation}",
                )
            )
            STATE["jobs_by_tag"][tag] = work_id
            now = time.monotonic()
            STATE["jobs"][work_id] = {
                "tag": tag,
                "polls": 0,
                "state": "queued",
                "createdAt": now,
                "updatedAt": now,
            }
        STATE["jobs_by_request"][request_key] = work_id
        return work_id, STATE["jobs"][work_id]


def refresh_response(work_id):
    with STATE["lock"]:
        cleanup_jobs()
        job = STATE["jobs"].get(work_id)
        if job is None:
            return None
        job["polls"] += 1
        if job["state"] == "complete":
            state = "complete"
        elif job["polls"] == 1:
            state = "running"
        else:
            state = "complete"
            STATE["refreshed"].add(job["tag"])
            STATE["refresh_counts"][job["tag"]] = STATE["refresh_counts"].get(job["tag"], 0) + 1
        job["state"] = state
        job["updatedAt"] = time.monotonic()
        return {
            "refresh_id": work_id,
            "tag": job["tag"],
            "status": "pending" if state == "queued" else "leased" if state == "running" else state,
            "outcome": "created",
        }


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "ClashLensFixture/1"

    def log_message(self, format, *_args):
        return

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json(200, {"ok": True, "fixture": FIXTURE_VERSION})
            return
        if not self.verify_request():
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if path in {
            "/v1/leaderboards/tracked",
            "/v1/leaderboards/live",
            "/v1/leaderboards/frozen",
        }:
            try:
                limit = int(query.get("limit", ["25"])[0])
                offset = int(query.get("offset", ["0"])[0])
            except ValueError:
                limit, offset = 25, 0
            view = "daily" if path == "/v1/leaderboards/frozen" else query.get("view", ["live"])[0]
            season = query.get("official_season_id", [None])[0]
            try:
                day = int(query["season_day_number"][0]) if "season_day_number" in query else None
            except ValueError:
                day = None
            if offset >= len(PLAYER_SPECS) and offset:
                self.send_json(404, {"error": "missing"})
                return
            if view == "daily" and season == "fixture-422":
                self.send_json(422, {"error": "invalid_request"})
                return
            if view == "daily" and season is not None and (season, day) not in {
                ("2026-08", 21), ("2026-07", 28)
            }:
                self.send_json(404, {"error": "missing"})
                return
            self.send_json(200, leaderboard(limit, view, offset, season, day))
            return
        if path == "/v1/players/search":
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            if not 1 <= limit <= 50:
                limit = 50
            self.send_json(200, search_players(query.get("q", [""])[0], limit))
            return
        if path.startswith("/v1/players/"):
            tag = normalize_tag(path[len("/v1/players/") :])
            page = player_page(tag) if tag else None
            if page is None:
                self.send_json(404, {"error": "missing"})
            else:
                self.send_json(200, page)
            return
        if path.startswith("/v1/refresh/") or path.startswith("/v1/refreshes/"):
            prefix = "/v1/refreshes/" if path.startswith("/v1/refreshes/") else "/v1/refresh/"
            work_id = path[len(prefix) :]
            with STATE["lock"]:
                job = STATE["jobs"].get(work_id)
                status_unavailable = job is not None and job["tag"] == "#2PY"
            if status_unavailable:
                self.send_json(503, {"error": "unavailable"})
                return
            work = refresh_response(work_id)
            if work is None:
                self.send_json(404, {"error": "missing"})
            elif normalize_tag(os.environ.get("CLASHLENS_FIXTURE_REFRESH_STATUS_ERROR_TAG", "")) == work["tag"]:
                self.send_json(503, {"error": "unavailable"})
            else:
                self.send_json(200, work)
            return
        if path == "/v1/account":
            account, _subject, error = self.account_context()
            if error is not None:
                self.send_json(403, {"error": error})
            else:
                self.send_json(200, account_payload(account))
            return
        if path == "/v1/account/summary":
            account, subject, error = self.account_context()
            if error is not None:
                self.send_json(403, {"error": error})
            else:
                self.send_json(200, summary_payload(account, subject))
            return
        if path == "/v1/account/saved-tags":
            _account, subject, error = self.account_context()
            if error is not None:
                self.send_json(403, {"error": error})
            else:
                self.send_json(200, saved_players_payload(subject))
            return
        if path == "/v1/account/groups":
            _account, subject, error = self.account_context()
            if error is not None:
                self.send_json(403, {"error": error})
            else:
                self.send_json(200, groups_payload(subject))
            return
        if path.startswith("/v1/users/"):
            username = normalize_account_username(path[len("/v1/users/") :])
            subject = STATE["accounts_by_username"].get(username) if username else None
            account = STATE["accounts"].get(subject) if subject else None
            if account is None:
                self.send_json(404, {"error": "missing"})
            else:
                self.send_json(200, summary_payload(account, subject))
            return
        self.send_json(404, {"error": "missing"})

    def do_POST(self):
        if self.path == "/__fixture/reset":
            if not self.is_loopback():
                self.send_json(403, {"error": "forbidden"})
                return
            reset_state()
            self.send_json(200, {"ok": True})
            return
        if not self.verify_request():
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path == "/v1/account":
            self.create_account_route()
            return
        if path.startswith("/v1/account/providers/"):
            self.change_provider_route(path, action="link")
            return
        if path == "/v1/account/saved-tags":
            self.add_saved_tag_route()
            return
        if path == "/v1/account/groups":
            self.create_group_route()
            return
        if path.startswith("/v1/players/") and path.endswith("/verifytoken"):
            self.verify_token_route(path)
            return
        if path.startswith("/v1/players/") and path.endswith("/refresh"):
            tag = normalize_tag(path[len("/v1/players/") : -len("/refresh")])
            if tag is None or spec_for(tag) is None:
                self.send_json(404, {"error": "missing"})
                return
            if tag == "#2PV" or os.environ.get("CLASHLENS_FIXTURE_REFRESH_MODE") == "unavailable":
                self.send_json(503, {"error": "unavailable"})
                return
            idempotency_key = self.headers.get("X-ClashLens-Request-Id")
            if self.verified_body:
                try:
                    payload = json.loads(self.verified_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.send_json(400, {"error": "invalid_input"})
                    return
                idempotency_key = payload.get("idempotency_key") if isinstance(payload, dict) else None
            if not isinstance(idempotency_key, str) or not UUID_PATTERN.fullmatch(idempotency_key):
                self.send_json(400, {"error": "invalid_input"})
                return
            work_id, _job = refresh_work(tag, idempotency_key)
            work = {
                "refresh_id": work_id,
                "tag": tag,
                "status": "pending",
                "outcome": "created",
            }
            self.send_json(202, work)
            return
        self.send_json(404, {"error": "missing"})

    def do_PATCH(self):
        if not self.verify_request():
            return
        path = unquote(urlsplit(self.path).path)
        if path == "/v1/account":
            self.update_account_route()
            return
        if path.startswith("/v1/account/groups/"):
            self.update_group_route(path)
            return
        self.send_json(404, {"error": "missing"})

    def do_DELETE(self):
        if not self.verify_request():
            return
        path = unquote(urlsplit(self.path).path)
        if path.startswith("/v1/account/providers/"):
            self.change_provider_route(path, action="unlink")
            return
        if path.startswith("/v1/account/saved-tags/"):
            self.remove_saved_tag_route(path)
            return
        if path.startswith("/v1/account/groups/"):
            self.delete_group_route(path)
            return
        self.send_json(404, {"error": "missing"})

    def read_body(self):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            return None
        if length < 0 or length > 1_048_576:
            return None
        return self.rfile.read(length)

    def verify_request(self):
        body = self.read_body()
        if body is None:
            self.send_json(413, {"error": "invalid_input"})
            return False
        self.verified_body = body
        try:
            names = [
                "X-ClashLens-Proof-Version",
                "X-ClashLens-Caller",
                "X-ClashLens-Key-Id",
                "X-ClashLens-Issued-At",
                "X-ClashLens-Expires-At",
                "X-ClashLens-Request-Id",
                "X-ClashLens-Provider",
                "X-ClashLens-Provider-Subject",
                "X-ClashLens-Signature",
            ]
            values = {}
            for name in names:
                headers = self.headers.get_all(name) or []
                if len(headers) != 1:
                    raise ValueError("proof header count")
                values[name] = headers[0]
            if values["X-ClashLens-Proof-Version"] != PROOF_VERSION:
                raise ValueError("proof version")
            caller = decode_text(values["X-ClashLens-Caller"])
            key_id = decode_text(values["X-ClashLens-Key-Id"])
            provider = decode_text(values["X-ClashLens-Provider"])
            provider_subject = decode_text(values["X-ClashLens-Provider-Subject"])
            if not caller or not key_id or bool(provider) != bool(provider_subject):
                raise ValueError("proof identity")
            if caller != configured_identity("CLASHLENS_FIXTURE_HMAC_CALLER", "typescript-website"):
                raise ValueError("proof caller")
            if key_id != configured_identity("CLASHLENS_FIXTURE_HMAC_KEY_ID", "current"):
                raise ValueError("proof key")
            if provider and provider not in ALLOWED_PROVIDERS:
                raise ValueError("proof provider")
            if values["X-ClashLens-Caller"] != b64url(caller.encode("utf-8")):
                raise ValueError("caller encoding")
            issued = values["X-ClashLens-Issued-At"]
            expires = values["X-ClashLens-Expires-At"]
            request_id = values["X-ClashLens-Request-Id"]
            if not UUID_PATTERN.fullmatch(request_id):
                raise ValueError("proof request ID")
            if not re.fullmatch(r"0|[1-9][0-9]*", issued) or not re.fullmatch(r"0|[1-9][0-9]*", expires):
                raise ValueError("proof time")
            issued_int = int(issued)
            expires_int = int(expires)
            now = int(time.time())
            if not 1 <= expires_int - issued_int <= 30 or not issued_int - 5 <= now <= expires_int + 5:
                raise ValueError("proof window")
            target_bytes = self.path.encode("ascii")
            input_bytes = "\n".join(
                [
                    PROOF_VERSION,
                    "caller:" + values["X-ClashLens-Caller"],
                    "key-id:" + values["X-ClashLens-Key-Id"],
                    "audience:" + AUDIENCE,
                    "method:" + self.command,
                    "target:" + b64url(target_bytes),
                    "body-sha256:" + hashlib.sha256(body).hexdigest(),
                    "issued-at:" + issued,
                    "expires-at:" + expires,
                    "request-id:" + values["X-ClashLens-Request-Id"],
                    "provider:" + values["X-ClashLens-Provider"],
                    "provider-subject:" + values["X-ClashLens-Provider-Subject"],
                ]
            ).encode("ascii")
            signature = b64url(hmac.new(configured_key(), input_bytes, hashlib.sha256).digest())
            if not hmac.compare_digest(signature, values["X-ClashLens-Signature"]):
                raise ValueError("proof signature")
        except (UnicodeError, ValueError, TypeError):
            self.send_json(401, {"error": "forbidden"})
            return False
        self.verified_provider = provider
        self.verified_provider_subject = provider_subject
        self.verified_request_id = values["X-ClashLens-Request-Id"]
        return True

    def is_loopback(self):
        return self.client_address[0] in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

    def account_context(self, allow_unresolved=False):
        provider = getattr(self, "verified_provider", "")
        subject = getattr(self, "verified_provider_subject", "")
        if provider not in ALLOWED_PROVIDERS or not subject:
            return None, None, "caller_operation_not_authorized"
        with STATE["lock"]:
            for account in STATE["accounts"].values():
                if account["identities"].get(provider) == subject:
                    return account, subject, None
        if allow_unresolved:
            return None, subject, None
        return None, subject, "account_not_found"

    def json_body(self):
        content_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            return None
        try:
            payload = json.loads(self.verified_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def replay_or_none(self, request_id, binding):
        replayed = lookup_replay(request_id, binding)
        if replayed is None:
            return None
        self.send_json(*replayed)
        return replayed

    def change_provider_route(self, path, action):
        provider = path[len("/v1/account/providers/") :]
        if provider not in ALLOWED_PROVIDERS:
            self.send_json(404, {"error": "provider_not_found"})
            return
        account, subject, error = self.account_context()
        if error is not None:
            self.send_json(403, {"error": error})
            return
        if account is None or subject is None:
            self.send_json(403, {"error": "account_not_found"})
            return
        payload = self.json_body()
        if not isinstance(payload, dict):
            self.send_json(422, {"error": "invalid_request"})
            return
        new_subject = payload.get("provider_subject")
        if (
            not isinstance(new_subject, str)
            or not 1 <= len(new_subject) <= 255
        ):
            self.send_json(422, {"error": "invalid_request"})
            return
        binding = (subject, f"providers.{action}", action.upper(), path)
        if self.replay_or_none(self.verified_request_id, binding) is not None:
            return
        with STATE["lock"]:
            for other in STATE["accounts"].values():
                if other is account:
                    continue
                if other["identities"].get(provider) == new_subject and action == "link":
                    # A collision never merges or moves identities.
                    result = (409, {"error": "provider_identity_conflict"})
                    store_replay(self.verified_request_id, binding, *result)
                    self.send_json(*result)
                    return
            providers = list(account["providers"])
            if action == "link":
                if provider not in providers:
                    account["identities"][provider] = new_subject
                    providers.append(provider)
                    providers.sort()
                    account["providers"] = providers
                elif account["identities"].get(provider) != new_subject:
                    # The account already holds this provider with another subject.
                    result = (409, {"error": "provider_identity_conflict"})
                    store_replay(self.verified_request_id, binding, *result)
                    self.send_json(*result)
                    return
                else:
                    pass  # Idempotent relink of the same identity.
                result = (200, {"providers": list(providers)})
            else:
                if (
                    provider not in providers
                    or account["identities"].get(provider) != new_subject
                ):
                    result = (404, {"error": "provider_not_linked"})
                    store_replay(self.verified_request_id, binding, *result)
                    self.send_json(*result)
                    return
                if len(providers) <= 1:
                    result = (409, {"error": "final_provider"})
                    store_replay(self.verified_request_id, binding, *result)
                    self.send_json(*result)
                    return
                providers.remove(provider)
                account["providers"] = providers
                account["identities"].pop(provider, None)
                result = (200, {"providers": list(providers)})
        store_replay(self.verified_request_id, binding, *result)
        self.send_json(*result)

    def create_account_route(self):
        _account, subject, error = self.account_context(allow_unresolved=True)
        if error is not None:
            self.send_json(403, {"error": error})
            return
        if subject is None:
            self.send_json(403, {"error": "caller_operation_not_authorized"})
            return
        binding = (subject, "account.create", "POST", "/v1/account")
        if self.replay_or_none(self.verified_request_id, binding) is not None:
            return
        if subject in STATE["accounts"]:
            self.send_json(409, {"error": "account_exists"})
            return
        payload = self.json_body()
        if payload is None:
            self.send_json(422, {"error": "invalid_request"})
            return
        username = normalize_account_username(payload.get("username", ""))
        display_name = normalize_account_name(payload.get("display_name", ""))
        if username is None or display_name is None:
            self.send_json(422, {"error": "invalid_request"})
            return
        with STATE["lock"]:
            owner = STATE["accounts_by_username"].get(username)
            if owner is not None:
                self.send_json(409, {"error": "username_unavailable"})
                return
            STATE["accounts"][subject] = {
                "username": username,
                "display_name": display_name,
                "preferences": {},
                "providers": [self.verified_provider],
                "identities": {self.verified_provider: subject},
            }
            STATE["accounts_by_username"][username] = subject
            STATE["saved_tags"].setdefault(subject, [])
            STATE["groups"].setdefault(subject, {})
            STATE["verified_players"].setdefault(subject, [])
            created = account_payload(STATE["accounts"][subject])
        store_replay(self.verified_request_id, binding, 201, created)
        self.send_json(201, created)

    def update_account_route(self):
        account, subject, error = self.account_context()
        if error is not None:
            self.send_json(403, {"error": error})
            return
        if account is None or subject is None:
            self.send_json(403, {"error": "account_not_found"})
            return
        binding = (subject, "account.update", "PATCH", "/v1/account")
        if self.replay_or_none(self.verified_request_id, binding) is not None:
            return
        payload = self.json_body()
        if payload is None:
            self.send_json(422, {"error": "invalid_request"})
            return
        username = normalize_account_username(payload.get("username", ""))
        display_name = normalize_account_name(payload.get("display_name", ""))
        preferences = payload.get("preferences")
        if (
            username is None
            or display_name is None
            or not isinstance(preferences, dict)
        ):
            self.send_json(422, {"error": "invalid_request"})
            return
        if len(json.dumps(preferences, separators=(",", ":")).encode("utf-8")) > 4096:
            self.send_json(422, {"error": "invalid_request"})
            return
        with STATE["lock"]:
            owner = STATE["accounts_by_username"].get(username)
            if owner is not None and owner != subject:
                self.send_json(409, {"error": "username_unavailable"})
                return
            old_username = account["username"]
            if old_username != username:
                STATE["accounts_by_username"].pop(old_username, None)
            account["username"] = username
            account["display_name"] = display_name
            account["preferences"] = preferences
            STATE["accounts_by_username"][username] = subject
            updated = account_payload(account)
        store_replay(self.verified_request_id, binding, 200, updated)
        self.send_json(200, updated)

    def add_saved_tag_route(self):
        _account, subject, error = self.account_context()
        if error is not None:
            self.send_json(403, {"error": error})
            return
        binding = (subject, "saved_tags.add", "POST", "/v1/account/saved-tags")
        if self.replay_or_none(self.verified_request_id, binding) is not None:
            return
        payload = self.json_body()
        if payload is None:
            self.send_json(422, {"error": "invalid_request"})
            return
        tag = normalize_tag(payload.get("tag", ""))
        if tag is None:
            self.send_json(422, {"error": "invalid_tag"})
            return
        with STATE["lock"]:
            saved = STATE["saved_tags"].setdefault(subject, [])
            if tag not in saved:
                if len(saved) >= MAX_SAVED_TAGS_PER_ACCOUNT:
                    self.send_json(409, {"error": "limit_exceeded"})
                    return
                saved.append(tag)
        store_replay(self.verified_request_id, binding, 200, {"tag": tag, "saved": True})
        self.send_json(200, {"tag": tag, "saved": True})

    def remove_saved_tag_route(self, path):
        tag = normalize_tag(path[len("/v1/account/saved-tags/") :])
        if tag is None:
            self.send_json(422, {"error": "invalid_tag"})
            return
        _account, subject, error = self.account_context()
        if error is not None:
            self.send_json(403, {"error": error})
            return
        binding = (subject, "saved_tags.remove", "DELETE", f"/v1/account/saved-tags/{tag}")
        if self.replay_or_none(self.verified_request_id, binding) is not None:
            return
        with STATE["lock"]:
            saved = STATE["saved_tags"].get(subject, [])
            if tag in saved:
                saved.remove(tag)
        store_replay(
            self.verified_request_id, binding, 200, {"tag": tag, "saved": False}
        )
        self.send_json(200, {"tag": tag, "saved": False})

    def create_group_route(self):
        _account, subject, error = self.account_context()
        if error is not None:
            self.send_json(403, {"error": error})
            return
        binding = (subject, "groups.create", "POST", "/v1/account/groups")
        if self.replay_or_none(self.verified_request_id, binding) is not None:
            return
        payload = self.json_body()
        if payload is None:
            self.send_json(422, {"error": "invalid_request"})
            return
        name = normalize_account_name(payload.get("name", ""))
        tags = normalize_group_tags(payload.get("tags"))
        if name is None or tags is None:
            self.send_json(422, {"error": "invalid_request"})
            return
        with STATE["lock"]:
            groups = STATE["groups"].setdefault(subject, {})
            if len(groups) >= MAX_GROUPS_PER_ACCOUNT:
                self.send_json(409, {"error": "limit_exceeded"})
                return
            if any(group["name"].casefold() == name.casefold() for group in groups.values()):
                self.send_json(409, {"error": "group_name_conflict"})
                return
            group_id = str(uuid.uuid4())
            groups[group_id] = {"name": name, "tags": tags}
            created = {"group_id": group_id, "name": name, "tags": list(tags)}
        store_replay(self.verified_request_id, binding, 201, created)
        self.send_json(201, created)

    def update_group_route(self, path):
        group_id = path[len("/v1/account/groups/") :]
        if not UUID_PATTERN.fullmatch(group_id):
            self.send_json(422, {"error": "invalid_request"})
            return
        _account, subject, error = self.account_context()
        if error is not None:
            self.send_json(403, {"error": error})
            return
        binding = (subject, "groups.update", "PATCH", f"/v1/account/groups/{group_id}")
        if self.replay_or_none(self.verified_request_id, binding) is not None:
            return
        payload = self.json_body()
        if payload is None:
            self.send_json(422, {"error": "invalid_request"})
            return
        name = normalize_account_name(payload.get("name", ""))
        tags = normalize_group_tags(payload.get("tags"))
        if name is None or tags is None:
            self.send_json(422, {"error": "invalid_request"})
            return
        with STATE["lock"]:
            groups = STATE["groups"].setdefault(subject, {})
            group = groups.get(group_id)
            if group is None:
                self.send_json(404, {"error": "group_not_found"})
                return
            if any(
                other_id != group_id and other["name"].casefold() == name.casefold()
                for other_id, other in groups.items()
            ):
                self.send_json(409, {"error": "group_name_conflict"})
                return
            group["name"] = name
            group["tags"] = tags
            updated = {"group_id": group_id, "name": name, "tags": list(tags)}
        store_replay(self.verified_request_id, binding, 200, updated)
        self.send_json(200, updated)

    def delete_group_route(self, path):
        group_id = path[len("/v1/account/groups/") :]
        if not UUID_PATTERN.fullmatch(group_id):
            self.send_json(422, {"error": "invalid_request"})
            return
        _account, subject, error = self.account_context()
        if error is not None:
            self.send_json(403, {"error": error})
            return
        binding = (subject, "groups.delete", "DELETE", f"/v1/account/groups/{group_id}")
        if self.replay_or_none(self.verified_request_id, binding) is not None:
            return
        with STATE["lock"]:
            groups = STATE["groups"].get(subject, {})
            if group_id not in groups:
                self.send_json(404, {"error": "group_not_found"})
                return
            del groups[group_id]
            deleted = {"deleted": True, "group_id": group_id}
        store_replay(self.verified_request_id, binding, 200, deleted)
        self.send_json(200, deleted)

    def verify_token_route(self, path):
        tag = normalize_tag(path[len("/v1/players/") : -len("/verifytoken")])
        if tag is None:
            self.send_json(422, {"error": "invalid_tag"})
            return
        _account, subject, error = self.account_context()
        if error is not None:
            self.send_json(403, {"error": error})
            return
        binding = (subject, "player_links.verify", "POST", f"/v1/players/{tag}/verifytoken")
        if self.replay_or_none(self.verified_request_id, binding) is not None:
            return
        if tag not in {spec[0] for spec in PLAYER_SPECS}:
            self.send_json(404, {"error": "missing"})
            return
        content_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            self.send_json(422, {"error": "invalid_request"})
            return
        try:
            payload = json.loads(self.verified_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(422, {"error": "invalid_request"})
            return
        if (
            not isinstance(payload, dict)
            or set(payload) != {"token"}
            or not isinstance(payload["token"], str)
        ):
            self.send_json(422, {"error": "invalid_request"})
            return
        token = payload["token"]
        if (
            not 1 <= len(token) <= MAX_VERIFICATION_TOKEN_LENGTH
            or not token.isascii()
            or any(character.isspace() or not character.isprintable() for character in token)
        ):
            self.send_json(422, {"error": "invalid_request"})
            return
        with STATE["lock"]:
            if token == FIXTURE_VERIFY_UNAVAILABLE_TOKEN:
                result = (503, {"status": "verification_unavailable", "tag": tag})
            elif token == FIXTURE_VERIFY_PREFIX + tag[1:]:
                owner = STATE["verified_owner"].get(tag)
                if owner == subject:
                    result = (200, {"status": "already_linked", "tag": tag})
                elif owner is None:
                    STATE["verified_players"].setdefault(subject, []).append(tag)
                    STATE["verified_owner"][tag] = subject
                    result = (200, {"status": "linked", "tag": tag})
                else:
                    result = (
                        409,
                        {
                            "status": "support_required",
                            "tag": tag,
                            "verification_request_id": self.verified_request_id,
                        },
                    )
            else:
                result = (401, {"status": "invalid_token", "tag": tag})
            store_replay(self.verified_request_id, binding, result[0], result[1])
        self.send_json(*result)

    def send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Clash Lens deterministic Python fixture")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    arguments = parser.parse_args()
    reset_state()
    server = ReusableThreadingHTTPServer((arguments.host, arguments.port), FixtureHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
