from __future__ import annotations

from uuid import uuid4

from clashlens_prototype.api_db import ApiDatabase, RequestBinding
from test_api_db_organization import account_binding, create_owner
from test_api_db_public_ops import NOW, seed_profile
from test_api_migration import migrated_production_database


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
                snapshot_public_id = uuid4()
                leaderboard_id = connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboards (
                        public_id, boundary_at, version, ordering_rule_version, coverage
                    ) VALUES (
                        %s, '2026-08-06T05:00:00Z', 1, 'frozen-position-v1',
                        '{"state":"complete","tracked_players":1}'::jsonb
                    ) RETURNING id
                    """,
                    (snapshot_public_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboard_entries (
                        leaderboard_id, position, player_id, trophies, observed_at,
                        freshness, confidence, official_rank
                    ) VALUES (%s, 1, %s, 6001, %s, 'fresh', 'exact', 42)
                    """,
                    (leaderboard_id, player_id, NOW),
                )
                connection.commit()

            frozen = database.get_frozen_leaderboard(limit=100)
            assert frozen == {
                "kind": "frozen",
                "snapshot_id": str(snapshot_public_id),
                "boundary_at": "2026-08-06T05:00:00+00:00",
                "version": 1,
                "ordering_rule_version": "frozen-position-v1",
                "coverage": {"state": "complete", "tracked_players": 1},
                "entries": [
                    {
                        "position": 1,
                        "tag": "#2PP",
                        "name": "Player #2PP",
                        "trophies": 6001,
                        "observed_at": NOW.isoformat(),
                        "freshness": "fresh",
                        "confidence": "exact",
                        "official_rank": 42,
                    }
                ],
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
            assert database.scalar(
                "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_export'"
            ) == 1
            assert database.scalar(
                "SELECT count(*) FROM python_processing_jobs WHERE observation_id IS NULL"
            ) == 1
        finally:
            database.close()
