"""Reset settlement verdicts through the real worker, database and permissions.

One player's named check (a profile, then a battle log) is saved with the
05:00 Reset pair and processed by the actual worker in different orders. A
synthetic settled previous Reset roots the target: it shows the rule's
behavior, not production coverage. Public day results never read the verdict.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from test_domain_processing_postgres import _role_connection
from test_reconciliation_postgres import BATTLE_FIXTURE, DAY_END, _processor, _profile

from clashlens import reset_settlement
from clashlens.boundary import lock_boundary_publication
from clashlens.collector_db import BATTLE_PARSER_VERSION, PROFILE_PARSER_VERSION
from clashlens.domain import allocate_trophies, ranked_day_for
from clashlens.season_finalization_guard import close_blockers

TAG = "#2PP"
RESET = DAY_END  # Wednesday August 5, 2026 05:00 UTC
DAY, MINUTE = timedelta(days=1), timedelta(minutes=1)
START = 5000  # the synthetic settled previous Reset
OPPONENTS = [f"#Q{a}{b}" for a in "28PYLGRJCUV" for b in "28PYLGRJCUV"]
SWITCH = reset_settlement.NEW_PROOFS_SWITCH
# The parsers production uses; older ones keep the uncorrected trophy rule.
PARSERS = {"profile": PROFILE_PARSER_VERSION, "battle_log": BATTLE_PARSER_VERSION}


def _row(attack: bool, at: datetime, stars: int, destruction: int, opponent: str) -> dict:
    template = json.loads(BATTLE_FIXTURE.read_bytes())["items"][0]
    return {**template, "attack": attack, "stars": stars,
            "destructionPercentage": destruction, "opponentPlayerTag": opponent,
            "battleTimestamp": at.strftime("%Y%m%dT%H%M%S.000Z")}


def _battles(boundary: datetime, *, new_day: bool = False) -> tuple[bytes, int, int]:
    """A battle log reaching before both days: eight prior defenses, then
    seven defenses and three attacks. Returns it, the ended day's net and
    its automatic loss."""
    opponents = iter(OPPONENTS)
    prior_from = boundary - 2 * DAY + 5 * MINUTE
    rows = [_row(False, prior_from - 60 * MINUTE, 1, 60, next(opponents))]
    results = [(False, prior_from + (i + 1) * 60 * MINUTE, 1, 40 + 5 * i) for i in range(8)]
    ended_from = prior_from + DAY
    results += [(False, ended_from + (i + 1) * 60 * MINUTE, 2, 60 + 5 * i) for i in range(7)]
    results += [(True, ended_from + (i + 9) * 60 * MINUTE, 3, 100) for i in range(3)]
    if new_day:
        results.append((True, boundary + 35 * MINUTE, 3, 100))
    rows += [_row(*result, next(opponents)) for result in results]
    loss = [allocate_trophies(s, d).defender_loss for attack, at, s, d in results if not attack]
    gain = sum(allocate_trophies(s, d).attacker_gain
               for attack, at, s, d in results if attack and at < boundary)
    automatic = sum(loss) // 15 * (8 - 7)
    body = json.dumps({"items": sorted(rows, key=lambda r: r["battleTimestamp"], reverse=True)})
    return body.encode(), gain - sum(loss[8:]), automatic


def _save(connection_info, archive_server, endpoint: str, body: bytes, at: datetime) -> tuple[int, int]:
    return store_observation(connection_info, archive_server,
                             occurrence_key=f"{endpoint}-{at.isoformat()}",
                             endpoint=endpoint, body=body, observed_at=at,
                             normalized_tag=TAG, parser_version=PARSERS[endpoint])


def _scenario(connection_info, archive_server, *, profile_offset: int = 0,
              work_status: str = "complete", with_root: bool = True,
              check_rows: tuple[dict, ...] = ()) -> dict[str, int]:
    """Save a Reset pair, the named check, its log holding ``check_rows`` too,
    and a later post-battle profile and log; return each response's
    processing job."""
    log, net, automatic = _battles(RESET)
    check_log = (
        json.dumps({"items": [*json.loads(log)["items"], *check_rows]}).encode()
        if check_rows else log
    )
    target = START + net - automatic
    jobs: dict[str, int] = {}
    ids: dict[str, int] = {}
    for name, endpoint, body, at in (
        ("early_profile", "profile", _profile(target + automatic), RESET + timedelta(seconds=31)),
        ("early_log", "battle_log", log, RESET + timedelta(seconds=33)),
        ("profile", "profile", _profile(target + profile_offset), RESET + timedelta(minutes=23, seconds=56)),
        ("log", "battle_log", check_log, RESET + timedelta(minutes=23, seconds=58)),
        ("newer_log", "battle_log", _battles(RESET, new_day=True)[0], RESET + 36 * MINUTE),
        ("newer_profile", "profile", _profile(target + 40), RESET + 40 * MINUTE),
    ):
        if name.startswith(("profile", "log")) and work_status == "failed":
            continue
        ids[name], jobs[name] = _save(connection_info, archive_server, endpoint, body, at)
    with psycopg.connect(connection_info) as connection:
        player = connection.execute("SELECT id FROM players WHERE normalized_tag = %s", (TAG,)).fetchone()[0]
        sweep = connection.execute(
            "INSERT INTO collector_reset_sweeps (boundary_at, member_ids, membership_captured_at)"
            " VALUES (%s, %s, %s) RETURNING id", (RESET, [player], RESET),
        ).fetchone()[0]
        work = {}
        for kind, lane, profile, log_id, due, status in (
            ("reset_baseline", "reset", ids["early_profile"], ids["early_log"], RESET, "complete"),
            ("reset_settlement", "ordinary", ids.get("profile"), ids.get("log"),
             RESET + 20 * MINUTE, work_status),
        ):
            work[kind] = connection.execute(
                """
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
                    coalescing_key, status, profile_status, battle_log_status,
                    profile_observation_id, battle_log_observation_id
                ) VALUES (%s, %s, 'player', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (kind, lane, player, TAG, sweep, due, f"{kind}:{sweep}", status,
                 "observed" if profile else "failed", "observed" if log_id else "failed",
                 profile, log_id),
            ).fetchone()[0]
        connection.execute(
            "INSERT INTO reset_boundary_settlements (player_id, boundary_at, sweep_id,"
            " delayed_work_id) VALUES (%s, %s, %s, %s)",
            (player, RESET, sweep, work["reset_settlement"]),
        )
        if with_root:
            _settle_root(connection, player)
    return {**jobs, "player": player, "target": target, "automatic": automatic,
            "work": work["reset_settlement"]}


def _settle_root(connection, player: int, *, delayed_work_id: int | None = None) -> None:
    connection.execute(
        """
        INSERT INTO reset_boundary_settlements (
            player_id, boundary_at, delayed_work_id, state, selected_trophies,
            proof_kind, proof_rule_version, proof_fingerprint, proof_json
        ) VALUES (%s, %s, %s, 'settled', %s, 'observed_adjustment', 'test',
                  'root', '{"observations": [901, 902, 903]}')
        """,
        (player, RESET - DAY, delayed_work_id, START),
    )


def _process(connection_info, archive_server, jobs: list[int]) -> None:
    """Process ``jobs``, then the day calculations they queued, as the
    worker does: a check pools the saved day before's defenses."""
    database, processor = _processor(connection_info, archive_server)
    try:
        for job in jobs:
            assert processor.process_job(job, owner=f"job-{job}") is not None
        while True:
            with database.pool.connection() as connection:
                pending = connection.execute(
                    "SELECT id FROM python_processing_jobs WHERE status = 'pending'"
                    " AND work_type = 'reconcile_ranked_day' ORDER BY id LIMIT 1"
                ).fetchone()
            if pending is None:
                return
            processor.process_job(int(pending[0]), owner="reconcile")
    finally:
        database.close()


