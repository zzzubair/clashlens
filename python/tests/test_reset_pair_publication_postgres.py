from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from test_reconciliation import _input
from test_reconciliation_postgres import (
    DAY_END,
    DAY_START,
    _battle_log,
    _processor,
    _store_baseline_pair,
)
from test_snapshot_publication_postgres import _process_snapshot_and_analytics

from clashlens import reconciliation_db
from clashlens.domain import ranked_day_for
from clashlens.profile import PROFILE_PARSER_VERSION
from clashlens.reconciliation import (
    BattleContribution,
    PreviousRankedDay,
    reconcile_ranked_day,
)
from clashlens.worker import ObservationProcessor


@pytest.mark.parametrize("shielded", [False, True])
@pytest.mark.parametrize("repair", [False, True])
def test_repair_rebuilds_dependent_results_in_one_job(monkeypatch, shielded, repair):
    database = MagicMock()
    connection = database.pool.connection.return_value.__enter__.return_value
    days = [DAY_START + timedelta(days=offset) for offset in range(3)]
    connection.execute.return_value.fetchall.return_value = [(days[2],), (days[1],)]
    saved = {}

    def recalculate(_database, _connection, *, day_start, **_versions):
        offset = days.index(day_start)
        previous = saved.get(day_start - timedelta(days=1))
        previous_day = (
            PreviousRankedDay(
                complete=previous.state == "Complete",
                coverage_complete=previous.coverage_complete,
                observed_defense_count=previous.defense_count,
                observed_defense_loss=previous.observed_defense_loss,
                shield_run_length=previous.shield_duration_days or 0,
            )
            if previous is not None else None
        )
        base = _input()
        contributions = (
            () if shielded else tuple(
                BattleContribution(f"defense-{index}", "defense", 20)
                for index in range(8 if offset == 0 else 4)
            )
        )
        saved[day_start] = reconcile_ranked_day(
            replace(
                base,
                ranked_day=ranked_day_for(day_start),
                now=day_start + timedelta(days=1, minutes=1),
                start_trophies=6000 if shielded else 6000 - offset * 160,
                next_start_trophies=6000 if shielded else 6000 - (offset + 1) * 160,
                coverage_observations=tuple(
                    replace(item, observed_at=item.observed_at + timedelta(days=offset))
                    for item in base.coverage_observations
                ),
                contributions=contributions,
                previous_day=previous_day,
            )
        )

    saved[days[0]] = reconcile_ranked_day(_input(now=DAY_START + timedelta(hours=1)))
    for day in days[1:]:
        recalculate(database, connection, day_start=day)
    monkeypatch.setattr(reconciliation_db, "recalculate_ranked_day", recalculate)
    inputs = {"player_id": 1, "ranked_day_start": DAY_START.isoformat()}
    if repair:
        inputs.update(
            recalculate_season="1783918800",
            last_ranked_day_start=days[1].isoformat(),
        )
    claim = SimpleNamespace(
        input_json=inputs, parser_version="test", processing_version="test",
        domain_rule_version="test", analytics_rule_version="test",
    )
    reconciliation_db.complete_reconciliation(database, claim)
    assert saved[days[0]].state == "Complete"
    if shielded:
        assert saved[days[0]].shield_duration_days == 1
        assert saved[days[1]].shield_duration_days == (2 if repair else 1)
        assert saved[days[2]].shield_state == (
            "uncertain_sequence" if repair else "inferred_shielded"
        )
    elif repair:
        for day in days[1:]:
            assert saved[day].state == "Complete"
            assert saved[day].automatic_defense_loss == 80
            assert "automatic_defense_basis_unavailable" not in saved[day].failure_reasons
    else:
        assert "automatic_defense_basis_unavailable" in saved[days[1]].failure_reasons


