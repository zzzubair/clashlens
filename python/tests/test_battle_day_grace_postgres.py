"""A battle reported in the first 5 minutes after a Reset counts on the day before.

Battles saved under the old rule, by their timestamp's own day, are moved by
migration 0057; the republish command then rebuilds the published days.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from domain_test_support import domain_database, store_observation
from psycopg.types.json import Jsonb
from test_domain_processing_postgres import (
    LIVE_BATTLE_PARSER_VERSION,
    _live_battle_row,
    _processor,
    _seed_battle_anchor,
)

from clashlens import battle, battle_day_repair, domain, reconciliation_db
from clashlens.db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
)
from clashlens.domain import RANKED_DAY_DURATION, ranked_day_for

MIGRATION = (
    Path(__file__).parents[2] / "deploy/migrations/0057_battle_day_grace.sql"
)
ANCHOR = datetime(2026, 8, 3, 5, tzinfo=UTC)
DAY = ANCHOR + timedelta(days=1)
NEXT = DAY + timedelta(days=1)
TAG = "#8PP"
_LETTERS = "PYLQGRJCUV"


def _opponent(index: int) -> str:
    return "#2" + _LETTERS[index // 10] + _LETTERS[index % 10]


def _row(attack: bool, at: datetime, opponent: str) -> dict:
    return _live_battle_row(
        attack=attack, battle_timestamp=at, opponent_tag=opponent, opponent_name="x"
    )


def _player_log() -> list[dict]:
    """#8PP's own log, with every case from the 2026-10-03 production check."""
    rows = [
        # Finished at the start of DAY, so it belongs to the unpublished day
        # before; its opponent's log was never saved.
        _row(True, DAY + timedelta(seconds=86), _opponent(0)),
        # Attacks by #8PP on V and U's attack on #8PP, both at NEXT's Reset.
        _row(True, NEXT + timedelta(seconds=26), "#2VV"),
        _row(False, NEXT, "#2UU"),
        # A defense first reported at 05:07:20 began after the Reset.
        _row(False, NEXT + timedelta(minutes=7, seconds=20), _opponent(1)),
    ]
    for hour in range(1, 8):
        if hour <= 5:
            rows.append(_row(True, DAY + timedelta(hours=hour), _opponent(10 + hour)))
        rows.append(_row(False, DAY + timedelta(hours=hour), _opponent(20 + hour)))
        rows.append(_row(True, NEXT + timedelta(hours=hour), _opponent(30 + hour)))
        rows.append(_row(False, NEXT + timedelta(hours=hour), _opponent(40 + hour)))
    return rows


def _save(
    connection_info,
    archive_server,
    processor,
    tag: str,
    rows: list,
    observed_at: datetime = NEXT + timedelta(hours=9),
) -> None:
    job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"log-{tag}-{observed_at.isoformat()}",
        endpoint="battle_log",
        body=json.dumps({"items": rows}).encode(),
        observed_at=observed_at,
        normalized_tag=tag,
        parser_version=LIVE_BATTLE_PARSER_VERSION,
    )[1]
    result = processor.process_job(job_id, owner=f"test-{job_id}")
    assert result is not None and result.outcome == "processed"


def _counts(connection_info: str) -> dict[datetime, tuple[int, int]]:
    """#8PP's latest published attack and defense counts by day."""
    with psycopg.connect(connection_info) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT ON (log.ranked_day_start)
                   log.ranked_day_start, log.attack_count, log.defense_count
            FROM api_player_daily_logs AS log
            JOIN players AS player ON player.id = log.player_id
            WHERE player.normalized_tag = %s
            ORDER BY log.ranked_day_start, log.version DESC
            """,
            (TAG,),
        ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def _battles(connection_info: str) -> list[tuple]:
    """Each saved battle: day, attacker, defender and the sides it holds."""
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT b.ranked_day_start, attacker.normalized_tag,
                   defender.normalized_tag, b.disagreement_state,
                   array_agg(p.perspective ORDER BY p.perspective),
                   array_agg(p.evidence_id ORDER BY p.perspective)
            FROM legend_battles AS b
            JOIN players AS attacker ON attacker.id = b.attacker_player_id
            JOIN players AS defender ON defender.id = b.defender_player_id
            LEFT JOIN battle_perspectives AS p ON p.battle_id = b.id
            GROUP BY b.id, attacker.normalized_tag, defender.normalized_tag
            ORDER BY 1, 2, 3
            """
        ).fetchall()