VERDICT = """
    SELECT state, selected_trophies, reasons, proof_fingerprint, change_number,
           proof_json -> 'readings' -> 'settlement_profile' ->> 'observation_id'
    FROM reset_boundary_settlements WHERE boundary_at = %s
"""


def _verdict(connection_info, boundary: datetime = RESET) -> tuple:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(VERDICT, (boundary,)).fetchone()


ORDERS = {
    "named_check_last": ["early_profile", "early_log", "newer_profile", "newer_log", "profile", "log"],
    "log_before_profile": ["log", "profile", "early_log", "early_profile", "newer_log", "newer_profile"],
    "early_pair_last": ["newer_log", "profile", "log", "newer_profile", "early_log", "early_profile"],
}


def test_dependency_arriving_last_and_newest_first_give_same_verdict(
    database_url: str, archive_server, monkeypatch
) -> None:
    monkeypatch.setenv(SWITCH, "true")
    verdicts = {}
    for name, order in ORDERS.items():
        with domain_database(database_url, include_coordinator=True) as connection_info:
            scenario = _scenario(connection_info, archive_server)
            _process(connection_info, archive_server, [scenario[job] for job in order])
            state, trophies, reasons, fingerprint, _, profile = _verdict(connection_info)
            assert (state, trophies, reasons) == ("settled", scenario["target"], []), name
            verdicts[name] = (fingerprint, profile)
            with psycopg.connect(connection_info) as connection:
                live = connection.execute(
                    "SELECT version.trophies FROM players JOIN player_profile_versions AS"
                    " version ON version.id = players.current_profile_version_id"
                    " WHERE players.normalized_tag = %s", (TAG,),
                ).fetchone()[0]
            # Processing the older named profile never moved the live profile back.
            assert live == scenario["target"] + 40
            assert scenario["automatic"] > 0
    assert len(set(verdicts.values())) == 1


def test_a_battle_log_rechecks_an_unusable_battle_report(
    database_url: str, archive_server, monkeypatch
) -> None:
    monkeypatch.setenv(SWITCH, "true")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        _process(connection_info, archive_server,
                 [scenario[job] for job in ORDERS["named_check_last"]])
        database, _ = _processor(connection_info, archive_server)
        try:
            with psycopg.connect(connection_info) as connection:
                # The check was judged while a battle's reports still disagreed.
                connection.execute(
                    "UPDATE reset_boundary_settlements SET state = 'unresolved',"
                    " selected_trophies = NULL, proof_kind = NULL,"
                    " reasons = '[\"battle_report_unusable\"]' WHERE boundary_at = %s",
                    (RESET,),
                )
                saved = dict(connection.execute(
                    "SELECT endpoint, id FROM collector_observations"
                    " WHERE response_completed_at IN (%s, %s)",
                    (RESET + timedelta(seconds=33), RESET + 40 * MINUTE),
                ).fetchall())
                # A profile cannot change a battle report.
                reset_settlement.refresh_for_observation(database, connection, saved["profile"])
                assert connection.execute(VERDICT, (RESET,)).fetchone()[2] == [
                    "battle_report_unusable"]
                # A battle log saved four days later that reports the ended
                # days' battles can, however long after the Reset it arrives.
                connection.execute(
                    "UPDATE collector_observations SET response_completed_at = %s WHERE id = %s",
                    (RESET + 4 * DAY, saved["battle_log"]),
                )
                reset_settlement.refresh_for_observation(
                    database, connection, saved["battle_log"])
                assert connection.execute(VERDICT, (RESET,)).fetchone()[:3] == (
                    "settled", scenario["target"], [])
        finally:
            database.close()


def test_a_report_rechecks_the_reset_just_before_it_only_within_the_grace(
    database_url: str, archive_server, monkeypatch
) -> None:
    monkeypatch.setenv(SWITCH, "true")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        _process(connection_info, archive_server,
                 [scenario[job] for job in ORDERS["named_check_last"]])
        database, _ = _processor(connection_info, archive_server)
        try:
            with psycopg.connect(connection_info) as connection:
                log_id = connection.execute(
                    "SELECT id FROM collector_observations WHERE response_completed_at = %s",
                    (RESET + timedelta(seconds=33),),
                ).fetchone()[0]
                # A 05:01 report counts on the day the 05:00 Reset ended; one
                # past the 5-minute grace, or from 04:54 the next morning,
                # belongs to a later day.
                for reported_at, rechecked in ((RESET + MINUTE, True),
                                               (RESET + 6 * MINUTE, False),
                                               (RESET + DAY - 6 * MINUTE, False)):
                    connection.execute(
                        "UPDATE reset_boundary_settlements SET state = 'unresolved',"
                        " selected_trophies = NULL, proof_kind = NULL,"
                        " reasons = '[\"battle_report_unusable\"]' WHERE boundary_at = %s",
                        (RESET,),
                    )
                    connection.execute(
                        "UPDATE battle_evidence SET battle_timestamp = %s WHERE battle_id IN"
                        " (SELECT battle_id FROM battle_evidence WHERE observation_id = %s)",
                        (reported_at, log_id),
                    )
                    reset_settlement.refresh_for_observation(database, connection, log_id)
                    reasons = connection.execute(VERDICT, (RESET,)).fetchone()[2]
                    assert (reasons != ["battle_report_unusable"]) is rechecked, reported_at
                    connection.rollback()
        finally:
            database.close()


def test_switch_off_keeps_the_candidate_and_never_suppresses_invalidation(
    database_url: str, archive_server, monkeypatch
) -> None:
    monkeypatch.delenv(SWITCH, raising=False)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        _process(connection_info, archive_server,
                 [scenario[job] for job in ORDERS["named_check_last"]])
        state, trophies, reasons, *_ = _verdict(connection_info)
        assert (state, trophies, reasons) == ("provisional", None, ["new_reset_proofs_disabled"])
        with psycopg.connect(connection_info) as connection:
            candidate = connection.execute(
                "SELECT proof_json ->> 'verdict', proof_json -> 'catchup' ->> 'target'"
                " FROM reset_boundary_settlements WHERE boundary_at = %s", (RESET,),
            ).fetchone()
        assert candidate == ("settled", str(scenario["target"]))

        database, _ = _processor(connection_info, archive_server)
        player = scenario["player"]

        def refresh(boundary: datetime = RESET) -> None:
            # The worker's own role records verdicts.
            with _role_connection(connection_info, "clashlens_python_worker") as worker:
                reset_settlement.refresh_boundary(database, worker, player, boundary)

        try:
            monkeypatch.setenv(SWITCH, "true")
            refresh()
            settled = _verdict(connection_info)
            assert settled[:3] == ("settled", scenario["target"], [])
            refresh()  # The same inputs again change nothing.
            assert _verdict(connection_info) == settled
            # Switched off, an accepted proof stays accepted...
            monkeypatch.setenv(SWITCH, "false")
            refresh()
            assert _verdict(connection_info) == settled
            # ...but a late old-day battle still takes it away.
            late = json.loads(_battles(RESET)[0])
            late["items"].append(_row(False, RESET - 30 * MINUTE, 1, 50, OPPONENTS[-1]))
            _, job = _save(connection_info, archive_server, "battle_log",
                           json.dumps(late).encode(), RESET + 50 * MINUTE)
            _process(connection_info, archive_server, [job])
            state, trophies, reasons, *_ = _verdict(connection_info)
            assert (state, trophies) == ("unresolved", None)
            assert "battle_reports_changed_after_log" in reasons
        finally:
            database.close()


