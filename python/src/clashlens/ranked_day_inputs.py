"""What one player's Legend day is calculated from, read from saved evidence.

Daily calculation and the Reset settlement check read the same battle-log
coverage, own-side battle reports and previous saved day, so they cannot
disagree about which battles a day holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import battle, domain
from .db import PROCESSING_VERSION, Database, _text_value
from .domain import RankedDay
from .reconciliation import (
    RECONCILIATION_RULE_VERSION,
    BattleContribution,
    CoverageObservation,
    PreviousRankedDay,
)


def _source_rows(database: Database) -> tuple[str, str, str]:
    """Where a battle log's rows live, their ID column, and how rows join their report."""
    content_dedup = getattr(database, "_supports_content_dedup", False)
    source_rows_relation = (
        "battle_log_observation_source_rows" if content_dedup else "battle_source_rows"
    )
    source_row_id_column = "source_row_id" if content_dedup else "id"
    evidence_join = (
        "be.id = sr.evidence_id"
        if getattr(database, "_supports_compact_battles", False) else
        "(sr.observation_row_id IS NOT NULL AND be.observation_row_id = sr.observation_row_id)"
        " OR (sr.observation_row_id IS NULL AND be.source_row_id = sr.source_row_id)"
        if content_dedup
        else "be.source_row_id = sr.id"
    )
    return source_rows_relation, source_row_id_column, evidence_join


def load_coverage(
    database: Database,
    connection: Any,
    player_id: int,
    ranked_day: RankedDay,
    start_battle_log_observation_id: int | None,
    end_battle_log_observation_id: int | None,
) -> tuple[CoverageObservation, ...]:
    """The player's battle logs from the start to the end Reset log, in order."""
    source_rows_relation, source_row_id_column, evidence_join = _source_rows(database)
    coverage_rows = connection.execute(
        f"""
        SELECT
            blo.observation_id,
            blo.observed_at,
            blo.row_count,
            blo.has_row_gap,
            COALESCE(evidence.battle_identities, ARRAY[]::text[]),
            COALESCE(evidence.source_row_ids, ARRAY[]::bigint[]),
            COALESCE(row_flags.malformed_count, 0),
            COALESCE(row_flags.unclassified_count, 0),
            COALESCE(processing.outcome = 'processed', false),
            observed.response_hash,
            blo.parser_version,
            processing.processing_version
        FROM battle_log_observations AS blo
        JOIN collector_observations AS observed
          ON observed.id = blo.observation_id
        LEFT JOIN observation_processing_outcomes AS processing
          ON processing.observation_id = blo.observation_id
         AND processing.parser_version = blo.parser_version
        LEFT JOIN LATERAL (
            SELECT
                array_agg(be.battle_id::text ORDER BY be.id)
                    FILTER (WHERE be.battle_id IS NOT NULL)
                    AS battle_identities,
                array_agg(sr.{source_row_id_column} ORDER BY sr.{source_row_id_column})
                    FILTER (WHERE sr.{source_row_id_column} IS NOT NULL)
                    AS source_row_ids
            FROM {source_rows_relation} AS sr
            LEFT JOIN battle_evidence AS be
              ON {evidence_join}
            WHERE sr.battle_log_observation_id = blo.id
        ) AS evidence ON true
        LEFT JOIN LATERAL (
            SELECT
                count(*) FILTER (
                    WHERE sr.outcome = 'malformed_legend_row'
                       OR sr.failure_category LIKE 'malformed%%'
                       OR sr.failure_category LIKE 'unsupported%%'
                       OR sr.failure_category LIKE 'identity%%'
                ) AS malformed_count,
                count(*) FILTER (
                    WHERE sr.failure_category LIKE 'unclassified%%'
                ) AS unclassified_count
            FROM {source_rows_relation} AS sr
            WHERE sr.battle_log_observation_id = blo.id
        ) AS row_flags ON true
        WHERE blo.player_id = %s
          AND blo.observed_at >= COALESCE(
              (SELECT start_blo.observed_at
                 FROM battle_log_observations AS start_blo
                WHERE start_blo.observation_id = %s),
              %s
          )
          AND blo.observed_at <= COALESCE(
              (SELECT end_blo.observed_at
                 FROM battle_log_observations AS end_blo
                WHERE end_blo.observation_id = %s),
              %s
          )
        ORDER BY blo.observed_at, blo.id
        """,
        (
            player_id,
            start_battle_log_observation_id,
            ranked_day.start,
            end_battle_log_observation_id,
            ranked_day.end,
        ),
    ).fetchall()
    if start_battle_log_observation_id is not None:
        start_index = next(
            (
                index
                for index, row in enumerate(coverage_rows)
                if int(row[0]) == start_battle_log_observation_id
            ),
            None,
        )
        if start_index is not None:
            coverage_rows = coverage_rows[start_index:]
    if end_battle_log_observation_id is not None:
        end_index = next(
            (
                index
                for index, row in enumerate(coverage_rows)
                if int(row[0]) == end_battle_log_observation_id
            ),
            None,
        )
        if end_index is not None:
            coverage_rows = coverage_rows[: end_index + 1]
    if getattr(database, "_supports_compact_battles", False):
        # Keep both ends of identical runs. Interior duplicate polls
        # add no overlap/quality evidence, and their later expiry
        # must not manufacture a new ranked-day publication.
        coverage_rows = [
            row for index, row in enumerate(coverage_rows)
            if index in (0, len(coverage_rows) - 1)
            or row[2:] != coverage_rows[index - 1][2:]
            or row[2:] != coverage_rows[index + 1][2:]
        ]
    return tuple(
        CoverageObservation(
            observation_id=int(row[0]),
            observed_at=row[1],
            row_count=int(row[2]),
            has_row_gap=bool(row[3]),
            battle_identities=tuple(str(value) for value in row[4]),
            source_row_ids=tuple(int(value) for value in row[5]),
            malformed_row_count=int(row[6]),
            unclassified_row_count=int(row[7]),
            valid=bool(row[8]),
            response_hash=_text_value(row[9]),
            parser_version=_text_value(row[10]),
            processing_version=(
                _text_value(row[11]) if row[11] is not None else None
            ),
        )
        for row in coverage_rows
    )


