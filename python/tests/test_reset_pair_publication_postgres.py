from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from psycopg.types.json import Jsonb
from test_reconciliation import _input
from test_reconciliation_postgres import (
    DAY_END,
    DAY_START,
    _battle_log,
    _processor,
    _profile,
    _seed_reset_collection_identity,
    _store_baseline_pair,
)
from test_snapshot_publication_postgres import _process_snapshot_and_analytics

from clashlens import reconciliation_db, reset_baselines
from clashlens.collector_db import CollectorDatabase
from clashlens.db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
)
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
            # A failed pair reports why and finishes only the day it ends; it
            # is no starting evidence for the day it starts.
            report = repair()
            assert (report["evaluated_count"], report["failure_reasons"]) == (
                1, {"profile_after_first_event": 1}
            )
            assert list(queued_days(report["job_ids"])) == [
                iso(DAY_START - timedelta(days=1))
            ]
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
            assert repair() == {
                "job_ids": [],
                "evaluated_count": 0,
                "failure_reasons": {},
                "failed_blockers": [],
            }
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


def test_republication_retries_failed_reset_repair_left_live(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Production job 1681969 on 2026-10-02: a repaired pair's day rebuild ran
    # out of lease attempts, so its day stayed Live and the pair stayed complete.
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
        middle = store_observation(
            connection_info,
            archive_server,
            occurrence_key="middle",
            endpoint="battle_log",
            body=_battle_log(),
            observed_at=DAY_START + timedelta(hours=7),
            normalized_tag="#2PP",
        )[1]
        database, processor = _processor(connection_info, archive_server)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(
                    reconciliation_db.reset_baselines,
                    "_refresh_reset_baseline_evidence",
                    lambda *args, **kwargs: None,
                )
                for job_id in (*pairs[0][2:], middle, *pairs[1][2:]):
                    assert (
                        processor.process_job(job_id, owner="source").outcome
                        == "processed"
                    )
            with database.pool.connection() as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
                # The day was last calculated while it was still Live.
                with monkeypatch.context() as patch:
                    original = reconciliation_db.reconcile_ranked_day
                    patch.setattr(
                        reconciliation_db,
                        "reconcile_ranked_day",
                        lambda data: original(
                            replace(data, now=DAY_START + timedelta(hours=7))
                        ),
                    )
                    reconciliation_db.recalculate_ranked_day(
                        database,
                        connection,
                        player_id=player_id,
                        day_start=DAY_START,
                        parser_version=DEFAULT_PARSER_VERSION,
                        processing_version=PROCESSING_VERSION,
                        domain_rule_version=DOMAIN_RULE_VERSION,
                        analytics_rule_version=ANALYTICS_RULE_VERSION,
                    )
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

            def repair(max_jobs: int = 100) -> dict:
                return reconciliation_db.enqueue_current_season_republication(
                    database, max_jobs=max_jobs
                )

            def jobs(ids: list[int]) -> list[tuple]:
                with database.pool.connection() as connection:
                    return connection.execute(
                        "SELECT id, status, deduplication_key, input_json"
                        " FROM python_processing_jobs WHERE id = ANY(%s) ORDER BY id",
                        (ids,),
                    ).fetchall()

            def day_state() -> str:
                with database.pool.connection() as connection:
                    return text(
                        connection.execute(
                            """
                            SELECT state FROM ranked_day_versions
                            WHERE player_id = %s AND ranked_day_start = %s
                            ORDER BY version DESC, id DESC LIMIT 1
                            """,
                            (player_id, DAY_START),
                        ).fetchone()[0]
                    )

            def set_status(ids: list[int], status: str, category=None) -> None:
                with database.pool.connection() as connection:
                    connection.execute(
                        "UPDATE python_processing_jobs SET status = %s,"
                        " failure_category = %s, attempt_count = CASE WHEN"
                        " %s = 'failed' THEN max_attempts ELSE 0 END"
                        " WHERE id = ANY(%s)",
                        (status, category, status, ids),
                    )

            def add_job(key: str, input_json: dict) -> int:
                with database.pool.connection() as connection:
                    return connection.execute(
                        "INSERT INTO python_processing_jobs_worker (observation_id,"
                        " work_type, deduplication_key, input_json, state, due_at,"
                        " parser_version, processing_version, domain_rule_version,"
                        " analytics_rule_version) VALUES (NULL, 'reconcile_ranked_day',"
                        " %s, %s, 'pending', clock_timestamp(), %s, %s, %s, %s)"
                        " RETURNING id",
                        (
                            key,
                            Jsonb(input_json),
                            DEFAULT_PARSER_VERSION,
                            PROCESSING_VERSION,
                            DOMAIN_RULE_VERSION,
                            ANALYTICS_RULE_VERSION,
                        ),
                    ).fetchone()[0]

            def iso(moment) -> str:
                return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

            with database.pool.connection() as connection:
                season = text(
                    connection.execute(
                        "SELECT current_league_season_id FROM legend_season_anchors"
                        " WHERE state = 'confirmed'"
                    ).fetchone()[0]
                )
            earlier_day = DAY_START - timedelta(days=2)
            # This rebuilds its own day and every later saved day of the
            # Season, so it reaches the Live day although it starts earlier.
            season_rebuild = {
                "player_id": player_id,
                "ranked_day_start": iso(earlier_day),
                "last_ranked_day_start": iso(earlier_day),
                "recalculate_season": season,
            }
            idle = {
                "job_ids": [],
                "evaluated_count": 0,
                "failure_reasons": {},
                "failed_blockers": [],
            }

            # An older failed repair that needs investigating.
            blocker = add_job("reconcile:reset-baseline:investigate", season_rebuild)
            set_status([blocker], "failed", "invalid_work_input")
            reported_blocker = {
                "job_id": blocker,
                "player_id": player_id,
                "ranked_day_start": iso(earlier_day),
                "failure_category": "invalid_work_input",
            }

            first = repair()
            assert first["evaluated_count"] == 2 and len(first["job_ids"]) == 2
            set_status(first["job_ids"], "failed", "lease_expired_max_attempts")
            assert day_state() == "Live"
            failed = jobs(first["job_ids"])

            # Other work whose rebuild reaches that day is left to finish.
            active = add_job("test:active", season_rebuild)
            assert repair() == idle
            set_status([active], "cancelled")

            # Both failed repairs rebuild the Live day, so one batch queues
            # only the first again, with its original inputs; the failed jobs
            # keep their history and a repeat queues nothing more.
            second = repair(max_jobs=3)
            assert second["evaluated_count"] == 1
            assert second["failed_blockers"] == [reported_blocker]
            recoveries = jobs(second["job_ids"])
            assert [row[1:] for row in recoveries] == [
                (
                    "pending",
                    f"reconcile:reset-recovery:{failed[0][0]}",
                    {**failed[0][3], "recovers_job_id": failed[0][0]},
                )
            ]
            assert [row[1] for row in jobs(first["job_ids"])] == ["failed", "failed"]
            assert repair() == idle

            # Older blockers do not use up the batch: the other failed repair
            # is still queued once the first recovery has failed too.
            set_status([recoveries[0][0]], "failed", "lease_expired_max_attempts")
            third = repair(max_jobs=1)
            assert third["evaluated_count"] == 1
            assert third["failed_blockers"] == [reported_blocker]
            assert [row[2] for row in jobs(third["job_ids"])] == [
                f"reconcile:reset-recovery:{failed[1][0]}"
            ]

            # A recovery that fails too is reported, not queued again. The day
            # still Live gets one rebuild of its own, never repeated.
            set_status(third["job_ids"], "cancelled")
            blocked = {
                **idle,
                "failed_blockers": [
                    reported_blocker,
                    {
                        "job_id": recoveries[0][0],
                        "player_id": player_id,
                        "ranked_day_start": failed[0][3]["ranked_day_start"],
                        "failure_category": "lease_expired_max_attempts",
                    },
                ],
            }
            fourth = repair()
            assert {**fourth, "job_ids": []} == blocked
            assert [row[2].split(":")[1] for row in jobs(fourth["job_ids"])] == [
                "ended-live"
            ]
            set_status(fourth["job_ids"], "cancelled")
            assert repair() == blocked
            set_status([recoveries[0][0]], "pending")
            assert (
                processor.process_job(recoveries[0][0], owner="recovery").outcome
                == "processed"
            )
            assert day_state() != "Live"
            assert repair() == idle
        finally:
            database.close()


@pytest.mark.parametrize(
    ("profile_at", "battle_log_at", "state", "reasons"),
    [
        (DAY_START + timedelta(minutes=10), DAY_START + timedelta(minutes=10), "complete", set()),
        (DAY_START, DAY_START + timedelta(minutes=10), "complete", set()),
        # After the next Reset: no battles recorded for the day it belongs to
        # cannot show this profile came before the first one.
        (
            DAY_END + timedelta(minutes=10),
            DAY_END + timedelta(minutes=10),
            "failed",
            {"profile_late", "battle_log_late"},
        ),
        # An empty battle log from 05:00 cannot show a battle at 05:05, so a
        # profile retried at 05:10 is not the Reset trophy count.
        (
            DAY_START + timedelta(minutes=10),
            DAY_START,
            "failed",
            {"battle_log_before_profile"},
        ),
    ],
)
def test_reset_pair_proves_the_reset_only_when_collected_in_time_and_order(
    database_url: str, archive_server, profile_at, battle_log_at, state, reasons
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        profile_id, profile_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="order-profile",
            endpoint="profile",
            body=_profile(6040),
            observed_at=profile_at,
            normalized_tag="#2PP",
            parser_version=PROFILE_PARSER_VERSION,
        )
        battle_log_id, battle_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="order-battle",
            endpoint="battle_log",
            body=_battle_log(empty=True),
            observed_at=battle_log_at,
            normalized_tag="#2PP",
        )
        _seed_reset_collection_identity(
            connection_info,
            key="order",
            boundary=DAY_START,
            profile_observation_id=profile_id,
            battle_observation_id=battle_log_id,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            for job_id in (profile_job, battle_job):
                assert processor.process_job(job_id, owner=f"order-{job_id}") is not None
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            evidence = connection.execute(
                "SELECT state, failure_reasons FROM reset_baseline_evidence"
                " ORDER BY version DESC, id DESC LIMIT 1"
            ).fetchone()
    assert evidence[0] == state
    assert reasons <= set(evidence[1])


def test_reset_evidence_holds_its_pair_while_the_collector_saves_a_retry(
    database_url: str, archive_server, monkeypatch
) -> None:
    # 2026-10-03 05:01: a 504 profile's evidence was being saved when the
    # collector saved the pair's retried battle log. The evidence check then
    # saw a different pair, refused the row, and the worker exited.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observed = {}
        jobs = {}
        for key, endpoint, body, minutes in (
            ("profile", "profile", _profile(6040), 1),
            ("first_log", "battle_log", _battle_log(empty=True), 1),
            ("retried_log", "battle_log", _battle_log(empty=True), 2),
        ):
            observed[key], jobs[key] = store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"race-{key}",
                endpoint=endpoint,
                body=body,
                observed_at=DAY_START + timedelta(minutes=minutes),
                normalized_tag="#2PP",
                parser_version=PROFILE_PARSER_VERSION
                if endpoint == "profile"
                else None,
            )
        _seed_reset_collection_identity(
            connection_info,
            key="race",
            boundary=DAY_START,
            profile_observation_id=observed["profile"],
            battle_observation_id=observed["first_log"],
        )
        saved = threading.Event()
        collector_pid = []

        def collector_saves_retry() -> None:
            with psycopg.connect(connection_info) as connection:
                collector_pid.append(connection.info.backend_pid)
                work_id = connection.execute(
                    "SELECT id FROM collector_work WHERE kind = 'reset_baseline'"
                ).fetchone()[0]
                CollectorDatabase._record_intent_endpoint(
                    connection,
                    SimpleNamespace(
                        collector_work_id=work_id, endpoint="battle_log", http_status=200
                    ),
                    observed["retried_log"],
                )
            saved.set()

        collector = threading.Thread(target=collector_saves_retry)
        load_endpoint = reset_baselines._load_reset_endpoint_evidence

        def save_retry_after_reading(*args, **kwargs):
            result = load_endpoint(*args, **kwargs)
            if kwargs["endpoint"] == "battle_log" and collector.ident is None:
                collector.start()
                with psycopg.connect(connection_info, autocommit=True) as watcher:
                    deadline = time.monotonic() + 10
                    while not saved.is_set() and time.monotonic() < deadline:
                        if collector_pid and watcher.execute(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks"
                            " WHERE pid = %s AND NOT granted)",
                            (collector_pid[0],),
                        ).fetchone()[0]:
                            break
                        time.sleep(0.01)
            return result

        monkeypatch.setattr(
            reset_baselines,
            "_load_reset_endpoint_evidence",
            save_retry_after_reading,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            result = processor.process_job(jobs["profile"], owner="race")
        finally:
            database.close()
        collector.join(timeout=10)
        assert result is not None and result.outcome == "processed"
        assert saved.is_set()
        with psycopg.connect(connection_info) as connection:
            evidence = connection.execute(
                "SELECT battle_log_observation_id FROM reset_baseline_evidence"
            ).fetchall()
            work = connection.execute(
                "SELECT battle_log_observation_id FROM collector_work"
                " WHERE kind = 'reset_baseline'"
            ).fetchone()
    # The evidence names the pair it read; the retry lands after it.
    assert evidence == [(observed["first_log"],)]
    assert work == (observed["retried_log"],)


def _live_day_with_battle(connection_info, archive_server, monkeypatch, *, start=True):
    """A day with one battle, last calculated while Live, and a complete
    starting Reset check unless ``start`` is false."""
    jobs = _store_baseline_pair(
        connection_info, archive_server, key="start", boundary=DAY_START,
        trophies=6000, empty_battle_log=True,
        profile_parser_version=PROFILE_PARSER_VERSION,
    )[2:] if start else ()
    middle = store_observation(
        connection_info, archive_server, occurrence_key="middle",
        endpoint="battle_log", body=_battle_log(),
        observed_at=DAY_START + timedelta(hours=7), normalized_tag="#2PP",
    )[1]
    database, processor = _processor(connection_info, archive_server)
    for job_id in (*jobs, middle):
        assert processor.process_job(job_id, owner="source").outcome == "processed"
    with database.pool.connection() as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = '#2PP'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE python_processing_jobs SET status = 'cancelled'"
            " WHERE work_type = 'reconcile_ranked_day' AND status = 'pending'"
        )
        with monkeypatch.context() as patch:
            original = reconciliation_db.reconcile_ranked_day
            patch.setattr(
                reconciliation_db,
                "reconcile_ranked_day",
                lambda data: original(replace(data, now=DAY_START + timedelta(hours=7))),
            )
            reconciliation_db.recalculate_ranked_day(
                database, connection, player_id=player_id, day_start=DAY_START,
                parser_version=DEFAULT_PARSER_VERSION,
                processing_version=PROCESSING_VERSION,
                domain_rule_version=DOMAIN_RULE_VERSION,
                analytics_rule_version=ANALYTICS_RULE_VERSION,
            )
        connection.commit()
    return database, processor, player_id


