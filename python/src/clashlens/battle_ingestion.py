from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from . import (
    army_ingestion,
    claimed_jobs,
    first_battle_log,
    queue_refresh,
    reconciliation_db,
    reset_baselines,
    source_storage,
)
from .battle import ParsedBattleLog, ParsedBattleRow
from .battle_day_repair import refresh_disagreements
from .db import (
    RESET_LOCK_WAIT,
    Claim,
    Database,
    enqueue_discovered_players,
)
from .db_rows import text_value
from .domain import (
    RANKED_DAY_DURATION,
    SEASON_ANCHOR_RULE_VERSION,
    SEASON_DURATION,
    DomainRuleError,
    ranked_day_for,
)
from .stage_metrics import Laps


def _battle_log_reset_baseline(
    connection: Any, claim: Claim
) -> tuple[int, int, datetime] | None:
    """The (collector work id, player, Reset) whose baseline this battle log records."""
    if claim.observation_id is None:
        return None
    context = reset_baselines.load_reset_baseline_context(
        connection, claim.observation_id
    )
    return None if context is None else (context.work_id, context.player_id, context.boundary_at)


def supersede_battle_log(
    database: Database, claim: Claim, battle_log: ParsedBattleLog
) -> bool:
    """Skip a battle log when newer evidence already covers every stored row.

    A newer processed log from the same player and Legend day must exist, so
    that day's battles and daily result were already refreshed after this log.
    Rows may come from that log or, once the window has slid, from older
    processed ones. Each battle's selected perspective must also have been
    confirmed at or after this log. Any uncovered row means the log runs.
    """
    day_end = ranked_day_for(battle_log.observed_at).end
    report_hashes = sorted({_report_hash(battle_log, row) for row in battle_log.rows})

    def covered(connection: Any) -> bool:
        row = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM battle_log_observations AS log
                WHERE log.player_id = player.id AND log.parser_version = %s
                  AND log.observed_at > %s AND log.observed_at < %s
            ) AND NOT EXISTS (
                SELECT 1 FROM unnest(%s::text[]) AS wanted (report_hash)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM battle_source_rows AS source
                    WHERE source.report_hash = wanted.report_hash
                      AND (
                          EXISTS (
                              SELECT 1 FROM battle_payload_row_lists AS list
                              WHERE list.source_row_ids @> ARRAY[source.id]
                                AND list.reporting_player_id = player.id
                          ) OR EXISTS (
                              SELECT 1 FROM battle_payload_rows AS member
                              WHERE member.source_row_id = source.id
                                AND member.reporting_player_id = player.id
                          )
                      )
                      AND (
                          source.outcome <> 'valid_legend' OR EXISTS (
                              SELECT 1
                              FROM battle_evidence AS evidence
                              JOIN battle_perspectives AS selected
                                ON selected.battle_id = evidence.battle_id
                               AND selected.perspective = evidence.perspective
                              WHERE evidence.source_row_id = source.id
                                AND evidence.reporting_player_id = player.id
                                AND evidence.observation_row_id IS NULL
                                AND selected.source_observed_at >= %s
                          )
                      )
                )
            )
            FROM players AS player
            WHERE player.normalized_tag = %s
            """,
            (
                battle_log.parser_version,
                battle_log.observed_at,
                day_end,
                report_hashes,
                battle_log.observed_at,
                battle_log.normalized_tag,
            ),
        ).fetchone()
        return row is not None and bool(row[0])

    return claimed_jobs.complete_superseded(database, claim, covered)


def complete_battle_log(
    database: Database, claim: Claim, battle_log: ParsedBattleLog
) -> None:
    (
        observation_id,
        _http_status,
        response_hash,
        _observed_at,
        endpoint,
        schema_version,
    ) = claimed_jobs.observation_source(claim)
    with database.connection() as connection:
        with connection.transaction():
            laps = Laps(getattr(database, "stage_metrics", None))
            from .season_lifecycle import acquire_retirement_reader

            acquire_retirement_reader(connection)
            job = claimed_jobs.lock_live_claim(connection, claim)
            all_battle_rows = [row for row in battle_log.rows if row.battle is not None]
            valid_rows = _guard_battle_rows(connection, all_battle_rows)
            if all_battle_rows and not valid_rows:
                raise DomainRuleError(
                    "season_detail_retired",
                    "battle log contains only retired-season detail",
                )
            _recheck_battle_rows(connection, valid_rows)
            parsed_payload_id = source_storage.record_parsed_payload(
                connection,
                endpoint=endpoint,
                response_hash=response_hash,
                parser_version=battle_log.parser_version,
                schema_version=schema_version,
                parse_outcome=_parse_outcome(battle_log),
                parsed_json={"items": [row.source_json for row in battle_log.rows]},
                representation="battle_payload_rows",
            )
            player_tags = {battle_log.normalized_tag}
            for row in valid_rows:
                assert row.battle is not None
                player_tags.add(row.battle.attacker_tag)
                player_tags.add(row.battle.defender_tag)
            connection.execute(
                """
                INSERT INTO players (normalized_tag, active, eligibility_state)
                SELECT DISTINCT unnest(%s::text[]), false, 'unknown'
                ON CONFLICT (normalized_tag) DO NOTHING
                """,
                (sorted(player_tags),),
            )
            player_rows = connection.execute(
                """
                SELECT id, normalized_tag
                FROM players
                WHERE normalized_tag = ANY(%s::text[])
                """,
                (sorted(player_tags),),
            ).fetchall()
            player_ids = {text_value(row[1]): int(row[0]) for row in player_rows}
            reporter_id = player_ids[battle_log.normalized_tag]
            log_row = connection.execute(
                """
                INSERT INTO battle_log_observations (
                    observation_id, player_id, parser_version, observed_at,
                    row_count, has_row_gap
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (observation_id, parser_version) DO UPDATE SET
                    row_count = EXCLUDED.row_count,
                    has_row_gap = EXCLUDED.has_row_gap
                RETURNING id
                """,
                (
                    observation_id,
                    reporter_id,
                    battle_log.parser_version,
                    battle_log.observed_at,
                    battle_log.row_count,
                    battle_log.has_row_gap,
                ),
            ).fetchone()
            assert log_row is not None
            log_id = int(log_row[0])
            source_row_ids = _record_battle_sources(
                connection,
                battle_log,
                parsed_payload_id,
                log_id,
                reporter_id,
            )
            affected_battle_ids: set[int] = set()
            shared_state_changed_battle_ids: set[int] = set()

            discoveries: list[dict[str, int]] = []
            if valid_rows:
                battles = []
                for row in valid_rows:
                    battle = row.battle
                    assert battle is not None
                    attacker_id = player_ids[battle.attacker_tag]
                    defender_id = player_ids[battle.defender_tag]
                    battles.append(
                        {
                            "ranked_day_start": battle.ranked_day_start.isoformat(),
                            "attacker_player_id": attacker_id,
                            "defender_player_id": defender_id,
                        }
                    )
                    discoveries.append(
                        {
                            "player_id": (
                                defender_id
                                if battle.perspective == "attacker"
                                else attacker_id
                            ),
                            "source_row_index": row.source_row_index,
                        }
                    )
                connection.execute(
                    """
                    WITH input AS (
                        SELECT * FROM jsonb_to_recordset(%s::jsonb) AS discovery (
                            player_id bigint, source_row_index integer
                        )
                    )
                    INSERT INTO known_player_discoveries (
                        player_id, observation_id, source_row_index,
                        source_kind, discovered_at
                    )
                    SELECT player_id, %s, source_row_index,
                           'battle_opponent', %s
                    FROM input
                    ORDER BY player_id, source_row_index
                    ON CONFLICT DO NOTHING
                    """,
                    (Jsonb(discoveries), observation_id, battle_log.observed_at),
                )
                canonical_ids = _save_battles(connection, battles)
                evidence_input = []
                for row in valid_rows:
                    battle = row.battle
                    assert battle is not None
                    attacker_id = player_ids[battle.attacker_tag]
                    defender_id = player_ids[battle.defender_tag]
                    evidence_input.append(
                        {
                            "battle_id": canonical_ids[
                                (
                                    battle.ranked_day_start,
                                    attacker_id,
                                    defender_id,
                                )
                            ],
                            "source_row_id": source_row_ids[row.source_row_index],
                            "perspective": battle.perspective,
                            "battle_timestamp": battle.battle_timestamp.isoformat(),
                            "stars": battle.stars,
                            "destruction_percentage": battle.destruction_percentage,
                            "army_share_code": battle.army_share_code,
                            "reporter_trophies": battle.reporter_trophies,
                            "opponent_trophies": battle.opponent_trophies,
                            "attacker_gain": battle.attacker_gain,
                            "defender_loss": battle.defender_loss,
                            "trophy_rule_version": battle.trophy_rule_version,
                        }
                    )
                connection.execute(
                    """
                    WITH input AS (
                        SELECT * FROM jsonb_to_recordset(%s::jsonb) AS evidence (
                            battle_id bigint, source_row_id bigint,
                            perspective text, battle_timestamp timestamptz,
                            stars integer, destruction_percentage integer,
                            army_share_code text, reporter_trophies integer,
                            opponent_trophies integer, attacker_gain integer,
                            defender_loss integer, trophy_rule_version text
                        )
                    )
                    INSERT INTO battle_evidence (
                        battle_id, source_row_id, observation_id,
                        reporting_player_id, perspective, battle_timestamp,
                        stars, destruction_percentage, army_share_code,
                        reporter_trophies, opponent_trophies, attacker_gain,
                        defender_loss, trophy_rule_version, source_observed_at,
                        parser_version
                    )
                    SELECT battle_id, source_row_id, %s, %s,
                           perspective, battle_timestamp, stars, destruction_percentage,
                           army_share_code, reporter_trophies, opponent_trophies,
                           attacker_gain, defender_loss, trophy_rule_version,
                           %s, %s
                    FROM input
                    WHERE NOT EXISTS (
                        SELECT 1 FROM battle_perspectives AS p
                        JOIN battle_evidence AS e ON e.id = p.evidence_id
                        WHERE p.battle_id = input.battle_id
                          AND p.perspective = input.perspective
                          AND e.source_row_id = input.source_row_id
                          AND (e.source_observed_at, e.observation_id) <= (%s, %s)
                    )
                    ON CONFLICT (observation_id, source_row_id, parser_version)
                        WHERE observation_row_id IS NULL DO NOTHING
                    """,
                    (
                        Jsonb(evidence_input),
                        observation_id,
                        reporter_id,
                        battle_log.observed_at,
                        battle_log.parser_version,
                        battle_log.observed_at,
                        observation_id,
                    ),
                )
                evidence_rows = connection.execute(
                    """
                    SELECT DISTINCT ON (e.source_row_id)
                           e.id, e.battle_id, e.perspective, %s::timestamptz
                    FROM battle_evidence AS e
                    LEFT JOIN battle_perspectives AS p ON p.evidence_id = e.id
                    WHERE e.source_row_id = ANY(%s::bigint[])
                      AND e.observation_row_id IS NULL
                      AND (e.observation_id = %s OR p.evidence_id IS NOT NULL)
                    ORDER BY e.source_row_id, e.id DESC
                    """,
                    (
                        battle_log.observed_at,
                        [source_row_ids[row.source_row_index] for row in valid_rows],
                        observation_id,
                    ),
                ).fetchall()
                perspectives = [
                    {
                        "evidence_id": int(row[0]),
                        "battle_id": int(row[1]),
                        "perspective": text_value(row[2]),
                        "source_observed_at": row[3].isoformat(),
                    }
                    for row in evidence_rows
                ]
                affected_battle_ids.update(int(row[1]) for row in evidence_rows)
                previous_disagreement_states = {
                    int(row[0]): text_value(row[1])
                    for row in connection.execute(
                        """
                        SELECT id, disagreement_state
                        FROM legend_battles
                        WHERE id = ANY(%s::bigint[])
                        """,
                        (sorted(affected_battle_ids),),
                    ).fetchall()
                }
                connection.execute(
                    """
                    WITH input AS (
                        SELECT * FROM jsonb_to_recordset(%s::jsonb) AS perspective (
                            evidence_id bigint, battle_id bigint,
                            perspective text, source_observed_at timestamptz
                        )
                    ), chosen AS (
                        SELECT DISTINCT ON (battle_id, perspective) *
                        FROM input
                        ORDER BY battle_id, perspective,
                                 source_observed_at DESC, evidence_id DESC
                    )
                    INSERT INTO battle_perspectives (
                        battle_id, perspective, evidence_id,
                        source_observed_at
                    )
                    SELECT battle_id, perspective, evidence_id,
                           source_observed_at
                    FROM chosen
                    ON CONFLICT (battle_id, perspective) DO UPDATE SET
                        evidence_id = EXCLUDED.evidence_id,
                        source_observed_at = EXCLUDED.source_observed_at,
                        updated_at = clock_timestamp()
                    WHERE EXCLUDED.source_observed_at
                              > battle_perspectives.source_observed_at
                       OR (
                           EXCLUDED.source_observed_at
                               = battle_perspectives.source_observed_at
                           AND EXCLUDED.evidence_id
                               > battle_perspectives.evidence_id
                       )
                    """,
                    (Jsonb(perspectives),),
                )
                refresh_disagreements(
                    connection,
                    sorted(affected_battle_ids),
                )
                current_disagreement_states = {
                    int(row[0]): text_value(row[1])
                    for row in connection.execute(
                        """
                        SELECT id, disagreement_state
                        FROM legend_battles
                        WHERE id = ANY(%s::bigint[])
                        """,
                        (sorted(affected_battle_ids),),
                    ).fetchall()
                }
                shared_state_changed_battle_ids.update(
                    battle_id
                    for battle_id, state in current_disagreement_states.items()
                    if previous_disagreement_states.get(battle_id) != state
                )
            laps.lap("python_battle_log_save_battles")
            army_ingestion._upsert_army_decodes(
                database,
                connection,
                sorted(affected_battle_ids),
                reset_baseline=_battle_log_reset_baseline(connection, claim),
                reset_lock_wait=RESET_LOCK_WAIT,
                skip_busy_battles=True,
            )
            laps.lap("python_battle_log_army_check")

            outcome = "processed_with_gaps" if battle_log.has_row_gap else "processed"
            claimed_jobs.record_processing_outcome(
                connection,
                claim,
                outcome=outcome,
                parsed_payload_id=parsed_payload_id,
            )
            reset_baselines._refresh_reset_baseline_evidence(
                database, connection, claim
            )
            # With the job's outcome row, saved just before.
            laps.lap("python_battle_log_reset_evidence")
            ranked_day = ranked_day_for(battle_log.observed_at)
            live_player_ids = {reporter_id}
            if shared_state_changed_battle_ids:
                perspective_players = connection.execute(
                    """
                    SELECT DISTINCT CASE p.perspective
                        WHEN 'attacker' THEN battle.attacker_player_id
                        ELSE battle.defender_player_id
                    END AS player_id
                    FROM legend_battles AS battle
                    JOIN battle_perspectives AS p ON p.battle_id = battle.id
                    WHERE battle.id = ANY(%s::bigint[])
                      AND battle.ranked_day_start = %s
                    """,
                    (sorted(shared_state_changed_battle_ids), ranked_day.start),
                ).fetchall()
                live_player_ids.update(int(row[0]) for row in perspective_players)
            source_failures = sorted(
                (
                    row.outcome,
                    row.failure_category or "",
                )
                for row in battle_log.rows
                if row.outcome not in {"valid_legend", "ignored_non_legend"}
            )
            source_quality = (
                {
                    "has_row_gap": battle_log.has_row_gap,
                    "failures": source_failures,
                }
                if battle_log.has_row_gap or source_failures
                else None
            )
            for live_player_id in sorted(live_player_ids):
                reconciliation_db._enqueue_live_reconciliation(
                    connection,
                    player_id=live_player_id,
                    ranked_day_start=ranked_day.start,
                    source_quality=(
                        source_quality if live_player_id == reporter_id else None
                    ),
                )
            first_battle_log.queue_earlier_days(
                connection, reporter_id, battle_log.observed_at, valid_rows
            )
            queue_refresh.queue_for_reading(
                connection, reporter_id, f"log:{observation_id}", battle_log.observed_at
            )
            queue_refresh.queue_for_battles(
                connection, observation_id, shared_state_changed_battle_ids,
                battle_log.observed_at,
            )
            if discoveries:
                enqueue_discovered_players(
                    connection,
                    database,
                    claim,
                    (item["player_id"] for item in discoveries),
                )
            laps.lap("python_battle_log_queue_recalculations")
            claimed_jobs.finish_claim(
                connection, claim, job, state="complete", outcome=outcome
            )


def _save_battles(
    connection: Any, battles: list[dict[str, Any]]
) -> dict[tuple[Any, int, int], int]:
    """Each battle's id by (day, attacker, defender), saving the new ones.

    A battle saved before is locked as an update would lock it, so its two
    sides' reports still take turns, but not rewritten: a battle log repeats
    each battle for hours, and rewriting each one only to read its id back
    made this statement write about 3 GB of change log a day on production.
    The lock alone writes about 60% less.
    """
    connection.execute(
        """
        WITH input AS (
            SELECT DISTINCT *
            FROM jsonb_to_recordset(%s::jsonb) AS battle (
                ranked_day_start timestamptz,
                attacker_player_id bigint,
                defender_player_id bigint
            )
        )
        INSERT INTO legend_battles (
            ranked_day_start, attacker_player_id,
            defender_player_id
        )
        SELECT ranked_day_start, attacker_player_id,
               defender_player_id
        FROM input
        ORDER BY ranked_day_start, attacker_player_id,
                 defender_player_id
        ON CONFLICT (
            ranked_day_start, attacker_player_id,
            defender_player_id
        ) DO UPDATE SET updated_at = clock_timestamp()
        WHERE false
        """,
        (Jsonb(battles),),
    )
    # A later statement sees every battle the insert found, committed by now.
    return {
        (row[1], int(row[2]), int(row[3])): int(row[0])
        for row in connection.execute(
            """
            SELECT battle.id, battle.ranked_day_start,
                   battle.attacker_player_id, battle.defender_player_id
            FROM jsonb_to_recordset(%s::jsonb) AS input (
                ranked_day_start timestamptz,
                attacker_player_id bigint,
                defender_player_id bigint
            )
            JOIN legend_battles AS battle
              USING (ranked_day_start, attacker_player_id, defender_player_id)
            """,
            (Jsonb(battles),),
        ).fetchall()
    }


def _parse_outcome(battle_log: ParsedBattleLog) -> str:
    """The saved parse verdict: with gaps when any row was rejected.

    Unlike the log's own gap, it still counts "no opponent, no battle" rows,
    so identical bytes saved before those rows stopped counting as gaps keep
    the same saved verdict.
    """
    return (
        "valid_with_gaps"
        if any(row.outcome == "malformed_legend_row" for row in battle_log.rows)
        else "valid"
    )


def _guard_battle_rows(connection: Any, rows: list[Any]) -> list[Any]:
    """Lock resolved seasons before filtering rolling battle input.

    A ranked-day version is not required to identify a canonical season:
    finalization can race a partial season while its day row is absent.
    Unknown days remain writable until canonical season metadata exists;
    finalization itself requires canonical bounds and takes the global gate.
    """
    from .season_lifecycle import (
        acquire_season_lock_shared,
        filter_live_rows,
        retired_day_ranges,
    )

    seasons: set[str] = set()
    anchors = connection.execute(
        """
        SELECT current_league_season_id, previous_league_season_id,
               current_start, previous_start
        FROM legend_season_anchors
        WHERE state = 'confirmed' AND anchor_rule_version = %s
        """,
        (SEASON_ANCHOR_RULE_VERSION,),
    ).fetchall()
    canonical_bounds: list[tuple[str, Any, Any]] = []
    for anchor in anchors:
        current_start, previous_start = anchor[2], anchor[3]
        canonical_bounds.append(
            (
                text_value(anchor[0]),
                current_start,
                current_start + SEASON_DURATION,
            )
        )
        canonical_bounds.append(
            (
                text_value(anchor[1]),
                previous_start,
                previous_start + SEASON_DURATION,
            )
        )
    days = {item.battle.ranked_day_start for item in rows if item.battle is not None}
    for day in sorted(days):
        canonical_ids = {
            season_id
            for season_id, start, end in canonical_bounds
            if start <= day < end
        }
        seasons.update(canonical_ids)
        # A ranked day always ends RANKED_DAY_DURATION after it starts. The
        # range on ranked_day_end keeps the (ranked_day_end, id DESC) index as
        # the only ordered path; an equality lets PostgreSQL walk the primary
        # key backwards through every newer day's rows instead.
        day_end = day + RANKED_DAY_DURATION
        season_rows = connection.execute(
            """
            SELECT official_season_id FROM ranked_day_versions
            WHERE ranked_day_end >= %s AND ranked_day_end <= %s
              AND ranked_day_start = %s
            ORDER BY ranked_day_end, id DESC LIMIT 1
            """,
            (day_end, day_end, day),
        ).fetchall()
        seasons.update(text_value(row[0]) for row in season_rows)
        retired_rows = []
        retired_rows = connection.execute(
            """
            SELECT official_season_id FROM season_detail_retirements
            WHERE status IN ('finalized', 'retired')
              AND season_start <= %s AND season_end > %s
            """,
            (day, day),
        ).fetchall()
        seasons.update(text_value(row[0]) for row in retired_rows)
    for season_id in sorted(seasons):
        acquire_season_lock_shared(connection, season_id)
    # The first read only discovers lock keys. Re-read after blocking on
    # those locks so a just-committed finalization is never stale here.
    retired_ranges = retired_day_ranges(connection)
    return filter_live_rows(
        rows,
        lambda item: item.battle.ranked_day_start,
        retired_ranges,
    )


def _recheck_battle_rows(connection: Any, rows: list[Any]) -> None:
    """Recheck the retirement fence while the season locks are held."""
    from .season_lifecycle import filter_live_rows, retired_day_ranges

    if len(
        filter_live_rows(
            rows,
            lambda item: item.battle.ranked_day_start,
            retired_day_ranges(connection),
        )
    ) != len(rows):
        raise DomainRuleError(
            "season_detail_retired",
            "battle log crossed a committed season detail fence",
        )


def _report_hash(battle_log: ParsedBattleLog, row: ParsedBattleRow) -> str:
    # Position and poll time are not report identity. Reporter and parser
    # are: two perspectives or two interpretations must not overwrite.
    identity = json.dumps(
        [
            battle_log.normalized_tag,
            battle_log.parser_version,
            row.outcome,
            row.failure_category,
            row.source_json,
            (
                row.battle.attacker_gain,
                row.battle.defender_loss,
                row.battle.trophy_rule_version,
            )
            if row.battle is not None
            else None,
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _record_battle_sources(
    connection: Any,
    battle_log: ParsedBattleLog,
    payload_id: int,
    log_id: int,
    reporter_id: int,
) -> dict[int, int]:
    """Each source row index's saved battle report id, saving the new ones.

    A battle log repeats most of its rows from the player's last one, and
    looking a report up by its hash reads a random part of a large index.
    So the rows the newest earlier listed log of this player already holds
    are matched by hash in that list, read by id, and only the rest are
    looked up or saved by hash. A report's hash names exactly one row, so
    the ids are the ones a lookup by hash would find.
    """
    rows = [
        {
            "source_row_index": row.source_row_index,
            "outcome": row.outcome,
            "failure_category": row.failure_category,
            "source_json": row.source_json,
            "report_hash": _report_hash(battle_log, row),
        }
        for row in battle_log.rows
    ]
    known = {
        text_value(row[0]): int(row[1])
        for row in connection.execute(
            """
            WITH input AS (
                SELECT * FROM jsonb_to_recordset(%s::jsonb) AS row_data (
                    source_row_index integer, outcome text, failure_category text,
                    source_json jsonb, report_hash text
                )
            ), known AS (
                SELECT source.report_hash, source.id
                FROM (
                    SELECT log.parsed_payload_id FROM battle_log_observations AS log
                    WHERE log.player_id = %s AND log.parser_version = %s
                      AND log.id <> %s AND log.parsed_payload_id IS NOT NULL
                    ORDER BY log.observed_at DESC, log.id DESC LIMIT 1
                ) AS earlier
                JOIN battle_payload_row_lists AS list
                  ON list.parsed_payload_id = earlier.parsed_payload_id
                 AND list.reporting_player_id = %s
                CROSS JOIN LATERAL unnest(list.source_row_ids) AS member (id)
                JOIN battle_source_rows AS source ON source.id = member.id
                WHERE source.report_hash IS NOT NULL
            ), saved AS (
                INSERT INTO battle_source_rows (
                    report_hash, source_row_index, outcome, failure_category,
                    source_json
                )
                SELECT report_hash, 0, outcome, failure_category, source_json
                FROM input
                WHERE report_hash NOT IN (SELECT report_hash FROM known)
                ORDER BY report_hash
                ON CONFLICT (report_hash) WHERE report_hash IS NOT NULL DO NOTHING
            )
            SELECT report_hash, id FROM known
            """,
            (
                Jsonb(rows),
                reporter_id,
                battle_log.parser_version,
                log_id,
                reporter_id,
            ),
        ).fetchall()
    }
    # One row lists the whole log; array position N is source row N - 1.
    # A payload first listed per row, before the list table, stays so.
    assert [row["source_row_index"] for row in rows] == list(range(len(rows)))
    listed = connection.execute(
        """
        WITH input AS (
            SELECT * FROM jsonb_to_recordset(%s::jsonb) AS member (
                source_row_index integer, report_hash text, source_row_id bigint
            )
        ), members AS (
            INSERT INTO battle_payload_row_lists (
                parsed_payload_id, reporting_player_id, source_row_ids
            )
            SELECT %s, %s, array_agg(
                -- Only a report the earlier list did not hold is looked up.
                COALESCE(input.source_row_id, (
                    SELECT source.id FROM battle_source_rows AS source
                    WHERE source.report_hash = input.report_hash
                ))
                ORDER BY input.source_row_index
            )
            FROM input
            HAVING count(*) > 0 AND NOT EXISTS (
                SELECT 1 FROM battle_payload_rows AS member
                WHERE member.parsed_payload_id = %s
                  AND member.reporting_player_id = %s
            )
            ON CONFLICT (parsed_payload_id, reporting_player_id) DO NOTHING
            RETURNING source_row_ids
        ), updated AS (
            UPDATE battle_log_observations SET parsed_payload_id = %s
            WHERE id = %s
        )
        SELECT source_row_ids FROM members
        """,
        (
            Jsonb(
                [
                    {
                        "source_row_index": row["source_row_index"],
                        "report_hash": row["report_hash"],
                        "source_row_id": known.get(row["report_hash"]),
                    }
                    for row in rows
                ]
            ),
            payload_id,
            reporter_id,
            payload_id,
            reporter_id,
            payload_id,
            log_id,
        ),
    ).fetchone()
    if listed is not None:
        # The list just saved is the one this log reads back.
        return {
            index: int(source_row_id)
            for index, source_row_id in enumerate(listed[0])
            if source_row_id is not None
        }
    return {
        int(row[0]): int(row[1])
        for row in connection.execute(
            """
            SELECT source_row_index, source_row_id
            FROM battle_log_observation_source_rows
            WHERE battle_log_observation_id = %s
            """,
            (log_id,),
        ).fetchall()
    }