def test_profile_off_target_and_missing_root_stay_unresolved(
    database_url: str, archive_server, monkeypatch
) -> None:
    monkeypatch.setenv(SWITCH, "true")
    for offset, with_root, expected in (
        # The named profile still reads the early total: no catch-up proof.
        (None, True, {"observed_drop_mismatch", "profile_catchup_unknown"}),
        # No settled previous Reset; a Complete saved day is not a root.
        (0, False, {"independent_root_missing"}),
    ):
        with domain_database(database_url, include_coordinator=True) as connection_info:
            _, _, automatic = _battles(RESET)
            scenario = _scenario(connection_info, archive_server, with_root=with_root,
                                 profile_offset=automatic if offset is None else offset)
            _process(connection_info, archive_server,
                     [scenario[job] for job in ORDERS["named_check_last"]])
            state, trophies, reasons, *_ = _verdict(connection_info)
            assert (state, trophies) == ("unresolved", None)
            assert expected <= set(reasons)


def test_terminal_work_refresh_and_fence_use_dependency_days(
    database_url: str, archive_server, monkeypatch
) -> None:
    monkeypatch.setenv(SWITCH, "true")
    now = datetime.now(UTC)
    recent = now.replace(hour=5, minute=0, second=0, microsecond=0)
    recent -= DAY if recent > now else timedelta(0)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, _ = _processor(connection_info, archive_server)
        try:
            with psycopg.connect(connection_info) as connection:
                player = connection.execute(
                    "INSERT INTO players (normalized_tag) VALUES (%s) RETURNING id", (TAG,)
                ).fetchone()[0]
                checks = {}
                for boundary, status in ((recent, "failed"), (recent - DAY, "pending"),
                                         (recent - 3 * DAY, "failed"), (RESET, "failed")):
                    sweep = connection.execute(
                        "INSERT INTO collector_reset_sweeps (boundary_at, member_ids,"
                        " membership_captured_at) VALUES (%s, %s, %s) RETURNING id",
                        (boundary, [player], boundary),
                    ).fetchone()[0]
                    checks[boundary] = connection.execute(
                        """
                        INSERT INTO collector_work (
                            kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
                            coalescing_key, status, profile_status, battle_log_status
                        ) VALUES ('reset_settlement', 'ordinary', 'player', %s, %s, %s,
                                  %s, %s, %s, 'pending', 'pending')
                        RETURNING id
                        """,
                        (player, TAG, sweep, boundary + 20 * MINUTE, f"s:{sweep}", status),
                    ).fetchone()[0]
                    connection.execute(
                        "INSERT INTO reset_boundary_settlements (player_id, boundary_at,"
                        " sweep_id, delayed_work_id) VALUES (%s, %s, %s, %s)",
                        (player, boundary, sweep, checks[boundary]),
                    )
                ended = ranked_day_for(RESET - DAY)
                connection.execute(
                    "INSERT INTO season_detail_retirements (official_season_id,"
                    " season_start, season_end) VALUES (%s, %s, %s)",
                    (ended.official_season_id, ended.season_start, ended.season_end),
                )
            # A check that failed before saving anything has no response to
            # process: only the maintenance pass judges it, however old,
            # unless its Season is finalized.
            assert reset_settlement.refresh_terminal_work(database) == 2
            for boundary in (recent, recent - 3 * DAY):
                assert _verdict(connection_info, boundary)[:3] == (
                    "unresolved", None, ["settlement_profile_missing"])
            assert _verdict(connection_info, RESET)[:3] == ("provisional", None, [])
            assert reset_settlement.refresh_terminal_work(database) == 0

            # An unfinished check, or a judged one whose saved battle log is
            # still unprocessed, holds its two days' detail from retirement.
            log_id, _ = _save(connection_info, archive_server, "battle_log",
                              b'{"items": []}', recent + 25 * MINUTE)
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE collector_work SET battle_log_status = 'observed',"
                    " battle_log_observation_id = %s WHERE id = %s",
                    (log_id, checks[recent]),
                )
                blocking = close_blockers(
                    connection, "season", recent - 2 * DAY, recent - DAY + timedelta(seconds=1))
                assert len(blocking.get("reset_settlement_checks", [])) == 2
                assert "reset_settlement_checks" not in close_blockers(
                    connection, "season", recent + DAY, recent + 2 * DAY)
                connection.execute(
                    "UPDATE reset_boundary_settlements SET state = 'unresolved'"
                    " WHERE delayed_work_id = %s", (checks[recent - DAY],))
                for status, held in (("pending", 1), ("waiting_retry", 1), ("complete", 0),
                                     ("failed", 0), ("cancelled", 0)):
                    connection.execute(
                        "UPDATE collector_work SET status = %s WHERE id = %s",
                        (status, checks[recent - DAY]))
                    assert len(close_blockers(
                        connection, "season", recent - 2 * DAY, recent - 2 * DAY
                    ).get("reset_settlement_checks", [])) == held
                # A finished check with nothing saved, or any other still
                # provisional one, holds its days; a judged candidate does not.
                for reasons, held in (([], 1), (["early_reading_pending"], 1),
                                      (["new_reset_proofs_disabled"], 0)):
                    connection.execute(
                        "UPDATE reset_boundary_settlements SET reasons = %s::jsonb"
                        " WHERE boundary_at = %s", (json.dumps(reasons), RESET))
                    assert len(close_blockers(
                        connection, "season", RESET - 2 * DAY, RESET - DAY + timedelta(seconds=1)
                    ).get("reset_settlement_checks", [])) == held
                connection.rollback()

            # Finalizing the ended day's Season freezes the verdict.
            with psycopg.connect(connection_info) as connection:
                reset_settlement.refresh_boundary(database, connection, player, RESET)
            assert _verdict(connection_info, RESET)[:3] == ("provisional", None, [])
        finally:
            database.close()


def test_invalidated_root_rejudges_the_next_reset(
    database_url: str, archive_server, monkeypatch
) -> None:
    monkeypatch.setenv(SWITCH, "true")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server, with_root=False)
        _process(connection_info, archive_server,
                 [scenario[job] for job in ORDERS["named_check_last"]])
        assert _verdict(connection_info)[2] == ["independent_root_missing"]
        database, _ = _processor(connection_info, archive_server)
        try:
            with psycopg.connect(connection_info) as connection:
                # The previous Reset settled with its own check still pending,
                # as a later re-judgement would find.
                sweep = connection.execute(
                    "INSERT INTO collector_reset_sweeps (boundary_at, member_ids,"
                    " membership_captured_at) VALUES (%s, %s, %s) RETURNING id",
                    (RESET - DAY, [scenario["player"]], RESET - DAY),
                ).fetchone()[0]
                work = connection.execute(
                    "INSERT INTO collector_work (kind, lane, scope, player_id, normalized_tag,"
                    " sweep_id, due_at, coalescing_key, status, profile_status,"
                    " battle_log_status) VALUES ('reset_settlement', 'ordinary', 'player',"
                    " %s, %s, %s, %s, 'root', 'pending', 'pending', 'pending') RETURNING id",
                    (scenario["player"], TAG, sweep, RESET - DAY + 20 * MINUTE),
                ).fetchone()[0]
                _settle_root(connection, scenario["player"], delayed_work_id=work)
                reset_settlement.refresh_boundary(database, connection, scenario["player"], RESET)
                assert connection.execute(VERDICT, (RESET,)).fetchone()[:3] == (
                    "settled", scenario["target"], [])
                # Re-judging the root takes its proof away, and with it the next one's.
                reset_settlement.refresh_boundary(
                    database, connection, scenario["player"], RESET - DAY)
                assert connection.execute(VERDICT, (RESET - DAY,)).fetchone()[:3] == (
                    "provisional", None, ["settlement_check_pending"])
                assert connection.execute(VERDICT, (RESET,)).fetchone()[:3] == (
                    "unresolved", None, ["independent_root_missing"])
        finally:
            database.close()