def _latest_day(database, player_id, day_start=DAY_START):
    with database.pool.connection() as connection:
        return connection.execute(
            """
            SELECT log.state, version.state, version.final_trophies_before_reset,
                   version.net_trophy_change, version.failure_reasons,
                   version.attack_count + version.defense_count, version.id,
                   (SELECT count(*) FROM ranked_day_versions AS other
                    WHERE other.player_id = log.player_id
                      AND other.ranked_day_start = log.ranked_day_start)
            FROM api_player_daily_logs AS log
            JOIN ranked_day_versions AS version
              ON version.id = log.ranked_day_version_id
            WHERE log.player_id = %s AND log.ranked_day_start = %s
            ORDER BY log.version DESC LIMIT 1
            """,
            (player_id, day_start),
        ).fetchone()


@pytest.mark.parametrize("start", [True, False])
@pytest.mark.parametrize(
    ("profile_at", "battle_log_at", "reason"),
    [
        # The ending profile arrived after the next day's first battle.
        (DAY_END + timedelta(days=1, minutes=10), DAY_END + timedelta(days=1, minutes=10),
         "profile_late"),
        # The battle log was read before the profile, so it cannot prove it.
        (DAY_END + timedelta(minutes=10), DAY_END, "battle_log_before_profile"),
    ],
)
def test_failed_ending_reset_finishes_the_day_without_inventing_a_total(
    database_url: str, archive_server, monkeypatch, profile_at, battle_log_at, reason,
    start,
) -> None:
    # Production on 2026-10-03: 4,703 ended days stayed Live because a failed
    # ending Reset check queued no recalculation of the day it ended.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor, player_id = _live_day_with_battle(
            connection_info, archive_server, monkeypatch, start=start
        )
        try:
            assert _latest_day(database, player_id)[:2] == ("Live", "Live")
            ending = {}
            for endpoint, body, observed_at in (
                ("profile", _profile(6100), profile_at),
                ("battle_log", _battle_log(empty=True), battle_log_at),
            ):
                ending[endpoint] = store_observation(
                    connection_info, archive_server, occurrence_key=f"end-{endpoint}",
                    endpoint=endpoint, body=body, observed_at=observed_at,
                    normalized_tag="#2PP",
                    parser_version=PROFILE_PARSER_VERSION if endpoint == "profile" else None,
                )
            _seed_reset_collection_identity(
                connection_info, key="end", boundary=DAY_END,
                profile_observation_id=ending["profile"][0],
                battle_observation_id=ending["battle_log"][0],
            )
            for _, job_id in ending.values():
                assert processor.process_job(job_id, owner="end").outcome == "processed"

            def reset_jobs() -> list[int]:
                with database.pool.connection() as connection:
                    return [
                        row[0] for row in connection.execute(
                            "SELECT id FROM python_processing_jobs"
                            " WHERE deduplication_key LIKE 'reconcile:reset-baseline:%%'"
                            " AND input_json->>'ranked_day_start' = %s",
                            (DAY_START.strftime("%Y-%m-%dT%H:%M:%SZ"),),
                        ).fetchall()
                    ]

            with database.pool.connection() as connection:
                evidence = connection.execute(
                    "SELECT state, failure_reasons FROM reset_baseline_evidence"
                    " WHERE boundary_at = %s ORDER BY version DESC, id DESC LIMIT 1",
                    (DAY_END,),
                ).fetchone()
            assert text(evidence[0]) == "failed" and reason in evidence[1]
            # Each new failed reading queues the day once.
            jobs = reset_jobs()
            for job in jobs:
                assert processor.process_job(job, owner="finish").outcome == "processed"
            finished = _latest_day(database, player_id)
            assert finished[0] == "Partial"
            assert finished[1] in {"Partial", "Malformed", "Inconsistent"}
            # An untrusted reading gives no next start at all.
            assert "missing_end_baseline" in finished[4]
            assert finished[5] == 1
            # A total comes only from a proven start plus the recorded battle,
            # never from the failed reading's 6,100.
            assert finished[2:4] == ((6040, 40) if start else (None, None))

            # Handing the same failed evidence over again changes nothing.
            with database.pool.connection() as connection:
                with connection.transaction():
                    queued, _ = reset_baselines._evaluate_reset_baseline(
                        database, connection,
                        observation_id=ending["battle_log"][0],
                        observation_endpoint="battle_log",
                        parser_version=DEFAULT_PARSER_VERSION,
                        processing_version=PROCESSING_VERSION,
                    )
            assert queued == [] and reset_jobs() == jobs
            assert _latest_day(database, player_id) == finished
        finally:
            database.close()


