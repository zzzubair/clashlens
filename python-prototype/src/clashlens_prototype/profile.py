from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ENDPOINT_VERSION = "profile-v1"
SCHEMA_VERSION = "profile-schema-v1"
PARSER_VERSION = "profile-parser-v1"
LEGEND_I_TIER_ID = 105000036
LEGEND_I_TIER_NAME = "Legend I"
_PLAYER_TAG_RE = re.compile(r"^#[0289PYLQGRJCUV]+$")


class ProfileParseError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(f"{category}: {message}")
        self.category = category


class LeagueTierV1(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str


class PlayerProfileV1(BaseModel):
    model_config = ConfigDict(
        alias_generator=None,
        extra="ignore",
        populate_by_name=True,
    )

    tag: str
    name: str
    trophies: int = Field(ge=0)
    league_tier: LeagueTierV1 = Field(alias="leagueTier")
    current_league_season_id: str = Field(alias="currentLeagueSeasonId")
    previous_league_season_id: str = Field(alias="previousLeagueSeasonId")
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
) -> ParsedProfile:
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
    eligibility = (
        "eligible"
        if profile.league_tier.id == LEGEND_I_TIER_ID
        and profile.league_tier.name == LEGEND_I_TIER_NAME
        else "uncertain"
    )
    return ParsedProfile(
        normalized_tag=source_normalized,
        name=profile.name,
        trophies=profile.trophies,
        league_tier_id=profile.league_tier.id,
        league_tier_name=profile.league_tier.name,
        eligibility_state=eligibility,
        observed_at=observed_utc,
        endpoint_version=endpoint_version,
        schema_version=SCHEMA_VERSION,
        parser_version=PARSER_VERSION,
        profile_json=profile.model_dump(by_alias=True, mode="json"),
    )
