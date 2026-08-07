from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .domain import DomainRuleError, validate_season_anchor

ENDPOINT_VERSION = "profile-v1"
SCHEMA_VERSION = "profile-schema-v1"
PARSER_VERSION = "supercell-source-parser-v1"
SUPPORTED_PARSER_VERSIONS = frozenset(
    {PARSER_VERSION, "supercell-source-parser-v2"}
)
LEGEND_I_TIER_ID = 105000036
LEGEND_I_TIER_NAME = "Legend I"
RECOGNIZED_NON_LEGEND_TIERS_V1 = {105000035: "Legend II"}
_PLAYER_TAG_RE = re.compile(r"^#[0289PYLQGRJCUV]+$")


class ProfileParseError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(f"{category}: {message}")
        self.category = category


class PlayerProfileV1(BaseModel):
    model_config = ConfigDict(
        alias_generator=None,
        extra="ignore",
        populate_by_name=True,
    )

    tag: str
    name: str
    trophies: int = Field(ge=0)
    league_tier: Any = Field(default=None, alias="leagueTier")
    current_league_season_id: Any = Field(default=None, alias="currentLeagueSeasonId")
    previous_league_season_id: Any = Field(default=None, alias="previousLeagueSeasonId")
    exp_level: int | None = Field(default=None, alias="expLevel")
    best_trophies: int | None = Field(default=None, alias="bestTrophies")


@dataclass(frozen=True, slots=True)
class ParsedProfile:
    normalized_tag: str
    name: str
    trophies: int
    league_tier_id: int
    league_tier_name: str
    eligibility_state: str
    eligibility_reason: str
    source_contract_state: str
    current_league_season_id: str | None
    previous_league_season_id: str | None
    season_anchor_state: str
    observed_at: datetime
    endpoint_version: str
    schema_version: str
    parser_version: str
    profile_json: dict[str, Any]


def normalize_player_tag(value: str) -> str:
    normalized = value.strip().upper()
    if not _PLAYER_TAG_RE.fullmatch(normalized):
        raise ProfileParseError(
            "invalid_player_tag", "profile tag is not a valid normalized player tag"
        )
    return normalized


def parse_profile(
    body: bytes,
    *,
    expected_tag: str,
    observed_at: datetime,
    endpoint_version: str,
    parser_version: str = PARSER_VERSION,
) -> ParsedProfile:
    if parser_version not in SUPPORTED_PARSER_VERSIONS:
        raise ProfileParseError(
            "unsupported_parser_version", "profile parser version is not installed"
        )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileParseError(
            "malformed_json", "profile body is not valid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise ProfileParseError(
            "unsupported_profile_schema", "profile JSON must be an object"
        )
    try:
        profile = PlayerProfileV1.model_validate(payload)
    except ValidationError as error:
        raise ProfileParseError(
            "unsupported_profile_schema", "profile does not match profile-schema-v1"
        ) from error

    expected_normalized = normalize_player_tag(expected_tag)
    try:
        source_normalized = normalize_player_tag(profile.tag)
    except ProfileParseError as error:
        raise ProfileParseError(
            "source_identity_mismatch", "profile tag is invalid"
        ) from error
    if source_normalized != expected_normalized:
        raise ProfileParseError(
            "source_identity_mismatch",
            f"profile tag {source_normalized} does not match expected observation tag {expected_normalized}",
        )
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ProfileParseError(
            "invalid_observation_time", "observation time must include a UTC offset"
        )
    observed_utc = observed_at.astimezone(UTC)
    (
        league_tier_id,
        league_tier_name,
        eligibility,
        eligibility_reason,
        tier_contract_state,
    ) = _classify_league_tier(profile.league_tier)
    current_season = _source_string(profile.current_league_season_id)
    previous_season = _source_string(profile.previous_league_season_id)
    season_anchor_state = "valid"
    try:
        if current_season is None or previous_season is None:
            raise DomainRuleError(
                "invalid_season_anchor", "profile season values are missing or malformed"
            )
        validate_season_anchor(current_season, previous_season)
    except DomainRuleError:
        season_anchor_state = "conflict"
    source_contract_state = (
        "accepted"
        if tier_contract_state == "accepted" and season_anchor_state == "valid"
        else "conflict"
    )
    return ParsedProfile(
        normalized_tag=source_normalized,
        name=profile.name,
        trophies=profile.trophies,
        league_tier_id=league_tier_id,
        league_tier_name=league_tier_name,
        eligibility_state=eligibility,
        eligibility_reason=eligibility_reason,
        source_contract_state=source_contract_state,
        current_league_season_id=current_season,
        previous_league_season_id=previous_season,
        season_anchor_state=season_anchor_state,
        observed_at=observed_utc,
        endpoint_version=endpoint_version,
        schema_version=SCHEMA_VERSION,
        parser_version=parser_version,
        profile_json=payload,
    )


def _classify_league_tier(value: Any) -> tuple[int, str, str, str, str]:
    if value is None:
        return 0, "", "uncertain", "missing_league_tier", "conflict"
    if not isinstance(value, dict):
        return 0, "", "uncertain", "malformed_league_tier", "conflict"
    tier_id = value.get("id")
    tier_name = value.get("name")
    if isinstance(tier_id, bool) or not isinstance(tier_id, int):
        return 0, "", "uncertain", "malformed_league_tier", "conflict"
    if not isinstance(tier_name, str) or not tier_name:
        return tier_id, "", "uncertain", "malformed_league_tier", "conflict"
    if tier_id == LEGEND_I_TIER_ID:
        if tier_name == LEGEND_I_TIER_NAME:
            return tier_id, tier_name, "eligible", "confirmed_legend_i", "accepted"
        return tier_id, tier_name, "uncertain", "known_tier_name_conflict", "conflict"
    expected_non_legend_name = RECOGNIZED_NON_LEGEND_TIERS_V1.get(tier_id)
    if expected_non_legend_name is None:
        return tier_id, tier_name, "uncertain", "unknown_tier_id", "conflict"
    if tier_name != expected_non_legend_name:
        return tier_id, tier_name, "uncertain", "known_tier_name_conflict", "conflict"
    return tier_id, tier_name, "ineligible", "confirmed_non_legend_i", "accepted"


def _source_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
