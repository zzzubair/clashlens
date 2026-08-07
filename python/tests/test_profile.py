from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from clashlens.profile import (
    PARSER_VERSION,
    RECOGNIZED_NON_LEGEND_TIERS_V1,
    SCHEMA_VERSION,
    parse_profile,
)

FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"


def test_synthetic_legend_i_fixture_parses_as_versioned_eligible_profile() -> None:
    body = FIXTURE.read_bytes()

    profile = parse_profile(
        body,
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        endpoint_version="profile-v1",
    )

    assert profile.normalized_tag == "#2PP"
    assert profile.name == "Synthetic Legend I"
    assert profile.trophies == 6123
    assert profile.eligibility_state == "eligible"
    assert profile.league_tier_id == 105000036
    assert profile.current_league_season_id == "1783918800"
    assert profile.previous_league_season_id == "1781499600"
    assert profile.season_anchor_state == "valid"
    assert profile.schema_version == SCHEMA_VERSION
    assert profile.parser_version == PARSER_VERSION


def test_profile_parser_keeps_missing_or_conflicting_tier_as_uncertain() -> None:
    payload = json.loads(FIXTURE.read_bytes())
    payload.pop("leagueTier")

    missing = parse_profile(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime.now(UTC),
        endpoint_version="profile-v1",
    )

    payload["leagueTier"] = {"id": 105000036, "name": "Changed Legend Name"}
    conflicting = parse_profile(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime.now(UTC),
        endpoint_version="profile-v1",
    )

    assert missing.eligibility_state == "uncertain"
    assert missing.source_contract_state == "conflict"
    assert missing.eligibility_reason == "missing_league_tier"
    assert conflicting.eligibility_state == "uncertain"
    assert conflicting.eligibility_reason == "known_tier_name_conflict"


def test_profile_parser_deactivates_only_an_adapter_recognized_non_legend_tier() -> None:
    tier_id, tier_name = next(iter(RECOGNIZED_NON_LEGEND_TIERS_V1.items()))
    payload = json.loads(FIXTURE.read_bytes())
    payload["leagueTier"] = {"id": tier_id, "name": tier_name}

    profile = parse_profile(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime.now(UTC),
        endpoint_version="profile-v1",
    )

    assert profile.eligibility_state == "ineligible"
    assert profile.source_contract_state == "accepted"


def test_profile_parser_preserves_invalid_season_values_as_contract_conflict() -> None:
    payload = json.loads(FIXTURE.read_bytes())
    payload["previousLeagueSeasonId"] = "1781499601"

    profile = parse_profile(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime.now(UTC),
        endpoint_version="profile-v1",
    )

    assert profile.current_league_season_id == "1783918800"
    assert profile.previous_league_season_id == "1781499601"
    assert profile.season_anchor_state == "conflict"
    assert profile.source_contract_state == "conflict"


def test_profile_parser_rejects_source_identity_mismatch() -> None:
    body = json.dumps(
        {
            "tag": "#3PP",
            "name": "Wrong",
            "trophies": 1,
            "leagueTier": {"id": 105000036, "name": "Legend I"},
            "currentLeagueSeasonId": "1783918800",
            "previousLeagueSeasonId": "1781499600",
        }
    ).encode()

    try:
        parse_profile(
            body,
            expected_tag="#2PP",
            observed_at=datetime.now(UTC),
            endpoint_version="profile-v1",
        )
    except ValueError as error:
        assert "identity" in str(error)
    else:
        raise AssertionError("profile parser accepted a mismatched source identity")