@pytest.mark.parametrize("anchor_moved_on", [False, True])
def test_republication_finishes_ended_days_left_live(
    database_url: str, archive_server, monkeypatch, anchor_moved_on
) -> None:
    # Production on 2026-10-03: ended days already calculated under the
    # current rule while Live were never selected again, including days with
    # no ending Reset check. After the next Season starts they are in the
    # previous Season and must still be finished.
    today = ranked_day_for(datetime.now(UTC))
    yesterday = ranked_day_for(today.start - timedelta(days=1))
    season = yesterday.season_start + timedelta(days=28 if anchor_moved_on else 0)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        profile_job = store_observation(
            connection_info, archive_server, occurrence_key="profile",
            endpoint="profile", body=_profile(6000), observed_at=DAY_START,
            normalized_tag="#2PP", parser_version=PROFILE_PARSER_VERSION,
        )[1]
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_job(profile_job, owner="p").outcome == "processed"
            with database.pool.connection() as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
                connection.execute(
                    """
                    UPDATE legend_season_anchors
                    SET current_league_season_id = %s, previous_league_season_id = %s,
                        current_start = %s, previous_start = %s
                    WHERE state = 'confirmed'
                    """,
                    (
                        str(int(season.timestamp())),
                        str(int((season - timedelta(days=28)).timestamp())),
                        season,
                        season - timedelta(days=28),
                    ),
                )
                # Yesterday was last calculated while it was Live; today is.
                original = reconciliation_db.reconcile_ranked_day
                for day, now in (
                    (yesterday, yesterday.start + timedelta(hours=1)),
                    (today, None),
                ):
                    with monkeypatch.context() as patch:
                        if now is not None:
                            patch.setattr(
                                reconciliation_db,
                                "reconcile_ranked_day",
                                lambda data, now=now: original(replace(data, now=now)),
                            )
                        reconciliation_db.recalculate_ranked_day(
                            database, connection, player_id=player_id,
                            day_start=day.start,
                            parser_version=DEFAULT_PARSER_VERSION,
                            processing_version=PROCESSING_VERSION,
                            domain_rule_version=DOMAIN_RULE_VERSION,
                            analytics_rule_version=ANALYTICS_RULE_VERSION,
                        )
                # An earlier republication request for yesterday finished.
                day_text = yesterday.start.strftime("%Y-%m-%dT%H:%M:%SZ")
                finished_id = connection.execute(
                    "INSERT INTO python_processing_jobs_worker (observation_id,"
                    " work_type, deduplication_key, input_json, state, due_at,"
                    " parser_version, processing_version, domain_rule_version,"
                    " analytics_rule_version) VALUES (NULL, 'reconcile_ranked_day',"
                    " %s, %s, 'pending', clock_timestamp(), %s, %s, %s, %s)"
                    " RETURNING id",
                    (
                        (
                            f"reconcile:current-season:{player_id}:{day_text}:"
                            f"{reconciliation_db.RECONCILIATION_RULE_VERSION}"
                        ),
                        Jsonb({
                            "player_id": player_id,
                            "ranked_day_start": day_text,
                            "official_season_id": yesterday.official_season_id,
                            "trigger": "current_season_republication",
                        }),
                        DEFAULT_PARSER_VERSION,
                        PROCESSING_VERSION,
                        DOMAIN_RULE_VERSION,
                        ANALYTICS_RULE_VERSION,
                    ),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE python_processing_jobs SET status = CASE WHEN id = %s"
                    " THEN 'complete' ELSE 'cancelled' END"
                    " WHERE work_type = 'reconcile_ranked_day' AND status = 'pending'",
                    (finished_id,),
                )
                connection.commit()
            assert _latest_day(database, player_id, yesterday.start)[:3] == (
                "Live", "Live", None
            )
            assert _latest_day(database, player_id, today.start)[:2] == ("Live", "Live")

            def repair() -> list[int]:
                return reconciliation_db.enqueue_current_season_republication(
                    database, max_jobs=10
                )["job_ids"]

            [job] = repair()
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT input_json->>'ranked_day_start' FROM python_processing_jobs"
                    " WHERE id = %s",
                    (job,),
                ).fetchone()[0] == day_text
            # Work already queued for the day is left to finish.
            assert repair() == []
            assert processor.process_job(job, owner="repair").outcome == "processed"
            finished = _latest_day(database, player_id, yesterday.start)
            assert finished[:2] == ("Partial", "Partial")
            assert finished[2:4] == (None, None)
            assert {"missing_start_baseline", "missing_end_baseline"} <= set(finished[4])
            # Today is rebuilt after yesterday but stays Live, with no total.
            assert _latest_day(database, player_id, today.start)[:4] == (
                "Live", "Live", None, None
            )
            assert repair() == []

            # A finished day saved with 8 undisputed attacks and 8 undisputed
            # defenses but no net is rebuilt once too; a disputed one is not.
            def saved_with(reasons: list[str]) -> None:
                with database.pool.connection() as connection:
                    connection.execute(
                        "UPDATE ranked_day_versions SET attack_count = 8,"
                        " defense_count = 8, failure_reasons = %s WHERE id = %s",
                        (Jsonb(reasons), finished[6]),
                    )

            saved_with([*finished[4], "perspective_disagreement"])
            assert repair() == []
            saved_with(finished[4])
            [job] = repair()
            assert processor.process_job(job, owner="eight").outcome == "processed"
            assert repair() == []
        finally:
            database.close()


