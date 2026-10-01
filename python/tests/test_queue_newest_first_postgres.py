from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database, store_observation, text
from psycopg import sql
from test_domain_processing_postgres import (
    LIVE_BATTLE_PARSER_VERSION,
    PROFILE_FIXTURE,
    _live_battle_row,
    _prepare_reset_baseline_pair,
    _processor,
    _seed_battle_anchor,
)

from clashlens import api_leaderboard, job_outcomes
from clashlens.api_db import ApiDatabase
from clashlens.domain import ranked_day_for

DAY = ranked_day_for(datetime(2026, 8, 4, 12, tzinfo=UTC))


def _profile(
    connection_info: str,
    archive_server,
    *,
    tag: str,
    trophies: int,
    observed_at: datetime,
) -> tuple[int, int]:
    payload = json.loads(PROFILE_FIXTURE.read_bytes())
    payload["tag"] = tag
    payload["trophies"] = trophies
    return store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"profile-{tag}-{observed_at.isoformat()}",
        endpoint="profile",
        body=json.dumps(payload).encode(),
        observed_at=observed_at,
        normalized_tag=tag,
    )


def _job_outcomes(connection_info: str, job_ids: list[int]) -> list[tuple[str, str]]:
    with psycopg.connect(connection_info) as connection:
        rows = connection.execute(
            """
            SELECT job.status, job.outcome
            FROM python_processing_jobs AS job
            WHERE job.id = ANY(%s)
            ORDER BY job.id
            """,
            (job_ids,),
        ).fetchall()
    return [(text(row[0]), text(row[1])) for row in rows]


