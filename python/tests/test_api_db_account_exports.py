from __future__ import annotations

from test_api_db_organization import account_binding, create_owner
from test_api_db_public_ops import NOW, seed_profile
from test_api_migration import migrated_production_database

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
