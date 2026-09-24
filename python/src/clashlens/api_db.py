from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import BoundedSemaphore, Lock
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .catalog import catalog_name
from .operating import database_pool_health
from .profile import normalize_player_tag

API_CONTRACT_VERSION = 2
VERIFICATION_RESERVATION_SECONDS = 45
PLAYER_SCREEN_READY_VERSION = "api-player-daily-log-v3"
ARMY_ANALYTICS_CACHE_CAPACITY = 128
ARMY_ANALYTICS_QUERY_WORK_MEM = "256MB"
ARMY_ANALYTICS_REQUEST_TIMEOUT_SECONDS = 5.0
ARMY_ANALYTICS_ADMISSION_TIMEOUT_SECONDS = 0.1
ARMY_ANALYTICS_PRIMARY_POOL_TIMEOUT_SECONDS = 0.1
ARMY_ANALYTICS_PARALLEL_POOL_TIMEOUT_SECONDS = 0.3


@dataclass(frozen=True, slots=True)
class AccountContext:
    internal_id: int
    public_id: str
    username: str
    display_name: str


@dataclass(frozen=True, slots=True)
class RequestBinding:
    request_id: str
    caller: str
    provider: str
    provider_subject: str
    account_id: int | None
    operation: str
    method: str
    request_target: str
    identity: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OperationResult:
    status_code: int
    payload: dict[str, Any]
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class PermitResult:
    granted: bool
    reason: str


@dataclass(frozen=True, slots=True)
class VerificationReservation:
    fresh: bool
    result: OperationResult | None


class ApiDatabase:
    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        timeout_seconds: float = 5.0,
        army_cache_capacity: int = ARMY_ANALYTICS_CACHE_CAPACITY,
    ) -> None:
        if min_size < 0 or max_size < 1 or min_size > max_size:
            raise ValueError("API database pool bounds are invalid")
        if army_cache_capacity < 0:
            raise ValueError("army analytics cache capacity is invalid")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("API database pool timeout is invalid")
        self._army_cache_capacity = army_cache_capacity
        self._army_request_timeout_seconds = min(
            timeout_seconds, ARMY_ANALYTICS_REQUEST_TIMEOUT_SECONDS
        )
        self._army_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        self._army_cache_lock = Lock()
        self._army_troop_slots = BoundedSemaphore(max(1, max_size // 2))
        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout_seconds,
            open=True,
        )
        self._supports_content_dedup: bool | None = None

    def _current_profile_metadata(self, connection: Any) -> tuple[str, str]:
        if self._supports_content_dedup is None:
            self._supports_content_dedup = bool(
                connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'player_profile_effects'
                          AND column_name = 'endpoint_version'
                    )
                    """
                ).fetchone()[0]
            )
        if not self._supports_content_dedup:
            return "", (
                "profile.source_http_status, profile.endpoint_version, "
                "profile.schema_version, profile.parser_version"
            )
        return (
            """
                LEFT JOIN LATERAL (
                    SELECT effect.source_http_status, effect.endpoint_version,
                           effect.schema_version, effect.parser_version
                    FROM player_profile_effects AS effect
                    WHERE effect.profile_version_id = profile.id
                      AND effect.effect_kind = 'current_profile'
                    ORDER BY effect.observed_at DESC, effect.id DESC
                    LIMIT 1
                ) AS current_effect ON true
            """,
            (
                "COALESCE(current_effect.source_http_status, profile.source_http_status), "
                "COALESCE(current_effect.endpoint_version, profile.endpoint_version), "
                "COALESCE(current_effect.schema_version, profile.schema_version), "
                "COALESCE(current_effect.parser_version, profile.parser_version)"
            ),
        )

    def close(self) -> None:
        self.pool.close()

    def pool_health(self) -> dict[str, int]:
        return database_pool_health(self.pool)

    def _army_cache_get(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        if not self._army_cache_capacity:
            return None
        with self._army_cache_lock:
            result = self._army_cache.pop(key, None)
            if result is None:
                return None
            self._army_cache[key] = result
            return deepcopy(result)

    def _army_cache_put(self, key: tuple[Any, ...], result: dict[str, Any]) -> None:
        if not self._army_cache_capacity:
            return
        with self._army_cache_lock:
            self._army_cache[key] = deepcopy(result)
            self._army_cache.move_to_end(key)
            while len(self._army_cache) > self._army_cache_capacity:
                self._army_cache.popitem(last=False)

    def is_ready(
        self, *, expected_contract_version: int = API_CONTRACT_VERSION
    ) -> bool:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT version,
                       to_regclass('clash_lens_accounts') IS NOT NULL,
                       to_regclass('private_api_requests') IS NOT NULL,
                       to_regclass('shared_api_credentials') IS NOT NULL
                FROM clash_lens_contract
                WHERE singleton = true
                """
            ).fetchone()
            return bool(
                row is not None
                and int(row[0]) >= expected_contract_version
                and row[1]
                and row[2]
                and row[3]
            )

    def scalar(self, query: str, params: Iterable[Any] = ()) -> Any:
        with self.pool.connection() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
            return None if row is None else _text(row[0])


