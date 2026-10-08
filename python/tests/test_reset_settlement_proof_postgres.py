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
from domain_test_support import domain_database, store_observation
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
    database, processor = _processor(connection_info, archive_server)
    try:
        for job in jobs:
            assert processor.process_job(job, owner=f"job-{job}") is not None
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
    database_url: str,
) -> None:
    """A Complete day can balance on two Reset readings that both miss the
    same delayed credit: a start reading of 5,200 that misses the day
    before's last attack, a day netting nothing, and an end reading of
    5,200 that misses its 04:50 attack. A profile read at 05:10, after the
    end reading and before any battle of the next day, showing 5,240
    disproves it, so the board shows the last reading plus later battles,
    uncertain. One showing the day's end, or none, leaves it proven."""
    from test_boundary_manifest_postgres import (
        _ARCHIVE,
        DAY_2_RESET,
        _build_board,
        _october,
        _seed_board,
        _seed_days,
    )

    from clashlens.db import Database
    from clashlens.domain import RANKED_DAY_DURATION

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
        season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
        later_at = _october(7, 5, 10)
        for player, trophies in ((1, 5240), (2, 5200)):
            tag = readings[player - 1][0]
            observation_id = store_observation(
                connection_info, _ARCHIVE, occurrence_key=f"later-{player}",
                endpoint="profile", body=f"later {player}".encode(),
                observed_at=later_at, normalized_tag=tag,
            )[0]
            with psycopg.connect(connection_info) as connection:
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute(
                    """
                    WITH later AS (
                        INSERT INTO player_profile_versions (
                            player_id, observation_id, normalized_tag,
                            endpoint_version, schema_version, parser_version,
                            observed_at, source_http_status, name, trophies,
                            league_tier_id, league_tier_name, eligibility_state,
                            profile_json, source_contract_state,
                            current_league_season_id
                        ) VALUES (%(player)s, %(observation)s, %(tag)s, 'v1', 'v1',
                                  'parser', %(at)s, 200, %(tag)s, %(trophies)s,
                                  105000034, 'Legend League', 'eligible', '{}',
                                  'accepted', %(season)s)
                        RETURNING id
                    )
                    INSERT INTO player_profile_effects (
                        profile_version_id, observation_id, effect_kind,
                        observed_at, source_http_status, endpoint_version,
                        schema_version, parser_version
                    )
                    SELECT id, %(observation)s, 'current_profile', %(at)s, 200,
                           'v1', 'v1', 'parser'
                    FROM later
                    """,
                    {"player": player, "observation": observation_id, "tag": tag,
                     "at": later_at, "trophies": trophies, "season": season},
                )
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