def test_reset_profile_waits_for_the_publication_lock_before_its_reset_lock(
    database_url: str, archive_server, monkeypatch
) -> None:
    monkeypatch.setenv(SWITCH, "true")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        database, processor = _processor(connection_info, archive_server)

        def outcome(job: str) -> str:
            result = processor.process_job(scenario[job], owner=job)
            return result.outcome if result is not None else "unclaimed"

        try:
            assert outcome("early_log") == "processed"
            with psycopg.connect(connection_info) as late_log, ThreadPoolExecutor(1) as pool:
                # A late battle log's army refresh holds the Reset's publication lock.
                lock_boundary_publication(late_log, RESET)
                profile = pool.submit(outcome, "early_profile")
                deadline = time.monotonic() + 30
                while not late_log.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                ).fetchone()[0]:
                    assert time.monotonic() < deadline and not profile.done()
                    time.sleep(0.05)
                # The waiting Reset profile holds no Reset lock, so the late
                # log can still re-judge the Reset instead of deadlocking.
                assert late_log.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"reset-settlement:{scenario['player']}:{RESET.isoformat()}",),
                ).fetchone()[0]
                late_log.execute("SET LOCAL lock_timeout = '5s'")
                reset_settlement.refresh_boundary(database, late_log, scenario["player"], RESET)
                late_log.commit()
                assert profile.result(timeout=60) == "processed"
        finally:
            database.close()


def _wait_for_advisory_wait(connection, future) -> int:
    """Wait until another session waits for an advisory lock; return its pid."""
    deadline = time.monotonic() + 30
    while (row := connection.execute(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    ).fetchone()) is None:
        assert time.monotonic() < deadline and not future.done()
        time.sleep(0.05)
    return row[0]


def test_reset_pair_takes_its_reset_lock_before_any_generation_row(
    database_url: str, archive_server, monkeypatch
) -> None:
    monkeypatch.setenv(SWITCH, "true")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        database, processor = _processor(connection_info, archive_server)

        def outcome(job: str) -> str:
            result = processor.process_job(scenario[job], owner=job)
            return result.outcome if result is not None else "unclaimed"

        try:
            assert outcome("early_log") == "processed"
            with psycopg.connect(connection_info) as holder, ThreadPoolExecutor(1) as pool:
                holder.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                               (f"reset-settlement:{scenario['player']}:{RESET.isoformat()}",))
                profile = pool.submit(outcome, "early_profile")
                waiting = _wait_for_advisory_wait(holder, profile)
                # The pair holds the publication lock but has locked or
                # written no generation row while it waits for the Reset.
                assert not holder.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"boundary-publication:{RESET.isoformat()}",),
                ).fetchone()[0]
                assert holder.execute(
                    """
                    SELECT count(*) FROM pg_locks
                    WHERE pid = %s AND relation = 'boundary_publication_generations'::regclass
                      AND mode IN ('RowShareLock', 'RowExclusiveLock')
                    """,
                    (waiting,),
                ).fetchone()[0] == 0
                holder.commit()
                assert profile.result(timeout=60) == "processed"
            with psycopg.connect(connection_info) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM boundary_publication_generations WHERE boundary_at = %s",
                    (RESET,),
                ).fetchone()[0] == 1
        finally:
            database.close()


def test_a_response_rechecks_a_reset_judged_while_it_waited(
    database_url: str, archive_server, monkeypatch
) -> None:
    monkeypatch.setenv(SWITCH, "true")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        _process(connection_info, archive_server,
                 [scenario[job] for job in ORDERS["named_check_last"]])
        database, _ = _processor(connection_info, archive_server)
        try:
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE reset_boundary_settlements SET state = 'provisional',"
                    " selected_trophies = NULL, proof_kind = NULL,"
                    " reasons = '[\"settlement_processing_pending\"]' WHERE boundary_at = %s",
                    (RESET,),
                )
                later_profile = connection.execute(
                    "SELECT id FROM collector_observations WHERE response_completed_at = %s",
                    (RESET + 40 * MINUTE,),
                ).fetchone()[0]

            def refresh() -> None:
                with psycopg.connect(connection_info) as connection:
                    reset_settlement.refresh_for_observation(database, connection, later_profile)

            with psycopg.connect(connection_info) as named, ThreadPoolExecutor(1) as pool:
                # A named-check job judges the pending Reset from evidence
                # read before the later profile's job saved its own.
                named.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                              (f"reset-settlement:{scenario['player']}:{RESET.isoformat()}",))
                named.execute(
                    "UPDATE reset_boundary_settlements SET state = 'settled',"
                    " selected_trophies = 1, proof_kind = 'observed_adjustment',"
                    " reasons = '[]' WHERE boundary_at = %s",
                    (RESET,),
                )
                later = pool.submit(refresh)
                _wait_for_advisory_wait(named, later)
                named.commit()
                later.result(timeout=60)
            assert _verdict(connection_info)[:3] == ("settled", scenario["target"], [])
        finally:
            database.close()


@pytest.mark.parametrize("path", ["current", "before_content_dedup"])
def test_battle_log_takes_its_reset_locks_before_any_army_or_generation_row(
    database_url: str, archive_server, monkeypatch, path: str
) -> None:
    monkeypatch.setenv(SWITCH, "true")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        _process(connection_info, archive_server, [scenario["early_log"], scenario["early_profile"]])
        # A later log reports one more battle of the ended day, so its army is new.
        body = json.loads(_battles(RESET)[0])
        body["items"].insert(0, _row(True, RESET - 30 * MINUTE, 3, 100, OPPONENTS[-1]))
        _, job = _save(connection_info, archive_server, "battle_log",
                       json.dumps(body).encode(), RESET + 50 * MINUTE)
        database, processor = _processor(connection_info, archive_server)
        # The older battle-log path, still used before parsed-content dedup.
        database._supports_content_dedup = path == "current"
        if path == "before_content_dedup":
            # That path relies on the one-report-per-row rule 0012 dropped.
            with psycopg.connect(connection_info) as connection:
                connection.execute("ALTER TABLE battle_evidence"
                                   " ADD UNIQUE (source_row_id)")
        try:
            with psycopg.connect(connection_info) as holder, ThreadPoolExecutor(1) as pool:
                # The completed Reset pair created the Reset's publication record.
                assert holder.execute(
                    "SELECT count(*) FROM boundary_publication_generations WHERE boundary_at = %s",
                    (RESET,),
                ).fetchone()[0] == 1
                holder.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                               (f"reset-settlement:{scenario['player']}:{RESET.isoformat()}",))
                log = pool.submit(processor.process_job, job, owner="late-log")
                waiting = _wait_for_advisory_wait(holder, log)
                assert not holder.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"boundary-publication:{RESET.isoformat()}",),
                ).fetchone()[0]
                assert holder.execute(
                    """
                    SELECT count(*) FROM pg_locks
                    WHERE pid = %s AND mode IN ('RowShareLock', 'RowExclusiveLock')
                      AND relation IN ('boundary_publication_generations'::regclass,
                                       'battle_army_decodes'::regclass)
                    """,
                    (waiting,),
                ).fetchone()[0] == 0
                holder.commit()
                assert log.result(timeout=60).outcome == "processed"
            with psycopg.connect(connection_info) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM battle_army_decodes AS decode"
                    " JOIN battle_evidence AS report ON report.battle_id = decode.battle_id"
                    " WHERE report.battle_timestamp = %s",
                    (RESET - 30 * MINUTE,),
                ).fetchone()[0] > 0
        finally:
            database.close()


