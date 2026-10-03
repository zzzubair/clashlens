"""Reset settlement verdicts through the real worker, database and permissions.

One player's named check (a profile, then a battle log) is saved with the
05:00 Reset pair and processed by the actual worker in different orders. A
synthetic settled previous Reset roots the target: it shows the rule's
behavior, not production coverage. Public day results never read the verdict.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database, store_observation
from test_domain_processing_postgres import _role_connection
from test_reconciliation_postgres import BATTLE_FIXTURE, DAY_END, _processor, _profile

from clashlens import reset_settlement
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
              work_status: str = "complete", with_root: bool = True) -> dict[str, int]:
    """Save a Reset pair, the named check and a later post-battle profile and
    log; return each response's processing job."""
    log, net, automatic = _battles(RESET)
    target = START + net - automatic
    jobs: dict[str, int] = {}
    ids: dict[str, int] = {}
    for name, endpoint, body, at in (
        ("early_profile", "profile", _profile(target + automatic), RESET + timedelta(seconds=31)),
        ("early_log", "battle_log", log, RESET + timedelta(seconds=33)),
        ("profile", "profile", _profile(target + profile_offset), RESET + timedelta(minutes=23, seconds=56)),
        ("log", "battle_log", log, RESET + timedelta(minutes=23, seconds=58)),
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

