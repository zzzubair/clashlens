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
            tracked, *untracked = [
                row[0]
                for row in connection.execute(
                    """
                    INSERT INTO players (normalized_tag, active, eligibility_state)
                    SELECT '#Q' || n, n = 0, CASE WHEN n = 0 THEN 'eligible' ELSE 'unknown' END
                    FROM generate_series(0, 4) AS n ORDER BY n RETURNING id
                    """
                ).fetchall()
            ]
            for day, attacker, defender, saved_ago in (
                (today, tracked, untracked[0], timedelta(hours=2)),  # counted
                (today - timedelta(days=1), untracked[1], tracked, timedelta(hours=20)),  # counted
                (today, tracked, untracked[2], timedelta(minutes=10)),  # discovery still has time
                (today - timedelta(days=2), tracked, untracked[3], timedelta(days=2)),  # too old
            ):
                connection.execute(
                    "INSERT INTO legend_battles (ranked_day_start, attacker_player_id,"
                    " defender_player_id, created_at) VALUES (%s, %s, %s, %s)",
                    (day, attacker, defender, now - saved_ago),
                )
        capsys.readouterr()
        alerts.completeness_probe()
        assert int(capsys.readouterr().out) == 2
