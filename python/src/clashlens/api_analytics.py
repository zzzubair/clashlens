from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import partial
from time import monotonic
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

from .api_db import (
    ARMY_ANALYTICS_ADMISSION_TIMEOUT_SECONDS,
    ARMY_ANALYTICS_PARALLEL_POOL_TIMEOUT_SECONDS,
    ARMY_ANALYTICS_PRIMARY_POOL_TIMEOUT_SECONDS,
    ARMY_ANALYTICS_QUERY_WORK_MEM,
    ApiDatabase,
    _json_array,
    _public_army,
    _season_reset_waiting_sql,
    _text,
)
from .army_analytics import (
    ArmyAnalyticsSelection,
    ArmyAnalyticsUnavailable,
    CurrentSeasonEmpty,
    build_army_result,
    finish_army_result,
    public_army_states,
    row_label,
)
from .army_decoder import CURRENT_DECODES, DECODER_VERSION
from .army_history import HISTORY_READ_CATEGORIES, HISTORY_SORTS, usage_rows
from .army_rank_bands import (
    band_of,
    fact_rank_band_digest,
    rank_band_digest,
    read_rank_band_totals,
)
from .army_season_summaries import PROJECTION_VERSION as ARMY_HISTORY_VERSION
from .catalog import CATALOG_VERSION
from .domain import (
    RANKED_DAY_DURATION,
    SEASON_ANCHOR_RULE_VERSION,
    SEASON_DURATION,
    ranked_day_for,
    season_opening_reset,
)


