"""Parser and storage for the official per-player league-history endpoint.

``/players/{tag}/leaguehistory`` answers one row per past season. These rows
supply official past-season results; the validated current-season ID from the
profile anchors the active season. The collector fetches league history at
initial collection, at each season-ending Reset and again 20 minutes later
(``league_history_refresh``); the full raw response is archived like every
other stored response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from .db import Claim, Database
from .job_outcomes import (
    _observation_source,
    _record_parsed_payload,
    _record_processing_outcome,
    _upsert_player,
)
from .profile import ProfileParseError, normalize_player_tag
from .source_observation_contract import (
    LEAGUE_HISTORY_SOURCE_OBSERVATION_CONTRACT,
)

LEAGUE_HISTORY_PARSER_VERSION = "supercell-league-history-parser-v1"
LEAGUE_HISTORY_ENDPOINT_VERSION = (
    LEAGUE_HISTORY_SOURCE_OBSERVATION_CONTRACT.endpoint_version
)
LEAGUE_HISTORY_SCHEMA_VERSION = (
    LEAGUE_HISTORY_SOURCE_OBSERVATION_CONTRACT.schema_version
)

# Season rows carry integer counters; only leagueSeasonId is required.
_INTEGER_FIELDS = (
    "leagueTrophies",
    "leagueTierId",
    "placement",
    "attackWins",
    "attackLosses",
    "attackStars",
    "defenseWins",
    "defenseLosses",
    "defenseStars",
    "maxBattles",
)


class LeagueHistoryParseError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(f"{category}: {message}")
        self.category = category


@dataclass(frozen=True, slots=True)
class ParsedLeagueHistoryEntry:
    source_row_index: int
    league_season_id: str
    league_trophies: int | None
    league_tier_id: int | None
    placement: int | None
    attack_wins: int | None
    attack_losses: int | None
    attack_stars: int | None
    defense_wins: int | None
    defense_losses: int | None
    defense_stars: int | None
    max_battles: int | None
    source_json: dict[str, Any] | Any


@dataclass(frozen=True, slots=True)
class ParsedLeagueHistory:
    normalized_tag: str
    observed_at: datetime
    row_count: int
    entries: tuple[ParsedLeagueHistoryEntry, ...]
    has_row_gap: bool
    outcome: str
    endpoint_version: str
    schema_version: str
    parser_version: str


def parse_league_history(
    body: bytes,
    *,
    expected_tag: str,
    observed_at: datetime,
    parser_version: str = LEAGUE_HISTORY_PARSER_VERSION,
    endpoint_version: str = LEAGUE_HISTORY_ENDPOINT_VERSION,
) -> ParsedLeagueHistory:
    if (
        parser_version
        not in LEAGUE_HISTORY_SOURCE_OBSERVATION_CONTRACT.supported_parser_versions
    ):
        raise LeagueHistoryParseError(
            "unsupported_parser_version",
            "league-history parser version is not installed",
        )
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise LeagueHistoryParseError(
            "invalid_observation_time",
            "observation time must include a UTC offset",
        )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LeagueHistoryParseError(
            "malformed_json", "league-history body is not valid JSON"
        ) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise LeagueHistoryParseError(
            "unsupported_league_history_schema",
            "league-history JSON must be an object with an items array",
        )
    try:
        reporting_tag = normalize_player_tag(expected_tag)
    except ProfileParseError as error:
        raise LeagueHistoryParseError(
            error.category, "observation player tag is invalid"
        ) from error

    entries: list[ParsedLeagueHistoryEntry] = []
    has_row_gap = False
    for index, item in enumerate(payload["items"]):
        entry = _parse_entry(index, item)
        if entry is None:
            has_row_gap = True
        else:
            entries.append(entry)
    return ParsedLeagueHistory(
        normalized_tag=reporting_tag,
        observed_at=observed_at.astimezone(UTC),
        row_count=len(payload["items"]),
        entries=tuple(entries),
        has_row_gap=has_row_gap,
        outcome="official_partial" if has_row_gap else "official_observed",
        endpoint_version=endpoint_version,
        schema_version=LEAGUE_HISTORY_SCHEMA_VERSION,
        parser_version=parser_version,
    )


def _parse_entry(index: int, source: Any) -> ParsedLeagueHistoryEntry | None:
    if not isinstance(source, dict):
        return None
    season_id = source.get("leagueSeasonId")
    if isinstance(season_id, bool):
        return None
    if isinstance(season_id, int):
        season_id_text = str(season_id)
    elif isinstance(season_id, str) and season_id.isdigit():
        season_id_text = str(int(season_id))
    else:
        return None
    values: dict[str, int | None] = {}
    for name in _INTEGER_FIELDS:
        value = source.get(name)
        if value is None:
            values[name] = None
        elif isinstance(value, bool) or not isinstance(value, int):
            return None
        else:
            values[name] = value
    return ParsedLeagueHistoryEntry(
        source_row_index=index,
        league_season_id=season_id_text,
        league_trophies=values["leagueTrophies"],
        league_tier_id=values["leagueTierId"],
        placement=values["placement"],
        attack_wins=values["attackWins"],
        attack_losses=values["attackLosses"],
        attack_stars=values["attackStars"],
        defense_wins=values["defenseWins"],
        defense_losses=values["defenseLosses"],
        defense_stars=values["defenseStars"],
        max_battles=values["maxBattles"],
        source_json=source,
    )


def complete_league_history(
    database: Database, claim: Claim, history: ParsedLeagueHistory
) -> None:
    """Store one parsed league-history response inside the claimed transaction.

    Kept out of ``db.py`` (already oversized); the write mirrors
    ``complete_rankings`` — parsed payload first, then the deduplicated rows,
    the processing outcome, and the fenced claim completion.
    """
    (
        observation_id,
        _http_status,
        response_hash,
        observed_at,
        endpoint,
        schema_version,
    ) = _observation_source(claim)
    with database.pool.connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            parsed_payload_id = _record_parsed_payload(
                connection,
                endpoint=endpoint,
                response_hash=response_hash,
                parser_version=history.parser_version,
                schema_version=schema_version,
                parse_outcome=(
                    "valid_with_gaps" if history.has_row_gap else "valid"
                ),
                parsed_json={
                    "items": [entry.source_json for entry in history.entries]
                },
            )
            player_row = connection.execute(
                "SELECT player_id FROM collector_observations WHERE id = %s",
                (observation_id,),
            ).fetchone()
            player_id = None if player_row is None else player_row[0]
            if player_id is None:
                player_id = _upsert_player(
                    connection, history.normalized_tag, active=False
                )
            for entry in history.entries:
                connection.execute(
                    """
                    INSERT INTO player_league_history_entries (
                        player_id, league_season_id, observed_at,
                        observation_id, parsed_payload_id, league_trophies,
                        league_tier_id, placement, attack_wins, attack_losses,
                        attack_stars, defense_wins, defense_losses,
                        defense_stars, max_battles, source_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s)
                    ON CONFLICT (player_id, league_season_id) DO UPDATE SET
                        observed_at = EXCLUDED.observed_at,
                        observation_id = EXCLUDED.observation_id,
                        parsed_payload_id = EXCLUDED.parsed_payload_id,
                        league_trophies = EXCLUDED.league_trophies,
                        league_tier_id = EXCLUDED.league_tier_id,
                        placement = EXCLUDED.placement,
                        attack_wins = EXCLUDED.attack_wins,
                        attack_losses = EXCLUDED.attack_losses,
                        attack_stars = EXCLUDED.attack_stars,
                        defense_wins = EXCLUDED.defense_wins,
                        defense_losses = EXCLUDED.defense_losses,
                        defense_stars = EXCLUDED.defense_stars,
                        max_battles = EXCLUDED.max_battles,
                        source_json = EXCLUDED.source_json,
                        updated_at = clock_timestamp()
                    WHERE EXCLUDED.observed_at
                        > player_league_history_entries.observed_at
                    """,
                    (
                        player_id,
                        entry.league_season_id,
                        observed_at,
                        observation_id,
                        parsed_payload_id,
                        entry.league_trophies,
                        entry.league_tier_id,
                        entry.placement,
                        entry.attack_wins,
                        entry.attack_losses,
                        entry.attack_stars,
                        entry.defense_wins,
                        entry.defense_losses,
                        entry.defense_stars,
                        entry.max_battles,
                        Jsonb(entry.source_json),
                    ),
                )
            _recalculate_season_ends(
                connection, player_id, history, observation_id, observed_at
            )
            _record_processing_outcome(
                database,
                connection,
                claim,
                outcome=(
                    "processed_with_gaps" if history.has_row_gap else "processed"
                ),
                parsed_payload_id=parsed_payload_id,
            )
            database._finish_claim(
                connection,
                claim,
                job,
                state="complete",
                outcome=history.outcome,
            )


def _recalculate_season_ends(
    connection: Any, player_id: int, history: ParsedLeagueHistory,
    observation_id: int, observed_at: datetime,
) -> None:
    """Queue the last day of a Season that ended up to 3 days before this
    response when the player's official Legend I total it saved can give
    that day's end (see ``reconciliation_db._official_final``): the day is
    still saved without an end, or with a different official total.
    Older Seasons are left as saved."""
    from . import first_battle_log
    from .db import PYTHON_BACKFILL_PRIORITY
    from .profile import LEGEND_I_TIER_ID
    from .reconciliation import RECONCILIATION_RULE_VERSION

    for entry in history.entries:
        if entry.league_tier_id != LEGEND_I_TIER_ID or entry.league_trophies is None:
            continue
        ended = int(entry.league_season_id)
        if not ended <= observed_at.timestamp() <= ended + 3 * 86400:
            continue
        season_end = datetime.fromtimestamp(ended, UTC)
        day_start = season_end - timedelta(days=1)
        stale = connection.execute(
            """
            SELECT day.failure_reasons ? 'missing_end_baseline'
                   OR (day.input_evidence -> 'end_baseline_evidence'
                       ->> 'official_final_trophies')::integer
                      <> entry.league_trophies
            FROM player_league_history_entries AS entry
            CROSS JOIN LATERAL (
                SELECT failure_reasons, input_evidence
                FROM ranked_day_versions
                WHERE player_id = entry.player_id AND ranked_day_start = %s
                  AND reconciliation_rule_version = %s
                ORDER BY version DESC, id DESC LIMIT 1
            ) AS day
            WHERE entry.player_id = %s AND entry.league_season_id = %s
              AND entry.observation_id = %s
            """,
            (day_start, RECONCILIATION_RULE_VERSION, player_id,
             entry.league_season_id, observation_id),
        ).fetchone()
        if stale is None or not stale[0]:
            continue
        first_battle_log._queue(
            connection, int(player_id), day_start, None,
            key=(f"reconcile:official-final:{player_id}:"
                 f"{day_start:%Y-%m-%dT%H:%M:%SZ}:{RECONCILIATION_RULE_VERSION}:"
                 f"{observation_id}"),
            trigger="official_final", later_days=False,
            priority=PYTHON_BACKFILL_PRIORITY,
        )
