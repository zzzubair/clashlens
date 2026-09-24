from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .api_db import (
    ApiDatabase,
    _public_confidence,
    _public_snapshot_confidence,
    _text,
)


def get_live_leaderboard(
    database: ApiDatabase,
    *,
    limit: int,
    offset: int = 0,
    now: datetime,
    freshness_seconds: int,
) -> dict[str, Any] | None:
    if offset < 0 or offset % limit:
        raise ValueError("offset must be non-negative and aligned to limit")
    with database.pool.connection() as connection:
        rows = connection.execute(
            """
            WITH selected AS MATERIALIZED (
                SELECT player.normalized_tag, profile.name, profile.trophies,
                       player.current_observed_at AS observed_at,
                       player.eligibility_state,
                       profile.profile_json -> 'clan' ->> 'name' AS clan
                FROM players AS player
                JOIN player_profile_versions AS profile
                  ON profile.id = player.current_profile_version_id
                WHERE player.active = true
                  AND profile.source_contract_state = 'accepted'
            ), stats AS (
                SELECT count(*) AS total_entries,
                       count(*) FILTER (
                           WHERE observed_at < %s - make_interval(secs => %s)
                       ) AS stale_count,
                       min(observed_at) AS oldest_observed_at,
                       max(observed_at) AS newest_observed_at
                FROM selected
            ), page AS MATERIALIZED (
                SELECT * FROM selected
                ORDER BY trophies DESC, md5(normalized_tag), normalized_tag
                LIMIT %s OFFSET %s
            ), totals AS (
                SELECT count(*)::bigint AS tracked_population FROM players WHERE active
            )
            SELECT page.normalized_tag, page.name, page.trophies,
                   page.observed_at, page.eligibility_state, page.clan,
                   CASE WHEN page.normalized_tag IS NOT NULL THEN
                       row_number() OVER (
                           ORDER BY page.trophies DESC, md5(page.normalized_tag),
                                    page.normalized_tag
                       ) + %s
                   END AS position,
                   stats.total_entries, stats.stale_count,
                   stats.oldest_observed_at, stats.newest_observed_at,
                   totals.tracked_population
            FROM stats CROSS JOIN totals
            LEFT JOIN page ON true
            ORDER BY position NULLS LAST
            """,
            (now, freshness_seconds, limit, offset, offset),
        ).fetchall()
        total_entries = int(rows[0][7]) if rows else 0
        if offset and offset >= total_entries:
            return None
        tracked_population = int(rows[0][11])
        stale_count = int(rows[0][8])
        oldest_observed_at = rows[0][9]
        newest_observed_at = rows[0][10]
        entries = []
        for row in rows:
            if row[0] is None:
                continue
            observed_at = row[3].astimezone(UTC)
            age = max(0.0, (now.astimezone(UTC) - observed_at).total_seconds())
            age_seconds = int(age)
            freshness = "fresh" if age <= freshness_seconds else "stale"
            entries.append(
                {
                    "position": int(row[6]),
                    "tag": _text(row[0]),
                    "name": _text(row[1]),
                    "trophies": int(row[2]),
                    "observed_at": observed_at.isoformat(),
                    "age_seconds": age_seconds,
                    "freshness": freshness,
                    "confidence": _text(row[4]),
                    "public_confidence": _public_confidence(True, _text(row[4])),
                    "clan": None if row[5] is None else _text(row[5]),
                    "official_rank": None,
                }
            )
        page_count = (total_entries + limit - 1) // limit
        page = offset // limit + 1
        return {
            "kind": "live",
            "ordering_rule_version": "tracked-trophies-md5-v1",
            "generated_at": now.astimezone(UTC).isoformat(),
            "tracked_population": tracked_population,
            "total_entries": total_entries,
            "page": page,
            "page_size": limit,
            "page_count": page_count,
            "has_previous": page > 1,
            "has_next": page < page_count,
            "coverage": {
                "state": "partial",
                "tracked_players": tracked_population,
                "measured_percent": (
                    100.0 * total_entries / tracked_population
                    if tracked_population
                    else 0.0
                ),
                "note": "Tracked-player publication; complete Legend I coverage is not claimed.",
            },
            "provenance": {
                "source": "current accepted player profiles",
                "observed_at": (
                    None
                    if newest_observed_at is None
                    else newest_observed_at.astimezone(UTC).isoformat()
                ),
                "freshness": "stale" if stale_count else "fresh",
                "confidence": "partial",
                "coverage": "partial",
                "version": "tracked-trophies-md5-v1",
            },
            "source_observations": {
                "oldest_observed_at": (
                    None
                    if oldest_observed_at is None
                    else oldest_observed_at.astimezone(UTC).isoformat()
                ),
                "newest_observed_at": (
                    None
                    if newest_observed_at is None
                    else newest_observed_at.astimezone(UTC).isoformat()
                ),
                "stale_count": stale_count,
            },
            "quality_states": ["partial"] + (["stale"] if stale_count else []),
            "entries": entries,
        }


