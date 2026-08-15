from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .domain import DomainRuleError, allocate_trophies, ranked_day_for
from .profile import ProfileParseError, normalize_player_tag
from .source_observation_contract import BATTLE_LOG_SOURCE_OBSERVATION_CONTRACT

BATTLE_LOG_ENDPOINT_VERSION = BATTLE_LOG_SOURCE_OBSERVATION_CONTRACT.endpoint_version
BATTLE_LOG_SCHEMA_VERSION = BATTLE_LOG_SOURCE_OBSERVATION_CONTRACT.schema_version
LEGACY_SOURCE_PARSER_VERSION = "supercell-source-parser-v1"
LIVE_SOURCE_PARSER_VERSION = (
    BATTLE_LOG_SOURCE_OBSERVATION_CONTRACT.default_parser_version
)
# The endpoint envelope is unchanged, but the source row shape changed. Keep
# the v1 parser available for replaying old observations and use v2 for new
# live Legend I observations.
SOURCE_PARSER_VERSION = LIVE_SOURCE_PARSER_VERSION
SUPPORTED_SOURCE_PARSER_VERSIONS = (
    BATTLE_LOG_SOURCE_OBSERVATION_CONTRACT.supported_parser_versions
)


class BattleLogParseError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(f"{category}: {message}")
        self.category = category


@dataclass(frozen=True, slots=True)
class ParsedBattle:
    reporting_tag: str
    perspective: str
    attacker_tag: str
    defender_tag: str
    opponent_tag: str
    opponent_name: str | None
    battle_timestamp: datetime
    ranked_day_start: datetime
    stars: int
    destruction_percentage: int
    army_share_code: str
    reporter_trophies: int | None
    opponent_trophies: int | None
    attacker_gain: int
    defender_loss: int
    trophy_rule_version: str


@dataclass(frozen=True, slots=True)
class ParsedBattleRow:
    source_row_index: int
    outcome: str
    source_json: dict[str, Any] | Any
    battle: ParsedBattle | None = None
    failure_category: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedBattleLog:
    normalized_tag: str
    observed_at: datetime
    row_count: int
    rows: tuple[ParsedBattleRow, ...]
    has_row_gap: bool
    endpoint_version: str
    schema_version: str
    parser_version: str


def parse_battle_log(
    body: bytes,
    *,
    expected_tag: str,
    observed_at: datetime,
    parser_version: str = SOURCE_PARSER_VERSION,
    endpoint_version: str = BATTLE_LOG_ENDPOINT_VERSION,
) -> ParsedBattleLog:
    if parser_version not in SUPPORTED_SOURCE_PARSER_VERSIONS:
        raise BattleLogParseError(
            "unsupported_parser_version", "battle-log parser version is not installed"
        )
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise BattleLogParseError(
            "invalid_observation_time", "observation time must include a UTC offset"
        )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BattleLogParseError(
            "malformed_json", "battle-log body is not valid JSON"
        ) from error
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items = payload["items"]
    else:
        raise BattleLogParseError(
            "unsupported_battle_log_schema",
            "battle-log JSON must be an array or an object with an items array",
        )

    try:
        reporting_tag = normalize_player_tag(expected_tag)
    except ProfileParseError as error:
        raise BattleLogParseError(
            error.category, "observation player tag is invalid"
        ) from error

    rows = tuple(
        _parse_row(index, item, reporting_tag, parser_version)
        for index, item in enumerate(items)
    )
    return ParsedBattleLog(
        normalized_tag=reporting_tag,
        observed_at=observed_at.astimezone(UTC),
        row_count=len(items),
        rows=rows,
        has_row_gap=any(row.outcome == "malformed_legend_row" for row in rows),
        endpoint_version=endpoint_version,
        schema_version=BATTLE_LOG_SCHEMA_VERSION,
        parser_version=parser_version,
    )


def _parse_row(
    index: int,
    source: Any,
    reporting_tag: str,
    parser_version: str,
) -> ParsedBattleRow:
    if not isinstance(source, dict):
        return _gap(
            index, source, "unsupported_legend_row", "battle row is not an object"
        )
    if source.get("battleType") != "legend":
        return ParsedBattleRow(
            source_row_index=index,
            outcome="ignored_non_legend",
            source_json=source,
        )
    try:
        is_attacker = _parse_direction(source, parser_version)
        timestamp = _parse_battle_timestamp(source["battleTimestamp"], parser_version)
        stars = _required_int(source, "stars")
        destruction = _required_int(source, "destructionPercentage")
        army_share_code = source["armyShareCode"]
        if not isinstance(army_share_code, str) or not army_share_code:
            raise BattleLogParseError(
                "missing_army_share_code", "Legend I row must contain armyShareCode"
            )
        opponent_tag, opponent_name, opponent_trophies = _parse_opponent(
            source, parser_version
        )
        if opponent_tag == reporting_tag:
            raise BattleLogParseError(
                "identity_conflict", "reporting player and opponent must differ"
            )
        allocation = allocate_trophies(stars, destruction)
        attacker_tag, defender_tag = (
            (reporting_tag, opponent_tag)
            if is_attacker
            else (opponent_tag, reporting_tag)
        )
        day = ranked_day_for(timestamp)
        reporter_trophies = _optional_int(
            source.get("trophies", source.get("playerTrophies")), "reporter trophies"
        )
    except DomainRuleError as error:
        return _gap(index, source, error.category, str(error))
    except (BattleLogParseError, KeyError, TypeError) as error:
        category = (
            error.category
            if isinstance(error, BattleLogParseError)
            else "unsupported_legend_row"
        )
        return _gap(index, source, category, str(error))

    return ParsedBattleRow(
        source_row_index=index,
        outcome="valid_legend",
        source_json=source,
        battle=ParsedBattle(
            reporting_tag=reporting_tag,
            perspective="attacker" if is_attacker else "defender",
            attacker_tag=attacker_tag,
            defender_tag=defender_tag,
            opponent_tag=opponent_tag,
            opponent_name=opponent_name,
            battle_timestamp=timestamp,
            ranked_day_start=day.start,
            stars=stars,
            destruction_percentage=destruction,
            army_share_code=army_share_code,
            reporter_trophies=reporter_trophies,
            opponent_trophies=opponent_trophies,
            attacker_gain=allocation.attacker_gain,
            defender_loss=allocation.defender_loss,
            trophy_rule_version=allocation.rule_version,
        ),
    )