_DELETE_JOBS = "DELETE FROM python_processing_jobs WHERE id = ANY(%s)"


def _sql(connection_info: str, statement: str, job_ids: list[int]) -> None:
    with psycopg.connect(connection_info) as connection:
        connection.execute(statement, (job_ids,))


def _player_id(connection_info: str) -> int:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
        ).fetchone()[0]


def _add_job(database, input_json: dict) -> int:
    with database.pool.connection() as connection:
        return connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                observation_id, work_type, deduplication_key, input_json,
                state, due_at, parser_version, processing_version,
                domain_rule_version, analytics_rule_version
            ) VALUES (
                NULL, 'reconcile_ranked_day', 'test:active', %s, 'pending',
                clock_timestamp(), %s, %s, %s, %s
            ) RETURNING id
            """,
            (
                Jsonb(input_json),
                DEFAULT_PARSER_VERSION,
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ANALYTICS_RULE_VERSION,
            ),
        ).fetchone()[0]


def _drain(connection_info: str, processor) -> None:
    with psycopg.connect(connection_info) as connection:
        job_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM python_processing_jobs_worker"
                " WHERE state = 'pending' AND work_type = 'reconcile_ranked_day'"
                " ORDER BY id"
            ).fetchall()
        ]
    for job_id in job_ids:
        assert processor.process_job(job_id, owner="drain") is not None


def _rebuild_jobs(connection_info: str) -> int:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT count(*) FROM python_processing_jobs "
            "WHERE deduplication_key LIKE 'reconcile:battle-day:%%'"
        ).fetchone()[0]


def _v_battle(connection_info: str) -> tuple:
    """The state and sides of #8PP's attack on V reported at NEXT's Reset."""
    (row,) = [
        row
        for row in _battles(connection_info)
        if row[0] == DAY and row[2] == "#2VV"
    ]
    return row[3], row[4], row[5]


def _apply_migration(connection_info: str) -> None:
    with psycopg.connect(connection_info, autocommit=True) as connection:
        connection.execute(MIGRATION.read_text())