def test_one_pass_shows_every_players_newest_profile_and_skips_older_copies(
    database_url: str,
    archive_server,
) -> None:
    """A backlog drains newest first per player and the stalest player goes first."""

    tags = ("#9PP", "#2PP", "#8PP")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            # Leaderboard ages before the backlog: #9PP oldest, #8PP newest.
            for hour, tag in enumerate(tags, start=1):
                _observation, job_id = _profile(
                    connection_info,
                    archive_server,
                    tag=tag,
                    trophies=5000,
                    observed_at=DAY.start + timedelta(hours=hour),
                )
                assert processor.process_job(job_id, owner="seed").outcome == "processed"

            queued: dict[str, list[tuple[int, int, datetime]]] = {}
            for minutes in (0, 10, 20):
                for index, tag in enumerate(tags):
                    observed_at = DAY.start + timedelta(hours=4, minutes=minutes)
                    trophies = 5100 + minutes + index
                    observation_id, job_id = _profile(
                        connection_info,
                        archive_server,
                        tag=tag,
                        trophies=trophies,
                        observed_at=observed_at,
                    )
                    queued.setdefault(tag, []).append(
                        (observation_id, job_id, observed_at)
                    )
            newest = {tag: queued[tag][-1] for tag in tags}

            first_pass = [
                processor.process_once(owner="lane") for _ in range(len(tags))
            ]
            assert [result.job_id for result in first_pass] == [
                newest[tag][1] for tag in tags
            ]
            assert {result.outcome for result in first_pass} == {"processed"}

            now = DAY.start + timedelta(hours=4, minutes=25)
            board = api_leaderboard.get_live_leaderboard(api, limit=10, now=now)
            shown = {
                entry["tag"]: (entry["trophies"], entry["observed_at"])
                for entry in board["entries"]
            }
            assert shown == {
                tag: (5120 + index, newest[tag][2].isoformat())
                for index, tag in enumerate(tags)
            }
            assert board["source_observations"]["stale_count"] == 0

            rest = processor.process_until_idle(owner="lane", max_jobs=20)
            older_jobs = sorted(
                job_id for tag in tags for _obs, job_id, _at in queued[tag][:-1]
            )
            assert sorted(result.job_id for result in rest) == older_jobs
            assert {result.outcome for result in rest} == {"superseded"}
            assert _job_outcomes(connection_info, older_jobs) == [
                ("complete", "superseded")
            ] * len(older_jobs)

            older_observations = [
                observation_id
                for tag in tags
                for observation_id, _job, _at in queued[tag][:-1]
            ]
            with psycopg.connect(connection_info) as connection:
                kept, applied, attempts = connection.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM collector_observations
                         WHERE id = ANY(%s)),
                        (SELECT count(*) FROM player_profile_effects
                         WHERE observation_id = ANY(%s)),
                        (SELECT count(*) FROM python_processing_attempts
                         WHERE job_id = ANY(%s) AND state = 'complete'
                           AND outcome = 'superseded')
                    """,
                    (older_observations, older_observations, older_jobs),
                ).fetchone()
            # The raw responses stay for replay; nothing older was applied.
            assert (kept, applied, attempts) == (len(older_jobs), 0, len(older_jobs))
        finally:
            api.close()
            database.close()


def test_profiles_are_skipped_only_within_their_own_legend_day(
    database_url: str,
    archive_server,
) -> None:
    reset = DAY.end
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _reset_profile, _reset_battle, reset_profile_job, _battle_job = (
            _prepare_reset_baseline_pair(
                connection_info, archive_server, boundary=reset
            )
        )
        _before, before_reset_job = _profile(
            connection_info,
            archive_server,
            tag="#2PP",
            trophies=5200,
            observed_at=reset - timedelta(minutes=10),
        )
        _earlier, earlier_before_reset_job = _profile(
            connection_info,
            archive_server,
            tag="#2PP",
            trophies=5100,
            observed_at=reset - timedelta(minutes=30),
        )
        _same_day, same_day_job = _profile(
            connection_info,
            archive_server,
            tag="#2PP",
            trophies=5300,
            observed_at=reset + timedelta(minutes=30),
        )
        _newest, newest_job = _profile(
            connection_info,
            archive_server,
            tag="#2PP",
            trophies=5400,
            observed_at=reset + timedelta(hours=1),
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            outcomes = {
                name: processor.process_job(job_id, owner=name).outcome
                for name, job_id in (
                    ("newest", newest_job),
                    ("same_day", same_day_job),
                    ("reset_sweep", reset_profile_job),
                    ("before_reset", before_reset_job),
                    ("earlier_before_reset", earlier_before_reset_job),
                )
            }
        finally:
            database.close()
    assert outcomes == {
        "newest": "processed",
        "same_day": "superseded",
        # The Reset sweep feeds the day's baseline, and the last profile of
        # the previous Legend day is what its end-of-day snapshot reads.
        "reset_sweep": "processed",
        "before_reset": "processed",
        # A backlog left over at Reset still skips older copies of that day.
        "earlier_before_reset": "superseded",
    }


def test_battle_log_is_skipped_only_when_every_row_is_already_stored(
    database_url: str,
    archive_server,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        day = ranked_day_for(now)
        _seed_battle_anchor(connection_info, day.start)
        rows = [
            _live_battle_row(
                attack=True,
                battle_timestamp=day.start + timedelta(hours=1, minutes=index),
                opponent_tag=opponent,
                opponent_name=f"Defender {index}",
            )
            for index, opponent in enumerate(("#8PP", "#9PP", "#PYY", "#QQQ", "#RRR"))
        ]

        def battle_log(name: str, minutes: int, items: list[dict]) -> tuple[int, int]:
            return store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"battle-log-{name}",
                endpoint="battle_log",
                body=json.dumps({"items": items}).encode(),
                observed_at=day.start + timedelta(hours=2, minutes=minutes),
                normalized_tag="#2PP",
                parser_version=LIVE_BATTLE_PARSER_VERSION,
            )

        _earlier, earlier_job = battle_log("earlier", 0, [rows[0]])
        slid_observation, slid_job = battle_log("slid", 10, [rows[1], rows[0]])
        _confirmed, confirmed_job = battle_log("confirmed", 15, [rows[0]])
        _gap, gap_job = battle_log("gap", 20, [rows[4], rows[1]])
        _newest, newest_job = battle_log("newest", 30, [rows[3], rows[2], rows[1]])
        database, processor = _processor(connection_info, archive_server)
        try:
            outcomes = [
                processor.process_job(job_id, owner=f"battle-{job_id}").outcome
                for job_id in (
                    earlier_job,
                    confirmed_job,
                    newest_job,
                    slid_job,
                    gap_job,
                )
            ]
            with database.pool.connection() as connection:
                battles, slid_logs = connection.execute(
                    """
                    SELECT (SELECT count(*) FROM legend_battles),
                           (SELECT count(*) FROM battle_log_observations
                            WHERE observation_id = %s)
                    """,
                    (slid_observation,),
                ).fetchone()
        finally:
            database.close()
    # Battle 0 left the window before the newest log, but the earlier log
    # already confirmed it after the slid log. Battle 4 is only in the gap log.
    assert outcomes == [
        "processed",
        "processed",
        "processed",
        "superseded",
        "processed",
    ]
    assert (battles, slid_logs) == (5, 0)


@pytest.mark.parametrize("attack", [True, False])
def test_returning_battle_report_is_applied_after_the_battle_leaves_the_window(
    database_url: str,
    archive_server,
    attack: bool,
) -> None:
    with domain_database(database_url) as connection_info:
        _seed_battle_anchor(connection_info, DAY.start)
        original = _live_battle_row(
            attack=attack,
            battle_timestamp=DAY.start + timedelta(hours=1),
            opponent_tag="#8PP",
            opponent_name="Opponent",
        )
        corrected = {**original, "stars": 2, "destructionPercentage": 80}
        jobs = []
        for hour, items in ((3, [original]), (4, [corrected]), (5, [original]), (7, [])):
            _observation, job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"correction-{hour}",
                endpoint="battle_log",
                body=json.dumps({"items": items}).encode(),
                observed_at=DAY.start + timedelta(hours=hour),
                normalized_tag="#2PP",
                parser_version=LIVE_BATTLE_PARSER_VERSION,
            )
            jobs.append(job_id)
        database, processor = _processor(connection_info, archive_server)
        try:
            outcomes = [
                processor.process_job(jobs[index], owner="correction").outcome
                for index in (0, 1, 3, 2)
            ]
            with database.pool.connection() as connection:
                selected = connection.execute(
                    """
                    SELECT evidence.stars, selected.source_observed_at,
                           evidence.source_observed_at
                    FROM battle_perspectives AS selected
                    JOIN battle_evidence AS evidence ON evidence.id = selected.evidence_id
                    """
                ).fetchone()
        finally:
            database.close()
    assert outcomes == ["processed"] * 4
    assert selected == (
        3, DAY.start + timedelta(hours=5), DAY.start + timedelta(hours=5)
    )


@pytest.mark.parametrize("attack", [True, False])
@pytest.mark.parametrize("latest_stars", [2, 3])
def test_current_battle_report_confirmed_after_a_queued_log_covers_it(
    database_url: str,
    archive_server,
    attack: bool,
    latest_stars: int,
) -> None:
    with domain_database(database_url) as connection_info:
        _seed_battle_anchor(connection_info, DAY.start)
        original = _live_battle_row(
            attack=attack,
            battle_timestamp=DAY.start + timedelta(hours=1),
            opponent_tag="#8PP",
            opponent_name="Opponent",
        )
        latest = {**original, "stars": latest_stars}
        if latest_stars == 2:
            latest["destructionPercentage"] = 80
        jobs = []
        for hour, item in ((3, original), (5, original), (7, latest)):
            _observation, job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"confirmed-{hour}",
                endpoint="battle_log",
                body=json.dumps({"items": [item]}).encode(),
                observed_at=DAY.start + timedelta(hours=hour),
                normalized_tag="#2PP",
                parser_version=LIVE_BATTLE_PARSER_VERSION,
            )
            jobs.append(job_id)
        database, processor = _processor(connection_info, archive_server)
        try:
            outcomes = [
                processor.process_job(jobs[index], owner="confirmed").outcome
                for index in (0, 2, 1)
            ]
            with database.pool.connection() as connection:
                selected = connection.execute(
                    """
                    SELECT evidence.stars, selected.source_observed_at
                    FROM battle_perspectives AS selected
                    JOIN battle_evidence AS evidence ON evidence.id = selected.evidence_id
                    """
                ).fetchone()
        finally:
            database.close()
    assert outcomes == ["processed", "processed", "superseded"]
    assert selected == (latest_stars, DAY.start + timedelta(hours=7))


def test_limited_newest_plan_keeps_each_players_latest_responses_together(
    database_url: str,
    archive_server,
) -> None:
    tags = ("#9PP", "#2PP", "#8PP", "#QQQ", "#RRR")
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            for tag, hour in zip(tags[:3], (2, 1, 1), strict=True):
                _observation, job_id = _profile(
                    connection_info,
                    archive_server,
                    tag=tag,
                    trophies=5000,
                    observed_at=DAY.start + timedelta(hours=hour),
                )
                assert processor.process_job(job_id, owner="seed").outcome == "processed"
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    """
                    UPDATE players SET current_profile_confirmed_at = %s
                    WHERE normalized_tag = '#2PP'
                    """,
                    (DAY.start + timedelta(hours=3),),
                )
            newest = {}
            for tag in tags:
                for minutes in (0, 20):
                    _profile_observation, profile_job = _profile(
                        connection_info,
                        archive_server,
                        tag=tag,
                        trophies=5100 + minutes,
                        observed_at=DAY.start + timedelta(hours=4, minutes=minutes),
                    )
                    _battle_observation, battle_job = store_observation(
                        connection_info,
                        archive_server,
                        occurrence_key=f"planned-battle-{tag}-{minutes}",
                        endpoint="battle_log",
                        body=json.dumps({"items": []}).encode(),
                        observed_at=DAY.start
                        + timedelta(hours=4, minutes=minutes + 10),
                        normalized_tag=tag,
                        parser_version=LIVE_BATTLE_PARSER_VERSION,
                    )
                    newest[tag] = (profile_job, battle_job)
            expected = [
                job_id
                for tag in ("#QQQ", "#RRR", "#8PP", "#9PP", "#2PP")
                for job_id in newest[tag]
            ]
            assert database.newest_job_plan(limit=4) == expected[:4]
            assert database.newest_job_plan(limit=10) == expected
        finally:
            database.close()


@pytest.mark.parametrize("endpoint", ["profile", "battle_log"])
@pytest.mark.parametrize(
    ("field", "unusable_value"),
    [
        ("claim_compatibility_version", 99),
        ("parser_version", "future-parser"),
        ("endpoint_version", "future-endpoint"),
        ("schema_version", "future-schema"),
        ("processing_version", "future-processing"),
        ("domain_rule_version", "future-domain"),
        ("attempt_count", 3),
    ],
)
def test_unclaimable_newest_response_does_not_hide_the_players_usable_response(
    database_url: str,
    archive_server,
    endpoint: str,
    field: str,
    unusable_value: str | int,
) -> None:
    with domain_database(database_url) as connection_info:
        jobs = []
        for hour in (1, 2):
            _observation, job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"supported-plan-{hour}",
                endpoint=endpoint,
                body=(
                    PROFILE_FIXTURE.read_bytes()
                    if endpoint == "profile"
                    else b'{"items":[]}'
                ),
                observed_at=DAY.start + timedelta(hours=hour),
                normalized_tag="#2PP",
            )
            jobs.append(job_id)
        usable_job, unusable_job = jobs
        with psycopg.connect(connection_info) as connection:
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(
                sql.SQL("UPDATE python_processing_jobs SET {} = %s WHERE id = %s").format(
                    sql.Identifier(field)
                ),
                (unusable_value, unusable_job),
            )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert database.newest_job_plan(limit=1) == [usable_job]
            result = processor.process_once(owner="supported-plan")
            assert result is not None
            assert (result.job_id, result.outcome) == (usable_job, "processed")
            with database.pool.connection() as connection:
                status, attempts = connection.execute(
                    "SELECT status, attempt_count FROM python_processing_jobs WHERE id = %s",
                    (unusable_job,),
                ).fetchone()
            assert text(status) == "pending"
            assert attempts == (3 if field == "attempt_count" else 0)
        finally:
            database.close()


def test_collector_owned_observations_cannot_be_finished_as_superseded(
    database_url: str,
    archive_server,
) -> None:
    with domain_database(database_url) as connection_info:
        observation_ids, job_ids = [], []
        for endpoint in ("profile", "battle_log", "league_history"):
            observation_id, job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"owned-{endpoint}",
                endpoint=endpoint,
                body=(
                    PROFILE_FIXTURE.read_bytes()
                    if endpoint == "profile"
                    else b'{"items":[]}'
                ),
                observed_at=DAY.start,
                normalized_tag="#2PP",
            )
            observation_ids.append(observation_id)
            job_ids.append(job_id)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, due_at,
                    coalescing_key, status, profile_status, battle_log_status,
                    league_history_status, profile_observation_id,
                    battle_log_observation_id, league_history_observation_id, completed_at
                )
                SELECT 'initial_collection', 'interactive', 'player', id,
                       normalized_tag, %s, 'owned-observations', 'complete',
                       'observed', 'observed', 'observed', %s, %s, %s, %s
                FROM players WHERE normalized_tag = '#2PP'
                """,
                (DAY.start, *observation_ids, DAY.start),
            )
        database, _ = _processor(connection_info, archive_server)
        try:
            for job_id in job_ids:
                claim = database.claim_job(owner="owned", job_id=job_id)
                assert claim is not None
                assert not job_outcomes.complete_superseded(
                    database, claim, lambda _: True
                )
            with database.pool.connection() as connection:
                statuses = connection.execute(
                    "SELECT status FROM python_processing_jobs WHERE id = ANY(%s)",
                    (job_ids,),
                ).fetchall()
            assert [text(row[0]) for row in statuses] == ["leased"] * 3
        finally:
            database.close()


