from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .profile import ProfileParseError, normalize_player_tag
from .source_observation_contract import (
    GLOBAL_PLAYER_RANKINGS_SOURCE_OBSERVATION_CONTRACT,
)

GLOBAL_RANKING_ENDPOINT_VERSION = (
    GLOBAL_PLAYER_RANKINGS_SOURCE_OBSERVATION_CONTRACT.endpoint_version
)
GLOBAL_RANKING_SCHEMA_VERSION = (
    GLOBAL_PLAYER_RANKINGS_SOURCE_OBSERVATION_CONTRACT.schema_version
)
SOURCE_PARSER_VERSION = (
    GLOBAL_PLAYER_RANKINGS_SOURCE_OBSERVATION_CONTRACT.default_parser_version
)
SUPPORTED_SOURCE_PARSER_VERSIONS = (
    GLOBAL_PLAYER_RANKINGS_SOURCE_OBSERVATION_CONTRACT.supported_parser_versions
)


class RankingParseError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(f"{category}: {message}")
        self.category = category


@dataclass(frozen=True, slots=True)
class OfficialRankingEntry:
    normalized_tag: str
    rank: int
    source_row_index: int
    name: str | None
    trophies: int | None
    source_json: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ParsedOfficialRankings:
    entries: tuple[OfficialRankingEntry, ...]
    outcome: str
    failure_reasons: tuple[str, ...]
    official_season_id: None
    season_provenance: str
    endpoint_version: str
    schema_version: str
    parser_version: str


def parse_global_player_rankings(
    body: bytes,
    *,
    parser_version: str = SOURCE_PARSER_VERSION,
    endpoint_version: str = GLOBAL_RANKING_ENDPOINT_VERSION,
) -> ParsedOfficialRankings:
    if parser_version not in SUPPORTED_SOURCE_PARSER_VERSIONS:
        raise RankingParseError(
            "unsupported_parser_version",
            "global player rankings parser version is not installed",
        )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RankingParseError(
            "malformed_json", "global player ranking body is not valid JSON"
        ) from error
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("items"), list)
        or not isinstance(payload.get("paging"), dict)
    ):
        raise RankingParseError(
            "unsupported_global_ranking_schema",
            "global player rankings require items and paging objects",
        )

    reasons: set[str] = set()
    entries: list[OfficialRankingEntry] = []
    for source_row_index, source in enumerate(payload["items"]):
        entry = _parse_entry(source, source_row_index)
        if entry is None:
            reasons.add("malformed_entry")
            continue
        entries.append(entry)

    if len(payload["items"]) != 200:
        reasons.add("entry_count")
    tags = [entry.normalized_tag for entry in entries]
    ranks = [entry.rank for entry in entries]
    if len(tags) != len(set(tags)):
        reasons.add("duplicate_tag")
    if len(ranks) != len(set(ranks)):
        reasons.add("duplicate_rank")
    if set(ranks) != set(range(1, 201)):
        reasons.add("rank_set")

    cursors = payload["paging"].get("cursors")
    if not isinstance(cursors, dict) or any(
        value not in (None, "") for value in cursors.values()
    ):
        reasons.add("unexpected_cursor")

    contract_change_reasons = {"malformed_entry", "unexpected_cursor"}
    if reasons & contract_change_reasons:
        outcome = "official_contract_changed"
    elif reasons:
        outcome = "official_partial"
    else:
        outcome = "official_observed"

    return ParsedOfficialRankings(
        entries=tuple(sorted(entries, key=lambda entry: entry.rank)),
        outcome=outcome,
        failure_reasons=tuple(sorted(reasons)),
        official_season_id=None,
        season_provenance="not_supplied",
        endpoint_version=endpoint_version,
        schema_version=GLOBAL_RANKING_SCHEMA_VERSION,
        parser_version=parser_version,
    )


def _parse_entry(source: Any, source_row_index: int) -> OfficialRankingEntry | None:
    if not isinstance(source, dict):
        return None
    rank = source.get("rank")
    if isinstance(rank, bool) or not isinstance(rank, int):
        return None
    try:
        normalized_tag = normalize_player_tag(source["tag"])
    except (KeyError, ProfileParseError):
        return None
    name = source.get("name")
    if name is not None and not isinstance(name, str):
        return None
    trophies = source.get("trophies")
    if trophies is not None and (
        isinstance(trophies, bool) or not isinstance(trophies, int) or trophies < 0
    ):
        return None
    return OfficialRankingEntry(
        normalized_tag=normalized_tag,
        rank=rank,
        source_row_index=source_row_index,
        name=name,
        trophies=trophies,
        source_json=source,
    )
