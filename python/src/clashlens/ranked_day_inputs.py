"""What one player's Legend day is calculated from, read from saved evidence.

Daily calculation and the Reset settlement check read the same battle-log
coverage, own-side battle reports and previous saved day, so they cannot
disagree about which battles a day holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from . import battle, domain, reading_rule
from .db import PROCESSING_VERSION, Database, _text_value
from .domain import RankedDay
from .reconciliation import (
    RECONCILIATION_RULE_VERSION,
    BattleContribution,
    CoverageObservation,
    PreviousRankedDay,
    log_has_row_gap,
    logs_leave_gap,
)

# Reasons after which a day's end cannot start the next day: a 9th attack or
# defense means the game returned more than its own cap, and a day the player
# was not enrolled or not in Legend I is not a Legend day at all.
CHAIN_BREAK_REASONS = frozenset({
    "attack_count_exceeds_eight", "defense_count_exceeds_eight",
    "not_enrolled", "player_not_eligible",
})


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


def only_no_opponent_gaps_sql(database: Database, log: str) -> str:
    """SQL: every row battle log ``log`` rejected, at least one, is a "no
    opponent, no battle" row, so its gap flag and gap outcome hide no battle.

    Logs saved before those rows stopped counting as gaps keep their saved
    flag and outcome; their saved rows decide instead.
    """
    relation, _, _ = _source_rows(database)
    no_opponent = battle.no_opponent_row_sql("gap_row", f"{log}.parser_version")
    # An aggregate subquery stays a per-log index lookup; an EXISTS can be
    # planned as a scan of every saved row.
    return f"""COALESCE((
        SELECT bool_and({no_opponent}) FROM {relation} AS gap_row
        WHERE gap_row.battle_log_observation_id = {log}.id
          AND gap_row.outcome = 'malformed_legend_row'
    ), false)"""


def _coverage_sql(database: Database, *, shared_read: bool) -> str:
    """SQL: the player's battle logs between two Reset logs, with each log's
    coverage and quality read from its saved rows.

    ``shared_read`` reads each log's rows once for both. That is exact only
    when a row matches at most one battle report, as compact storage's report
    link guarantees; older joins can match several reports per row, which
    would multiply the quality counts, so they read the rows a second time.
    """
    source_rows_relation, source_row_id_column, evidence_join = _source_rows(database)
    no_opponent = battle.no_opponent_row_sql("sr", "blo.parser_version")
    quality = f"""
                count(*) FILTER (
                    WHERE (sr.outcome = 'malformed_legend_row'
                           OR sr.failure_category LIKE 'malformed%%'
                           OR sr.failure_category LIKE 'unsupported%%'
                           OR sr.failure_category LIKE 'identity%%')
                      AND NOT {no_opponent}
                ) AS malformed_count,
                count(*) FILTER (
                    WHERE sr.failure_category LIKE 'unclassified%%'
                ) AS unclassified_count,
                COALESCE(bool_and({no_opponent}) FILTER (
                    WHERE sr.outcome = 'malformed_legend_row'
                ), false) AS only_no_opponent"""
    if shared_read:
        evidence_quality, row_flags_read, flags = f",{quality}", "", "evidence"
    else:
        evidence_quality, flags = "", "row_flags"
        row_flags_read = f"""
        LEFT JOIN LATERAL (
            SELECT{quality}
            FROM {source_rows_relation} AS sr
            WHERE sr.battle_log_observation_id = blo.id
        ) AS row_flags ON true"""
    return f"""
        SELECT
            blo.observation_id,
            blo.observed_at,
            blo.row_count,
            -- A log saved before "no opponent, no battle" rows stopped
            -- counting as gaps keeps its saved flag and outcome; its rows
            -- decide instead.
            blo.has_row_gap AND NOT {flags}.only_no_opponent,
            COALESCE(evidence.battle_identities, ARRAY[]::text[]),
            COALESCE(evidence.source_row_ids, ARRAY[]::bigint[]),
            COALESCE({flags}.malformed_count, 0),
            COALESCE({flags}.unclassified_count, 0),
            COALESCE(
                processing.outcome = 'processed'
                OR (processing.outcome = 'processed_with_gaps'
                    AND {flags}.only_no_opponent),
                false
            ),
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
                    AS source_row_ids{evidence_quality}
            FROM {source_rows_relation} AS sr
            LEFT JOIN battle_evidence AS be
              ON {evidence_join}
            WHERE sr.battle_log_observation_id = blo.id
        ) AS evidence ON true{row_flags_read}
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
        """


def load_coverage(
    database: Database,
    connection: Any,
    player_id: int,
    ranked_day: RankedDay,
    start_battle_log_observation_id: int | None,
    end_battle_log_observation_id: int | None,
) -> tuple[CoverageObservation, ...]:
    """The player's battle logs from the start to the end Reset log, in order."""
    coverage_rows = connection.execute(
        _coverage_sql(
            database,
            shared_read=getattr(database, "_supports_compact_battles", False),
        ),
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
            END,
            CASE
                WHEN e.parser_version <> 'supercell-source-parser-v1'
                     AND source_row.source_json ->> 'battleTimestamp' IS NOT NULL
                    THEN source_row.source_json ->> 'battleTime'
            END
        FROM legend_battles AS b
        JOIN battle_perspectives AS p ON p.battle_id = b.id
        JOIN battle_evidence AS e ON e.id = p.evidence_id
        JOIN battle_source_rows AS source_row
          ON source_row.id = e.source_row_id
        WHERE b.ranked_day_start BETWEEN %s::timestamptz - interval '1 day'
                                     AND %s::timestamptz + interval '1 day'
          AND e.battle_timestamp >= %s
          AND e.battle_timestamp < %s
          AND (
              (p.perspective = 'attacker' AND b.attacker_player_id = %s)
              OR
              (p.perspective = 'defender' AND b.defender_player_id = %s)
          )
        ORDER BY b.id, p.perspective
        """,
        (ranked_day.start, ranked_day.start, *domain.battle_window(ranked_day.start),
         player_id, player_id),
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
            battle_seconds=(
                int(row[18]) if row[18] is not None and str(row[18]).isdigit() else None
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
            input_hash,
            end_baseline_id,
            COALESCE((formula_components ->> 'unsettled_automatic_loss')::int, 0),
            COALESCE((input_evidence ->> 'zero_result_defense_slots')::int, 0),
            COALESCE((formula_components ->> 'next_start_reading_correction')::int, 0),
            expected_next_start_trophies,
            failure_reasons
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
    if previous_row is None:
        return None
    reasons = previous_row[14] if isinstance(previous_row[14], list) else []
    # Continuous logs, a saved Legend day and no 9th attack or defense: every
    # battle of that day is known, whether or not its readings were usable.
    battles_known = bool(previous_row[5]) and _text_value(
        previous_row[1]
    ) in {"Complete", "Partial"} and not any(
        reason in CHAIN_BREAK_REASONS for reason in reasons
    )
    # With no defense slot used, only a reading shows the automatic loss for
    # all 8 (see ``reconciliation._zero_defense_loss``), so an end no
    # reading settled cannot start the next day.
    end_known = battles_known and (
        _text_value(previous_row[1]) == "Complete"
        or int(previous_row[3]) + int(previous_row[11]) > 0
    )
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
            end_baseline_id=(
                int(previous_row[9]) if previous_row[9] is not None else None
            ),
            unsettled_automatic_loss=int(previous_row[10]),
            zero_result_defense_slots=int(previous_row[11]),
            reset_reading_correction=int(previous_row[12]),
            expected_next_start=(
                int(previous_row[13])
                if end_known and previous_row[13] is not None
                else None
            ),
            battles_known=battles_known,
        )
    )


def previous_end_start(
    baseline: dict[str, Any] | None, previous: PreviousRankedDay, *, complete: bool
) -> dict[str, Any]:
    """The day before's calculated end as a day's start, in the shape of a
    Reset reading that could not give one. The saved Reset evidence, if any,
    stays in place; ``complete`` is whether a battle log proves the day's
    battles from the Reset."""
    evidence = (
        dict(baseline["evidence"])
        if baseline is not None
        else {"reset_reading": None, "battle_log_observation_id": None}
    )
    evidence["start_trophies_source"] = "previous_day_end"
    evidence["previous_day_version_id"] = previous.version_id
    return {
        "id": baseline["id"] if baseline is not None else None,
        "version": baseline["version"] if baseline is not None else None,
        "state": baseline["state"] if baseline is not None else "previous_day_end",
        "complete": complete,
        "trophies": previous.expected_next_start,
        "eligibility_state": "eligible",
        "evidence": evidence,
    }


def load_zero_result_slots(
    connection: Any, coverage: tuple[CoverageObservation, ...]
) -> frozenset[tuple[datetime, bool]]:
    """Each "no opponent, no battle" row these battle logs hold, as (report
    time, is an attack). A live log keeps the row for days, so each later
    log repeating it adds nothing. None is a battle; the automatic defense
    loss alone counts them as used attack and defense slots."""
    parsers = {
        row_id: observation.parser_version
        for observation in coverage
        if observation.parser_version is not None
        for row_id in observation.source_row_ids
    }
    rows = connection.execute(
        """
        SELECT id, source_json FROM battle_source_rows
        WHERE id = ANY(%s) AND (source_json -> 'battleTime')::text = '0'
        """,
        (list(parsers),),
    ).fetchall() if parsers else []
    return _slots((parsers[int(row_id)], source) for row_id, source in rows)


def load_late_zero_result_slots(
    database: Database, connection: Any, player_id: int, ranked_day: RankedDay,
    after: datetime,
) -> frozenset[tuple[datetime, bool]]:
    """``load_zero_result_slots`` of the player's battle logs saved after
    ``after``, the end Reset log, until 10 minutes past the day's battle
    window: a row can first be returned after that log."""
    relation, _, _ = _source_rows(database)
    until = domain.battle_window(ranked_day.start)[1] + timedelta(minutes=10)
    parsers = dict(_log_ids(
        connection, "player_id = %s AND observed_at > %s AND observed_at <= %s",
        (player_id, after, until),
    ))
    rows = connection.execute(
        f"""
        SELECT sr.battle_log_observation_id, sr.source_json FROM {relation} AS sr
        WHERE sr.battle_log_observation_id = ANY(%s)
          AND (sr.source_json -> 'battleTime')::text = '0'
        """,
        (list(parsers),),
    ).fetchall() if parsers else []
    return _slots((parsers[log_id], source) for log_id, source in rows)


def _slots(rows: Any) -> frozenset[tuple[datetime, bool]]:
    slots = set()
    for parser, source in rows:
        if not battle.is_no_opponent_row(source, parser) or not isinstance(
            source.get("attack"), bool
        ):
            continue
        try:
            slots.add((battle._parse_battle_timestamp(
                battle._battle_timestamp_value(source, parser), parser
            ), source["attack"]))
        except battle.BattleLogParseError:
            continue
    return frozenset(slots)


def slot_counts(
    slots: frozenset[tuple[datetime, bool]], since: datetime, until: datetime
) -> tuple[int, int]:
    """The attack and defense slots reported in ``[since, until)``."""
    attacks = [attack for at, attack in slots if since <= at < until]
    return sum(attacks), len(attacks) - sum(attacks)


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
        if battle.is_no_opponent_row(source, parsers[log_id]):
            continue
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
    accepted, eligible profile of this player or a saved battle log. A log
    saved with gaps that were only "no opponent, no battle" rows counts as
    processed."""
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
               profile.trophies,
               outcome.outcome = 'processed'
               OR (outcome.outcome = 'processed_with_gaps'
                   AND {only_no_opponent_gaps_sql(database, "log")})
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
        usable=bool(row[6]) and bool(row[4]), trophies=row[5],
    )