def test_reset_pair_with_production_parser_versions_publishes_army_day(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Production processes profiles with parser v3 and battle logs with v2,
    # usually at the same moment. Each Reset check used to look for both
    # results under its own job's parser version, and before the other job
    # committed, so no Reset pair was ever complete, ended days stayed Live
    # and no army analytics job was ever queued.
    monkeypatch.setattr(
        ObservationProcessor, "_process_claim", ObservationProcessor._process_claim_once
    )
    with domain_database(database_url, include_coordinator=True) as connection_info:
        pairs = [
            _store_baseline_pair(
                connection_info,
                archive_server,
                key=key,
                boundary=boundary,
                trophies=trophies,
                empty_battle_log=empty,
                profile_parser_version=PROFILE_PARSER_VERSION,
            )
            for key, boundary, trophies, empty in (
                ("start", DAY_START, 6000, True),
                ("end", DAY_END, 6040, False),
            )
        ]
        # The Reset battle log repeats an already decoded battle, as most do,
        # so neither Reset job takes the pair's lock before its own result.
        _middle_observation, middle_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="middle-battle",
            endpoint="battle_log",
            body=_battle_log(),
            observed_at=DAY_START + timedelta(hours=7),
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            for job_id in (*pairs[0][2:], middle_job):
                result = processor.process_job(job_id, owner=f"source-{job_id}")
                assert result is not None and result.outcome == "processed"
            outcomes: dict[int, str] = {}

            def process(job_id: int) -> None:
                db, own_processor = _processor(connection_info, archive_server)
                try:
                    result = own_processor.process_job(job_id, owner=f"pair-{job_id}")
                    outcomes[job_id] = result.outcome if result else "unclaimed"
                finally:
                    db.close()

            with psycopg.connect(connection_info) as holder:
                lock_key = "reset-baseline:" + str(
                    holder.execute(
                        "SELECT id FROM collector_work WHERE profile_observation_id = %s",
                        (pairs[1][0],),
                    ).fetchone()[0]
                )
                holder.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
                )
                # Start the battle log first: a profile job ahead of it can
                # hold a player row the battle log then waits on.
                threads = []
                for expected, job_id in enumerate(reversed(pairs[1][2:]), start=1):
                    threads.append(threading.Thread(target=process, args=(job_id,)))
                    threads[-1].start()
                    waiting = 0
                    for _ in range(200):
                        waiting = holder.execute(
                            """
                            SELECT count(*) FROM pg_locks
                            WHERE locktype = 'advisory' AND NOT granted
                              AND objid = (hashtextextended(%s, 0) & 4294967295)::oid
                            """,
                            (lock_key,),
                        ).fetchone()[0]
                        if waiting == expected:
                            break
                        time.sleep(0.05)
                    assert waiting == expected, "both Reset jobs must overlap"
            for thread in threads:
                thread.join(timeout=60)
            assert outcomes == dict.fromkeys(pairs[1][2:], "processed")

            boundary = DAY_END.strftime("%Y-%m-%dT%H:%M:%SZ")

            def next_job(work_type: str, boundary: str | None = None) -> int | None:
                with database.pool.connection() as connection:
                    row = connection.execute(
                        """
                        SELECT id FROM python_processing_jobs
                        WHERE work_type = %s AND status = 'pending'
                          AND (%s::text IS NULL OR input_json->>'boundary_at' = %s)
                        ORDER BY id LIMIT 1
                        """,
                        (work_type, boundary, boundary),
                    ).fetchone()
                return None if row is None else int(row[0])

            while (reconcile := next_job("reconcile_ranked_day")) is not None:
                processor.process_job(reconcile, owner="reconcile")
            snapshot = next_job("build_snapshot", boundary)
            assert snapshot is not None, "the ended day never reached publication"
            _process_snapshot_and_analytics(
                connection_info, database, processor, snapshot, owner_prefix="snapshot"
            )
            army_job = next_job("build_army_analytics", boundary)
            assert army_job is not None, "no army analytics job was queued"
            army = processor.process_job(army_job, owner="army")
            assert army is not None and army.outcome == "processed"
            with database.pool.connection() as connection:
                completed_day = connection.execute(
                    "SELECT 1 FROM army_analytics_completed_days"
                    " WHERE ranked_day_start = %s",
                    (DAY_START,),
                ).fetchone()
            assert completed_day is not None
        finally:
            database.close()