def load_contributions(
    connection: Any, player_id: int, ranked_day: RankedDay
) -> tuple[BattleContribution, ...]:
    """The player's own report of each battle whose report time is in the day."""
    contribution_rows = connection.execute(
        """
        SELECT
            b.id,
            p.perspective,
            e.id,
            e.source_row_id,
            e.observation_id,
            e.source_observed_at,
            e.battle_timestamp,
            e.stars,
            e.destruction_percentage,
            e.army_share_code,
            e.attacker_gain,
            e.defender_loss,
            e.trophy_rule_version,
            b.disagreement_state,
            source_row.outcome,
            source_row.failure_category,
            CASE e.parser_version
                WHEN 'supercell-source-parser-v1'
                    THEN source_row.source_json -> 'opponent' ->> 'tag'
                ELSE source_row.source_json ->> 'opponentPlayerTag'
            END,
            CASE e.parser_version
                WHEN 'supercell-source-parser-v1'
                    THEN source_row.source_json -> 'opponent' ->> 'name'
                ELSE source_row.source_json ->> 'opponentName'
            END
        FROM legend_battles AS b
        JOIN battle_perspectives AS p ON p.battle_id = b.id
        JOIN battle_evidence AS e ON e.id = p.evidence_id
        JOIN battle_source_rows AS source_row
          ON source_row.id = e.source_row_id
        WHERE e.battle_timestamp >= %s
          AND e.battle_timestamp < %s
          AND (
              (p.perspective = 'attacker' AND b.attacker_player_id = %s)
              OR
              (p.perspective = 'defender' AND b.defender_player_id = %s)
          )
        ORDER BY b.id, p.perspective
        """,
        (*domain.battle_window(ranked_day.start), player_id, player_id),
    ).fetchall()
    return tuple(
        BattleContribution(
            battle_identity=str(row[0]),
            lens=(
                "offense"
                if _text_value(row[1]) == "attacker"
                else "defense"
            ),
            trophy_amount=int(
                row[10] if _text_value(row[1]) == "attacker" else row[11]
            ),
            source_rule_version=_text_value(row[12]),
            valid=_text_value(row[14]) == "valid_legend",
            failure_reason=(
                _text_value(row[15]) if row[15] is not None else None
            ),
            disagreement=_text_value(row[13]) == "disagreement",
            source_observation_id=int(row[4]),
            source_evidence_id=int(row[2]),
            source_row_id=int(row[3]),
            source_observed_at=row[5],
            battle_timestamp=row[6],
            stars=int(row[7]),
            destruction_percentage=int(row[8]),
            army_share_code=_text_value(row[9]),
            attacker_gain=int(row[10]),
            defender_loss=int(row[11]),
            opponent_tag=(
                _text_value(row[16]) if row[16] is not None else None
            ),
            opponent_name=(
                _text_value(row[17]) if row[17] is not None else None
            ),
        )
        for row in contribution_rows
    )


