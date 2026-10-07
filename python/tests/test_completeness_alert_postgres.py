"""The completeness alert counts recent Legend I battlers nobody tracks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from clashlens import alerts


def test_completeness_probe_counts_untracked_battlers_seen_over_an_hour_ago(
    database_url, tmp_path, monkeypatch, capsys
):
    now = datetime.now(UTC)
    today = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now < today:
        today -= timedelta(days=1)
    with domain_database(database_url) as connection_info:
        url_file = tmp_path / "database-url"
        options = conninfo_to_dict(connection_info).get("options", "")
        url_file.write_text(
            make_conninfo(connection_info, options=f"{options} -c role=clashlens_python_worker")
        )
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url_file))
        with psycopg.connect(connection_info) as connection:
            # Battle evidence and profiles are seeded without their source rows.
            connection.execute("SET LOCAL session_replication_role = replica")
            tracked, *untracked = [
                row[0]
                for row in connection.execute(
                    """
                    INSERT INTO players (normalized_tag, active)
                    SELECT '#Q' || n, n = 0
                    FROM generate_series(0, 6) AS n ORDER BY n RETURNING id
                    """
                ).fetchall()
            ]
            for day, attacker, defender, fought_ago, saved_ago in (
                (today, tracked, untracked[0], timedelta(hours=3), timedelta(hours=2)),  # counted
                (today - timedelta(days=1), untracked[1], tracked,
                 timedelta(hours=21), timedelta(hours=20)),  # counted
                (today, tracked, untracked[2], timedelta(minutes=20),
                 timedelta(minutes=10)),  # discovery still has time
                (today - timedelta(days=2), tracked, untracked[3],
                 timedelta(days=2), timedelta(days=2)),  # too old
                # Demoted: saved after the lower-tier profile, but fought before it.
                (today - timedelta(days=1), untracked[4], tracked,
                 timedelta(hours=3), timedelta(minutes=90)),
                (today - timedelta(days=1), untracked[5], tracked,
                 timedelta(hours=21), timedelta(hours=20)),  # promoted
            ):
                connection.execute(
                    """
                    WITH battle AS (
                        INSERT INTO legend_battles (ranked_day_start, attacker_player_id,
                                                    defender_player_id, created_at)
                        VALUES (%(day)s, %(attacker)s, %(defender)s, %(saved)s)
                        RETURNING id
                    )
                    INSERT INTO battle_evidence (
                        battle_id, source_row_id, observation_id, reporting_player_id,
                        perspective, battle_timestamp, stars, destruction_percentage,
                        army_share_code, attacker_gain, defender_loss,
                        trophy_rule_version, source_observed_at, parser_version
                    )
                    SELECT id, -id, -id, %(attacker)s, 'attacker', %(fought)s, 3, 100,
                           '', 40, 40, 'test', %(saved)s, 'test'
                    FROM battle
                    """,
                    {"day": day, "attacker": attacker, "defender": defender,
                     "fought": now - fought_ago, "saved": now - saved_ago},
                )
            for player, seen_ago in ((untracked[4], timedelta(minutes=100)),
                                     (untracked[5], timedelta(hours=30))):
                connection.execute(
                    """
                    WITH version AS (
                        INSERT INTO player_profile_versions (
                            player_id, observation_id, normalized_tag, endpoint_version,
                            schema_version, parser_version, observed_at,
                            source_http_status, name, trophies, league_tier_id,
                            league_tier_name, eligibility_state, profile_json
                        ) VALUES (%(player)s, -%(player)s, '#Q', 'test', 'test', 'test',
                                  %(seen)s, 200, 'Q', 4900, 105000035, 'Legend II',
                                  'ineligible', '{}')
                        RETURNING id
                    )
                    INSERT INTO player_profile_effects (
                        profile_version_id, observation_id, effect_kind, observed_at,
                        source_http_status, endpoint_version, schema_version,
                        parser_version
                    )
                    SELECT id, -%(player)s, 'current_profile', %(seen)s, 200, 'test',
                           'test', 'test'
                    FROM version
                    """,
                    {"player": player, "seen": now - seen_ago},
                )
        capsys.readouterr()
        alerts.completeness_probe()
        assert int(capsys.readouterr().out) == 3
