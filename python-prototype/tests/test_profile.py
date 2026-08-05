from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from clashlens_prototype.profile import (
    PARSER_VERSION,
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
    assert profile.schema_version == SCHEMA_VERSION
    assert profile.parser_version == PARSER_VERSION


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
