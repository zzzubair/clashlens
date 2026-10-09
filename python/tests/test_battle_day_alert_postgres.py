"""The battle-day alert counts reports stamped in the Reset's no-attack window
and saved days with a 9th attack or defense."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from clashlens import alerts


def test_battle_day_probe_counts_reports_stamped_in_the_no_attack_window(
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
            # Battle evidence is seeded without its source rows.
            connection.execute("SET LOCAL session_replication_role = replica")
            attacker, *defenders = [
                row[0] for row in connection.execute(
                    "INSERT INTO players (normalized_tag)"
                    " SELECT '#Q' || n FROM generate_series(0, 7) AS n ORDER BY n RETURNING id"
                ).fetchall()
            ]
            for defender, (day, stamped) in zip(defenders, (
                (today - timedelta(days=1), today + timedelta(minutes=3, seconds=38)),  # ended day
                (today - timedelta(days=1), today + timedelta(minutes=3, seconds=40)),  # counted
                (today, today + timedelta(minutes=6, seconds=59)),  # counted
                (today, today + timedelta(minutes=7, seconds=20)),  # first new-day attacks
                (today - timedelta(days=2), today - timedelta(days=1, minutes=-4)),  # counted
                (today - timedelta(days=2), today - timedelta(days=2, minutes=-4)),  # two Resets ago
                (today - timedelta(days=3), today - timedelta(days=2, minutes=-4)),  # too old
            ), strict=True):
                connection.execute(
                    """
                    WITH battle AS (
                        INSERT INTO legend_battles (ranked_day_start, attacker_player_id,
                                                    defender_player_id)
                        VALUES (%(day)s, %(attacker)s, %(defender)s)
                        RETURNING id
                    )
                    INSERT INTO battle_evidence (
                        battle_id, source_row_id, observation_id, reporting_player_id,
                        perspective, battle_timestamp, stars, destruction_percentage,
                        army_share_code, attacker_gain, defender_loss,
                        trophy_rule_version, source_observed_at, parser_version
                    )
                    SELECT id, -id, -id, %(attacker)s, 'attacker', %(stamped)s, 3, 100,
                           '', 40, 40, 'test', %(stamped)s, 'test'
                    FROM battle
                    """,
                    {"day": day, "attacker": attacker, "defender": defender,
                     "stamped": stamped},
                )
            for player, day, attacks, defenses in (
                (attacker, today - timedelta(days=1), 9, 3),  # counted
                (defenders[0], today, 2, 9),  # counted
                (defenders[1], today - timedelta(days=1), 8, 8),
                (defenders[2], today - timedelta(days=2), 9, 0),  # too old
            ):
                connection.execute(
                    """
                    INSERT INTO ranked_day_versions (
                        player_id, ranked_day_start, ranked_day_end, official_season_id,
                        season_day_number, season_anchor_rule_version,
                        reconciliation_rule_version, input_hash, result_hash, version,
                        state, confidence, attack_count, defense_count
                    ) VALUES (%s, %s, %s, '2026-10', 7, 'test', 'test', repeat('a', 64),
                              repeat('b', 64), 1, 'Inconsistent', 'uncertain', %s, %s)
                    """,
                    (player, day, day + timedelta(days=1), attacks, defenses),
                )
        capsys.readouterr()
        alerts.battle_day_probe()
        assert int(capsys.readouterr().out) == 5