def get_frozen_leaderboard(
    database: ApiDatabase,
    *,
    limit: int,
    offset: int = 0,
    official_season_id: str | None = None,
    season_day_number: int | None = None,
    now: datetime | None = None,
    freshness_seconds: int = 900,
) -> dict[str, Any] | None:
    if offset < 0 or offset % limit:
        raise ValueError("offset must be non-negative and aligned to limit")
    if (official_season_id is None) != (season_day_number is None):
        raise ValueError("season and day must be supplied together")
    now = datetime.now(UTC) if now is None else now
    with database.pool.connection() as connection:
        coordinator_exists, snapshots_exist = connection.execute(
            """
            SELECT to_regclass('boundary_publication_generations') IS NOT NULL
                       AND to_regclass('boundary_publication_manifest_rows') IS NOT NULL,
                   to_regclass('leaderboard_snapshots') IS NOT NULL
            """
        ).fetchone()
        legacy_publications = """
            SELECT 'legacy'::text AS source, leaderboard.id,
                   leaderboard.boundary_at, leaderboard.version, 0 AS generation,
                   selector.official_season_id, selector.season_day_number
            FROM api_frozen_leaderboards AS leaderboard
            JOIN LATERAL (
                SELECT day.official_season_id, day.season_day_number
                FROM ranked_day_versions AS day
                WHERE day.ranked_day_end = leaderboard.boundary_at
                ORDER BY day.id DESC LIMIT 1
            ) AS selector ON true
        """
        if coordinator_exists:
            v2_publications = """
                SELECT 'v2'::text AS source, snapshot.id, snapshot.boundary_at,
                       snapshot.version, COALESCE(generation.generation, 0) AS generation,
                       COALESCE(generation_day.official_season_id, source_day.official_season_id) AS official_season_id,
                       COALESCE(generation_day.season_day_number, source_day.season_day_number) AS season_day_number
                FROM leaderboard_snapshots AS snapshot
                LEFT JOIN ranked_day_versions AS source_day
                  ON source_day.id = snapshot.source_ranked_day_version_id
                LEFT JOIN boundary_publication_generations AS generation
                  ON generation.snapshot_id = snapshot.id
                LEFT JOIN LATERAL (
                    SELECT ranked.official_season_id, ranked.season_day_number
                    FROM boundary_publication_manifest_rows AS member
                    JOIN ranked_day_versions AS ranked
                      ON ranked.id = member.ranked_day_version_id
                    WHERE member.manifest_id = generation.snapshot_manifest_id
                    ORDER BY ranked.id DESC LIMIT 1
                ) AS generation_day ON true
                WHERE snapshot.snapshot_kind = 'frozen' AND snapshot.state = 'published'
                  AND (generation.id IS NULL OR generation.snapshot_state <> 'superseded')
            """
        elif snapshots_exist:
            v2_publications = """
                SELECT 'v2'::text AS source, snapshot.id, snapshot.boundary_at,
                       snapshot.version, 0 AS generation, source_day.official_season_id,
                       source_day.season_day_number
                FROM leaderboard_snapshots AS snapshot
                LEFT JOIN ranked_day_versions AS source_day
                  ON source_day.id = snapshot.source_ranked_day_version_id
                WHERE snapshot.snapshot_kind = 'frozen' AND snapshot.state = 'published'
            """
        else:
            v2_publications = ""
        publications = (
            v2_publications + " UNION ALL " + legacy_publications
            if v2_publications
            else legacy_publications
        )
        locator = connection.execute(
            f"""
            WITH publications AS ({publications}), selected AS (
                SELECT * FROM publications
                WHERE (%s::text IS NULL OR
                       (official_season_id = %s AND season_day_number = %s))
                ORDER BY boundary_at DESC, (source = 'v2') DESC,
                         version DESC, generation DESC, id DESC LIMIT 1
            )
            SELECT selected.source, selected.id,
                   (SELECT json_build_object(
                               'official_season_id', p.official_season_id,
                               'season_day_number', p.season_day_number)
                    FROM publications p WHERE p.boundary_at < selected.boundary_at
                    ORDER BY p.boundary_at DESC, (p.source = 'v2') DESC,
                             p.version DESC, p.generation DESC, p.id DESC LIMIT 1),
                   (SELECT json_build_object(
                               'official_season_id', p.official_season_id,
                               'season_day_number', p.season_day_number)
                    FROM publications p WHERE p.boundary_at > selected.boundary_at
                    ORDER BY p.boundary_at, (p.source = 'v2') DESC,
                             p.version DESC, p.generation DESC, p.id DESC LIMIT 1)
            FROM selected
            """,
            (official_season_id, official_season_id, season_day_number),
        ).fetchone()
        if locator is None:
            return None
        snapshot = None
        if _text(locator[0]) == "v2":
            if coordinator_exists:
                snapshot_query = """
                    SELECT snapshot.id, snapshot.boundary_at, snapshot.version,
                           snapshot.ordering_rule_version,
                           snapshot.freshness_rule_version,
                           snapshot.measured_coverage,
                           snapshot.eligible_population_count,
                           snapshot.included_entry_count, snapshot.stale_entry_count,
                           snapshot.fresh_entry_count, snapshot.excluded_missing_count,
                           snapshot.excluded_invalid_count,
                           snapshot.excluded_malformed_count,
                           snapshot.excluded_conflicting_count,
                           COALESCE(generation_day.official_season_id, source_day.official_season_id) AS official_season_id,
                           COALESCE(generation_day.season_day_number, source_day.season_day_number) AS season_day_number
                    FROM leaderboard_snapshots AS snapshot
                    LEFT JOIN ranked_day_versions AS source_day
                      ON source_day.id = snapshot.source_ranked_day_version_id
                    LEFT JOIN boundary_publication_generations AS generation
                      ON generation.snapshot_id = snapshot.id
                    LEFT JOIN LATERAL (
                        SELECT ranked.official_season_id, ranked.season_day_number
                        FROM boundary_publication_manifest_rows AS member
                        JOIN ranked_day_versions AS ranked
                          ON ranked.id = member.ranked_day_version_id
                        WHERE member.manifest_id = generation.snapshot_manifest_id
                        ORDER BY ranked.id DESC LIMIT 1
                    ) AS generation_day ON true
                    WHERE snapshot.id = %s
                      AND (generation.id IS NULL OR generation.snapshot_state <> 'superseded')
                    ORDER BY generation.generation DESC NULLS LAST
                    LIMIT 1
                """
            else:
                snapshot_query = """
                    SELECT snapshot.id, snapshot.boundary_at, snapshot.version,
                           snapshot.ordering_rule_version,
                           snapshot.freshness_rule_version,
                           snapshot.measured_coverage,
                           snapshot.eligible_population_count,
                           snapshot.included_entry_count, snapshot.stale_entry_count,
                           snapshot.fresh_entry_count, snapshot.excluded_missing_count,
                           snapshot.excluded_invalid_count,
                           snapshot.excluded_malformed_count,
                           snapshot.excluded_conflicting_count,
                           source_day.official_season_id,
                           source_day.season_day_number
                    FROM leaderboard_snapshots AS snapshot
                    LEFT JOIN ranked_day_versions AS source_day
                      ON source_day.id = snapshot.source_ranked_day_version_id
                    WHERE snapshot.id = %s
                """
            snapshot = connection.execute(snapshot_query, (locator[1],)).fetchone()
            assert snapshot is not None
            snapshot = (*snapshot, locator[2], locator[3])
        if snapshot is None:
            legacy_row = connection.execute(
                """
                SELECT leaderboard.id, leaderboard.public_id,
                       leaderboard.boundary_at, leaderboard.version,
                       leaderboard.ordering_rule_version, leaderboard.coverage,
                       selector.official_season_id, selector.season_day_number,
                       (SELECT count(*) FROM api_frozen_leaderboard_entries e
                        WHERE e.leaderboard_id = leaderboard.id),
                       (SELECT count(*) FROM api_frozen_leaderboard_entries e
                        WHERE e.leaderboard_id = leaderboard.id
                          AND e.freshness = 'stale')
                FROM api_frozen_leaderboards AS leaderboard
                JOIN LATERAL (
                    SELECT day.official_season_id, day.season_day_number
                    FROM ranked_day_versions AS day
                    WHERE day.ranked_day_end = leaderboard.boundary_at
                    ORDER BY day.id DESC LIMIT 1
                ) AS selector ON true
                WHERE leaderboard.id = %s
                """,
                (locator[1],),
            ).fetchone()
            assert legacy_row is not None
            legacy = (*legacy_row[:8], locator[2], locator[3], *legacy_row[8:])
            total_entries = int(legacy[10])
            if offset and offset >= total_entries:
                return None
            rows = connection.execute(
                """
                SELECT entry.position, player.normalized_tag, profile.name,
                       profile.profile_json -> 'clan' ->> 'name',
                       entry.trophies, entry.observed_at, entry.freshness,
                       entry.confidence, entry.official_rank
                FROM api_frozen_leaderboard_entries AS entry
                JOIN players AS player ON player.id = entry.player_id
                LEFT JOIN player_profile_versions AS profile
                  ON profile.id = player.current_profile_version_id
                WHERE entry.leaderboard_id = %s
                ORDER BY entry.position LIMIT %s OFFSET %s
                """,
                (legacy[0], limit, offset),
            ).fetchall()
            boundary_at = legacy[2].astimezone(UTC)
            season_start = boundary_at - timedelta(days=int(legacy[7]))
            season_end = season_start + timedelta(days=28)
            coverage = dict(legacy[5])
            measured = float(coverage.get("measured", 0))
            population = int(coverage.get("eligible_population", total_entries))
            page_count = (total_entries + limit - 1) // limit
            page = offset // limit + 1
            return {
                "kind": "frozen",
                "snapshot_id": str(legacy[1]),
                "boundary_at": boundary_at.isoformat(),
                "reset_at": boundary_at.isoformat(),
                "official_season_id": _text(legacy[6]),
                "season_day_number": int(legacy[7]),
                "season_start_at": season_start.isoformat(),
                "season_end_at": season_end.isoformat(),
                "previous_snapshot": legacy[8],
                "next_snapshot": legacy[9],
                "generated_at": boundary_at.isoformat(),
                "version": int(legacy[3]),
                "ordering_rule_version": _text(legacy[4]),
                "tracked_population": population,
                "total_entries": total_entries,
                "page": page,
                "page_size": limit,
                "page_count": page_count,
                "has_previous": page > 1,
                "has_next": page < page_count,
                "coverage": {
                    "state": "partial",
                    "tracked_players": population,
                    "measured_percent": measured * 100
                    if measured <= 1
                    else measured,
                    "note": "Published frozen snapshot coverage is measured from its accepted population.",
                },
                "provenance": {
                    "source": "published frozen leaderboard snapshot",
                    "observed_at": boundary_at.isoformat(),
                    "freshness": "stale" if int(legacy[11]) else "fresh",
                    "confidence": "partial",
                    "coverage": "partial",
                    "version": _text(legacy[4]),
                },
                "quality_states": ["partial"]
                + (["stale"] if int(legacy[11]) else []),
                "entries": [
                    {
                        "position": int(row[0]),
                        "tag": _text(row[1]),
                        "name": None if row[2] is None else _text(row[2]),
                        "clan": None if row[3] is None else _text(row[3]),
                        "trophies": int(row[4]),
                        "observed_at": row[5].astimezone(UTC).isoformat(),
                        "age_seconds": max(
                            0,
                            int(
                                (
                                    now.astimezone(UTC) - row[5].astimezone(UTC)
                                ).total_seconds()
                            ),
                        ),
                        "freshness": _text(row[6]),
                        "confidence": _text(row[7]),
                        "public_confidence": _public_snapshot_confidence(
                            _text(row[7])
                        ),
                        "official_rank": None if row[8] is None else int(row[8]),
                    }
                    for row in rows
                ],
            }
        total_entries = int(snapshot[7])
        if offset and offset >= total_entries:
            return None
        profile_identity_clause = (
            """
            version.id = entry.profile_version_id
               OR (entry.profile_version_id IS NULL
                   AND version.observation_id = entry.profile_observation_id)
            """
            if coordinator_exists
            else "version.observation_id = entry.profile_observation_id"
        )
        rows = connection.execute(
            f"""
            SELECT entry.position, player.normalized_tag, profile.name,
                   entry.trophies, entry.profile_observed_at,
                   entry.profile_freshness, entry.profile_confidence,
                   entry.official_rank, profile.clan
            FROM leaderboard_snapshot_entries AS entry
            JOIN players AS player ON player.id = entry.player_id
            LEFT JOIN LATERAL (
                SELECT version.name, version.profile_json -> 'clan' ->> 'name' AS clan
                FROM player_profile_versions AS version
                WHERE {profile_identity_clause}
                ORDER BY version.id DESC LIMIT 1
            ) AS profile ON true
            WHERE entry.snapshot_id = %s
            ORDER BY entry.position LIMIT %s OFFSET %s
            """,
            (snapshot[0], limit, offset),
        ).fetchall()
        boundary_at = snapshot[1].astimezone(UTC)
        season_start = boundary_at - timedelta(days=int(snapshot[15]))
        season_end = season_start + timedelta(days=28)
        measured = float(snapshot[5])
        page_count = (total_entries + limit - 1) // limit
        page = offset // limit + 1
        return {
            "kind": "frozen",
            "snapshot_id": str(snapshot[0]),
            "boundary_at": boundary_at.isoformat(),
            "reset_at": boundary_at.isoformat(),
            "official_season_id": _text(snapshot[14]),
            "season_day_number": int(snapshot[15]),
            "season_start_at": season_start.isoformat(),
            "season_end_at": season_end.isoformat(),
            "previous_snapshot": snapshot[16],
            "next_snapshot": snapshot[17],
            "generated_at": boundary_at.isoformat(),
            "version": int(snapshot[2]),
            "ordering_rule_version": _text(snapshot[3]),
            "tracked_population": int(snapshot[6]),
            "total_entries": total_entries,
            "page": page,
            "page_size": limit,
            "page_count": page_count,
            "has_previous": page > 1,
            "has_next": page < page_count,
            "coverage": {
                "state": "partial",
                "tracked_players": int(snapshot[6]),
                "measured_percent": measured * 100 if measured <= 1 else measured,
                "note": "Published frozen snapshot coverage is measured from its accepted population.",
            },
            "provenance": {
                "source": "published frozen leaderboard snapshot",
                "observed_at": boundary_at.isoformat(),
                "freshness": "stale" if int(snapshot[8]) else "fresh",
                "confidence": "partial",
                "coverage": "partial",
                "version": _text(snapshot[3]),
            },
            "quality_states": ["partial"] + (["stale"] if int(snapshot[8]) else []),
            "entries": [
                {
                    "position": int(row[0]),
                    "tag": _text(row[1]),
                    "name": None if row[2] is None else _text(row[2]),
                    "trophies": int(row[3]),
                    "observed_at": row[4].astimezone(UTC).isoformat(),
                    "age_seconds": max(
                        0,
                        int(
                            (
                                now.astimezone(UTC) - row[4].astimezone(UTC)
                            ).total_seconds()
                        ),
                    ),
                    "freshness": _text(row[5]),
                    "confidence": _text(row[6]),
                    "public_confidence": _public_snapshot_confidence(_text(row[6])),
                    "official_rank": None if row[7] is None else int(row[7]),
                    "clan": None if row[8] is None else _text(row[8]),
                }
                for row in rows
            ],
        }
