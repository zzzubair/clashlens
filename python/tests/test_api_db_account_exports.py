from __future__ import annotations

from test_api_db_organization import account_binding, create_owner
from test_api_db_public_ops import NOW, seed_profile
from test_api_migration import migrated_production_database, text

from clashlens.api_db import ApiDatabase


def test_account_update_frozen_leaderboard_and_export_scaffold(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = create_owner(database)
            updated = database.update_account(
                account_binding(
                    account_id,
                    "account.update",
                    "/v1/account",
                    {
                        "username": "renamedowner",
                        "display_name": "Renamed Owner",
                        "preferences": {"timezone": "UTC"},
                    },
                    method="PATCH",
                ),
                username="renamedowner",
                normalized_username="renamedowner",
                display_name="Renamed Owner",
                preferences={"timezone": "UTC"},
            )
            assert updated.payload == {
                "username": "renamedowner",
                "display_name": "Renamed Owner",
                "preferences": {"timezone": "UTC"},
                "providers": ["google"],
            }

            seed_profile(database, "#2PP", 6001)
            with database.pool.connection() as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
                profile_observation_id = connection.execute(
                    """SELECT observation_id FROM player_profile_versions
                       WHERE id = (SELECT current_profile_version_id FROM players WHERE id = %s)""",
                    (player_id,),
                ).fetchone()[0]
                ranked_day_id = connection.execute(
                    """
                    INSERT INTO ranked_day_versions (
                        player_id, ranked_day_start, ranked_day_end,
                        official_season_id, season_day_number,
                        season_anchor_rule_version, reconciliation_rule_version,
                        result_hash, version, state, confidence
                    ) VALUES (
                        %s, '2026-08-05T05:00:00Z', '2026-08-06T05:00:00Z',
                        '2026-08', 21, 'test-anchor-v1', 'test-reconcile-v1',
                        repeat('1', 64), 1, 'Complete', 'exact'
                    ) RETURNING id
                    """,
                    (player_id,),
                ).fetchone()[0]
                old_correction_id = connection.execute(
                    """
                    INSERT INTO leaderboard_snapshots (
                        snapshot_kind, boundary_at, version, ordering_rule_version,
                        freshness_rule_version, state, source_ranked_day_version_id,
                        measured_coverage, stale_entry_count,
                        eligible_population_count, included_entry_count, fresh_entry_count
                    ) VALUES (
                        'frozen', '2026-08-06T05:00:00Z', 1, 'frozen-position-v1',
                        'test-fresh-v1', 'superseded', %s, 1, 0, 1, 1, 1
                    ) RETURNING id
                    """,
                    (ranked_day_id,),
                ).fetchone()[0]
                leaderboard_id = connection.execute(
                    """
                    INSERT INTO leaderboard_snapshots (
                        snapshot_kind, boundary_at, version, correction_of_id,
                        ordering_rule_version, freshness_rule_version, state,
                        source_ranked_day_version_id, measured_coverage,
                        stale_entry_count, published_at, eligible_population_count,
                        included_entry_count, fresh_entry_count
                    ) VALUES (
                        'frozen', '2026-08-06T05:00:00Z', 2, %s,
                        'frozen-position-v1', 'test-fresh-v1', 'published', %s,
                        1, 0, clock_timestamp(), 1, 1, 1
                    ) RETURNING id
                    """,
                    (old_correction_id, ranked_day_id),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO leaderboard_snapshot_entries (
                        snapshot_id, position, player_id, trophies,
                        trophy_observation_id, trophy_observed_at,
                        observation_age_seconds, freshness, confidence, tie_hash,
                        official_rank, profile_observation_id, profile_observed_at,
                        profile_age_seconds, profile_freshness, profile_confidence
                    ) VALUES (
                        %s, 1, %s, 6001, %s, %s, 0, 'fresh', 'confirmed',
                        repeat('2', 64), 42, %s, %s, 0, 'fresh', 'confirmed'
                    )
                    """,
                    (leaderboard_id, player_id, profile_observation_id, NOW,
                     profile_observation_id, NOW),
                )
                older_day_id = connection.execute(
                    """
                    INSERT INTO ranked_day_versions (
                        player_id, ranked_day_start, ranked_day_end,
                        official_season_id, season_day_number,
                        season_anchor_rule_version, reconciliation_rule_version,
                        result_hash, version, state, confidence
                    ) VALUES (
                        %s, '2026-07-26T05:00:00Z', '2026-07-27T05:00:00Z',
                        '2026-07', 28, 'test-anchor-v1', 'test-reconcile-v1',
                        repeat('3', 64), 1, 'Complete', 'exact'
                    ) RETURNING id
                    """,
                    (player_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO leaderboard_snapshots (
                        snapshot_kind, boundary_at, version, ordering_rule_version,
                        freshness_rule_version, state, source_ranked_day_version_id,
                        measured_coverage, stale_entry_count, published_at
                    ) VALUES (
                        'frozen', '2026-07-27T05:00:00Z', 1, 'frozen-position-v1',
                        'test-fresh-v1', 'published', %s, 0, 0, clock_timestamp()
                    )
                    """,
                    (older_day_id,),
                )
                connection.execute("UPDATE players SET active = false WHERE id = %s", (player_id,))
                connection.commit()

            frozen = database.get_frozen_leaderboard(limit=100, now=NOW)
            assert frozen == {
                "kind": "frozen",
                "snapshot_id": str(leaderboard_id),
                "boundary_at": "2026-08-06T05:00:00+00:00",
                "version": 2,
                "ordering_rule_version": "frozen-position-v1",
                "generated_at": "2026-08-06T05:00:00+00:00",
                "tracked_population": 1,
                "total_entries": 1,
                "page": 1,
                "page_size": 100,
                "page_count": 1,
                "has_previous": False,
                "has_next": False,
                "reset_at": "2026-08-06T05:00:00+00:00",
                "official_season_id": "2026-08",
                "season_day_number": 21,
                "season_start_at": "2026-07-16T05:00:00+00:00",
                "season_end_at": "2026-08-13T05:00:00+00:00",
                "previous_snapshot": {
                    "official_season_id": "2026-07",
                    "season_day_number": 28,
                },
                "next_snapshot": None,
                "coverage": {
                    "state": "partial",
                    "tracked_players": 1,
                    "measured_percent": 100.0,
                    "note": "Published frozen snapshot coverage is measured from its accepted population.",
                },
                "provenance": {
                    "source": "published frozen leaderboard snapshot",
                    "observed_at": "2026-08-06T05:00:00+00:00",
                    "freshness": "fresh",
                    "confidence": "partial",
                    "coverage": "partial",
                    "version": "frozen-position-v1",
                },
                "quality_states": ["partial"],
                "entries": [
                    {
                        "position": 1,
                        "tag": "#2PP",
                        "name": "Player #2PP",
                        "clan": None,
                        "trophies": 6001,
                        "observed_at": NOW.isoformat(),
                        "age_seconds": 0,
                        "freshness": "fresh",
                        "confidence": "confirmed",
                        "public_confidence": "high",
                        "official_rank": 42,
                    }
                ],
            }
            older = database.get_frozen_leaderboard(
                limit=100, official_season_id="2026-07", season_day_number=28
            )
            assert older is not None
            assert older["season_start_at"] == "2026-06-29T05:00:00+00:00"
            assert older["season_end_at"] == "2026-07-27T05:00:00+00:00"
            assert older["next_snapshot"] == {
                "official_season_id": "2026-08",
                "season_day_number": 21,
            }

            export = database.submit_export(
                account_binding(
                    account_id,
                    "exports.submit",
                    "/v1/account/exports",
                    {"format": "google_sheets_scaffold"},
                ),
                export_format="google_sheets_scaffold",
            )
            export_id = export.payload["export_id"]
            assert export.status_code == 202
            assert export.payload == {
                "export_id": export_id,
                "format": "google_sheets_scaffold",
                "status": "pending",
            }
            assert database.get_export_status(account_id, export_id) == export.payload
            assert (
                database.scalar(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_export'"
                )
                == 1
            )
            assert (
                database.scalar(
                    "SELECT count(*) FROM python_processing_jobs WHERE observation_id IS NULL"
                )
                == 1
            )
        finally:
            database.close()


def test_legacy_frozen_leaderboards_keep_daily_selection_and_pagination(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6001)
            seed_profile(database, "#2PQ", 5999)
            with database.pool.connection() as connection:
                players = {
                    text(tag): player_id
                    for tag, player_id in connection.execute(
                        "SELECT normalized_tag, id FROM players ORDER BY normalized_tag"
                    ).fetchall()
                }
                for tag, start, end, season, day in (
                    ("#2PP", "2026-07-26T05:00:00Z", "2026-07-27T05:00:00Z", "2026-07", 28),
                    ("#2PP", "2026-08-05T05:00:00Z", "2026-08-06T05:00:00Z", "2026-08", 21),
                ):
                    connection.execute(
                        """
                        INSERT INTO ranked_day_versions (
                            player_id, ranked_day_start, ranked_day_end,
                            official_season_id, season_day_number,
                            season_anchor_rule_version, reconciliation_rule_version,
                            result_hash, version, state, confidence
                        ) VALUES (%s, %s, %s, %s, %s, 'test-anchor-v1',
                                  'test-reconcile-v1', repeat('4', 64), 1,
                                  'Complete', 'exact')
                        """,
                        (players[tag], start, end, season, day),
                    )
                older_id = connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboards (
                        public_id, boundary_at, version, ordering_rule_version, coverage
                    ) VALUES ('00000000-0000-0000-0000-000000000071',
                              '2026-07-27T05:00:00Z', 1, 'legacy-position-v1',
                              '{"measured": 1, "eligible_population": 1}')
                    RETURNING id
                    """
                ).fetchone()[0]
                latest_id = connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboards (
                        public_id, boundary_at, version, ordering_rule_version, coverage
                    ) VALUES ('00000000-0000-0000-0000-000000000081',
                              '2026-08-06T05:00:00Z', 1, 'legacy-position-v1',
                              '{"measured": 1, "eligible_population": 2}')
                    RETURNING id
                    """
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboard_entries (
                        leaderboard_id, position, player_id, trophies, observed_at,
                        freshness, confidence, official_rank
                    ) VALUES
                        (%s, 1, %s, 5900, %s, 'fresh', 'confirmed', 9),
                        (%s, 1, %s, 6001, %s, 'fresh', 'confirmed', 2),
                        (%s, 2, %s, 5999, %s, 'stale', 'uncertain', NULL)
                    """,
                    (
                        older_id, players["#2PP"], NOW,
                        latest_id, players["#2PP"], NOW,
                        latest_id, players["#2PQ"], NOW,
                    ),
                )
                connection.commit()

            latest = database.get_frozen_leaderboard(limit=1, offset=1, now=NOW)
            assert latest is not None
            assert latest["official_season_id"] == "2026-08"
            assert latest["season_day_number"] == 21
            assert latest["previous_snapshot"] == {
                "official_season_id": "2026-07", "season_day_number": 28
            }
            assert latest["next_snapshot"] is None
            assert latest["total_entries"] == 2
            assert latest["page"] == 2
            assert latest["page_count"] == 2
            assert latest["has_previous"] is True
            assert latest["has_next"] is False
            assert latest["entries"][0]["position"] == 2

            older = database.get_frozen_leaderboard(
                limit=1, official_season_id="2026-07", season_day_number=28, now=NOW
            )
            assert older is not None
            assert older["next_snapshot"] == {
                "official_season_id": "2026-08", "season_day_number": 21
            }

            with database.pool.connection() as connection:
                profile_observation_id = connection.execute(
                    """SELECT observation_id FROM player_profile_versions
                       WHERE id = (SELECT current_profile_version_id FROM players
                                   WHERE normalized_tag = '#2PP')"""
                ).fetchone()[0]
                ranked_day_id = connection.execute(
                    """
                    INSERT INTO ranked_day_versions (
                        player_id, ranked_day_start, ranked_day_end,
                        official_season_id, season_day_number,
                        season_anchor_rule_version, reconciliation_rule_version,
                        result_hash, version, state, confidence
                    ) VALUES (%s, '2026-08-06T05:00:00Z', '2026-08-07T05:00:00Z',
                              '2026-08', 22, 'test-anchor-v1', 'test-reconcile-v1',
                              repeat('5', 64), 1, 'Complete', 'exact') RETURNING id
                    """,
                    (players["#2PP"],),
                ).fetchone()[0]
                snapshot_id = connection.execute(
                    """
                    INSERT INTO leaderboard_snapshots (
                        snapshot_kind, boundary_at, version, ordering_rule_version,
                        freshness_rule_version, state, source_ranked_day_version_id,
                        measured_coverage, eligible_population_count,
                        included_entry_count, fresh_entry_count, stale_entry_count,
                        published_at
                    ) VALUES ('frozen', '2026-08-07T05:00:00Z', 1,
                              'frozen-position-v1', 'test-fresh-v1', 'published',
                              %s, 1, 1, 1, 1, 0, clock_timestamp()) RETURNING id
                    """,
                    (ranked_day_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO leaderboard_snapshot_entries (
                        snapshot_id, position, player_id, trophies,
                        trophy_observation_id, trophy_observed_at,
                        observation_age_seconds, freshness, confidence, tie_hash,
                        profile_observation_id, profile_observed_at,
                        profile_age_seconds, profile_freshness, profile_confidence
                    ) VALUES (%s, 1, %s, 6010, %s, %s, 0, 'fresh', 'confirmed',
                              repeat('6', 64), %s, %s, 0, 'fresh', 'confirmed')
                    """,
                    (snapshot_id, players["#2PP"], profile_observation_id, NOW,
                     profile_observation_id, NOW),
                )
                connection.commit()

            newest = database.get_frozen_leaderboard(limit=10, now=NOW)
            assert newest is not None
            assert newest["snapshot_id"] == str(snapshot_id)
            assert newest["season_start_at"] == "2026-07-16T05:00:00+00:00"
            assert newest["season_end_at"] == "2026-08-13T05:00:00+00:00"
            assert newest["previous_snapshot"] == {
                "official_season_id": "2026-08", "season_day_number": 21
            }
            legacy_latest = database.get_frozen_leaderboard(
                limit=10, official_season_id="2026-08", season_day_number=21
            )
            assert legacy_latest is not None
            assert legacy_latest["next_snapshot"] == {
                "official_season_id": "2026-08", "season_day_number": 22
            }
            assert database.scalar(
                "SELECT count(*) FROM api_frozen_leaderboard_entries"
            ) == 3
        finally:
            database.close()