@pytest.mark.parametrize(
    ("state", "reasons", "status", "rejudged"),
    [
        ("settled", [], "complete", True),
        ("provisional", [], "complete", True),
        ("provisional", ["settlement_check_pending"], "complete", False),
        ("provisional", ["later_profile_unprocessed"], "failed", True),
        ("unresolved", ["battle_report_unusable"], "failed", False),
    ],
)
def test_settlement_lookup_skips_its_reads_only_while_none_are_saved(
    database_url: str, archive_server, state, reasons, status, rejudged
) -> None:
    # With no settlements saved, the lookup cost about 137 ms per battle log
    # on 2026-10-03 while holding the day's Reset lock.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        _process(connection_info, archive_server,
                 [scenario[job] for job in ORDERS["named_check_last"]])
        player, ours = scenario["player"], [(scenario["player"], RESET)]
        with psycopg.connect(connection_info) as connection:
            named_profile, named_log = connection.execute(
                "SELECT profile_observation_id, battle_log_observation_id"
                " FROM collector_work WHERE id = %s",
                (scenario["work"],),
            ).fetchone()
            later_profile = connection.execute(
                "SELECT id FROM collector_observations WHERE response_completed_at = %s",
                (RESET + 40 * MINUTE,),
            ).fetchone()[0]
            settled = state == "settled"
            connection.execute(
                """
                UPDATE reset_boundary_settlements
                SET state = %s, reasons = %s, selected_trophies = %s, proof_kind = %s,
                    proof_rule_version = 'test', proof_fingerprint = 'test',
                    proof_json = '{"test": true}'
                WHERE player_id = %s AND boundary_at = %s
                """,
                (state, json.dumps(reasons), START if settled else None,
                 "observed_adjustment" if settled else None, player, RESET),
            )
            connection.execute(
                "UPDATE collector_work SET status = %s WHERE id = %s",
                (status, scenario["work"]),
            )
            # An unrelated player's settled check is never this player's.
            other = connection.execute(
                "INSERT INTO players (normalized_tag) VALUES ('#2YY') RETURNING id"
            ).fetchone()[0]
            other_work = connection.execute(
                """
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
                    coalescing_key, status, profile_status, battle_log_status
                ) SELECT 'reset_settlement', 'ordinary', 'player', %s, '#2YY',
                         sweep_id, due_at, 'other', 'complete', 'failed', 'failed'
                  FROM collector_work WHERE id = %s
                RETURNING id
                """,
                (other, scenario["work"]),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO reset_boundary_settlements (
                    player_id, boundary_at, delayed_work_id, state, selected_trophies,
                    proof_kind, proof_rule_version, proof_fingerprint, proof_json
                ) VALUES (%s, %s, %s, 'settled', %s, 'observed_adjustment', 'test',
                          'test', '{"test": true}')
                """,
                (other, RESET, other_work, START),
            )
            connection.commit()

            def lookup(observation: int, *, every: bool = False) -> list:
                found = reset_settlement._observation_resets(
                    connection, observation, every=every)
                connection.commit()
                return found

            assert lookup(named_profile) == ours
            assert lookup(named_log) == ours
            assert lookup(named_log, every=True) == ours
            assert lookup(later_profile) == (ours if rejudged else [])
            assert lookup(later_profile, every=True) == ours

            saved = connection.execute(
                "SELECT player_id, boundary_at, delayed_work_id"
                " FROM reset_boundary_settlements WHERE boundary_at = %s",
                (RESET,),
            ).fetchall()
            connection.execute("DELETE FROM reset_boundary_settlements")
            connection.commit()
            for observation in (named_profile, named_log, later_profile):
                assert lookup(observation) == []
                assert lookup(observation, every=True) == []
            # The first settlements of a Reset count at once.
            with psycopg.connect(connection_info) as collector:
                collector.cursor().executemany(
                    "INSERT INTO reset_boundary_settlements (player_id, boundary_at,"
                    " delayed_work_id) VALUES (%s, %s, %s)",
                    saved,
                )
            assert lookup(named_log, every=True) == ours
            assert lookup(named_profile) == ours


def test_a_later_reading_against_a_complete_day_keeps_its_board_entry_uncertain(
    database_url: str, archive_server,
) -> None:
    """A Complete day can balance on two Reset readings that both miss the
    same delayed credit: a start reading of 5,200 that misses the day
    before's last attack, a day netting nothing, and an end reading of
    5,200 that misses its 04:50 attack. A profile read at 05:10, after the
    end reading and before any battle of the next day, showing 5,240
    disproves it, so the board shows the last reading plus later battles,
    uncertain. One showing the day's end, or none, leaves it proven."""
    from test_boundary_manifest_postgres import (
        _build_board,
        _october,
        _seed_board,
        _seed_days,
    )

    from clashlens.db import Database

    readings = [
        ("#2QCYU8C2G", 5240, _october(7, 4, 55)),  # later reading disagrees
        ("#GURYYP99", 5240, _october(7, 4, 55)),  # later reading agrees
        ("#8R8URPPLR", 5240, _october(7, 4, 55)),  # no later reading
    ]
    battles = [
        ("defense", 40, _october(7, 3), True),
        ("offense", 40, _october(7, 4, 50), True),
    ]
    complete = {
        "state": "Complete", "failure_reasons": [], "final": 5200, "start": 5200,
        "end": 5200, "end_read_at": _october(7, 5, 2),
    }
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(
            connection_info, generation_id, {player: (True, battles) for player in (1, 2, 3)},
            {player: complete for player in (1, 2, 3)},
        )
        _later_profiles(connection_info, archive_server, [
            ("#2QCYU8C2G", 5240, _october(7, 5, 10)),
            ("#GURYYP99", 5200, _october(7, 5, 10)),
        ])
        database = Database(connection_info)
        try:
            board = _build_board(connection_info, database, generation_id)
        finally:
            database.close()

    assert sorted(board) == [
        ("#2QCYU8C2G", 5240, "uncertain"),
        ("#8R8URPPLR", 5200, "confirmed"),
        ("#GURYYP99", 5200, "confirmed"),
    ]


def test_board_takes_a_complete_days_end_over_a_reading_it_cannot_place(
    database_url: str,
) -> None:
    """Two Day 2 rows of the October 2026 research report, each a Complete
    day whose next Reset reading equals its end. #QQ98LYP2 read 5,139 at
    01:09:53, 4 minutes after a defense at 01:06:01, so the reading may or
    may not hold it, and lost 40 more at 04:53:17: its day starts at 5,025,
    nets +74 and ends at 5,099. #PVL2L2YQ8 read 5,167 with no battle after
    it, but its day starts at 5,109, nets +32 and ends at 5,141 with no
    automatic loss. Each board entry is the day's end, proven."""
    from test_boundary_manifest_postgres import (
        _build_board,
        _october,
        _seed_board,
        _seed_days,
    )

    from clashlens.db import Database

    readings = [
        ("#QQ98LYP2", 5139, datetime(2026, 10, 7, 1, 9, 53, tzinfo=UTC)),
        ("#PVL2L2YQ8", 5167, _october(7, 4, 40)),
    ]
    days = {
        1: (True, [
            ("offense", 40, _october(6, 10), True),
            ("offense", 40, _october(6, 12), True),
            ("offense", 40, _october(6, 14), True),
            ("defense", 6, datetime(2026, 10, 7, 1, 6, 1, tzinfo=UTC), True),
            ("defense", 40, datetime(2026, 10, 7, 4, 53, 17, tzinfo=UTC), True),
        ]),
        2: (True, [
            ("offense", 40, _october(6, 8), True),
            ("defense", 8, _october(6, 20), True),
        ]),
    }
    complete = {"state": "Complete", "failure_reasons": [], "end_read_at": _october(7, 5, 2)}
    results = {
        1: {**complete, "start": 5025, "final": 5099, "end": 5099},
        2: {**complete, "start": 5109, "final": 5141, "end": 5141},
    }
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(connection_info, generation_id, days, results)
        database = Database(connection_info)
        try:
            board = _build_board(connection_info, database, generation_id)
        finally:
            database.close()

    assert board == [("#PVL2L2YQ8", 5141, "confirmed"), ("#QQ98LYP2", 5099, "confirmed")]


def _later_profiles(
    connection_info: str, archive_server, readings: list[tuple[str, int, datetime]]
) -> None:
    """Save and process each (tag, trophies, read at) as a Legend I profile
    naming the October 2026 Season, the Day 2 board's; each seeded day then
    stores what its proof reads, as its calculation after them would."""
    from psycopg.types.json import Jsonb
    from test_boundary_manifest_postgres import DAY_2_RESET

    from clashlens.boundary_manifest import reset_proof_facts
    from clashlens.db import Database

    season = int(ranked_day_for(DAY_2_RESET - DAY).official_season_id)
    jobs = []
    for index, (tag, trophies, read_at) in enumerate(readings):
        payload = json.loads(_profile(trophies, tag))
        payload["currentLeagueSeasonId"] = season
        payload["previousLeagueSeasonId"] = season - 28 * 86400
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key=f"later-{index}-{tag}",
            endpoint="profile", body=json.dumps(payload).encode(),
            observed_at=read_at, normalized_tag=tag,
            parser_version=PROFILE_PARSER_VERSION,
        )[1])
    _process(connection_info, archive_server, jobs)
    database = Database(connection_info)
    with database.pool.connection() as connection:
        connection.execute("SET LOCAL session_replication_role = replica")
        for version, facts in reset_proof_facts(database, connection, [row[0] for row in (
            connection.execute("SELECT DISTINCT ON (player_id) id FROM ranked_day_versions"
                               " WHERE ranked_day_end = %s ORDER BY player_id, version DESC",
                               (DAY_2_RESET,)).fetchall())]).items():
            connection.execute("UPDATE ranked_day_versions SET formula_components = jsonb_set("
                               "COALESCE(formula_components, '{}'), '{reset_proof}', %s)"
                               " WHERE id = %s", (Jsonb(facts), version))
    database.close()


