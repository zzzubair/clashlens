#!/usr/bin/env python3
"""Deterministic private-API fixture for the website prototype.

This server is test-only. It uses screen-ready payloads and in-memory refresh work.
It does not call Supercell, PostgreSQL, or the real Python application.
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

STATE = {
    "jobs": {},
    "jobs_by_tag": {},
    "jobs_by_request": {},
    "refresh_counts": {},
    "refreshed": set(),
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


def freshness_for(age_seconds, state):
    return {
        "state": "stale" if state == "stale" else "unknown" if state == "uncertain" else "fresh",
        "observedAt": FIXTURE_OBSERVED_AT,
        "ageSeconds": age_seconds,
    }


def entry_for(spec, rank):
    tag, name, clan, trophies, age_seconds, state = spec
    return {
        "rank": rank,
        "tag": tag,
        "name": name,
        "clan": clan,
        "trophies": trophies,
        "freshness": freshness_for(age_seconds, state),
        "state": state,
        "confidence": "uncertain" if state == "uncertain" else "partial" if state == "stale" else "high",
        "officialRank": ((rank * 17 - 1) % 200) + 1 if rank <= 200 else None,
    }


def leaderboard(limit, view):
    limit = max(1, min(limit, len(PLAYER_SPECS)))
    entries = [entry_for(spec, index) for index, spec in enumerate(PLAYER_SPECS, 1)]
    return {
        "kind": "tracked-leaderboard",
        "view": view,
        "entries": entries[:limit],
        "totalTracked": len(PLAYER_SPECS),
        "coverage": {
            "state": "partial",
            "trackedPlayers": len(PLAYER_SPECS),
            "measuredPercent": 63.0,
            "note": "This fixture represents a measured tracked cohort, not the full Legend I population.",
        },
        "provenance": {
            "source": "deterministic-python-fixture",
            "observedAt": FIXTURE_OBSERVED_AT,
            "freshness": "fresh",
            "confidence": "partial",
            "coverage": "partial",
            "version": FIXTURE_VERSION,
        },
        "qualityStates": [
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


def day_for(tag, current):
    is_uncertain = tag in {"#2PP", "#2PY"}
    current_state = "Uncertain" if is_uncertain else "Live"
    return {
        "dayNumber": 24,
        "label": "Current Legend day",
        "period": "2026-08-05 05:00 UTC – 2026-08-06 05:00 UTC",
        "state": current_state,
        "offense": {"attacks": 7, "threeStars": 5, "trophyGain": 31},
        "defense": {"defenses": 6, "threeStarsAgainst": 2, "trophyLoss": 13},
        "trophyChange": 18,
        "completeness": {
            "state": "uncertain" if is_uncertain else "partial",
            "reason": "The paired battle-log evidence is not complete for this fixture observation.",
        },
        "uncertainty": [
            "Current day evidence is still being reconciled.",
            "This screen does not claim complete Legend I coverage.",
        ],
    }


def previous_day():
    return {
        "dayNumber": 23,
        "label": "Previous ranked day",
        "period": "2026-08-04 05:00 UTC – 2026-08-05 05:00 UTC",
        "state": "Complete",
        "offense": {"attacks": 8, "threeStars": 6, "trophyGain": 38},
        "defense": {"defenses": 8, "threeStarsAgainst": 3, "trophyLoss": 16},
        "trophyChange": 22,
        "completeness": {"state": "complete", "reason": "Fixture evidence reconciles this ended day."},
        "uncertainty": [],
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
    current = day_for(tag, refreshed)
    return {
        "kind": "player-page",
        "tag": tag,
        "profile": {
            "tag": tag,
            "name": name,
            "clan": clan,
            "trophies": trophies,
            "freshness": freshness_for(age_seconds, state),
            "confidence": "uncertain" if state == "uncertain" else "partial" if state == "stale" else "high",
            "coverage": "partial",
            "eligibility": "legend-i",
        },
        "season": {
            "id": "1783918800",
            "anchor": "2026-07-13T05:00:00Z",
            "currentDayNumber": 24,
            "dayCount": 28,
        },
        "currentDay": current,
        "recentDays": [current, previous_day()],
        "dataQuality": [
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
            "observedAt": FIXTURE_OBSERVED_AT if not refreshed else "2026-08-05T18:10:00Z",
            "freshness": "fresh" if refreshed else "stale" if state == "stale" else "fresh",
            "confidence": "uncertain" if state == "uncertain" else "partial",
            "coverage": "partial",
            "version": FIXTURE_VERSION,
        },
    }


def search_players(query):
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
                "freshness": result["freshness"],
                "state": result["state"],
                "context": "Exact normalized player tag",
            }
            for result in results
        ]
        return {"kind": "player-search", "query": query, "exactTag": exact_tag, "results": results, "knownOnly": False}
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
                        "freshness": result["freshness"],
                        "state": result["state"],
                        "context": "Known Clash Lens player",
                    }
                )
    return {"kind": "player-search", "query": query, "exactTag": None, "results": results, "knownOnly": True}


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
            work_id = f"refresh-{tag[1:].lower()}-{generation}"
            STATE["jobs_by_tag"][tag] = work_id
            now = time.monotonic()
            STATE["jobs"][work_id] = {
                "tag": tag,
                "polls": 0,
                "state": "queued",
                "publishedAt": None,
                "createdAt": now,
                "updatedAt": now,
            }
        STATE["jobs_by_request"][request_key] = work_id
        return work_id, STATE["jobs"][work_id]


def refresh_response(work_id, include_player=False):
    with STATE["lock"]:
        cleanup_jobs()
        job = STATE["jobs"].get(work_id)
        if job is None:
            return None
        if not include_player:
            job["polls"] += 1
        if job["state"] == "complete":
            state = "complete"
            progress = 100
            message = "Updated observation published."
        elif job["polls"] == 0:
            state = "queued"
            progress = 10
            message = "Refresh work accepted."
        elif job["polls"] == 1:
            state = "running"
            progress = 55
            message = "The saved observation is being processed."
        else:
            state = "complete"
            progress = 100
            message = "Updated observation published."
            job["publishedAt"] = "2026-08-05T18:10:00Z"
            STATE["refreshed"].add(job["tag"])
            STATE["refresh_counts"][job["tag"]] = STATE["refresh_counts"].get(job["tag"], 0) + 1
        job["state"] = state
        job["updatedAt"] = time.monotonic()
        page = player_page(job["tag"]) if state == "complete" else None
        return {
            "kind": "refresh-status" if not include_player else "refresh-work",
            "workId": work_id,
            "tag": job["tag"],
            "state": state,
            "progressPercent": progress,
            "message": message,
            "publishedAt": job["publishedAt"],
            "player": page,
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
        if path == "/v1/leaderboards/tracked":
            try:
                limit = int(query.get("limit", ["25"])[0])
            except ValueError:
                limit = 25
            self.send_json(200, leaderboard(limit, query.get("view", ["live"])[0]))
            return
        if path == "/v1/players/search":
            self.send_json(200, search_players(query.get("q", [""])[0]))
            return
        if path.startswith("/v1/players/"):
            tag = normalize_tag(path[len("/v1/players/") :])
            page = player_page(tag) if tag else None
            if page is None:
                self.send_json(404, {"error": "missing"})
            else:
                self.send_json(200, page)
            return
        if path.startswith("/v1/refresh/"):
            work_id = path[len("/v1/refresh/") :]
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
        self.send_json(404, {"error": "missing"})

    def do_POST(self):
        if not self.verify_request():
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path.startswith("/v1/players/") and path.endswith("/refresh"):
            tag = normalize_tag(path[len("/v1/players/") : -len("/refresh")])
            if tag is None or spec_for(tag) is None:
                self.send_json(404, {"error": "missing"})
                return
            if tag == "#2PV" or os.environ.get("CLASHLENS_FIXTURE_REFRESH_MODE") == "unavailable":
                self.send_json(503, {"error": "unavailable"})
                return
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
            work = refresh_response(work_id, include_player=True)
            self.send_json(202, work)
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
        return True

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