def load_profile_trophies(
    database: Database, connection: Any, player_id: int, after: datetime, until: datetime,
    *, season_id: str | None = None,
) -> tuple[tuple[datetime, int | None], ...]:
    """The trophies of each profile read in ``(after, until)``, or ``None``
    for one with no processed profile or, with ``season_id``, none accepted,
    eligible and naming that Season."""
    rows = connection.execute(
        f"""
        SELECT observed.response_completed_at,
               CASE WHEN outcome.outcome = 'processed'
                     AND (%(season)s::text IS NULL
                          OR (profile.source_contract_state = 'accepted'
                              AND profile.eligibility_state = 'eligible'
                              AND profile.current_league_season_id = %(season)s))
                    THEN profile.trophies END
        FROM collector_observations AS observed
        {_OUTCOME}
        {_profile_join(database)}
        WHERE observed.player_id = %(player)s AND observed.endpoint = 'profile'
          AND observed.response_completed_at > %(after)s
          AND observed.response_completed_at < %(until)s
          AND observed.http_status BETWEEN 200 AND 299
        ORDER BY observed.response_completed_at
        """,
        {"processing": PROCESSING_VERSION, "player": player_id,
         "after": after, "until": until, "season": season_id},
    ).fetchall()
    return tuple((at, None if trophies is None else int(trophies)) for at, trophies in rows)