def _parse_direction(source: dict[str, Any], parser_version: str) -> bool:
    if parser_version == LIVE_SOURCE_PARSER_VERSION:
        direction = source.get("attack")
        if not isinstance(direction, bool):
            raise BattleLogParseError(
                "unsupported_perspective",
                "attack must be a boolean for battle-log parser v2",
            )
        return direction
    if parser_version == LEGACY_SOURCE_PARSER_VERSION:
        direction = source["attackOrDefense"]
        if direction not in {"attack", "defense"}:
            raise BattleLogParseError(
                "unsupported_perspective",
                "attackOrDefense must be attack or defense for battle-log parser v1",
            )
        return direction == "attack"
    raise BattleLogParseError(
        "unsupported_parser_version", "battle-log parser version is not installed"
    )


def _parse_opponent(
    source: dict[str, Any], parser_version: str
) -> tuple[str, str | None, int | None]:
    if parser_version == LIVE_SOURCE_PARSER_VERSION:
        tag_value = source.get("opponentPlayerTag")
        name = source.get("opponentName")
        trophies = source.get("opponentTrophies")
    elif parser_version == LEGACY_SOURCE_PARSER_VERSION:
        opponent = source["opponent"]
        if not isinstance(opponent, dict):
            raise BattleLogParseError(
                "invalid_opponent", "Legend I row must contain opponent data"
            )
        tag_value = opponent.get("tag")
        name = opponent.get("name")
        trophies = opponent.get("trophies")
    else:
        raise BattleLogParseError(
            "unsupported_parser_version", "battle-log parser version is not installed"
        )

    if not isinstance(tag_value, str):
        raise BattleLogParseError(
            "invalid_opponent", "Legend I opponent tag is invalid"
        )
    try:
        opponent_tag = normalize_player_tag(tag_value)
    except ProfileParseError as error:
        raise BattleLogParseError(
            "invalid_opponent", "Legend I opponent tag is invalid"
        ) from error
    if name is not None and not isinstance(name, str):
        raise BattleLogParseError(
            "invalid_opponent", "opponent name must be text when supplied"
        )
    opponent_trophies = _optional_int(trophies, "opponent trophies")
    return opponent_tag, name, opponent_trophies


def _parse_battle_timestamp(value: Any, parser_version: str) -> datetime:
    if not isinstance(value, str):
        raise BattleLogParseError(
            "invalid_battle_timestamp", "battleTimestamp must be text"
        )
    try:
        if parser_version == LEGACY_SOURCE_PARSER_VERSION:
            if value.endswith("Z") and "-" not in value:
                parsed = datetime.strptime(value, "%Y%m%dT%H%M%S.%fZ").replace(
                    tzinfo=UTC
                )
            else:
                parsed = datetime.fromisoformat(value)
        elif (
            len(value) >= 9
            and value[:8].isdigit()
            and value[8] == "T"
            and value.endswith("Z")
        ):
            compact = value[:-1]
            parsed = datetime.strptime(
                compact,
                "%Y%m%dT%H%M%S.%f" if "." in compact else "%Y%m%dT%H%M%S",
            ).replace(tzinfo=UTC)
        else:
            parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise BattleLogParseError(
            "invalid_battle_timestamp",
            f"battleTimestamp is not accepted by adapter "
            f"{parser_version.rsplit('-', 1)[-1]}",
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BattleLogParseError(
            "invalid_battle_timestamp", "battleTimestamp must include a UTC offset"
        )
    return parsed.astimezone(UTC)


def _required_int(source: dict[str, Any], key: str) -> int:
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise BattleLogParseError("unsupported_legend_row", f"{key} must be an integer")
    return value


def _optional_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BattleLogParseError(
            "unsupported_legend_row", f"{label} must be a non-negative integer"
        )
    return value


def _gap(
    index: int,
    source: Any,
    category: str,
    _detail: str,
) -> ParsedBattleRow:
    return ParsedBattleRow(
        source_row_index=index,
        outcome="malformed_legend_row",
        source_json=source,
        failure_category=category,
    )