def _set_defenses(connection_info: str, defenses: dict[int, int]) -> None:
    """Set each seeded day's used defense slots."""
    with psycopg.connect(connection_info) as connection:
        connection.execute("SET LOCAL session_replication_role = replica")
        for version_id, count in defenses.items():
            connection.execute(
                "UPDATE ranked_day_versions SET defense_count = %s WHERE id = %s",
                (count, version_id),
            )


def test_a_board_retry_proves_what_its_frozen_inputs_proved(
    database_url: str, archive_server,
) -> None:
    """A board freezes the later readings and Reset checks its proof reads.
    A recovered profile read at 05:10 showing 5,240, saved after the board's
    inputs were frozen for a Complete day ending at 5,200, does not change
    a build of those inputs; the Season repair's check, reading the
    evidence saved now, lists the board for a rebuild."""
    from test_boundary_manifest_postgres import (
        DAY_2_RESET,
        _build_board,
        _october,
        _seed_board,
        _seed_days,
    )

    from clashlens import boundary
    from clashlens.db import Database

    battles = [
        ("defense", 40, _october(7, 3), True),
        ("offense", 40, _october(7, 4, 50), True),
    ]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(
            connection_info, [("#2QCYU8C2G", 5240, _october(7, 4, 55))]
        )
        _seed_days(connection_info, generation_id, {1: (True, battles)}, {1: {
            "state": "Complete", "failure_reasons": [], "final": 5200, "start": 5200,
            "end": 5200, "end_read_at": _october(7, 5, 2),
        }})
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                boundary._freeze_boundary_manifest(
                    database, connection, generation_id=generation_id,
                    artifact_kind="snapshot",
                )
            _later_profiles(
                connection_info, archive_server, [("#2QCYU8C2G", 5240, _october(7, 5, 10))]
            )
            board = _build_board(connection_info, database, generation_id)
            season = ranked_day_for(DAY_2_RESET - DAY).official_season_id
            rebuilds = boundary.queue_board_rebuilds(database, season, queue=False)
        finally:
            database.close()

    assert board == [("#2QCYU8C2G", 5200, "confirmed")]
    assert [entry["late_battles"] for entry in rebuilds["boards"]] == [1]


def test_board_proves_a_days_end_by_its_end_and_later_readings(
    database_url: str, archive_server,
) -> None:
    """Day 3 rows of the 7 October 2026 board whose day does not prove its
    own end. #289Y8RYJL, Partial with no start and 8 defenses: its reading
    of 5,001 at 04:56 may or may not hold a defense at 04:54:37, but its end
    Reset reading of 4,988 at 05:05:06 and a reading of 4,988 at 05:25:32
    agree, so 4,988 is proven. #8L2RVPU9Y, Inconsistent from a wrong start:
    4,981 at 05:06:05 and at 06:21:31, after its last battle at 05:00:12,
    prove 4,981. #R988P2Y9 read 5,017 at 05:08:33 and 4,977 at 05:22:14
    with no battle between: they disagree, so its entry stays uncertain.
    #Y9J9QC90Q, Partial with an unknown automatic loss, read 4,673 at
    04:48:08 without an attack of 20 stamped 04:47:35, then attacked for
    129: whatever its readings after the Reset show, 4,802 stays uncertain."""
    from test_boundary_manifest_postgres import (
        _build_board,
        _october,
        _seed_board,
        _seed_days,
    )

    from clashlens.db import Database

    def at(hour: int, minute: int, second: int, day: int = 7) -> datetime:
        return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)

    readings = [
        ("#289Y8RYJL", 5001, at(4, 56, 0)),
        ("#8L2RVPU9Y", 4901, at(4, 40, 0)),
        ("#R988P2Y9", 5017, at(4, 30, 0)),
        ("#Y9J9QC90Q", 4673, at(4, 48, 8)),
    ]
    days = {
        1: (True, [("defense", 2, _october(6, 6 + hour), True) for hour in range(7)]
            + [("defense", 13, at(4, 54, 37), True)]),
        2: (True, [
            ("offense", 40, _october(6, 10), True),
            ("offense", 38, _october(6, 14), True),
        ] + [("defense", 4, _october(6, 15 + hour), True) for hour in range(7)]
            + [("defense", 4, at(5, 0, 12), True)]),
        3: (True, [("defense", 5, _october(6, 6 + hour), True) for hour in range(8)]),
        4: (True, [
            ("offense", 40, at(2, 0, 0), True),
            ("offense", 33, at(3, 0, 0), True),
            ("offense", 20, at(4, 47, 35), True),
            ("offense", 40, at(4, 50, 0), True),
            ("offense", 40, at(4, 52, 0), True),
            ("offense", 40, at(4, 54, 0), True),
            ("offense", 9, at(4, 57, 0), True),
        ]),
    }
    no_start = {"failure_reasons": ["missing_start_baseline"], "start": None}
    results = {
        1: {**no_start, "state": "Partial", "end": 4988, "end_read_at": at(5, 5, 6)},
        2: {
            "state": "Inconsistent", "failure_reasons": ["trophy_equation_mismatch"],
            "start": 4930, "final": 4976, "end": 4981, "end_read_at": at(5, 6, 5),
        },
        3: {**no_start, "state": "Partial", "end": 5017, "end_read_at": at(5, 8, 33)},
        4: {
            "state": "Partial", "failure_reasons": ["automatic_defense_basis_unavailable"],
            "start": 4600, "automatic_state": "unknown", "end": 4822,
            "end_read_at": at(5, 2, 0),
        },
    }
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(connection_info, generation_id, days, results)
        _set_defenses(connection_info, {1: 8, 2: 8, 3: 8})
        _later_profiles(connection_info, archive_server, [
            ("#289Y8RYJL", 4988, at(5, 25, 32)),
            ("#8L2RVPU9Y", 4981, at(6, 21, 31)),
            ("#R988P2Y9", 4977, at(5, 22, 14)),
            ("#Y9J9QC90Q", 4822, at(5, 20, 0)),
        ])
        database = Database(connection_info)
        try:
            board = _build_board(connection_info, database, generation_id)
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            state = connection.execute(
                "SELECT state FROM ranked_day_versions WHERE id = 2"
            ).fetchone()[0]

    assert board == [
        ("#R988P2Y9", 5017, "uncertain"),
        ("#289Y8RYJL", 4988, "confirmed"),
        ("#8L2RVPU9Y", 4981, "confirmed"),
        ("#Y9J9QC90Q", 4802, "uncertain"),
    ]
    assert state == "Inconsistent"


