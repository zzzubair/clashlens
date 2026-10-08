"""The Monday re-check of the promotion list finds promoted players and saves them as due."""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from urllib.parse import unquote

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from test_reset_settlement_collection_postgres import _collector

from clashlens import promotion_recheck
from clashlens.collector_db import CollectorDatabase
from clashlens.collector_http import ApiKey, KeyPool

PROFILE = json.loads(
    (Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json").read_bytes()
)
# Answers carry the real time, so the test uses the real current week.
MONDAY = promotion_recheck.week_start(datetime.now(UTC))
LAST_WEEK = MONDAY - timedelta(days=6)


def _profile(tag: str, tier_id: int, tier_name: str, trophies: int) -> bytes:
    return json.dumps(
        {**PROFILE, "tag": tag, "trophies": trophies,
         "leagueTier": {"id": tier_id, "name": tier_name}}
    ).encode()


class _Provider(BaseHTTPRequestHandler):
    """A fake official API answering each player's profile from ``answers``."""

    protocol_version = "HTTP/1.1"
    answers: ClassVar[dict[str, tuple[int, bytes]]] = {}
    asked: ClassVar[list[str]] = []
    started: ClassVar[list[float]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        tag = unquote(self.path.rsplit("/", 1)[-1])
        type(self).started.append(time.monotonic())
        type(self).asked.append(tag)
        status, body = type(self).answers.get(tag, (404, b"{}"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _provider(answers: dict[str, tuple[int, bytes]]):
    _Provider.answers = answers
    _Provider.asked = []
    _Provider.started = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _seed(connection_info: str) -> None:
    with psycopg.connect(connection_info) as connection:
        # Monday's Reset collection has finished.
        connection.execute(
            "INSERT INTO collector_reset_sweeps (boundary_at, member_ids) VALUES (%s, '{}')",
            (MONDAY,),
        )
        connection.execute(
            """
            INSERT INTO promotion_candidates (normalized_tag, league_tier_id, trophies, checked_at)
            VALUES ('#8QQ', 105000035, 5300, %(old)s), ('#9QQ', 105000035, 5000, %(old)s),
                   ('#2QQ', 105000035, 4800, %(old)s), ('#CQQ', 105000035, 4700, %(old)s),
                   ('#0QQ', 105000035, 5200, %(new)s), ('#UQQ', 105000034, 4600, %(old)s)
            """,
            {"old": LAST_WEEK, "new": MONDAY},
        )


def _two_key_collector(origin: str, database: CollectorDatabase, tmp_path: Path):
    """Two regular keys of six slots, so two promotion requests leave a key's worth idle."""
    collector = _collector(origin, database, tmp_path)
    collector.regular_keys = KeyPool(
        [ApiKey("regular-1", "secret"), ApiKey("regular-2", "secret")],
        starts_per_second=25,
        concurrency_per_key=6,
    )
    return collector


def _check(
    origin: str,
    connection_info: str,
    tmp_path: Path,
    now: datetime,
    attempted: set[str] | None = None,
    rate: float = 1000.0,
):
    database = CollectorDatabase(connection_info)
    try:
        collector = _two_key_collector(origin, database, tmp_path)
        admit = promotion_recheck.Admission(collector, rate, clock=lambda: now)
        return asyncio.run(
            promotion_recheck.check_batch(collector, admit, set() if attempted is None else attempted)
        )
    finally:
        database.close()


def _admit(connection_info: str) -> None:
    """One collector admission pass, which turns due players into discovery checks."""
    with psycopg.connect(connection_info) as connection:
        connection.execute("SET ROLE clashlens_collector")
        connection.execute("SELECT clashlens_admit_discovery_profiles(clock_timestamp())")


def _pending(connection_info: str) -> list[tuple[str, bool, str]]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT player.normalized_tag, work.coalescing_key LIKE '%%Z', work.profile_status
            FROM collector_work AS work JOIN players AS player ON player.id = work.player_id
            WHERE work.kind = 'discovery_profile' AND work.status = 'pending'
              AND work.coalescing_key LIKE 'discovery-profile:%%'
            ORDER BY player.normalized_tag
            """
        ).fetchall()


def _checked_at(connection_info: str, tag: str) -> datetime:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT checked_at FROM promotion_candidates WHERE normalized_tag = %s", (tag,)
        ).fetchone()[0]


def test_monday_recheck_queues_promoted_players_and_refreshes_the_list(
    database_url: str, tmp_path: Path
) -> None:
    answers = {
        "#8QQ": (200, _profile("#8QQ", 105000036, "Legend I", 5000)),
        "#9QQ": (200, _profile("#9QQ", 105000035, "Legend II", 5100)),
        "#CQQ": (200, _profile("#CQQ", 105000033, "Electro League 33", 4400)),
        "#UQQ": (200, _profile("#UQQ", 105000034, "Legend III", 4650)),
        # #2QQ answers not found.
    }
    with domain_database(database_url) as connection_info, _provider(answers) as origin:
        _seed(connection_info)
        now = MONDAY + timedelta(minutes=70)
        database = CollectorDatabase(connection_info)
        try:
            # Legend II first; players checked since the Reset wait.
            assert promotion_recheck.due_tags(database, now, []) == [
                "#2QQ", "#8QQ", "#9QQ", "#CQQ",
            ]
        finally:
            database.close()
        assert _check(origin, connection_info, tmp_path, now) == Counter(
            asked=4, promoted=1, listed=1, removed=2, failed=0, queued=1
        )
        assert sorted(_Provider.asked) == ["#2QQ", "#8QQ", "#9QQ", "#CQQ"]
        # Legend III once no Legend II player is due.
        assert _check(origin, connection_info, tmp_path, now) == Counter(
            asked=1, listed=1, failed=0, queued=0
        )
        assert _Provider.asked[-1] == "#UQQ"
        with psycopg.connect(connection_info) as connection:
            listed = connection.execute(
                "SELECT normalized_tag, league_tier_id, trophies, checked_at > %s"
                " FROM promotion_candidates ORDER BY normalized_tag",
                (MONDAY,),
            ).fetchall()
            due = connection.execute(
                "SELECT normalized_tag, active FROM players WHERE eligibility_due_at IS NOT NULL"
            ).fetchall()
        assert listed == [
            ("#0QQ", 105000035, 5200, False),
            ("#8QQ", 105000035, 5000, True),
            ("#9QQ", 105000035, 5100, True),
            ("#UQQ", 105000034, 4650, True),
        ]
        # The saved discovery check, not this answer, starts tracking the player.
        assert due == [("#8QQ", False)]
        _admit(connection_info)
        assert _pending(connection_info) == [("#8QQ", True, "pending")]

        # Everyone due this week has been asked.
        assert _check(origin, connection_info, tmp_path, MONDAY + timedelta(minutes=71)) is None


def test_monday_recheck_waits_for_06_00_late_live_players_and_settlement(
    database_url: str, tmp_path: Path
) -> None:
    with domain_database(database_url) as connection_info, _provider({}) as origin:
        _seed(connection_info)
        # Settlement and the late-battle check come first, so 05:50 is too early.
        assert _check(origin, connection_info, tmp_path, MONDAY + timedelta(minutes=50)) is None
        now = MONDAY + timedelta(minutes=70)
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "INSERT INTO players (normalized_tag, active, eligibility_state, next_due_at)"
                " VALUES ('#2PP', true, 'eligible', %s) RETURNING id",
                (now - timedelta(minutes=3),),
            ).fetchone()[0]
        assert _check(origin, connection_info, tmp_path, now) is None
        with psycopg.connect(connection_info) as connection:
            connection.execute("UPDATE players SET next_due_at = %s WHERE id = %s", (now, player_id))
            # Monday's settlement check for that player has not run yet.
            connection.execute(
                """
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key,
                    sweep_id, profile_status, battle_log_status, league_history_status
                )
                SELECT 'reset_settlement', 'ordinary', 'player', %s, '#2PP', %s, 'settle',
                       id, 'pending', 'pending', 'not_applicable'
                FROM collector_reset_sweeps WHERE boundary_at = %s
                """,
                (player_id, MONDAY + timedelta(minutes=20), MONDAY),
            )
        assert _check(origin, connection_info, tmp_path, now) is None
        assert _Provider.asked == []
        with psycopg.connect(connection_info) as connection:
            connection.execute("UPDATE collector_work SET status = 'cancelled'")
        assert _check(origin, connection_info, tmp_path, now)["asked"] == 4


def test_requests_go_out_paced_and_only_while_a_key_is_idle(
    database_url: str, tmp_path: Path
) -> None:
    with domain_database(database_url) as connection_info, _provider({}) as origin:
        _seed(connection_info)
        now = MONDAY + timedelta(minutes=70)
        # After an idle stretch, requests still reach the API a twentieth of a second apart.
        assert _check(origin, connection_info, tmp_path, now, rate=20.0)["asked"] == 4
        gaps = [b - a for a, b in itertools.pairwise(_Provider.started)]
        assert len(gaps) == 3 and min(gaps) >= 0.04

        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "INSERT INTO promotion_candidates (normalized_tag, league_tier_id, checked_at)"
                " VALUES ('#8QQ', 105000035, %s)",
                (LAST_WEEK,),
            )
        _Provider.asked = []
        database = CollectorDatabase(connection_info)
        collector = _two_key_collector(origin, database, tmp_path)
        admit = promotion_recheck.Admission(collector, 20.0, clock=lambda: now)

        async def scenario() -> None:
            # Seven of the twelve regular slots are busy, so nothing goes out.
            release = asyncio.Event()

            async def hold(_key, start_request) -> None:
                await start_request()
                await release.wait()

            holders = [
                asyncio.create_task(collector.regular_keys.run(hold)) for _ in range(7)
            ]
            await asyncio.sleep(0.05)
            assert await promotion_recheck.check_batch(collector, admit, set()) is None
            assert _Provider.asked == []
            release.set()
            await asyncio.gather(*holders)
            assert (await promotion_recheck.check_batch(collector, admit, set()))["asked"] == 1
            assert _Provider.asked == ["#8QQ"]

        try:
            asyncio.run(scenario())
        finally:
            database.close()


def test_a_promoted_player_stays_due_while_the_discovery_queue_is_full(
    database_url: str, tmp_path: Path
) -> None:
    answers = {"#8QQ": (200, _profile("#8QQ", 105000036, "Legend I", 5000))}
    with domain_database(database_url) as connection_info, _provider(answers) as origin:
        _seed(connection_info)
        with psycopg.connect(connection_info) as connection:
            connection.execute("DELETE FROM promotion_candidates WHERE normalized_tag <> '#8QQ'")
            connection.execute(
                """
                WITH waiting AS (
                    INSERT INTO players (normalized_tag, active, eligibility_state)
                    SELECT '#Q' || n, false, 'unknown' FROM generate_series(1, 500) AS n
                    RETURNING id, normalized_tag
                )
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key,
                    profile_status, battle_log_status, league_history_status
                )
                SELECT 'discovery_profile', 'ordinary', 'player', id, normalized_tag,
                       now(), 'full:' || id, 'pending', 'not_applicable', 'pending'
                FROM waiting
                """
            )
        now = MONDAY + timedelta(minutes=70)
        # The answer is saved as due at once, so the list row is checked.
        assert _check(origin, connection_info, tmp_path, now)["queued"] == 1
        assert _checked_at(connection_info, "#8QQ") > MONDAY
        _admit(connection_info)
        assert _pending(connection_info) == []
        with psycopg.connect(connection_info) as connection:
            connection.execute("UPDATE collector_work SET status = 'cancelled'")
        # Once there is room, the next collector pass adds its check.
        _admit(connection_info)
        assert _pending(connection_info) == [("#8QQ", True, "pending")]


def test_a_promoted_player_whose_check_failed_this_week_gets_another(
    database_url: str, tmp_path: Path
) -> None:
    answers = {
        "#8QQ": (200, _profile("#8QQ", 105000036, "Legend I", 5000)),
        "#9QQ": (200, _profile("#9QQ", 105000036, "Legend I", 5100)),
    }
    with domain_database(database_url) as connection_info, _provider(answers) as origin:
        _seed(connection_info)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "DELETE FROM promotion_candidates WHERE normalized_tag NOT IN ('#8QQ', '#9QQ')"
            )
            # #8QQ's discovery check already failed this week.
            connection.execute(
                """
                WITH refused AS (
                    INSERT INTO players (normalized_tag, active, eligibility_state)
                    VALUES ('#8QQ', false, 'unknown') RETURNING id, normalized_tag
                )
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key,
                    status, profile_status, battle_log_status, league_history_status
                )
                SELECT 'discovery_profile', 'ordinary', 'player', id, normalized_tag, now(),
                       'discovery-profile:' || id || ':' || %s, 'failed', 'failed',
                       'not_applicable', 'pending'
                FROM refused
                """,
                (MONDAY.strftime("%Y-%m-%dT%H:%M:%SZ"),),
            )
        now = MONDAY + timedelta(minutes=70)
        assert _check(origin, connection_info, tmp_path, now) == Counter(
            asked=2, promoted=2, failed=0, queued=2
        )
        assert _checked_at(connection_info, "#8QQ") > MONDAY
        assert _checked_at(connection_info, "#9QQ") > MONDAY
        _admit(connection_info)
        # The failed check keeps its row; the new one has a key of its own.
        assert _pending(connection_info) == [("#8QQ", False, "pending"), ("#9QQ", True, "pending")]


def test_a_promoted_player_another_job_holds_stays_due(
    database_url: str, tmp_path: Path
) -> None:
    answers = {"#8QQ": (200, _profile("#8QQ", 105000036, "Legend I", 5000))}
    with domain_database(database_url) as connection_info, _provider(answers) as origin:
        _seed(connection_info)
        now = MONDAY + timedelta(minutes=70)
        with psycopg.connect(connection_info) as connection:
            connection.execute("DELETE FROM promotion_candidates WHERE normalized_tag <> '#8QQ'")
            connection.execute(
                "INSERT INTO players (normalized_tag, active, eligibility_state)"
                " VALUES ('#8QQ', false, 'unknown')"
            )
        with psycopg.connect(connection_info) as holder, holder.transaction():
            # An older profile job is still processing this player.
            holder.execute("SELECT 1 FROM players WHERE normalized_tag = '#8QQ' FOR NO KEY UPDATE")
            assert _check(origin, connection_info, tmp_path, now) == Counter(
                asked=1, promoted=1, failed=0, queued=0
            )
        assert _checked_at(connection_info, "#8QQ") == LAST_WEEK
        assert _check(origin, connection_info, tmp_path, now)["queued"] == 1
        assert _checked_at(connection_info, "#8QQ") > MONDAY


def test_a_promoted_player_whose_waiting_work_has_its_profile_gets_a_fresh_check(
    database_url: str, archive_server, tmp_path: Path
) -> None:
    legend_i = _profile("#8QQ", 105000036, "Legend I", 5000)
    answers = {"#8QQ": (200, legend_i)}
    with domain_database(database_url) as connection_info, _provider(answers) as origin:
        _seed(connection_info)
        now = MONDAY + timedelta(minutes=70)
        observation_id, _job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="promotion-held-profile",
            endpoint="profile",
            body=legend_i,
            observed_at=datetime.now(UTC) - timedelta(seconds=5),
            normalized_tag="#8QQ",
        )
        with psycopg.connect(connection_info) as connection:
            connection.execute("DELETE FROM promotion_candidates WHERE normalized_tag <> '#8QQ'")
            # The check already has its profile and waits only for league history.
            connection.execute(
                """
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key,
                    profile_status, battle_log_status, league_history_status,
                    profile_observation_id
                )
                SELECT 'discovery_profile', 'ordinary', 'player', id, normalized_tag, now(),
                       'held', 'observed', 'not_applicable', 'pending', %s
                FROM players WHERE normalized_tag = '#8QQ'
                """,
                (observation_id,),
            )
        assert _check(origin, connection_info, tmp_path, now) == Counter(
            asked=1, promoted=1, failed=0, queued=1
        )
        assert _checked_at(connection_info, "#8QQ") > MONDAY
        # The waiting check is left to finish first.
        _admit(connection_info)
        assert _pending(connection_info) == []
        with psycopg.connect(connection_info) as connection:
            connection.execute("UPDATE collector_work SET status = 'complete', completed_at = now()")
            connection.execute("UPDATE players SET eligibility_due_at = now()")
        # Its profile is older than the re-check's answer, so a fresh one is fetched.
        _admit(connection_info)
        assert _pending(connection_info) == [("#8QQ", True, "pending")]


def test_an_unreadable_or_uncertain_answer_stays_due_for_a_retry(
    database_url: str, tmp_path: Path
) -> None:
    answers = {
        "#8QQ": (200, b"{not json"),
        # A Legend III ID named Legend I is an uncertain tier.
        "#9QQ": (200, _profile("#9QQ", 105000034, "Legend I", 5000)),
    }
    with domain_database(database_url) as connection_info, _provider(answers) as origin:
        _seed(connection_info)
        now = MONDAY + timedelta(minutes=70)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "DELETE FROM promotion_candidates WHERE normalized_tag NOT IN ('#8QQ', '#9QQ')"
            )
        # Each pass asks again; nothing skips the players for the week.
        for _ in range(4):
            assert _check(origin, connection_info, tmp_path, now) == Counter(
                asked=2, failed=2, queued=0
            )
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT normalized_tag, league_tier_id, checked_at FROM promotion_candidates"
                " ORDER BY normalized_tag"
            ).fetchall() == [("#8QQ", 105000035, LAST_WEEK), ("#9QQ", 105000035, LAST_WEEK)]


def test_players_left_due_are_asked_again_after_the_rest_of_the_list(
    database_url: str, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(promotion_recheck, "BATCH_SIZE", 1)
    answers = {
        "#8QQ": (200, b"{not json"),
        "#9QQ": (200, _profile("#9QQ", 105000035, "Legend II", 5100)),
    }
    with domain_database(database_url) as connection_info, _provider(answers) as origin:
        _seed(connection_info)
        now = MONDAY + timedelta(minutes=70)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "DELETE FROM promotion_candidates WHERE normalized_tag NOT IN ('#8QQ', '#9QQ')"
            )
        attempted: set[str] = set()
        results = [_check(origin, connection_info, tmp_path, now, attempted) for _ in range(4)]
        # The oldest failing row does not hold back the next one, and it is
        # asked again only after the pass ends and its pause.
        assert [result and result["asked"] for result in results] == [1, 1, None, 1]
        assert _Provider.asked == ["#8QQ", "#9QQ", "#8QQ"]


def test_a_legend_ii_player_left_due_is_asked_again_before_any_legend_iii_player(
    database_url: str, tmp_path: Path
) -> None:
    answers = {
        "#8QQ": (200, b"{not json"),
        "#UQQ": (200, _profile("#UQQ", 105000034, "Legend III", 4650)),
    }
    with domain_database(database_url) as connection_info, _provider(answers) as origin:
        _seed(connection_info)
        now = MONDAY + timedelta(minutes=70)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "DELETE FROM promotion_candidates WHERE normalized_tag NOT IN ('#8QQ', '#UQQ')"
            )
        attempted: set[str] = set()
        results = [_check(origin, connection_info, tmp_path, now, attempted) for _ in range(3)]
        # The pass ends at the Legend II player left due instead of moving on.
        assert [result and result["asked"] for result in results] == [1, None, 1]
        answers["#8QQ"] = (200, _profile("#8QQ", 105000035, "Legend II", 5100))
        results = [_check(origin, connection_info, tmp_path, now, attempted) for _ in range(3)]
        assert [result and result["listed"] for result in results] == [None, 1, 1]
        assert _Provider.asked == ["#8QQ", "#8QQ", "#8QQ", "#UQQ"]


def test_a_failing_request_stops_the_rest_of_its_batch_first() -> None:
    cancelled = []

    async def database_call(function, *_args):
        return {
            promotion_recheck.has_spare_time: True,
            promotion_recheck.due_tags: ["#8QQ", "#2QQ"],
        }[function]

    async def fetch_player(_keys, tag: str, _endpoint: str):
        if tag == "#2QQ":
            raise RuntimeError("database guard failed")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(tag)

    async def scenario() -> None:
        collector = SimpleNamespace(
            _database_call=database_call,
            _stopping=asyncio.Event(),
            database=None,
            regular_keys=KeyPool(
                [ApiKey("regular-1", "secret")], starts_per_second=25, concurrency_per_key=2
            ),
            client=SimpleNamespace(fetch_player=fetch_player),
        )
        now = MONDAY + timedelta(minutes=70)
        admit = promotion_recheck.Admission(collector, 1000.0, clock=lambda: now)
        with pytest.raises(RuntimeError):
            await promotion_recheck.check_batch(collector, admit, set())
        # The slow request ended before the error came back, so no batch outlives its limit.
        assert cancelled == ["#8QQ"]

    asyncio.run(scenario())


def test_stopping_mid_batch_at_a_slow_rate_ends_the_batch_quickly() -> None:
    tags = [f"#{index}QQ" for index in range(200)]
    asked = []

    async def database_call(function, *_args):
        return {
            promotion_recheck.has_spare_time: True,
            promotion_recheck.due_tags: tags,
            promotion_recheck.record_answers: set(),
        }[function]

    async def fetch_player(_keys, tag: str, _endpoint: str):
        asked.append(tag)
        return SimpleNamespace(http_status=503, response_completed_at=datetime.now(UTC))

    async def scenario() -> None:
        stopping = asyncio.Event()
        collector = SimpleNamespace(
            _database_call=database_call,
            _stopping=stopping,
            database=None,
            regular_keys=KeyPool(
                [ApiKey("regular-1", "secret")], starts_per_second=25, concurrency_per_key=2
            ),
            client=SimpleNamespace(fetch_player=fetch_player),
        )
        now = MONDAY + timedelta(minutes=70)
        admit = promotion_recheck.Admission(collector, 1.0, clock=lambda: now)
        batch = asyncio.create_task(promotion_recheck.check_batch(collector, admit, set()))
        await asyncio.sleep(0.2)
        started = time.monotonic()
        stopping.set()
        result = await asyncio.wait_for(batch, timeout=5)
        assert time.monotonic() - started < 1
        assert asked == ["#0QQ"]
        assert result is not None and result["asked"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("setting", ["20/s", "nan"])
def test_an_unusable_rate_keeps_the_recheck_off_until_stopped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], setting: str
) -> None:
    monkeypatch.setenv("CLASHLENS_PROMOTION_RECHECK_PER_SECOND", setting)

    async def scenario() -> None:
        stopping = asyncio.Event()
        run = asyncio.create_task(promotion_recheck.run(SimpleNamespace(), stopping))
        await asyncio.sleep(0.1)
        assert not run.done()
        stopping.set()
        await asyncio.wait_for(run, timeout=1)

    asyncio.run(scenario())
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    expected = [{"event": "promotion_recheck", "status": "failed"}] if setting == "20/s" else []
    assert [{key: line[key] for key in ("event", "status")} for line in lines] == expected