def load_previous_day(
    connection: Any, player_id: int, ranked_day: RankedDay
) -> PreviousRankedDay | None:
    """The latest saved calculation of the day before, under the current rule."""
    previous_row = connection.execute(
        """
        SELECT
            id,
            state,
            confidence,
            defense_count,
            observed_defense_loss,
            coverage_complete,
            shield_state,
            shield_duration_days,
            input_hash
        FROM ranked_day_versions
        WHERE player_id = %s AND ranked_day_start = %s
          AND reconciliation_rule_version = %s
        ORDER BY version DESC, id DESC
        LIMIT 1
        """,
        (
            player_id,
            ranked_day.start - timedelta(days=1),
            RECONCILIATION_RULE_VERSION,
        ),
    ).fetchone()
    return (
        PreviousRankedDay(
            complete=(
                _text_value(previous_row[1]) == "Complete"
                and bool(previous_row[5])
            ),
            observed_defense_count=int(previous_row[3]),
            observed_defense_loss=int(previous_row[4]),
            shield_run_length=(
                int(previous_row[7] or 0)
                if _text_value(previous_row[6]) == "inferred_shielded"
                else 0
            ),
            coverage_complete=bool(previous_row[5]),
            shield_state=_text_value(previous_row[6]),
            version_id=int(previous_row[0]),
            ranked_day_start=ranked_day.start - timedelta(days=1),
            state=_text_value(previous_row[1]),
            confidence=_text_value(previous_row[2]),
            input_hash=(
                _text_value(previous_row[8])
                if previous_row[8] is not None
                else None
            ),
        )
        if previous_row is not None
        else None
    )


def _log_ids(connection: Any, condition: str, params: tuple[Any, ...]) -> list[Any]:
    # Look the logs up first: the row view only narrows to known log IDs.
    return connection.execute(
        f"SELECT id, parser_version FROM battle_log_observations WHERE {condition}", params
    ).fetchall()


def load_log_reports(
    database: Database, connection: Any, observation_id: int, parser_version: str
) -> tuple[tuple[str, str, datetime, int], ...]:
    """Each battle one battle log reported, as that log reported it.

    Rows are (battle ID, ``offense`` or ``defense``, report time, trophies).
    """
    relation, _, evidence_join = _source_rows(database)
    logs = _log_ids(connection, "observation_id = %s AND parser_version = %s",
                    (observation_id, parser_version))
    rows = connection.execute(
        f"""
        SELECT be.battle_id, be.perspective, be.battle_timestamp,
               be.attacker_gain, be.defender_loss
        FROM {relation} AS sr
        JOIN battle_evidence AS be ON {evidence_join}
        WHERE sr.battle_log_observation_id = ANY(%s)
        """,
        ([log[0] for log in logs],),
    ).fetchall()
    return tuple(
        (str(row[0]), "offense", row[2], int(row[3]))
        if _text_value(row[1]) == "attacker"
        else (str(row[0]), "defense", row[2], int(row[4]))
        for row in rows
    )


def load_unreadable_report_times(
    database: Database,
    connection: Any,
    player_id: int,
    after: datetime,
    until: datetime,
) -> list[datetime | None]:
    """When each unreadable row in the player's battle logs saved in
    ``(after, until]`` happened; ``None`` when even that is unreadable."""
    relation, _, _ = _source_rows(database)
    parsers = dict(_log_ids(
        connection, "player_id = %s AND observed_at > %s AND observed_at <= %s",
        (player_id, after, until),
    ))
    rows = connection.execute(
        f"""
        SELECT sr.battle_log_observation_id, sr.source_json FROM {relation} AS sr
        WHERE sr.battle_log_observation_id = ANY(%s)
          AND (sr.outcome = 'malformed_legend_row'
               OR sr.failure_category LIKE 'malformed%%'
               OR sr.failure_category LIKE 'unsupported%%'
               OR sr.failure_category LIKE 'identity%%'
               OR sr.failure_category LIKE 'unclassified%%')
        """,
        (list(parsers),),
    ).fetchall()
    times: list[datetime | None] = []
    for log_id, source in rows:
        try:
            times.append(battle._parse_battle_timestamp(
                battle._battle_timestamp_value(source, parsers[log_id]), parsers[log_id]
            ))
        except (AttributeError, battle.BattleLogParseError):
            times.append(None)
    return times