def test_two_readings_at_5000_after_a_weekly_reset_prove_nothing(
    database_url: str, archive_server,
) -> None:
    """At a Monday Reset the game raises a total at or below 5,000 to
    5,000. An Inconsistent day whose wrong start of 4,974 puts its end at
    5,020, though it really ended at 4,980, read 5,000 at 05:06 and 05:25:
    those readings can be the raise, so its entry stays uncertain."""
    from test_boundary_manifest_postgres import (
        _build_board,
        _october,
        _seed_board,
        _seed_days,
    )

    from clashlens.db import Database

    battles = [
        ("offense", 40, _october(6, 10), True),
        ("offense", 38, _october(6, 12), True),
    ] + [("defense", 4, _october(6, 13 + hour), True) for hour in range(8)]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(
            connection_info, [("#Q8RU2PJ0", 4980, _october(7, 4, 40))]
        )
        _seed_days(connection_info, generation_id, {1: (True, battles)}, {1: {
            "state": "Inconsistent", "failure_reasons": ["trophy_equation_mismatch"],
            "start": 4974, "final": 5020, "end": 5000,
            "end_read_at": _october(7, 5, 6), "boundary_kind": "weekly",
        }})
        _set_defenses(connection_info, {1: 8})
        _later_profiles(
            connection_info, archive_server, [("#Q8RU2PJ0", 5000, _october(7, 5, 25))]
        )
        database = Database(connection_info)
        try:
            board = _build_board(connection_info, database, generation_id)
        finally:
            database.close()

    assert board == [("#Q8RU2PJ0", 4980, "uncertain")]


def test_late_evidence_against_a_boards_frozen_end_proof_queues_its_correction(
    database_url: str, archive_server, monkeypatch,
) -> None:
    """A Partial day with no start is confirmed at 4,981 by its Reset reading
    at 05:06 and a reading at 05:25 on the published board. A recovered
    battle log then shows an attack of the next day at 05:08, so the 05:25
    reading was never the day's later reading: one correction of the board
    is queued, the same one when a profile at 05:07 then disagrees, and the
    worker publishes the rebuilt board with the entry no longer confirmed."""
    from test_boundary_manifest_postgres import (
        DAY_2_RESET,
        _october,
        _seed_board,
        _seed_days,
    )

    from clashlens import boundary, boundary_publication
    from clashlens.db import Database

    # A past Reset's builds and corrections otherwise wait out 04:30-07:00.
    monkeypatch.setattr(boundary, "past_reset_build_waits", lambda *_: False)
    monkeypatch.setattr(boundary, "past_reset_correction_waits", lambda *_: False)
    monkeypatch.setattr(
        boundary_publication, "past_reset_correction_waits", lambda *_: False
    )
    tag = "#2Q8PRV0LG"
    battles = [("defense", 5, _october(6, 6 + hour), True) for hour in range(8)]
    template = json.loads(BATTLE_FIXTURE.read_bytes())["items"][0]
    recovered_log = json.dumps({"items": [{
        **template, "attack": True, "stars": 3, "destructionPercentage": 100,
        "battleTimestamp": "20261007T050800.000Z", "opponentPlayerTag": "#Q28",
    }]}).encode()
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(connection_info, [(tag, 4990, _october(7, 4, 40))])
        _seed_days(connection_info, generation_id, {1: (True, battles)}, {1: {
            "state": "Partial", "failure_reasons": ["missing_start_baseline"],
            "start": None, "end": 4981, "end_read_at": _october(7, 5, 6),
        }})
        _set_defenses(connection_info, {1: 8})
        with psycopg.connect(connection_info) as connection:
            # Every member's results are in; none has army records.
            connection.execute(
                "UPDATE boundary_publication_generation_members"
                " SET status = 'terminal', snapshot_status = 'partial',"
                " army_status = 'unavailable' WHERE generation_id = %s",
                (generation_id,),
            )
        _later_profiles(connection_info, archive_server, [(tag, 4981, _october(7, 5, 25))])
        database = Database(connection_info)
        try:
            board = _publish_boards(database)
            _process(connection_info, archive_server, [store_observation(
                connection_info, archive_server, occurrence_key="recovered-log",
                endpoint="battle_log", body=recovered_log,
                observed_at=_october(7, 5, 9), normalized_tag=tag,
                parser_version=BATTLE_PARSER_VERSION,
            )[1]])
            _later_profiles(connection_info, archive_server, [(tag, 5000, _october(7, 5, 7))])
            season = ranked_day_for(DAY_2_RESET - DAY).official_season_id
            rebuilds = boundary.queue_board_rebuilds(database, season, queue=False)
            with psycopg.connect(connection_info) as connection:
                corrections = connection.execute(
                    "SELECT boundary_at, source_generation_id, state::text"
                    " FROM boundary_publication_corrections"
                ).fetchall()
            rebuilt = _publish_boards(database)
            with psycopg.connect(connection_info) as connection:
                newest = connection.execute(
                    "SELECT generation, snapshot_state::text"
                    " FROM boundary_publication_generations"
                    " ORDER BY generation DESC LIMIT 1"
                ).fetchone()
        finally:
            database.close()

    assert board == [(tag, 4981, "confirmed")]
    assert corrections == [(DAY_2_RESET, generation_id, "queued")]
    assert [
        (entry["late_battles"], entry["correction"]) for entry in rebuilds["boards"]
    ] == [(1, "already_queued")]
    assert newest == (2, "published")
    assert rebuilt == [(tag, 4990, "uncertain")]


def _publish_boards(database) -> list:
    """Run the worker's board publication until nothing waits: the
    coordinator's pass, which freezes each ready board's inputs, starts a
    published board's queued correction and queues its builds, then each
    leaderboard, analytics and army build. Return the entries of the newest
    published board."""
    from clashlens import boundary_publication
    from clashlens.worker import ObservationProcessor

    processor = ObservationProcessor(database, None)
    for _ in range(10):
        boundary_publication.reevaluate_boundary_publications(database)
        with database.pool.connection() as connection:
            jobs = [int(row[0]) for row in connection.execute(
                "SELECT id FROM python_processing_jobs WHERE status = 'pending'"
                " AND work_type IN"
                " ('build_snapshot', 'build_analytics', 'build_army_analytics')"
                " ORDER BY id"
            ).fetchall()]
        if not jobs:
            break
        for job in jobs:
            result = processor.process_job(job, owner="publication")
            assert result is not None and result.outcome == "processed"
    with database.pool.connection() as connection:
        return [
            (text(tag), trophies, text(confidence))
            for tag, trophies, confidence in connection.execute(
                """
                SELECT player.normalized_tag, entry.trophies, entry.confidence
                FROM leaderboard_snapshot_entries AS entry
                JOIN players AS player ON player.id = entry.player_id
                WHERE entry.snapshot_id = (
                    SELECT max(id) FROM leaderboard_snapshots
                    WHERE snapshot_kind = 'frozen' AND state = 'published'
                )
                ORDER BY entry.position
                """
            ).fetchall()
        ]


