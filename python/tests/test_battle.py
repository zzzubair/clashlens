from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clashlens.battle import (
    BATTLE_LOG_SCHEMA_VERSION,
    LEGACY_SOURCE_PARSER_VERSION,
    LIVE_SOURCE_PARSER_VERSION,
    PREVIOUS_LIVE_SOURCE_PARSER_VERSION,
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


@pytest.mark.parametrize(
    ("parser_version", "trophies", "rule_version"),
    [
        (SOURCE_PARSER_VERSION, 17, "legend-trophy-allocation-v2"),
        (PREVIOUS_LIVE_SOURCE_PARSER_VERSION, 18, "legend-trophy-allocation-v1"),
    ],
)
def test_two_star_55_percent_counts_17_only_under_the_corrected_parser(
    parser_version: str, trophies: int, rule_version: str
) -> None:
    attack = json.loads(FIXTURE.read_bytes())["items"][0]
    attack.update(stars=2, destructionPercentage=55)
    defense = {
        **attack,
        "attack": False,
        "opponentPlayerTag": "#2PP",
        "opponentName": "Synthetic Attacker",
    }
    observed_at = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)

    for tag, row in (("#2PP", attack), ("#8PP", defense)):
        parsed = parse_battle_log(
            json.dumps({"items": [row]}).encode(),
            expected_tag=tag,
            observed_at=observed_at,
            parser_version=parser_version,
        )
        battle = parsed.rows[0].battle
        assert parsed.rows[0].source_json == row
        assert battle is not None
        assert (battle.attacker_tag, battle.defender_tag) == ("#2PP", "#8PP")
        assert (battle.stars, battle.destruction_percentage) == (2, 55)
        assert (battle.attacker_gain, battle.defender_loss) == (trophies, trophies)
        assert battle.trophy_rule_version == rule_version


def test_corrected_parser_keeps_zero_star_defense_and_counts_event() -> None:
    row = json.loads(FIXTURE.read_bytes())["items"][0]
    row.update(attack=False, stars=0, destructionPercentage=40)

    parsed = parse_battle_log(
        json.dumps({"items": [row]}).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
    )

    battle = parsed.rows[0].battle
    assert parsed.rows[0].outcome == "valid_legend"
    assert battle is not None
    assert battle.perspective == "defender"
    assert (battle.attacker_gain, battle.defender_loss) == (4, 0)


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
    "timestamp",
    ["20260804T120000.000Z", "20260804T120000Z", 1785844800, "1785844800"],
)
def test_live_parser_accepts_compact_battle_timestamps(timestamp: object) -> None:
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


@pytest.mark.parametrize("timestamp", [True, "9" * 40, -2**70])
def test_live_parser_marks_unusable_battle_times_as_gaps(
    timestamp: object,
) -> None:
    payload = json.loads(FIXTURE.read_bytes())
    payload["items"][0]["battleTimestamp"] = timestamp

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    assert parsed.rows[0].outcome == "malformed_legend_row"
    assert parsed.rows[0].failure_category == "invalid_battle_timestamp"


def test_live_parser_prefers_battle_timestamp() -> None:
    payload = json.loads(FIXTURE.read_bytes())
    payload["items"][0]["battleTimestamp"] = "2026-08-04T12:30:00Z"

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    battle = parsed.rows[0].battle
    assert battle is not None
    assert battle.battle_timestamp == datetime(2026, 8, 4, 12, 30, tzinfo=UTC)


def test_live_parser_falls_back_to_legacy_battle_time() -> None:
    payload = json.loads(FIXTURE.read_bytes())
    del payload["items"][0]["battleTimestamp"]
    payload["items"][0]["battleTime"] = 1785846600

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    battle = parsed.rows[0].battle
    assert battle is not None
    assert battle.battle_timestamp == datetime(2026, 8, 4, 12, 30, tzinfo=UTC)


def test_live_parser_does_not_read_opponent_town_hall_level() -> None:
    payload = json.loads(FIXTURE.read_bytes())
    payload["items"][0]["opponentTownHallLevel"] = "not-a-timestamp"

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


# Live logs keep this row for days: no opponent, no battle.
NO_OPPONENT_ROW = {
    "battleType": "legend",
    "attack": False,
    "battleTime": 0,
    "battleTimestamp": "20260804T110000.000Z",
    "stars": 0,
    "destructionPercentage": 0,
    "opponentPlayerTag": None,
    "opponentName": None,
    "armyShareCode": None,
}