@dataclass(frozen=True, slots=True)
class Reading:
    """One saved response, and whether the worker accepted it."""

    observation_id: int
    request_started_at: datetime
    response_completed_at: datetime
    # The processing outcome, or ``None`` while it is still unprocessed.
    outcome: str | None = None
    parser_version: str | None = None
    usable: bool = False
    trophies: int | None = None


# Each response's latest processing outcome, and the profile it produced.
_OUTCOME = """
    LEFT JOIN LATERAL (
        SELECT outcome, parser_version FROM observation_processing_outcomes
        WHERE observation_id = observed.id AND processing_version = %(processing)s
        ORDER BY id DESC LIMIT 1
    ) AS outcome ON true
"""


def _profile_join(database: Database) -> str:
    if getattr(database, "_supports_content_dedup", False):
        return """
        LEFT JOIN player_profile_effects AS effect
          ON effect.observation_id = observed.id
         AND effect.parser_version = outcome.parser_version
        LEFT JOIN player_profile_versions AS profile
          ON profile.id = effect.profile_version_id
        """
    return """
        LEFT JOIN player_profile_versions AS profile
          ON profile.observation_id = observed.id
         AND profile.parser_version = outcome.parser_version
        """


def load_reading(
    database: Database, connection: Any, player_id: int, observation_id: int | None
) -> Reading | None:
    """One saved profile or battle log; usable once processed into an
    accepted, eligible profile of this player or a saved battle log."""
    if observation_id is None:
        return None
    row = connection.execute(
        f"""
        SELECT observed.request_started_at, observed.response_completed_at,
               outcome.outcome, outcome.parser_version,
               CASE observed.endpoint
                   WHEN 'profile' THEN profile.player_id = %(player)s
                       AND profile.source_contract_state = 'accepted'
                       AND profile.eligibility_state = 'eligible'
                   ELSE log.id IS NOT NULL
               END,
               profile.trophies
        FROM collector_observations AS observed
        {_OUTCOME}
        {_profile_join(database)}
        LEFT JOIN battle_log_observations AS log
          ON log.observation_id = observed.id
         AND log.parser_version = outcome.parser_version
        WHERE observed.id = %(observation)s
        """,
        {"processing": PROCESSING_VERSION, "player": player_id,
         "observation": observation_id},
    ).fetchone()
    if row is None:
        return None
    return Reading(
        observation_id, row[0], row[1], row[2], row[3],
        usable=row[2] == "processed" and bool(row[4]), trophies=row[5],
    )


def load_profile_trophies(
    database: Database, connection: Any, player_id: int, after: datetime, until: datetime
) -> tuple[tuple[datetime, int], ...]:
    """The trophies of each processed profile read in ``(after, until)``."""
    rows = connection.execute(
        f"""
        SELECT observed.response_completed_at, profile.trophies
        FROM collector_observations AS observed
        {_OUTCOME}
        {_profile_join(database)}
        WHERE observed.player_id = %(player)s AND observed.endpoint = 'profile'
          AND observed.response_completed_at > %(after)s
          AND observed.response_completed_at < %(until)s
          AND outcome.outcome = 'processed' AND profile.trophies IS NOT NULL
        ORDER BY observed.response_completed_at
        """,
        {"processing": PROCESSING_VERSION, "player": player_id,
         "after": after, "until": until},
    ).fetchall()
    return tuple((at, int(trophies)) for at, trophies in rows)


def load_first_reports(
    connection: Any, player_id: int, since: datetime, new_day_from: datetime,
    until: datetime,
) -> tuple[datetime | None, datetime | None]:
    """The earliest report, by either player, of a battle of this player at
    or after ``since``, and at or after ``new_day_from``, before ``until``."""
    row = connection.execute(
        """
        SELECT min(evidence.battle_timestamp)
                   FILTER (WHERE evidence.battle_timestamp >= %(since)s),
               min(evidence.battle_timestamp)
                   FILTER (WHERE evidence.battle_timestamp >= %(new_day)s)
        FROM legend_battles AS battle
        JOIN battle_evidence AS evidence ON evidence.battle_id = battle.id
        WHERE (battle.attacker_player_id = %(player)s
               OR battle.defender_player_id = %(player)s)
          AND evidence.battle_timestamp >= LEAST(%(since)s, %(new_day)s)
          AND evidence.battle_timestamp < %(until)s
        """,
        {"since": since, "new_day": new_day_from, "player": player_id, "until": until},
    ).fetchone()
    return row[0], row[1]