def get_army_season_summary(
    database: ApiDatabase,
    official_season_id: str,
    lens: str,
    category: str,
    sort: str,
    *, offset: int = 0,
) -> dict[str, Any] | None:
    """Read one historical whole-season army aggregate from its summary.

    The stored row is already the complete historical record for the
    whole season (Legend days 1-28, whole-season sample): this never
    touches battle facts, snapshot cohorts, or individual battles, and
    never accepts a day range or population filter. A missing summary
    returns None (the caller reports unavailable); it never falls back
    to live detail.
    """
    if not 0 <= offset <= 1_000_000:
        raise ValueError("invalid history offset")
    if category not in HISTORY_READ_CATEGORIES or sort not in HISTORY_SORTS:
        return None
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT days_observed, days_missing, missing_days,
                   coverage_state, total_attacks, usable_army_sample,
                   army_states, unknown_affected_attacks,
                   unknown_component_occurrences,
                   perspective_disagreement_count,
                   missing_trophy_membership_evidence, result_rows,
                   projection_version, content_digest, unit_usage
            FROM army_season_summaries
            WHERE official_season_id = %s AND lens = %s AND category = %s
            """,
            (official_season_id, lens, "troops" if category == "siege" else category),
        ).fetchone()
        if row is None:
            return None
        if row[14] is None or _text(row[12]) != ARMY_HISTORY_VERSION:
            return None
        rows = usage_rows(_json_array(row[14]), category, int(row[5]))
        sort_field = "usage_rate" if sort == "usage-rate" else "usage_count"
        rows.sort(key=lambda item: (-float(item[sort_field]), item["key"]))
        numeric_rows = [{key: value for key, value in item.items() if key != "label"}
                        for item in rows]
        read_digest = hashlib.sha256(json.dumps(
            numeric_rows, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        army_states = {
            _text(state): int(count) for state, count in dict(row[6] or {}).items()
        }
        # Summaries saved before unnamed ids counted kept old partial armies
        # apart; they count as read (army_analytics.with_unnamed_ids).
        army_states["fully_decoded"] = army_states.get(
            "fully_decoded", 0
        ) + army_states.pop("partial", 0)
        total_attacks = int(row[4])
        requested = {
            "lens": lens,
            "season": official_season_id,
            "start_day": 1,
            "end_day": 28,
            "population": "all",
            "category": category,
            "sort": sort,
        }
        publication_key = hashlib.sha256(
            json.dumps(requested, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "kind": "army-analytics",
            "history_usage_only": True,
            "pagination": {"offset": offset, "total_rows": len(rows),
                           "next_offset": offset + 200 if offset + 200 < len(rows) else None},
            "selection": requested,
            "total_attacks": total_attacks,
            "usable_army_sample": int(row[5]),
            "army_states": army_states,
            "army_states_sum_confirmed": sum(army_states.values()) == total_attacks,
            "unknown_affected_attacks": int(row[7]),
            "unknown_component_occurrences": int(row[8]),
            "perspective_disagreement_count": int(row[9]),
            "missing_trophy_membership_evidence": int(row[10]),
            "cohort_evidence": {
                "cohort_players": 0,
                "stale_or_uncertain_cohort_members": 0,
                "streak_excluded_players": 0,
                "shielded_player_days": 0,
            },
            "collection_coverage": {
                "state": "partial" if int(row[10]) else _text(row[3]),
                "completed_days": int(row[0]),
            },
            "freshness": {"state": "frozen"},
            "reproducibility": {
                "official_season_id": official_season_id,
                "legend_days": [1, 28],
                "snapshot_versions": [],
            },
            "versions": {
                "decoder": DECODER_VERSION,
                "catalog": CATALOG_VERSION,
                "analytics": _text(row[12]),
            },
            "publication_identity": (
                f"army-season-{publication_key[:24]}-{_text(row[13])[:16]}-{read_digest[:16]}"
            ),
            "rows": rows[offset:offset + 200],
        }


@contextmanager
def _army_troop_admission(database: ApiDatabase, selection: ArmyAnalyticsSelection, deadline: float):
    if selection.category != "troops":
        yield
        return
    if not database._army_troop_slots.acquire(
        timeout=_army_timeout(deadline, ARMY_ANALYTICS_ADMISSION_TIMEOUT_SECONDS)
    ):
        raise PoolTimeout("troop analytics capacity is busy")
    try:
        yield
    finally:
        database._army_troop_slots.release()


def consistent_top_members(
    connection, snapshot_ids: list[int], limit: int
) -> list[int]:
    """Players numbered within the top ``limit`` on every given saved board.

    Each board is the leaderboard saved just before a Reset from the last
    trophy counts seen. Old or uncertain counts still count; nobody below
    the cutoff moves up to fill a gap.
    """
    rows = connection.execute(
        """
        SELECT player_id FROM leaderboard_snapshot_entries
        WHERE snapshot_id=ANY(%s::bigint[]) AND position<=%s
        GROUP BY player_id HAVING count(DISTINCT snapshot_id)=%s
        """,
        (snapshot_ids, limit, len(snapshot_ids)),
    ).fetchall()
    return sorted(int(row[0]) for row in rows)


def get_army_analytics(
    database, selection: ArmyAnalyticsSelection, *, now: datetime | None = None
) -> dict[str, Any] | None:
    deadline = monotonic() + database._army_request_timeout_seconds
    # A bounded pre-checkout gate leaves one paired pool slot per admitted
    # troop request. Other categories never need the second connection.
    with (
        _army_troop_admission(database, selection, deadline),
        database.pool.connection(
            timeout=_army_timeout(
                deadline, ARMY_ANALYTICS_PRIMARY_POOL_TIMEOUT_SECONDS
            )
        ) as connection,
    ):
        with connection.transaction():
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            connection.execute(
                "SET LOCAL transaction_timeout = "
                f"'{_army_transaction_timeout_milliseconds(deadline)}ms'"
            )
            resolved = selection
            if selection.season == "current":
                anchor = connection.execute(
                    """
                    SELECT current_league_season_id, previous_league_season_id,
                           current_start
                    FROM legend_season_anchors
                    WHERE anchor_rule_version = %s AND state = 'confirmed'
                    """,
                    (SEASON_ANCHOR_RULE_VERSION,),
                ).fetchone()
                if anchor is None:
                    # Without a confirmed anchor there is no honest
                    # current-season identity; never fall back to the most
                    # recently published season.
                    raise CurrentSeasonEmpty(None)
                # Bound the default range using Reset chronology (05:00 UTC).
                # Publication delays do not make an ended day unfinished; the
                # population-specific coverage checks below decide availability.
                current_time = (
                    now.astimezone(UTC) if now is not None else datetime.now(tz=UTC)
                )
                if current_time >= anchor[2] + SEASON_DURATION:
                    # The anchor advances only when a newer profile is
                    # processed, which can lag the Reset. Once its 28 days
                    # have ended, the calendar's new season is current and
                    # has no anchor-confirmed day to serve yet.
                    ended_start = (
                        ranked_day_for(current_time).season_start - SEASON_DURATION
                    )
                    raise CurrentSeasonEmpty(str(int(ended_start.timestamp())))
                if current_time < anchor[2]:
                    latest_ended_day = 0
                else:
                    # A Legend day's interval ends one full day after the
                    # season anchor, so the count of fully elapsed days is
                    # the latest ended day number.
                    latest_ended_day = int(
                        (current_time - anchor[2]) // RANKED_DAY_DURATION
                    )
                clipped_end = min(selection.end_day, latest_ended_day)
                if clipped_end < selection.start_day:
                    if latest_ended_day == 0:
                        # No Legend-day interval has ended this season;
                        # name the previous season so callers can offer it
                        # explicitly instead of silently serving its
                        # publications for season=current.
                        raise CurrentSeasonEmpty(_text(anchor[1]))
                    # Intervals have ended but the requested range starts
                    # beyond them; name the affected days instead of
                    # clipping.
                    raise ArmyAnalyticsUnavailable(
                        list(range(selection.start_day, selection.end_day + 1))
                    )
                resolved = ArmyAnalyticsSelection.parse(
                    **{
                        **selection.as_dict(),
                        "season": _text(anchor[0]),
                        "end_day": clipped_end,
                    }
                )
            day_rows = connection.execute(
                """
                SELECT DISTINCT season_day_number, ranked_day_start
                FROM api_player_daily_logs
                WHERE official_season_id = %s
                  AND state = 'Complete' AND coverage = 'complete'
                  AND season_day_number BETWEEN %s AND %s
                """,
                (resolved.season, resolved.start_day, resolved.end_day),
            ).fetchall()
            day_starts = {int(row[0]): row[1] for row in day_rows}
            completed_day_rows = connection.execute(
                """
                SELECT season_day_number, fact_input_hash
                FROM army_analytics_completed_days
                WHERE official_season_id = %s
                  AND season_day_number BETWEEN %s AND %s
                ORDER BY season_day_number
                """,
                (resolved.season, resolved.start_day, resolved.end_day),
            ).fetchall()
            completed_days = {int(row[0]) for row in completed_day_rows}
            population = resolved.population
            streak = population.startswith("streak-top-")
            requested_days = list(range(resolved.start_day, resolved.end_day + 1))
            covered_days = [
                day
                for day in requested_days
                if day in day_starts and day in completed_days
            ]
            if (
                selection.season != "current" or streak
            ) and covered_days != requested_days:
                raise ArmyAnalyticsUnavailable(
                    [day for day in requested_days if day not in covered_days]
                )
            snapshot_versions: list[int] = []
            snapshot_ids: list[int] = []
            missing_trophies = 0
            member_ids: list[int] | None = None
            band_by_player: dict[int, int] | None = None
            streak_version_digest: str | None = None
            cohort_evidence: dict[str, int] | None = None
            minimum: int | None = None
            maximum: int | None = None
            boundary_by_day = {
                day: day_starts[day] + timedelta(days=1) for day in covered_days
            }
            snapshots = connection.execute(
                """
                SELECT DISTINCT ON (boundary_at)
                       boundary_at, id, version
                FROM leaderboard_snapshots
                WHERE snapshot_kind='frozen' AND state='published'
                  AND boundary_at = ANY(%s::timestamptz[])
                ORDER BY boundary_at, version DESC
                """,
                (list(boundary_by_day.values()),),
            ).fetchall()
            by_boundary = {row[0]: (int(row[1]), int(row[2])) for row in snapshots}
            # Ended requested days a Consistent top view cannot use: no
            # completed army data or no published saved board.
            streak_gap_days = [
                day
                for day in requested_days
                if day not in covered_days or boundary_by_day[day] not in by_boundary
            ]
            if (
                selection.season == "current"
                and not streak
                and not population.startswith("trophies-")
            ):
                ranked_days = [
                    day for day in covered_days if day not in streak_gap_days
                ]
                covered_days = [
                    day
                    for day in covered_days
                    if ranked_days and day <= ranked_days[-1]
                ]
            if not covered_days:
                raise ArmyAnalyticsUnavailable(requested_days)
            if selection.season == "current" and not streak:
                resolved = ArmyAnalyticsSelection.parse(
                    **{
                        **resolved.as_dict(),
                        "start_day": covered_days[0],
                        "end_day": covered_days[-1],
                    }
                )
            completed_day_signature = tuple(
                (int(row[0]), _text(row[1]))
                for row in completed_day_rows
                if int(row[0]) in covered_days
            )
            requested = resolved.as_dict()
            if population.startswith("trophies-"):
                minimum, maximum = map(int, population.split("-")[1:])
            else:
                needed_days = requested_days if streak else covered_days[-1:]
                unavailable_days = [
                    day
                    for day in needed_days
                    if boundary_by_day[day] not in by_boundary
                ]
                if unavailable_days:
                    raise ArmyAnalyticsUnavailable(unavailable_days)
                snapshot_ids = [
                    by_boundary[boundary_by_day[day]][0] for day in needed_days
                ]
                snapshot_versions = [
                    by_boundary[boundary_by_day[day]][1] for day in needed_days
                ]
                if streak:
                    limit = int(population.removeprefix("streak-top-"))
                    member_ids = consistent_top_members(
                        connection, snapshot_ids, limit
                    )
                else:
                    if population.startswith("top-"):
                        low, high = 1, int(population.removeprefix("top-"))
                    else:
                        low, high = map(
                            int, population.removeprefix("band-").split("-")
                        )
                    members = connection.execute(
                        """
                        SELECT player_id, position FROM leaderboard_snapshot_entries
                        WHERE snapshot_id=%s AND position BETWEEN %s AND %s
                        """,
                        (snapshot_ids[0], low, high),
                    ).fetchall()
                    band_by_player = {
                        int(row[0]): band_of(int(row[1])) for row in members
                    }
                    member_ids = sorted({int(row[0]) for row in members})
                excluded_players = 0
                shielded_player_days = 0
                if population.startswith("streak-top-"):
                    # Excluded streak players appear in the selected Top-N
                    # on at least one saved board but miss it on another.
                    any_snapshot_ids = {
                        int(row[0])
                        for row in connection.execute(
                            """
                            SELECT DISTINCT player_id
                            FROM leaderboard_snapshot_entries
                            WHERE snapshot_id = ANY(%s::bigint[])
                              AND position <= %s
                            """,
                            (snapshot_ids, limit),
                        ).fetchall()
                    }
                    excluded_players = len(any_snapshot_ids - set(member_ids))
                    # Shielded-day evidence for streak members:
                    # one row per member-day whose current ranked-day
                    # version inferred a shield.
                    if member_ids is not None:
                        version_row = connection.execute(
                            """
                            WITH selected_days AS (
                                SELECT unnest(%s::timestamptz[]) AS ranked_day_start
                            ), current_versions AS (
                                SELECT selected.player_id, days.ranked_day_start,
                                       current_version.id, current_version.shield_state
                                FROM unnest(%s::bigint[]) AS selected(player_id)
                                CROSS JOIN selected_days AS days
                                LEFT JOIN LATERAL (
                                    SELECT rv.id, rv.shield_state
                                    FROM ranked_day_versions AS rv
                                    WHERE rv.player_id = selected.player_id
                                      AND rv.ranked_day_start = days.ranked_day_start
                                    ORDER BY rv.version DESC
                                    LIMIT 1
                                ) AS current_version ON true
                            )
                            SELECT count(*) FILTER (
                                       WHERE shield_state = 'inferred_shielded'
                                   ),
                                   encode(
                                       sha256(convert_to(
                                           COALESCE(
                                               string_agg(
                                                   COALESCE(id::text, 'null') ||
                                                   ':' || COALESCE(
                                                       shield_state, 'null'
                                                   ),
                                                   ',' ORDER BY player_id,
                                                               ranked_day_start
                                               ), ''
                                           ), 'UTF8'
                                       )), 'hex'
                                   )
                            FROM current_versions
                            """,
                            (
                                [day_starts[day] for day in covered_days],
                                sorted(member_ids),
                            ),
                        ).fetchone()
                        assert version_row is not None
                        shielded_player_days = int(version_row[0])
                        streak_version_digest = _text(version_row[1])
                    # Members whose saved trophy count was old or
                    # uncertain on at least one day stay in the group;
                    # this only says how many there are.
                    stale_or_uncertain_members = int(
                        connection.execute(
                            """
                            SELECT count(*) FROM (
                                SELECT player_id
                                FROM leaderboard_snapshot_entries
                                WHERE snapshot_id = ANY(%s::bigint[])
                                  AND position <= %s
                                GROUP BY player_id
                                HAVING count(DISTINCT snapshot_id) = %s
                                   AND bool_or(
                                       NOT (freshness = 'fresh'
                                            AND confidence = 'confirmed'))
                            ) AS weak_members
                            """,
                            (snapshot_ids, limit, len(snapshot_ids)),
                        ).fetchone()[0]
                    )
                else:
                    low, high = (
                        (1, int(population.removeprefix("top-")))
                        if population.startswith("top-")
                        else map(int, population.removeprefix("band-").split("-"))
                    )
                    stale_or_uncertain_members = int(
                        connection.execute(
                            """
                            SELECT count(*) FROM leaderboard_snapshot_entries
                            WHERE snapshot_id = %s
                              AND position BETWEEN %s AND %s
                              AND NOT (freshness = 'fresh'
                                       AND confidence = 'confirmed')
                            """,
                            (snapshot_ids[0], low, high),
                        ).fetchone()[0]
                    )
                cohort_evidence = {
                    "cohort_players": len(member_ids),
                    "stale_or_uncertain_cohort_members": stale_or_uncertain_members,
                    "streak_excluded_players": excluded_players,
                    "shielded_player_days": shielded_player_days,
                }
            cache_key: tuple[Any, ...] = (
                json.dumps(
                    selection.as_dict(), sort_keys=True, separators=(",", ":")
                ),
                json.dumps(
                    resolved.as_dict(), sort_keys=True, separators=(",", ":")
                ),
                completed_day_signature,
                tuple(member_ids) if member_ids is not None else None,
                tuple(zip(snapshot_ids, snapshot_versions)),
                streak_version_digest,
                tuple(streak_gap_days),
            )
            cached = database._army_cache_get(cache_key)
            if cached is not None:
                return cached
            if resolved.category == "troops":
                connection.execute(
                    f"SET LOCAL work_mem = '{ARMY_ANALYTICS_QUERY_WORK_MEM}'"
                )
            if population.startswith("trophies-"):
                missing_trophies = int(
                    connection.execute(
                        """
                        SELECT count(*)
                        FROM army_analytics_battle_facts
                        WHERE official_season_id=%s
                          AND season_day_number = ANY(%s::integer[])
                          AND lens=%s AND is_current
                          AND battle_time_trophies IS NULL
                        """,
                        (resolved.season, covered_days, resolved.lens),
                    ).fetchone()[0]
                )
            component_column = _ARMY_ANALYTICS_COMPONENT_COLUMN[resolved.category]
            fact_filters = [
                "official_season_id = %s",
                "season_day_number = ANY(%s::integer[])",
                "lens = %s",
                "is_current",
            ]
            fact_params: list[Any] = [resolved.season, covered_days, resolved.lens]
            if member_ids is None:
                assert minimum is not None and maximum is not None
                fact_filters.append("battle_time_trophies BETWEEN %s AND %s")
                fact_params.extend((minimum, maximum))
            else:
                fact_filters.append("population_player_id = ANY(%s::bigint[])")
                fact_params.append(member_ids)
            banded = None
            band_digest: str | None = None
            if band_by_player is not None:
                banded = read_rank_band_totals(
                    connection,
                    snapshot_ids[0],
                    lens=resolved.lens,
                    category=resolved.category,
                    population=population,
                    day_markers=dict(completed_day_signature),
                )
            if banded is not None:
                # Same numbers as counting the facts, from at most 28 x 17
                # saved rows.
                result, band_digest = finish_army_result(banded[0], resolved), banded[1]
            elif resolved.category == "troops":
                result, source_hash = _query_troops_aggregates(
                    connection,
                    fact_filters=fact_filters,
                    fact_params=fact_params,
                    requested=requested,
                    snapshot_ids=snapshot_ids,
                    selection=resolved,
                    pool=database.pool,
                    deadline=deadline,
                    source_hash=None
                    if band_by_player is None
                    else lambda source_connection: _rank_band_source_hash(
                        fact_rank_band_digest(
                            source_connection,
                            season_id=resolved.season,
                            lens=resolved.lens,
                            population=population,
                            days=covered_days,
                            band_by_player=band_by_player,
                        ),
                        requested,
                        snapshot_ids,
                    ),
                )
            else:
                facts_rows = connection.execute(
                    f"""
                    SELECT id, battle_id, population_player_id,
                           battle_time_trophies, stars, destruction_percentage,
                           army_state, failure_reason,
                           {component_column} AS component_payload,
                           unresolved_components, perspective_disagreement,
                           input_hash, source_ranked_day_version_id,
                           season_day_number
                    FROM army_analytics_battle_facts_with_armies
                    WHERE {" AND ".join(fact_filters)}
                    ORDER BY battle_id
                    """,
                    tuple(fact_params),
                ).fetchall()
                source_digest = hashlib.sha256()
                source_digest.update(b'{"facts":[')
                band_facts: dict[tuple[int, int], list[str]] = {}

                def selected_facts():
                    separator = b""
                    for row in facts_rows:
                        fact_id = int(row[0])
                        input_hash = _text(row[11])
                        source_digest.update(separator)
                        source_digest.update(f'[{fact_id},"{input_hash}"]'.encode())
                        separator = b","
                        if band_by_player is not None:
                            band_facts.setdefault(
                                (int(row[13]), band_by_player[int(row[2])]), []
                            ).append(f"{fact_id}:{input_hash}")
                        components = {
                            "home_troops": [],
                            "spells": [],
                            "siege": [],
                            "cc_troops": [],
                            "heroes": [],
                        }
                        components[component_column] = row[8]
                        yield {
                            "id": fact_id,
                            "battle_id": int(row[1]),
                            "population_player_id": int(row[2]),
                            "battle_time_trophies": (
                                None if row[3] is None else int(row[3])
                            ),
                            "stars": int(row[4]),
                            "destruction_percentage": int(row[5]),
                            "army_state": _text(row[6]),
                            "failure_reason": None
                            if row[7] is None
                            else _text(row[7]),
                            **components,
                            "unresolved_components": row[9],
                            "perspective_disagreement": bool(row[10]),
                            "input_hash": input_hash,
                            "source_ranked_day_version_id": int(row[12]),
                        }

                result = build_army_result(selected_facts(), resolved)
                source_digest.update(b'],"selection":')
                source_digest.update(
                    json.dumps(
                        requested, sort_keys=True, separators=(",", ":")
                    ).encode()
                )
                source_digest.update(b',"snapshots":')
                source_digest.update(
                    json.dumps(snapshot_ids, separators=(",", ":")).encode()
                )
                source_digest.update(b"}")
                source_hash = source_digest.hexdigest()
                del facts_rows
                if band_by_player is not None:
                    band_digest = rank_band_digest(
                        population,
                        covered_days,
                        {
                            key: hashlib.sha256(",".join(facts).encode()).hexdigest()
                            for key, facts in band_facts.items()
                        },
                    )
            if band_digest is not None:
                # Saved totals and fact reads give a Top N or rank-band view
                # the same evidence hash for the same facts.
                source_hash = _rank_band_source_hash(
                    band_digest, requested, snapshot_ids
                )
            result["missing_trophy_membership_evidence"] = missing_trophies
            result["collection_coverage"] = {
                **result["collection_coverage"],
                "completed_days": len(covered_days),
                "covered_days": covered_days,
                "streak_gap_days": streak_gap_days,
            }
            if cohort_evidence is None:
                cohort_evidence = {
                    "cohort_players": 0,
                    "stale_or_uncertain_cohort_members": 0,
                    "streak_excluded_players": 0,
                    "shielded_player_days": 0,
                }
            result["cohort_evidence"] = cohort_evidence
            result["reproducibility"] = {
                "official_season_id": resolved.season,
                "legend_days": [resolved.start_day, resolved.end_day],
                "snapshot_versions": snapshot_versions,
            }
            # The result hash covers both the aggregates and the retained
            # source-evidence hash, so corrected inputs always change the
            # published identity.
            result_hash = hashlib.sha256(
                json.dumps(
                    {
                        "result": result,
                        "source_evidence": source_hash,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            result["reproducibility"]["source_evidence_hash"] = source_hash
            # Anonymous reads are calculated on the spot and never
            # persist a row per URL selection. The public identity is
            # derived deterministically from the selection and the result
            # hash (which already covers the source-evidence hash), so the
            # same retained inputs reproduce it and corrected inputs
            # change it without any database writes.
            publication_key = hashlib.sha256(
                json.dumps(
                    requested, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            result["selection"] = requested
            result["publication_identity"] = (
                f"army-publication-{publication_key[:24]}-{result_hash[:16]}"
            )
            database._army_cache_put(cache_key, result)
            return deepcopy(result)


def get_battle_army(
    database, battle_id: int, perspective: str
) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
        row = connection.execute(
            f"""
            SELECT battle_id, perspective, status, failure_category,
                   home_troops, spells, siege, cc_troops, heroes,
                   unresolved_components, decoder_version, catalog_version
            FROM {CURRENT_DECODES} AS decode WHERE perspective = %s
            """,
            (DECODER_VERSION, [battle_id], perspective),
        ).fetchone()
        return None if row is None else _public_army(row)


def get_basic_analytics(
    database: ApiDatabase,
    *,
    now: datetime,
    freshness_seconds: int,
) -> dict[str, Any]:
    with database.pool.connection() as connection:
        row = connection.execute(
            f"""
            SELECT count(*), avg(profile.trophies),
                   count(*) FILTER (
                       WHERE player.current_observed_at
                             >= %s - make_interval(secs => %s)
                   )
            FROM players AS player
            JOIN player_profile_versions AS profile
                ON profile.id = player.current_profile_version_id
            -- Trophies from before a player's Season reset are left out,
            -- including first-day trophies equal to the frozen final board or
            -- not explained by the day's recorded battles.
            WHERE player.active = true
              AND profile.current_league_season_id = %s
              AND NOT {_season_reset_waiting_sql(
                  "player.id", "profile.trophies", "%s"
              )}
            """,
            (
                now,
                freshness_seconds,
                ranked_day_for(now).official_season_id,
                season_opening_reset(now),
            ),
        ).fetchone()
        sample_size = int(row[0])
        fresh = int(row[2])
        return {
            "population": "tracked_players",
            "period": {"as_of": now.astimezone(UTC).isoformat()},
            "sample_size": sample_size,
            "coverage": {
                "profile_observations": sample_size,
                "battle_observations": 0,
            },
            "freshness": {"fresh": fresh, "stale": sample_size - fresh},
            "classification_state": "unclassified",
            "classification_version": None,
            "analytics_rule_version": "basic-profile-v1",
            "unclassified_count": 0,
            "results": {
                "average_trophies": None if row[1] is None else float(row[1])
            },
        }


def _army_timeout(deadline: float | None, ceiling: float) -> float:
    if deadline is None:
        return ceiling
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise PoolTimeout("army analytics request budget is exhausted")
    return min(ceiling, remaining)




def _army_transaction_timeout_milliseconds(deadline: float) -> int:
    remaining = int((deadline - monotonic()) * 1000)
    if remaining < 1:
        raise PoolTimeout("army analytics request budget is exhausted")
    return remaining




def _build_troops_result_from_sql(
    summary: tuple[Any, ...], selection: ArmyAnalyticsSelection
) -> dict[str, Any]:
    """Build the public troops result from PostgreSQL-owned aggregates."""
    (
        total_facts,
        usable_count,
        unknown_affected,
        unknown_occurrences,
        disagreement_count,
        state_counts,
        aggregate_rows,
    ) = summary
    army_states = public_army_states(
        {_text(key): int(value) for key, value in (state_counts or {}).items()}
    )

    rows = []
    for aggregate in aggregate_rows or []:
        key = _text(aggregate["key"])
        sample = int(aggregate["usage_count"])
        star_counts = [int(value) for value in aggregate["star_counts"]]
        star_rates = [count / sample if sample else 0 for count in star_counts]
        stars = int(aggregate["stars"])
        destruction = int(aggregate["destruction"])
        rows.append(
            {
                "key": key,
                "label": row_label(key),
                "usage_count": sample,
                "usage_denominator": int(usable_count),
                "usage_rate": sample / usable_count if usable_count else 0,
                "star_counts": star_counts,
                "star_rates": star_rates,
                "three_star_rate": star_rates[3],
                "average_stars": stars / sample if sample else 0,
                "average_destruction": destruction / sample if sample else 0,
                "unknown_excluded_attacks": 0,
            }
        )
    sort_field = {
        "usage-rate": "usage_rate",
        "usage-count": "usage_count",
        "three-star-rate": "three_star_rate",
        "average-stars": "average_stars",
        "average-destruction": "average_destruction",
    }[selection.sort]
    rows.sort(key=lambda row: (-float(row[sort_field]), row["key"]))
    return {
        "kind": "army-analytics",
        "total_attacks": int(total_facts),
        "usable_army_sample": int(usable_count),
        "army_states": army_states,
        "army_states_sum_confirmed": sum(army_states.values()) == int(total_facts),
        "unknown_affected_attacks": int(unknown_affected),
        "unknown_component_occurrences": int(unknown_occurrences),
        "perspective_disagreement_count": int(disagreement_count),
        "missing_trophy_membership_evidence": 0,
        "collection_coverage": {
            "state": "complete",
            "completed_days": selection.end_day - selection.start_day + 1,
        },
        "freshness": {"state": "frozen"},
        "versions": {
            "decoder": "army-decoder-v2",
            "catalog": CATALOG_VERSION,
            "analytics": "army-analytics-v2",
        },
        "rows": rows,
    }




def _selected_source_hash(
    connection: Any,
    *,
    fact_filters: list[str],
    fact_params: list[Any],
    requested: dict[str, str | int],
    snapshot_ids: list[int],
) -> str:
    """Hash ordered selected evidence in bounded PostgreSQL-built chunks."""
    digest = hashlib.sha256()
    digest.update(b'{"facts":[')
    chunks = connection.execute(
        f"""
        SELECT string_agg(
                   '[' || id::text || ',"' || input_hash || '"]',
                   ',' ORDER BY battle_id
               )
        FROM army_analytics_battle_facts
        WHERE {" AND ".join(fact_filters)}
        GROUP BY (battle_id - 1) / 10000
        ORDER BY (battle_id - 1) / 10000
        """,
        tuple(fact_params),
    )
    separator = b""
    for (chunk,) in chunks:
        digest.update(separator)
        digest.update(_text(chunk).encode())
        separator = b","
    digest.update(b'],"selection":')
    digest.update(json.dumps(requested, sort_keys=True, separators=(",", ":")).encode())
    digest.update(b',"snapshots":')
    digest.update(json.dumps(snapshot_ids, separators=(",", ":")).encode())
    digest.update(b"}")
    return digest.hexdigest()




def _rank_band_source_hash(
    band_digest: str, requested: dict[str, str | int], snapshot_ids: list[int]
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "rank_band_facts": band_digest,
                "selection": requested,
                "snapshots": snapshot_ids,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _query_troops_aggregates(
    connection: Any,
    *,
    fact_filters: list[str],
    fact_params: list[Any],
    requested: dict[str, str | int],
    snapshot_ids: list[int],
    selection: ArmyAnalyticsSelection,
    pool: ConnectionPool | None = None,
    deadline: float | None = None,
    source_hash: Callable[[Any], str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Aggregate the troops projection and source identity inside PostgreSQL.

    ``source_hash`` computes the source identity on a given connection; it
    defaults to ``_selected_source_hash`` over the selected facts.
    """
    if source_hash is None:
        source_hash = partial(
            _selected_source_hash,
            fact_filters=fact_filters,
            fact_params=fact_params,
            requested=requested,
            snapshot_ids=snapshot_ids,
        )
    fact_where = " AND ".join(fact_filters)
    state_rows = connection.execute(
        f"""
        SELECT army_state, count(*),
               count(*) FILTER (
                   WHERE CASE
                       WHEN jsonb_typeof(unresolved_components) = 'array'
                       THEN jsonb_array_length(unresolved_components) > 0
                       ELSE false
                   END
               ),
               COALESCE(sum(
                   CASE WHEN jsonb_typeof(unresolved_components) = 'array'
                        THEN jsonb_array_length(unresolved_components) ELSE 0 END
               ), 0),
               count(*) FILTER (WHERE perspective_disagreement)
        FROM army_analytics_battle_facts_with_armies
        WHERE {fact_where}
        GROUP BY army_state
        """,
        tuple(fact_params),
    ).fetchall()
    component_query = f"""
        SELECT component.key, count(*),
               count(*) FILTER (WHERE selected.stars = 0),
               count(*) FILTER (WHERE selected.stars = 1),
               count(*) FILTER (WHERE selected.stars = 2),
               count(*) FILTER (WHERE selected.stars = 3),
               sum(selected.stars),
               sum(selected.destruction_percentage)
        FROM army_analytics_battle_facts_with_armies AS selected
        CROSS JOIN LATERAL (
            SELECT DISTINCT
                   CASE jsonb_typeof(value -> 0)
                       WHEN 'boolean' THEN CASE value ->> 0
                           WHEN 'true' THEN 'True' ELSE 'False' END
                       WHEN 'null' THEN 'None'
                       ELSE value ->> 0
                   END AS key
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(selected.home_troops) = 'array'
                     THEN selected.home_troops ELSE '[]'::jsonb END
            ) AS item(value)
            WHERE CASE WHEN jsonb_typeof(value) = 'array'
                       THEN jsonb_array_length(value) > 0
                       ELSE false END
            UNION
            -- Old partial armies kept unnamed home troop ids apart; they
            -- count like any other (army_analytics.with_unnamed_ids).
            SELECT 'troop:' || (value ->> 'numeric_id')
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(selected.unresolved_components) = 'array'
                     THEN selected.unresolved_components ELSE '[]'::jsonb END
            ) AS item(value)
            WHERE jsonb_typeof(value) = 'object' AND value ->> 'section' = 'u'
              AND value ->> 'numeric_id' IS NOT NULL
        ) AS component
        WHERE {fact_where}
          AND selected.army_state IN ('decoded', 'partial')
        GROUP BY component.key
        ORDER BY component.key
        """

    if pool is not None and pool.max_size >= 2:
        snapshot_row = connection.execute("VALUES (pg_export_snapshot())").fetchone()
        assert snapshot_row is not None
        shared_snapshot = _text(snapshot_row[0])
        # Acquire the paired connection before either expensive query starts.
        # Contention fails quickly instead of consuming the caller's five-second
        # budget and then repeating the source hash sequentially.
        with pool.connection(
            timeout=_army_timeout(
                deadline, ARMY_ANALYTICS_PARALLEL_POOL_TIMEOUT_SECONDS
            )
        ) as source_connection:
            with source_connection.transaction():
                source_connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                source_connection.execute(
                    psycopg.sql.SQL("SET TRANSACTION SNAPSHOT {}").format(
                        psycopg.sql.Literal(shared_snapshot)
                    )
                )
                if deadline is not None:
                    source_connection.execute(
                        "SET LOCAL transaction_timeout = "
                        f"'{_army_transaction_timeout_milliseconds(deadline)}ms'"
                    )
                source_connection.execute(
                    f"SET LOCAL work_mem = '{ARMY_ANALYTICS_QUERY_WORK_MEM}'"
                )
                with ThreadPoolExecutor(max_workers=1) as executor:
                    source_future = executor.submit(source_hash, source_connection)
                    aggregate_rows = connection.execute(
                        component_query, tuple(fact_params)
                    ).fetchall()
                    source_hash_value = source_future.result()
    else:
        aggregate_rows = connection.execute(
            component_query, tuple(fact_params)
        ).fetchall()
        source_hash_value = source_hash(connection)
    state_counts = {_text(row[0]): int(row[1]) for row in state_rows}
    summary = (
        sum(state_counts.values()),
        sum(
            count
            for state, count in state_counts.items()
            if state in {"decoded", "partial"}
        ),
        sum(int(row[2]) for row in state_rows),
        sum(int(row[3]) for row in state_rows),
        sum(int(row[4]) for row in state_rows),
        state_counts,
        [
            {
                "key": _text(row[0]),
                "usage_count": int(row[1]),
                "star_counts": [int(value) for value in row[2:6]],
                "stars": int(row[6]),
                "destruction": int(row[7]),
            }
            for row in aggregate_rows
        ],
    )
    return (
        _build_troops_result_from_sql(summary, selection),
        source_hash_value,
    )


_ARMY_ANALYTICS_COMPONENT_COLUMN = {
    "troops": "home_troops",
    "spells": "spells",
    "siege": "siege",
    "cc-troops": "cc_troops",
    "cc-composition": "cc_troops",
    "heroes": "heroes",
    "pets": "heroes",
    "equipment": "heroes",
    "equipment-for-hero": "heroes",
    "hero-pet": "heroes",
    "hero-equipment": "heroes",
}
