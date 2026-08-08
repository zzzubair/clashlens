from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clashlens.battle import (
    BATTLE_LOG_SCHEMA_VERSION,
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
    row["attackOrDefense"] = "defense"
    row["opponent"] = {"tag": "#2PP", "name": "Synthetic Attacker", "trophies": 6040}

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