def test_saved_boundary_battles_move_to_the_day_before_and_days_republish(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _seed_battle_anchor(connection_info, ANCHOR)
            # Save and publish as the old rule did: each report on the day
            # of its own timestamp, each day counting timestamps in it.
            monkeypatch.setattr(battle, "battle_day_for", ranked_day_for)
            monkeypatch.setattr(
                domain,
                "battle_window",
                lambda start: (start, start + RANKED_DAY_DURATION),
            )
            _save(connection_info, archive_server, processor, TAG, _player_log())
            # V reports #8PP's attack 2:24 earlier, before the Reset; U reports
            # its attack on #8PP 3:01 after it.
            _save(
                connection_info,
                archive_server,
                processor,
                "#2VV",
                [_row(False, NEXT - timedelta(seconds=118), TAG)],
            )
            _save(
                connection_info,
                archive_server,
                processor,
                "#2UU",
                [_row(True, NEXT + timedelta(seconds=181), TAG)],
            )
            for day in (DAY, NEXT):
                job_id = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=day, now=day, request_key="old"
                )
                assert processor.process_job(job_id, owner="old") is not None
            # The nine-defense day, and the eight-attack day that has the
            # previous day's attack in it.
            assert _counts(connection_info) == {DAY: (6, 7), NEXT: (8, 9)}
            before = _battles(connection_info)
            monkeypatch.undo()

            _apply_migration(connection_info)
            after = _battles(connection_info)
            _apply_migration(connection_info)
            assert _battles(connection_info) == after

            # V's battle had its two reports on two days: it is now one.
            assert len(after) == len(before) - 1
            assert sorted(e for row in after for e in row[5]) == sorted(
                e for row in before for e in row[5]
            )
            moved = {
                (row[0], row[1], row[2]): (row[3], row[4])
                for row in after
                if row[1] in {"#2VV", "#2UU"} or row[2] in {"#2VV", "#2UU"}
                or row[0] == ANCHOR
            }
            assert moved == {
                (ANCHOR, TAG, _opponent(0)): ("single_perspective", ["attacker"]),
                (DAY, TAG, "#2VV"): ("single_perspective", ["attacker", "defender"]),
                (DAY, "#2UU", TAG): ("agreed", ["attacker", "defender"]),
            }
            with psycopg.connect(connection_info) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM battle_day_repairs"
                ).fetchone()[0] == 4

            # A Season rebuild queued from the unpublished day before DAY also
            # recalculates DAY and NEXT, so the battle-day rebuild waits. V's
            # battle compares its two reports even so.
            with psycopg.connect(connection_info) as connection:
                season = connection.execute(
                    "SELECT official_season_id FROM api_player_daily_logs"
                    " LIMIT 1"
                ).fetchone()[0]
            active = _add_job(
                database,
                {
                    "player_id": _player_id(connection_info),
                    "ranked_day_start": "2026-08-03T05:00:00Z",
                    "last_ranked_day_start": "2026-08-03T05:00:00Z",
                    "recalculate_season": season,
                },
            )
            waiting = battle_day_repair.enqueue_rebuilds(database, max_jobs=10)
            assert waiting["job_ids"] == [] and waiting["failed_blockers"] == []
            assert _v_battle(connection_info)[:2] == (
                "agreed",
                ["attacker", "defender"],
            )
            _sql(connection_info, _DELETE_JOBS, [active])

            failed = reconciliation_db.enqueue_current_season_republication(
                database, max_jobs=10
            )
            assert len(failed["job_ids"]) == 1
            # A failed rebuild is reported, not retried, until it is deleted.
            _sql(
                connection_info,
                "UPDATE python_processing_jobs SET status = 'failed',"
                " failure_category = 'invalid_work_input',"
                " attempt_count = max_attempts WHERE id = ANY(%s)",
                failed["job_ids"],
            )
            blocked = reconciliation_db.enqueue_current_season_republication(
                database, max_jobs=10
            )
            assert blocked["failed_blockers"] == [
                {
                    "job_id": failed["job_ids"][0],
                    "player_id": _player_id(connection_info),
                    "ranked_day_start": "2026-08-04T05:00:00Z",
                    "failure_category": "invalid_work_input",
                }
            ]
            assert _rebuild_jobs(connection_info) == 1
            _sql(connection_info, _DELETE_JOBS, failed["job_ids"])
            report = reconciliation_db.enqueue_current_season_republication(
                database, max_jobs=10
            )
            assert len(report["job_ids"]) == 1
            assert report["failed_blockers"] == []
            for job_id in report["job_ids"]:
                assert processor.process_job(job_id, owner="repair") is not None
            # Eight and eight; the day after keeps its 05:07:20 defense, and
            # its attacks fall from eight to seven: the eighth was the day
            # before's. The unpublished day before DAY is not created.
            assert _counts(connection_info) == {DAY: (6, 8), NEXT: (7, 8)}
            again = reconciliation_db.enqueue_current_season_republication(
                database, max_jobs=10
            )
            assert not any(
                job in report["job_ids"] for job in again["job_ids"]
            )
            # Finished jobs are deleted after 48 hours; the corrected days,
            # not the job, show the rebuild is done.
            _sql(connection_info, _DELETE_JOBS, report["job_ids"])
            reconciliation_db.enqueue_current_season_republication(
                database, max_jobs=10
            )
            assert _rebuild_jobs(connection_info) == 0

            # A later corrected report of the moved attack replaces it; the
            # day showing the correction is done too.
            moved_report = _v_battle(connection_info)[2]
            _save(
                connection_info,
                archive_server,
                processor,
                TAG,
                [
                    _live_battle_row(
                        attack=True,
                        battle_timestamp=NEXT + timedelta(seconds=26),
                        opponent_tag="#2VV",
                        opponent_name="corrected",
                    )
                ],
                observed_at=NEXT + timedelta(hours=10),
            )
            assert _v_battle(connection_info)[2] != moved_report
            job_id = reconciliation_db.enqueue_reconciliation(
                database, player_tag=TAG, day_start=DAY, now=DAY, request_key="fix"
            )
            assert processor.process_job(job_id, owner="fix") is not None
            _drain(connection_info, processor)
            assert _counts(connection_info) == {DAY: (6, 8), NEXT: (7, 8)}
            assert battle_day_repair.enqueue_rebuilds(
                database, max_jobs=10
            )["job_ids"] == []
            assert _rebuild_jobs(connection_info) == 0
        finally:
            database.close()