def test_republication_finishes_days_left_by_partial_reset_pairs(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Production state on 2026-10-02: both Reset results were processed but
    # each pair's latest check said partial, so its day stayed Live.
    opening = DAY_START - timedelta(days=22)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        pairs = [
            _store_baseline_pair(
                connection_info,
                archive_server,
                key=key,
                boundary=boundary,
                trophies=trophies,
                empty_battle_log=empty,
                observed_at=observed_at,
                profile_parser_version=PROFILE_PARSER_VERSION,
            )
            for key, boundary, trophies, empty, observed_at in (
                # The season's opening Reset is day 1's starting evidence.
                ("opening", opening, 5000, True, None),
                # This profile arrived after the day's first battle at 12:00.
                ("start", DAY_START, 6000, False, DAY_START + timedelta(hours=8)),
                ("end", DAY_END, 6040, True, None),
            )
        ]
        database, processor = _processor(connection_info, archive_server)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(
                    reconciliation_db.reset_baselines,
                    "_refresh_reset_baseline_evidence",
                    lambda *args, **kwargs: None,
                )
                for pair in pairs:
                    for job_id in pair[2:]:
                        assert (
                            processor.process_job(job_id, owner="pair").outcome
                            == "processed"
                        )
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO reset_baseline_evidence (
                        sweep_id, player_id, boundary_at, collector_work_id,
                        profile_observation_id, battle_log_observation_id,
                        state, failure_reasons, evidence_key
                    )
                    SELECT work.sweep_id, work.player_id, sweep.boundary_at, work.id,
                           work.profile_observation_id,
                           work.battle_log_observation_id, 'partial',
                           '["unprocessed_profile"]', md5(work.id::text) || md5('e')
                    FROM collector_work AS work
                    JOIN collector_reset_sweeps AS sweep ON sweep.id = work.sweep_id
                    WHERE work.profile_observation_id = ANY(%s)
                    """,
                    ([pair[0] for pair in pairs],),
                )
                connection.execute(
                    "UPDATE python_processing_jobs SET status = 'cancelled'"
                    " WHERE work_type = 'reconcile_ranked_day' AND status = 'pending'"
                )
                connection.commit()

            def latest_states() -> list[str]:
                with database.pool.connection() as connection:
                    return [
                        text(
                            connection.execute(
                                """
                                SELECT evidence.state
                                FROM reset_baseline_evidence AS evidence
                                JOIN collector_work AS work
                                  ON work.id = evidence.collector_work_id
                                WHERE work.profile_observation_id = %s
                                ORDER BY evidence.version DESC, evidence.id DESC
                                LIMIT 1
                                """,
                                (pair[0],),
                            ).fetchone()[0]
                        )
                        for pair in pairs
                    ]

            def repair() -> dict:
                return reconciliation_db.enqueue_current_season_republication(
                    database, max_jobs=1
                )

            def queued_days(job_ids: list[int]) -> dict[str, int]:
                with database.pool.connection() as connection:
                    return {
                        text(row[0]): int(row[1])
                        for row in connection.execute(
                            "SELECT input_json->>'ranked_day_start', id"
                            " FROM python_processing_jobs WHERE id = ANY(%s)",
                            (job_ids,),
                        ).fetchall()
                    }

            def iso(moment) -> str:
                return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

            # The opening Reset rebuilds day 1, which has ended, and queues
            # nothing for the previous season.
            report = repair()
            assert (report["evaluated_count"], report["failure_reasons"]) == (1, {})
            assert list(queued_days(report["job_ids"])) == [iso(opening)]
            with database.pool.connection() as connection:
                previous_season_jobs = connection.execute(
                    """
                    SELECT count(*) FROM python_processing_jobs
                    WHERE (work_type <> 'reconcile_ranked_day'
                           AND input_json->>'boundary_at' = %s)
                       OR input_json->>'ranked_day_start' = %s
                    """,
                    (iso(opening), iso(opening - timedelta(days=1))),
                ).fetchone()[0]
            assert previous_season_jobs == 0
            assert latest_states() == ["complete", "partial", "partial"]
            # A batch that queues nothing reports why and leaves later pairs.
            assert repair() == {
                "job_ids": [],
                "evaluated_count": 1,
                "failure_reasons": {"profile_after_first_event": 1},
            }
            assert latest_states() == ["complete", "failed", "partial"]
            # A repaired pair rebuilds the day it ends and the ended day it
            # starts, even if that day was already finished without it.
            report = repair()
            assert report["evaluated_count"] == 1
            days = queued_days(report["job_ids"])
            assert list(days) == [iso(DAY_START)]
            assert latest_states() == ["complete", "failed", "complete"]
            job = days[iso(DAY_START)]
            assert processor.process_job(job, owner="repair").outcome == "processed"
            assert repair() == {"job_ids": [], "evaluated_count": 0, "failure_reasons": {}}
            with database.pool.connection() as connection:
                day_states = connection.execute(
                    """
                    SELECT DISTINCT ON (ranked_day_start) ranked_day_start, state
                    FROM ranked_day_versions
                    WHERE ranked_day_start = ANY(%s)
                    ORDER BY ranked_day_start, id DESC
                    """,
                    ([DAY_START, DAY_END],),
                ).fetchall()
            assert [row[0] for row in day_states] == [DAY_START, DAY_END]
            assert all(text(row[1]) != "Live" for row in day_states)
        finally:
            database.close()