def test_collector_work_observation_lookups_are_available_and_repeatable(
    database_url: str,
) -> None:
    migration = (
        Path(__file__).parents[2]
        / "deploy/migrations/0044_collector_work_observation_lookup.sql"
    )
    fields = (
        "profile_observation_id",
        "battle_log_observation_id",
        "league_history_observation_id",
    )
    names = [f"collector_work_{field.removesuffix('_id')}_lookup" for field in fields]
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(migration.read_text())
            rows = connection.execute(
                """
                SELECT relation.relname, method.amname, attribute.attname,
                       definition.indisvalid, definition.indisready
                FROM pg_index AS definition
                JOIN pg_class AS relation ON relation.oid = definition.indexrelid
                JOIN pg_am AS method ON method.oid = relation.relam
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = definition.indrelid
                 AND attribute.attnum = definition.indkey[0]
                WHERE definition.indrelid = 'collector_work'::regclass
                  AND definition.indnkeyatts = 1
                  AND relation.relname = ANY(%s)
                """,
                (names,),
            ).fetchall()
            assert {
                text(row[0]): (text(row[1]), text(row[2]), row[3], row[4])
                for row in rows
            } == {
                name: ("btree", field, True, True)
                for name, field in zip(names, fields, strict=True)
            }
            assert connection.execute(
                "SELECT count(*) FROM clash_lens_schema_migrations WHERE version = 44"
            ).fetchone()[0] == 1
