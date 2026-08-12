from __future__ import annotations

import json
from pathlib import Path

import pytest

from clashlens.rankings import (
    GLOBAL_RANKING_SCHEMA_VERSION,
    RankingParseError,
    parse_global_player_rankings,
)

FIXTURE = Path(__file__).parents[1] / "testdata" / "global_top_200_v1.json"


def test_complete_official_top_200_fixture_is_accepted_without_season_provenance() -> (
    None
):
    parsed = parse_global_player_rankings(FIXTURE.read_bytes())

    assert parsed.outcome == "official_observed"
    assert parsed.schema_version == GLOBAL_RANKING_SCHEMA_VERSION
    assert parsed.failure_reasons == ()
    assert len(parsed.entries) == 200
    assert [entry.rank for entry in parsed.entries] == list(range(1, 201))
    assert parsed.official_season_id is None
    assert parsed.season_provenance == "not_supplied"


@pytest.mark.parametrize(
    ("mutation", "outcome", "reason"),
    [
        ("short", "official_partial", "entry_count"),
        ("duplicate_tag", "official_partial", "duplicate_tag"),
        ("duplicate_rank", "official_partial", "duplicate_rank"),
        ("rank_gap", "official_partial", "rank_set"),
        ("unexpected_cursor", "official_contract_changed", "unexpected_cursor"),
        ("malformed_entry", "official_contract_changed", "malformed_entry"),
    ],
)
def test_invalid_official_refresh_stays_classified_without_becoming_complete(
    mutation: str,
    outcome: str,
    reason: str,
) -> None:
    payload = json.loads(FIXTURE.read_bytes())
    if mutation == "short":
        payload["items"].pop()
    elif mutation == "duplicate_tag":
        payload["items"][1]["tag"] = payload["items"][0]["tag"]
    elif mutation == "duplicate_rank":
        payload["items"][1]["rank"] = 1
    elif mutation == "rank_gap":
        payload["items"][199]["rank"] = 201
    elif mutation == "unexpected_cursor":
        payload["paging"]["cursors"]["after"] = "opaque"
    elif mutation == "malformed_entry":
        payload["items"][0].pop("tag")

    parsed = parse_global_player_rankings(json.dumps(payload).encode())

    assert parsed.outcome == outcome
    assert reason in parsed.failure_reasons


def test_global_ranking_parser_distinguishes_malformed_json_and_schema_change() -> None:
    with pytest.raises(RankingParseError, match="malformed_json"):
        parse_global_player_rankings(b"not-json")
    with pytest.raises(RankingParseError, match="unsupported_global_ranking_schema"):
        parse_global_player_rankings(b"{}")


def test_global_ranking_parser_rejects_an_uninstalled_parser_version() -> None:
    with pytest.raises(RankingParseError, match="unsupported_parser_version"):
        parse_global_player_rankings(
            FIXTURE.read_bytes(), parser_version="supercell-source-parser-v99"
        )