@pytest.mark.parametrize(
    ("change", "gap"),
    [
        ({}, False),
        ({"opponentPlayerTag": KeyError}, False),
        ({"stars": 1}, True),
        ({"destructionPercentage": 12}, True),
        ({"battleTime": 31}, True),
        ({"stars": False}, True),
        ({"opponentPlayerTag": ""}, True),
    ],
)
def test_only_the_exact_no_opponent_row_leaves_its_log_complete(
    change: dict, gap: bool
) -> None:
    payload = json.loads(FIXTURE.read_bytes())
    # A real battle against an opponent that moved no trophies still counts.
    payload["items"][0] |= {"stars": 0, "destructionPercentage": 0}
    row = NO_OPPONENT_ROW | change
    payload["items"].append(
        {key: value for key, value in row.items() if value is not KeyError}
    )

    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    assert parsed.has_row_gap is gap
    assert parsed.rows[2].outcome == "malformed_legend_row"
    assert parsed.rows[2].battle is None
    assert parsed.rows[0].battle is not None


def test_legacy_rows_without_an_opponent_stay_gaps() -> None:
    parsed = parse_battle_log(
        json.dumps({"items": [NO_OPPONENT_ROW | {"attackOrDefense": "defense"}]}).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=LEGACY_SOURCE_PARSER_VERSION,
    )

    assert parsed.has_row_gap is True


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


@pytest.mark.parametrize(
    ("attack", "battle_timestamp", "battle_time", "day"),
    [
        # Production reports from 2026-09-30 and 2026-10-01. A report stamped
        # in the first 5 minutes after Reset belongs to the day before.
        (True, "20260930T050029.000Z", 135, "2026-09-29"),
        (False, "20260930T045753.000Z", 135, "2026-09-29"),
        # Stamp less length is 05:00:02, after Reset; the defender's copy is
        # 04:59:49, so subtracting the length is not the rule.
        (True, "20260930T050131.000Z", 89, "2026-09-29"),
        # Both reports of each boundary battle are at or after Reset.
        (False, "20260930T050000.000Z", 170, "2026-09-29"),
        (True, "20260930T050301.000Z", 170, "2026-09-29"),
        (False, "20261001T050001.000Z", 167, "2026-09-30"),
        (True, "20261001T050202.000Z", 167, "2026-09-30"),
        # A battle first reported at 05:07:20 started after Reset.
        (False, "20260930T050720.000Z", 160, "2026-09-30"),
    ],
)
def test_battle_reported_just_after_reset_belongs_to_the_day_before(
    attack: bool, battle_timestamp: str, battle_time: int, day: str
) -> None:
    row = {
        "attack": attack,
        "battleTimestamp": battle_timestamp,
        "battleTime": battle_time,
        "stars": 2,
        "destructionPercentage": 89,
        "armyShareCode": "u1x0-2x1",
        "opponentPlayerTag": "#8PP",
        "opponentName": "Synthetic Opponent",
        "battleType": "legend",
    }

    parsed = parse_battle_log(
        json.dumps({"items": [row]}).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 10, 1, 6, tzinfo=UTC),
        parser_version=SOURCE_PARSER_VERSION,
    )

    battle = parsed.rows[0].battle
    assert battle is not None
    assert battle.ranked_day_start == datetime.fromisoformat(f"{day}T05:00:00+00:00")
    assert battle.battle_timestamp.strftime("%Y%m%dT%H%M%S.000Z") == battle_timestamp


def test_a_live_row_without_a_timestamp_is_malformed_not_a_battle_in_1970() -> None:
    """Live battleTime is the battle's length in seconds; a row missing its
    battleTimestamp must not become a battle dated 1970 (lab finding F4)."""
    payload = json.loads(FIXTURE.read_bytes())
    row = payload["items"][0]
    del row["battleTimestamp"]
    row["battleTime"] = 180
    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=LIVE_SOURCE_PARSER_VERSION,
    )

    assert parsed.rows[0].battle is None
    assert parsed.rows[0].outcome == "malformed_legend_row"
    assert parsed.has_row_gap is True


def test_a_row_whose_type_is_not_recognised_is_a_gap_not_an_ignored_mode() -> None:
    """"LEGEND" or a non-text type may be a Legend battle in a shape we do not
    read: a gap. Another mode spelt in plain text is ignored as before."""
    payload = json.loads(FIXTURE.read_bytes())
    legend, other = payload["items"]
    shouting = {**legend, "battleType": "LEGEND"}
    untyped = {**legend, "battleType": None}
    payload["items"] = [legend, shouting, untyped, other]
    parsed = parse_battle_log(
        json.dumps(payload).encode(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        parser_version=LIVE_SOURCE_PARSER_VERSION,
    )

    assert [row.outcome for row in parsed.rows] == [
        "valid_legend", "malformed_legend_row", "malformed_legend_row",
        "ignored_non_legend",
    ]
    assert parsed.has_row_gap is True