def lookup_request(database: ApiDatabase, binding: RequestBinding) -> OperationResult | None:
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT caller, provider, provider_subject, account_id, operation,
                   method, request_target, identity_json, state,
                   response_status, response_json
            FROM private_api_requests
            WHERE request_id = %s
            """,
            (binding.request_id,),
        ).fetchone()
        if row is None:
            return None
        expected = (
            binding.caller,
            binding.provider,
            binding.provider_subject,
            binding.account_id,
            binding.operation,
            binding.method,
            binding.request_target,
            binding.identity,
        )
        actual = tuple(_text(value) for value in row[:8])
        if actual != expected:
            return OperationResult(
                409, {"error": "request_id_conflict"}, replayed=True
            )
        if _text(row[8]) != "complete":
            return OperationResult(202, {"status": "in_progress"}, replayed=True)
        return OperationResult(int(row[9]), dict(row[10]), replayed=True)


def _reserve_request(
    database: ApiDatabase,
    connection: Any,
    binding: RequestBinding,
    *,
    recover_expired_verification: bool = False,
) -> OperationResult | None:
    inserted = connection.execute(
        """
        INSERT INTO private_api_requests (
            request_id, caller, provider, provider_subject, account_id,
            operation, method, request_target, identity_json, state,
            in_progress_until
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, 'in_progress',
            clock_timestamp() + make_interval(secs => %s)
        )
        ON CONFLICT (request_id) DO NOTHING
        RETURNING request_id
        """,
        (
            binding.request_id,
            binding.caller,
            binding.provider,
            binding.provider_subject,
            binding.account_id,
            binding.operation,
            binding.method,
            binding.request_target,
            Jsonb(binding.identity),
            VERIFICATION_RESERVATION_SECONDS,
        ),
    ).fetchone()
    if inserted is not None:
        return None
    row = connection.execute(
        """
        SELECT caller, provider, provider_subject, account_id, operation,
               method, request_target, identity_json, state,
               response_status, response_json, in_progress_until
        FROM private_api_requests
        WHERE request_id = %s
        FOR UPDATE
        """,
        (binding.request_id,),
    ).fetchone()
    assert row is not None
    expected = (
        binding.caller,
        binding.provider,
        binding.provider_subject,
        binding.account_id,
        binding.operation,
        binding.method,
        binding.request_target,
        binding.identity,
    )
    actual = tuple(_text(value) for value in row[:8])
    if actual != expected:
        return OperationResult(409, {"error": "request_id_conflict"}, replayed=True)
    if _text(row[8]) != "complete":
        if (
            recover_expired_verification
            and _text(row[8]) == "in_progress"
            and row[11] is not None
            and row[11]
            <= connection.execute("SELECT clock_timestamp()").fetchone()[0]
        ):
            tag_row = connection.execute(
                """
                SELECT player.normalized_tag
                FROM player_link_verification_audits AS audit
                JOIN players AS player ON player.id = audit.player_id
                WHERE audit.request_id = %s AND audit.outcome = 'pending'
                FOR UPDATE OF audit
                """,
                (binding.request_id,),
            ).fetchone()
            if tag_row is not None:
                result = OperationResult(
                    503,
                    {
                        "status": "verification_unavailable",
                        "tag": _text(tag_row[0]),
                    },
                )
                updated = connection.execute(
                    """
                    UPDATE player_link_verification_audits
                    SET outcome = 'verification_unavailable',
                        completed_at = clock_timestamp()
                    WHERE request_id = %s AND outcome = 'pending'
                    """,
                    (binding.request_id,),
                )
                if updated.rowcount == 1:
                    _complete_request(connection, binding.request_id, result)
                    return result
        return OperationResult(202, {"status": "in_progress"}, replayed=True)
    return OperationResult(
        int(row[9]),
        dict(row[10]),
        replayed=True,
    )


def _assert_request_binding(connection: Any, binding: RequestBinding) -> None:
    row = connection.execute(
        """
        SELECT caller, provider, provider_subject, account_id, operation,
               method, request_target, identity_json, state
        FROM private_api_requests
        WHERE request_id = %s
        FOR UPDATE
        """,
        (binding.request_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("private API request reservation is missing")
    expected = (
        binding.caller,
        binding.provider,
        binding.provider_subject,
        binding.account_id,
        binding.operation,
        binding.method,
        binding.request_target,
        binding.identity,
    )
    actual = tuple(_text(value) for value in row[:8])
    if actual != expected or _text(row[8]) != "in_progress":
        raise RuntimeError("private API request binding is not active")


def _complete_request(
    connection: Any,
    request_id: str,
    result: OperationResult,
) -> None:
    updated = connection.execute(
        """
        UPDATE private_api_requests
        SET state = 'complete', response_status = %s, response_json = %s,
            in_progress_until = NULL, completed_at = clock_timestamp()
        WHERE request_id = %s AND state = 'in_progress'
        """,
        (result.status_code, Jsonb(result.payload), request_id),
    )
    if updated.rowcount != 1:
        raise RuntimeError("private API request reservation was lost")


def _ensure_player(connection: Any, normalized_tag: str) -> int:
    row = connection.execute(
        """
        INSERT INTO players (normalized_tag, active)
        VALUES (%s, false)
        ON CONFLICT (normalized_tag) DO UPDATE
            SET normalized_tag = EXCLUDED.normalized_tag
        RETURNING id
        """,
        (normalized_tag,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _account_context(row: Any) -> AccountContext:
    return AccountContext(
        internal_id=int(row[0]),
        public_id=str(row[1]),
        username=_text(row[2]),
        display_name=_text(row[3]),
    )


def _text(value: Any) -> Any:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _public_confidence(active: bool, eligibility_state: str) -> str:
    if eligibility_state == "eligible":
        return "high"
    if eligibility_state == "uncertain":
        return "uncertain"
    return "partial" if active else "uncertain"


def _public_snapshot_confidence(value: str) -> str:
    if value in {"exact", "confirmed", "high"}:
        return "high"
    if value in {"inferred", "partial"}:
        return "partial"
    return "uncertain"


def _public_army(row: Any) -> dict[str, Any]:
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
                    "name": catalog_name(typed_id) or typed_id,
                    "quantity": quantity,
                    "origin": origin,
                }
            )
        return result

    status = _text(row[2])
    components = []
    for value in row[4:8]:
        components.extend(facts(value))
    if isinstance(row[8], list):
        for hero in row[8]:
            if not isinstance(hero, Mapping):
                continue
            for typed_id in [
                hero.get("hero"),
                hero.get("pet"),
                *(hero.get("equipment") or []),
            ]:
                if isinstance(typed_id, str) and (name := catalog_name(typed_id)):
                    components.append(
                        {
                            "typed_id": typed_id,
                            "name": name,
                            "quantity": 1,
                            "origin": "hero",
                        }
                    )
    unknown = row[9] if isinstance(row[9], list) else []
    return {
        "state": status,
        "failure_reason": _text(row[3]) if row[3] is not None else None,
        "components": components,
        "unknown_components": unknown,
        "decoder_version": _text(row[10]),
        "catalog_version": _text(row[11]),
    }


def _screen_daily_log(day: dict[str, Any], profile_confidence: str) -> dict[str, Any]:
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
        "public_confidence": _public_snapshot_confidence(
            day["confidence"] if day["confidence"] is not None else profile_confidence
        ),
        "uncertainty_reasons": reasons,
    }


def _screen_daily_log_with_events(
    day: dict[str, Any], profile_confidence: str
) -> dict[str, Any]:
    screen_day = _screen_daily_log(day, profile_confidence)
    offense_events, defense_events = _screen_events(day.get("battles"))
    screen_day["offense_events"] = offense_events
    screen_day["defense_events"] = defense_events
    return screen_day


def _screen_events(
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
        parsed = _screen_event(battle)
        if parsed is None:
            continue
        lens, battle_id, timestamp, event = parsed
        candidates.append(
            (lens, timestamp, _battle_id_sort_key(battle_id), index, event)
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
    return offense, defense


def _screen_event(
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

    timestamp = _screen_event_timestamp(battle.get("battle_timestamp"))
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

    stars = _screen_event_int(battle.get("stars"), lower=0, upper=3)
    destruction = _screen_event_int(
        battle.get("destruction_percentage"), lower=0, upper=100
    )
    if stars is None or destruction is None:
        return None
    trophy_value = _screen_event_trophy_value(battle, lens)
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
    if isinstance(battle.get("army"), Mapping):
        event["army"] = battle["army"]
    return lens, battle_id, timestamp, event


def _screen_event_timestamp(value: Any) -> datetime | None:
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


def _screen_event_int(value: Any, *, lower: int, upper: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if lower <= value <= upper else None


def _screen_event_trophy_value(battle: Mapping[str, Any], lens: str) -> int | None:
    value = battle.get("trophy_change")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if (lens == "offense" and value < 0) or (lens == "defense" and value > 0):
        return None
    return value


def _battle_id_sort_key(value: str) -> tuple[int, Any]:
    try:
        return 0, int(value)
    except ValueError:
        return 1, value


def _daily_log(day: Any) -> dict[str, Any]:
    return {
        "ranked_day_start": day[0].astimezone(UTC).isoformat(),
        "ranked_day_end": None
        if day[1] is None
        else day[1].astimezone(UTC).isoformat(),
        "official_season_id": None if day[2] is None else _text(day[2]),
        "season_day_number": None if day[3] is None else int(day[3]),
        "version": int(day[4]),
        "state": _text(day[5]),
        "coverage": _text(day[6]),
        "confidence": None if day[7] is None else _text(day[7]),
        "attack_count": None if day[8] is None else int(day[8]),
        "attack_three_star_count": None if day[9] is None else int(day[9]),
        "attack_gain": None if day[10] is None else int(day[10]),
        "defense_count": None if day[11] is None else int(day[11]),
        "defense_three_star_count": None if day[12] is None else int(day[12]),
        "defense_loss": None if day[13] is None else int(day[13]),
        "net_trophy_change": None if day[14] is None else int(day[14]),
        "adjustments": _json_array(day[15]),
        "battles": _json_array(day[16]),
        "partial_reasons": _json_array(day[17]),
        "start_trophies": None if day[18] is None else int(day[18]),
    }


def _json_array(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _historical_season_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Project a stored compact season summary to the public shape.

    The stored row is already the complete historical record; this only
    normalizes timestamps and preserves numeric values (never rendered
    text) so future comparisons can reuse them.
    """

    def _optional_int(value: Any) -> int | None:
        return None if value is None else int(value)

    def _iso(value: Any) -> str | None:
        return None if value is None else value.astimezone(UTC).isoformat()

    return {
        "kind": "player-season-summary",
        "tag": _text(record["normalized_tag"]),
        "official_season_id": _text(record["official_season_id"]),
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
        "coverage_state": _text(record["coverage_state"]),
        "unresolved_flags": [
            _text(flag) for flag in (record["unresolved_flags"] or [])
        ],
        "daily_entries": _json_array(record["daily_entries"]),
        "projection_version": _text(record["projection_version"]),
        "published_at": _iso(record["published_at"]),
    }