def test_failed_season_opening_reset_finishes_only_the_closing_day(
    database_url: str, archive_server
) -> None:
    # As on 2026-10-05: one Reset ends the Season's last day and opens the next.
    opening = DAY_START + timedelta(days=6)
    assert ranked_day_for(opening).season_start == opening
    closing = opening - timedelta(days=1)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        late = opening + timedelta(days=1, minutes=10)
        observed = {
            endpoint: store_observation(
                connection_info, archive_server, occurrence_key=f"open-{endpoint}",
                endpoint=endpoint, body=body, observed_at=late, normalized_tag="#2PP",
                parser_version=PROFILE_PARSER_VERSION if endpoint == "profile" else None,
            )
            for endpoint, body in (
                ("profile", _profile(5000)), ("battle_log", _battle_log(empty=True))
            )
        }
        _seed_reset_collection_identity(
            connection_info, key="open", boundary=opening,
            profile_observation_id=observed["profile"][0],
            battle_observation_id=observed["battle_log"][0],
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            for _, job_id in observed.values():
                assert processor.process_job(job_id, owner="open").outcome == "processed"
            with database.pool.connection() as connection:
                jobs = connection.execute(
                    "SELECT id, input_json->>'ranked_day_start' FROM python_processing_jobs"
                    " WHERE deduplication_key LIKE 'reconcile:reset-baseline:%%'"
                ).fetchall()
            assert jobs and {row[1] for row in jobs} == {
                closing.strftime("%Y-%m-%dT%H:%M:%SZ")
            }
            for job_id, _ in jobs:
                assert processor.process_job(job_id, owner="close").outcome == "processed"
            # The reading names the old Season, so it gives no total either.
            finished = _latest_day(database, 1, closing)
            assert finished[:4] == ("Partial", "Partial", None, None)
            assert "missing_end_baseline" in finished[4]
            # The repair re-checks this Reset only as day 1's starting
            # evidence; failed evidence starts nothing.
            with database.pool.connection() as connection:
                with connection.transaction():
                    queued, _ = reset_baselines._evaluate_reset_baseline(
                        database, connection,
                        observation_id=observed["battle_log"][0],
                        observation_endpoint="battle_log",
                        parser_version=DEFAULT_PARSER_VERSION,
                        processing_version=PROCESSING_VERSION,
                        ends_day=False, starts_ended_day=True,
                    )
            assert queued == []
        finally:
            database.close()