def test_new_reports_count_on_the_day_before_without_a_repair(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _seed_battle_anchor(connection_info, ANCHOR)
            _save(connection_info, archive_server, processor, TAG, _player_log())
            _save(
                connection_info,
                archive_server,
                processor,
                "#2VV",
                [_row(False, NEXT - timedelta(seconds=118), TAG)],
            )
            for day in (DAY, NEXT):
                job_id = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=day, now=day, request_key="new"
                )
                assert processor.process_job(job_id, owner="new") is not None

            assert _counts(connection_info) == {DAY: (6, 8), NEXT: (7, 8)}
            with psycopg.connect(connection_info) as connection:
                assert connection.execute(
                    """
                    SELECT count(*) FROM legend_battles AS b
                    JOIN players AS defender ON defender.id = b.defender_player_id
                    WHERE defender.normalized_tag = '#2VV'
                    """
                ).fetchone()[0] == 1
        finally:
            database.close()


def test_a_battle_with_reports_from_two_days_stays_where_it_is(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _seed_battle_anchor(connection_info, ANCHOR)
            # Under the old rule two attacks on one opponent in one day, at
            # 05:01 and 16:00, share one saved battle.
            monkeypatch.setattr(battle, "battle_day_for", ranked_day_for)
            early = NEXT + timedelta(minutes=1)
            _save(
                connection_info,
                archive_server,
                processor,
                TAG,
                [
                    _row(True, early, "#2VV"),
                    _row(True, NEXT + timedelta(hours=11), "#2VV"),
                ],
            )
            monkeypatch.undo()
            with psycopg.connect(connection_info) as connection:
                # The battle shows the 05:01 report; the 16:00 one is kept.
                connection.execute(
                    """
                    UPDATE battle_perspectives AS p SET evidence_id = e.id
                    FROM battle_evidence AS e
                    WHERE e.battle_id = p.battle_id
                      AND e.perspective = p.perspective
                      AND e.battle_timestamp = %s
                    """,
                    (early,),
                )
                assert connection.execute(
                    "SELECT count(DISTINCT battle_id), count(*) FROM battle_evidence"
                ).fetchone() == (1, 2)
            before = _battles(connection_info)
            assert [row[0] for row in before] == [NEXT]

            _apply_migration(connection_info)

            assert _battles(connection_info) == before
            with psycopg.connect(connection_info) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM battle_day_repairs"
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(DISTINCT battle_id), count(*) FROM battle_evidence"
                ).fetchone() == (1, 2)
        finally:
            database.close()


def test_a_read_after_the_grace_settles_a_day_of_eight_attacks_and_defenses(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _seed_battle_anchor(connection_info, ANCHOR)
            # No Reset checks. The day before's attack shows the log reaches
            # back past the start of DAY.
            rows = [_row(True, DAY - timedelta(hours=1), _opponent(0))]
            for hour in range(1, 9):
                rows.append(_row(True, DAY + timedelta(hours=hour), _opponent(hour)))
                rows.append(
                    _row(False, DAY + timedelta(hours=hour, minutes=30), _opponent(10 + hour))
                )

            def published(observed_at: datetime) -> tuple:
                _save(connection_info, archive_server, processor, TAG, rows, observed_at)
                job_id = reconciliation_db.enqueue_reconciliation(
                    database,
                    player_tag=TAG,
                    day_start=DAY,
                    now=DAY,
                    request_key=observed_at.isoformat(),
                )
                assert processor.process_job(job_id, owner="net") is not None
                with psycopg.connect(connection_info) as connection:
                    return connection.execute(
                        """
                        SELECT attack_count, defense_count, attack_gain,
                               defense_loss, net_trophy_change
                        FROM api_player_daily_logs
                        WHERE ranked_day_start = %s
                        ORDER BY version DESC LIMIT 1
                        """,
                        (DAY,),
                    ).fetchone()

            # Read 3 minutes after the Reset: a late battle may still come.
            early = published(NEXT + timedelta(minutes=3))
            assert early[:2] == (8, 8) and early[4] is None
            late = published(NEXT + timedelta(minutes=6))
            assert late[:4] == early[:4]
            assert late[4] == late[2] - late[3]
        finally:
            database.close()
