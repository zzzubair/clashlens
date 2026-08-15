from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clashlens.battle import (
    BATTLE_LOG_SCHEMA_VERSION,
    LEGACY_SOURCE_PARSER_VERSION,
    LIVE_SOURCE_PARSER_VERSION,
    SOURCE_PARSER_VERSION,
    BattleLogParseError,
    parse_battle_log,
)
from clashlens.domain import TROPHY_ALLOCATION_RULE_VERSION

FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"


def test_battle_log_parser_retains_legend_evidence_and_ignores_other_battle_types() -> (
    None
):
    parsed = parse_battle_log(
        FIXTURE.read_bytes(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    assert parsed.schema_version == BATTLE_LOG_SCHEMA_VERSION
    assert parsed.parser_version == SOURCE_PARSER_VERSION
    assert SOURCE_PARSER_VERSION == LIVE_SOURCE_PARSER_VERSION
    assert BATTLE_LOG_SCHEMA_VERSION == "battle-log-schema-v1"
    assert parsed.row_count == 2
    assert parsed.has_row_gap is False
    assert parsed.rows[1].outcome == "ignored_non_legend"
    battle = parsed.rows[0].battle
    assert battle is not None
    assert battle.attacker_tag == "#2PP"
    assert battle.defender_tag == "#8PP"
    assert battle.reporting_tag == "#2PP"
    assert battle.perspective == "attacker"
    assert battle.ranked_day_start == datetime(2026, 8, 4, 5, 0, tzinfo=UTC)
    assert battle.army_share_code == "u1x0-2x1"
    assert battle.attacker_gain == 40
    assert battle.defender_loss == 40
    assert battle.trophy_rule_version == TROPHY_ALLOCATION_RULE_VERSION


def test_defender_report_uses_the_same_canonical_identity() -> None:
    payload = json.loads(FIXTURE.read_bytes())
    row = payload["items"][0]
    row["attack"] = False
    row["opponentPlayerTag"] = "#2PP"
    row["opponentName"] = "Synthetic Attacker"

    parsed = parse_battle_log(
        json.dumps({"items": [row]}).encode(),
        expected_tag="#8PP",
        observed_at=datetime(2026, 8, 4, 12, 6, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    battle = parsed.rows[0].battle
    assert battle is not None
    assert battle.attacker_tag == "#2PP"
    assert battle.defender_tag == "#8PP"
    assert battle.reporting_tag == "#8PP"
    assert battle.perspective == "defender"


def test_legacy_parser_replays_nested_opponent_shape() -> None:
    payload = {
        "items": [
            {
                "battleType": "legend",
                "attackOrDefense": "attack",
                "battleTimestamp": "2026-08-04T12:00:00Z",
                "stars": 3,
                "destructionPercentage": 100,
                "opponent": {
                    "tag": "#8PP",
                    "name": "Synthetic Defender",
                    "trophies": 6001,
                },
                "armyShareCode": "legacy-army-code",
            }
        ]
    }

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=LEGACY_SOURCE_PARSER_VERSION,
    )

    row = parsed.rows[0]
    assert row.outcome == "valid_legend"
    assert row.source_json == payload["items"][0]
    assert row.battle is not None
    assert row.battle.opponent_tag == "#8PP"
    assert row.battle.opponent_trophies == 6001


def test_live_parser_does_not_reinterpret_legacy_rows() -> None:
    payload = {
        "items": [
            {
                "battleType": "legend",
                "attackOrDefense": "attack",
                "battleTimestamp": "2026-08-04T12:00:00Z",
                "stars": 3,
                "destructionPercentage": 100,
                "opponent": {"tag": "#8PP"},
                "armyShareCode": "legacy-army-code",
            }
        ]
    }

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=LIVE_SOURCE_PARSER_VERSION,
    )

    assert parsed.rows[0].outcome == "malformed_legend_row"
    assert parsed.rows[0].failure_category == "unsupported_perspective"


@pytest.mark.parametrize("direction", [None, "true", 1, []])
def test_live_parser_requires_boolean_attack_direction(direction: object) -> None:
    payload = json.loads(FIXTURE.read_bytes())
    if direction is None:
        payload["items"][0].pop("attack")
    else:
        payload["items"][0]["attack"] = direction

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    assert parsed.rows[0].outcome == "malformed_legend_row"
    assert parsed.rows[0].failure_category == "unsupported_perspective"


@pytest.mark.parametrize(
    ("field", "value", "category"),
    [
        ("opponentPlayerTag", "not-a-tag", "invalid_opponent"),
        ("opponentPlayerTag", None, "invalid_opponent"),
        ("opponentName", 42, "invalid_opponent"),
        ("opponentPlayerTag", "#2PP", "identity_conflict"),
    ],
)
def test_live_parser_keeps_opponent_validation_and_identity_conflicts(
    field: str, value: object, category: str
) -> None:
    payload = json.loads(FIXTURE.read_bytes())
    if value is None:
        payload["items"][0].pop(field)
    else:
        payload["items"][0][field] = value

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    assert parsed.rows[0].outcome == "malformed_legend_row"
    assert parsed.rows[0].failure_category == category


@pytest.mark.parametrize(
    "timestamp", ["20260804T120000.000Z", "20260804T120000Z"]
)
def test_live_parser_accepts_compact_battle_timestamps(timestamp: str) -> None:
    payload = json.loads(FIXTURE.read_bytes())
    payload["items"][0]["battleTimestamp"] = timestamp

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    battle = parsed.rows[0].battle
    assert battle is not None
    assert battle.battle_timestamp == datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def test_legacy_parser_keeps_its_original_compact_timestamp_contract() -> None:
    payload = {
        "items": [
            {
                "battleType": "legend",
                "attackOrDefense": "attack",
                "battleTimestamp": "20260804T120000Z",
                "stars": 3,
                "destructionPercentage": 100,
                "opponent": {"tag": "#8PP"},
                "armyShareCode": "legacy-army-code",
            }
        ]
    }

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=LEGACY_SOURCE_PARSER_VERSION,
    )

    assert parsed.rows[0].outcome == "malformed_legend_row"
    assert parsed.rows[0].failure_category == "invalid_battle_timestamp"


def test_ranked_battle_type_is_ignored_as_non_legend() -> None:
    payload = json.loads(FIXTURE.read_bytes())
    payload["items"][0]["battleType"] = "ranked"

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    assert parsed.rows[0].outcome == "ignored_non_legend"
    assert parsed.rows[0].battle is None
    assert parsed.rows[0].source_json == payload["items"][0]


def test_invalid_legend_row_is_visible_as_a_coverage_gap_without_losing_valid_rows() -> (
    None
):
    payload = json.loads(FIXTURE.read_bytes())
    invalid = dict(payload["items"][0])
    invalid["stars"] = 3
    invalid["destructionPercentage"] = 99
    payload["items"].append(invalid)

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    assert parsed.has_row_gap is True
    assert parsed.rows[2].outcome == "malformed_legend_row"
    assert parsed.rows[2].failure_category == "impossible_trophy_allocation"
    assert parsed.rows[0].battle is not None


@pytest.mark.parametrize("body", [b"not-json", b"{}", b'{"items": {}}'])
def test_battle_log_parser_distinguishes_malformed_json_from_unsupported_schema(
    body: bytes,
) -> None:
    expected = (
        "malformed_json" if body == b"not-json" else "unsupported_battle_log_schema"
    )

    with pytest.raises(BattleLogParseError, match=expected):
        parse_battle_log(
            body,
            expected_tag="#2PP",
            observed_at=datetime.now(UTC),
            parser_version=SOURCE_PARSER_VERSION,
        )