def lock_ranked_day(connection: Any, player_id: int, ranked_day: RankedDay) -> None:
    """Serialize work deciding or saving one player's Legend day result."""
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"ranked-day:{player_id}:{ranked_day.start.isoformat()}",),
    )

def load_readings(
    database: Database, connection: Any, player_id: int, ranked_day: RankedDay,
    *, reset_profile_observation_id: int | None,
    end_battle_log_observation_id: int | None,
) -> tuple[reading_rule.Reading, ...]:
    """Every profile of this player read between the day's end Reset and the
    next, other than the Reset pair's own, ``reset_profile_observation_id``,
    which the day reads as its Reset reading, that can judge the day's end
    (``reading_rule``): one accepted,
    eligible and naming the day's Season, or a Legend I profile naming
    Season 0, which can only confirm. A reading can contradict only while
    the player's battle logs from the day's end Reset log on are continuous
    up to it: one taken after the last log before one that may have missed
    a battle, or after the newest, or the last successful check with its
    content, can only confirm, as a battle it shows may not be known.
    Readings from when an unreadable row of a battle log saved
    since the Reset happened are left out, and all of them when even that
    time is unreadable: a battle they may show cannot be placed."""
    until = ranked_day.end + timedelta(days=1)
    unreadable = load_unreadable_report_times(
        database, connection, player_id, ranked_day.end, until
    )
    if any(at is None for at in unreadable):
        return ()
    logs = load_coverage(
        database, connection, player_id, domain.ranked_day_for(ranked_day.end),
        end_battle_log_observation_id, None,
    )
    known_until = None
    for previous, current in pairwise((None, *logs)):
        if log_has_row_gap(current) or (
            previous is not None and logs_leave_gap(previous, current)
        ):
            break
        known_until = current.observed_at
    else:
        # A later successful check with the newest log's content saves no
        # log of its own, and still shows no battle came since.
        checked = connection.execute(
            """
            SELECT max(last_success_at) FROM collector_response_state
            WHERE player_id = %s AND endpoint = 'battle_log'
              AND last_observation_id = %s
            """,
            (player_id, logs[-1].observation_id),
        ).fetchone()[0] if logs else None
        if checked is not None and checked > known_until:
            known_until = checked
    rows = connection.execute(
        f"""
        SELECT observed.response_completed_at, profile.trophies,
               profile.source_contract_state = 'accepted'
        FROM collector_observations AS observed
        {_OUTCOME}
        {_profile_join(database)}
        WHERE observed.player_id = %(player)s AND observed.endpoint = 'profile'
          AND observed.response_completed_at > %(after)s
          AND observed.response_completed_at < %(until)s
          AND observed.id IS DISTINCT FROM %(reset)s
          AND observed.http_status BETWEEN 200 AND 299
          AND outcome.outcome = 'processed'
          AND profile.player_id = %(player)s
          AND profile.eligibility_state = 'eligible'
          AND ((profile.source_contract_state = 'accepted'
                AND profile.current_league_season_id = %(season)s)
               OR (profile.source_contract_state = 'conflict'
                   AND profile.current_league_season_id = '0'))
        ORDER BY observed.response_completed_at
        """,
        {"processing": PROCESSING_VERSION, "player": player_id,
         "after": ranked_day.end, "reset": reset_profile_observation_id,
         "until": min([until, *unreadable]),
         "season": ranked_day.official_season_id},
    ).fetchall()
    return tuple(
        reading_rule.Reading(
            at, int(trophies),
            confirm_only=not accepted or known_until is None or at > known_until,
        )
        for at, trophies, accepted in rows
        if trophies is not None
    )


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
          AND battle.ranked_day_start > LEAST(%(since)s, %(new_day)s) - interval '2 days'
          AND battle.ranked_day_start < %(until)s
          AND evidence.battle_timestamp >= LEAST(%(since)s, %(new_day)s)
          AND evidence.battle_timestamp < %(until)s
        """,
        {"since": since, "new_day": new_day_from, "player": player_id, "until": until},
    ).fetchone()
    return row[0], row[1]