def test_a_check_is_judged_again_when_the_day_before_its_ended_day_changes(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The check pools the saved day before's defenses. Calculated again
    with a gap in its battle logs, that day leaves the settled check
    unresolved; calculated again whole, the check settles again."""
    from dataclasses import replace

    from clashlens import reconciliation_db

    monkeypatch.setenv(SWITCH, "true")
    prior_day = RESET - 2 * DAY
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        _process(connection_info, archive_server,
                 [scenario[job] for job in ORDERS["named_check_last"]])
        settled = _verdict(connection_info)[:3]

        def recalculate(key: str) -> None:
            database, _ = _processor(connection_info, archive_server)
            try:
                job = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=prior_day, now=RESET,
                    request_key=key,
                )
            finally:
                database.close()
            _process(connection_info, archive_server, [job])

        original = reconciliation_db.reconcile_ranked_day
        monkeypatch.setattr(
            reconciliation_db, "reconcile_ranked_day",
            lambda data: replace(original(data), coverage_complete=False),
        )
        recalculate("with-gap")
        gap = _verdict(connection_info)[:3]
        monkeypatch.setattr(reconciliation_db, "reconcile_ranked_day", original)
        recalculate("whole")
        whole = _verdict(connection_info)[:3]

    assert settled == ("settled", scenario["target"], [])
    assert gap == ("unresolved", None, ["previous_day_defenses_unknown"])
    assert whole == settled


def test_season_repair_judges_old_rule_checks_again(
    database_url: str, archive_server, monkeypatch
) -> None:
    """A check rejected under the old rule, whose named log had to reach
    back before the day before, is judged again by the Season repair under
    the present one, and settles."""
    from domain_test_support import repair_season

    monkeypatch.setenv(SWITCH, "true")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        _process(connection_info, archive_server,
                 [scenario[job] for job in ORDERS["named_check_last"]])
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                UPDATE reset_boundary_settlements
                SET state = 'unresolved', selected_trophies = NULL, proof_kind = NULL,
                    proof_rule_version = 'reset-settlement-observed-adjustment-v1',
                    proof_fingerprint = 'old', reasons = '["battle_log_too_short"]'
                WHERE boundary_at = %s
                """,
                (RESET,),
            )
        repair_season(connection_info, ranked_day_for(RESET - DAY).official_season_id)
        with psycopg.connect(connection_info) as connection:
            rule = connection.execute(
                "SELECT proof_rule_version FROM reset_boundary_settlements"
                " WHERE boundary_at = %s",
                (RESET,),
            ).fetchone()[0]

        assert _verdict(connection_info)[:3] == ("settled", scenario["target"], [])
    assert rule == reset_settlement.PROOF_RULE_VERSION


def _sweep_with_a_gap_the_day_before(monkeypatch) -> None:
    """The late-battle sweep finds the day before the ended day, and every
    recalculation finds a gap in that day's battle logs."""
    from dataclasses import replace

    from clashlens import late_battle_sweep, reconciliation_db

    monkeypatch.setattr(late_battle_sweep, "_STALE_DAYS", f"""
        SELECT player.id, %(boundary)s::timestamptz - interval '2 days'
        FROM players AS player WHERE player.normalized_tag = '{TAG}'
          AND %(window_start)s::timestamptz IS NOT NULL AND %(rule)s IS NOT NULL
    """)
    monkeypatch.setattr(late_battle_sweep, "_OUTDATED_DAYS", """
        SELECT NULL::bigint, NULL::timestamptz WHERE %(rule)s IS NULL
    """)
    original = reconciliation_db.reconcile_ranked_day
    monkeypatch.setattr(reconciliation_db, "reconcile_ranked_day",
                        lambda data: replace(original(data), coverage_complete=False))


def test_late_battle_sweep_judges_the_check_its_day_before_feeds_again(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The scheduled late-battle sweep recalculates the day before the
    ended day with a gap in its battle logs: the settled check that pooled
    that day's defenses is judged again and no longer settles."""
    from clashlens import late_battle_sweep

    monkeypatch.setenv(SWITCH, "true")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        _process(connection_info, archive_server,
                 [scenario[job] for job in ORDERS["named_check_last"]])
        settled = _verdict(connection_info)[:3]
        # The sweep finds the day before's late report.
        _sweep_with_a_gap_the_day_before(monkeypatch)
        database, _ = _processor(connection_info, archive_server)
        try:
            assert late_battle_sweep.sweep_late_battles(
                database, now=RESET + timedelta(hours=1)
            ) == (1, 0)
        finally:
            database.close()
        rejudged = _verdict(connection_info)[:3]

    assert settled == ("settled", scenario["target"], [])
    assert rejudged == ("unresolved", None, ["previous_day_defenses_unknown"])


def test_late_battle_sweep_locks_every_day_before_any_reset(
    database_url: str, archive_server, monkeypatch
) -> None:
    """A recovered battle log holds the ended day's lock and then wants the
    Reset ending the day before. The sweep recalculating both days waits for
    the ended day's lock before it recalculates the day before, so it holds
    no lock of that Reset meanwhile, and both finish."""
    from clashlens import late_battle_sweep, ranked_day_inputs, reconciliation_db

    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        _process(connection_info, archive_server,
                 [scenario[job] for job in ORDERS["named_check_last"]])
        # The day before's result changes, so recalculating it locks its Reset.
        _sweep_with_a_gap_the_day_before(monkeypatch)
        monkeypatch.setattr(reconciliation_db, "limit_lock_waits", lambda _connection: None)
        database, _ = _processor(connection_info, archive_server)
        try:
            with psycopg.connect(connection_info) as late_log, ThreadPoolExecutor(1) as pool:
                ranked_day_inputs.lock_ranked_day(
                    late_log, scenario["player"], ranked_day_for(RESET - DAY)
                )
                sweep = pool.submit(
                    late_battle_sweep.sweep_late_battles, database,
                    now=RESET + timedelta(hours=1),
                )
                _wait_for_advisory_wait(late_log, sweep)
                late_log.execute("SET LOCAL lock_timeout = '5s'")
                lock_boundary_publication(late_log, RESET - DAY)
                late_log.commit()
                assert sweep.result(timeout=60) == (1, 0)
        finally:
            database.close()


def test_a_stored_season_summary_follows_the_rejudged_check(
    database_url: str, archive_server, monkeypatch
) -> None:
    """A Season summary is stored while the day before the ended day has a
    gap and the check is unresolved. Calculated again whole, that day
    settles the check, and the stored summary is stored again from it: it
    is what the summary's days and their proofs give now."""
    from dataclasses import replace

    from clashlens import reconciliation_db
    from clashlens.season_summaries import _digest, _project, materialize_player_season

    monkeypatch.setenv(SWITCH, "true")
    prior_day = RESET - 2 * DAY
    season = ranked_day_for(RESET - DAY).official_season_id
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = _scenario(connection_info, archive_server)
        _process(connection_info, archive_server,
                 [scenario[job] for job in ORDERS["named_check_last"]])
        player = scenario["player"]

        def recalculate(key: str) -> None:
            database, _ = _processor(connection_info, archive_server)
            try:
                job = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=prior_day, now=RESET,
                    request_key=key,
                )
            finally:
                database.close()
            _process(connection_info, archive_server, [job])

        original = reconciliation_db.reconcile_ranked_day
        monkeypatch.setattr(
            reconciliation_db, "reconcile_ranked_day",
            lambda data: replace(original(data), coverage_complete=False),
        )
        recalculate("with-gap")
        with psycopg.connect(connection_info) as connection:
            materialize_player_season(connection, player_id=player, season_id=season)
        unresolved = _verdict(connection_info)[:3]
        monkeypatch.setattr(reconciliation_db, "reconcile_ranked_day", original)
        recalculate("whole")
        settled = _verdict(connection_info)[:3]
        with psycopg.connect(connection_info) as connection:
            stored = connection.execute(
                "SELECT content_digest FROM player_season_summaries"
                " WHERE player_id = %s AND official_season_id = %s",
                (player, season),
            ).fetchone()[0]
            projected = _digest(_project(player, season, connection))

    assert unresolved == ("unresolved", None, ["previous_day_defenses_unknown"])
    assert settled == ("settled", scenario["target"], [])
    assert str(stored) == projected
