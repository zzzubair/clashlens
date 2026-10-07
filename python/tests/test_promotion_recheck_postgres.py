"""The Monday re-check of the promotion list finds and queues promoted players."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote

import psycopg
from domain_test_support import domain_database
from test_reset_settlement_collection_postgres import _collector

from clashlens import promotion_recheck
from clashlens.collector_db import CollectorDatabase

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

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        tag = unquote(self.path.rsplit("/", 1)[-1])
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
                   ('#2QQ', 105000034, 4800, %(old)s), ('#CQQ', 105000034, 4700, %(old)s),
                   ('#0QQ', 105000035, 5200, %(new)s)
            """,
            {"old": LAST_WEEK, "new": MONDAY},
        )


def _check(origin: str, connection_info: str, tmp_path: Path, now: datetime):
    database = CollectorDatabase(connection_info)
    try:
        collector = _collector(origin, database, tmp_path)
        return asyncio.run(promotion_recheck.check_batch(collector, now, Counter(), 1000.0))
    finally:
        database.close()


def test_monday_recheck_queues_promoted_players_and_refreshes_the_list(
    database_url: str, tmp_path: Path
) -> None:
    answers = {
        "#8QQ": (200, _profile("#8QQ", 105000036, "Legend I", 5000)),
        "#9QQ": (200, _profile("#9QQ", 105000035, "Legend II", 5100)),
        "#CQQ": (200, _profile("#CQQ", 105000033, "Electro League 33", 4400)),
        # #2QQ answers not found.
    }
    with domain_database(database_url) as connection_info, _provider(answers) as origin:
        _seed(connection_info)
        assert _check(origin, connection_info, tmp_path, MONDAY + timedelta(minutes=40)) == Counter(
            promoted=1, listed=1, removed=2, failed=0, queued=1
        )
        # Legend II first; a player already checked since the Reset is not asked.
        assert sorted(_Provider.asked[:2]) == ["#8QQ", "#9QQ"]
        assert sorted(_Provider.asked) == ["#2QQ", "#8QQ", "#9QQ", "#CQQ"]
        with psycopg.connect(connection_info) as connection:
            listed = connection.execute(
                "SELECT normalized_tag, league_tier_id, trophies, checked_at > %s"
                " FROM promotion_candidates ORDER BY normalized_tag",
                (MONDAY,),
            ).fetchall()
            queued = connection.execute(
                """
                SELECT player.normalized_tag, player.active, work.league_history_status
                FROM collector_work AS work JOIN players AS player ON player.id = work.player_id
                WHERE work.kind = 'discovery_profile' AND work.status = 'pending'
                """
            ).fetchall()
        assert listed == [
            ("#0QQ", 105000035, 5200, False),
            ("#8QQ", 105000035, 5000, True),
            ("#9QQ", 105000035, 5100, True),
        ]
        # The saved discovery check, not this answer, starts tracking the player.
        assert queued == [("#8QQ", False, "pending")]

        # Everyone due this week has been asked.
        assert _check(origin, connection_info, tmp_path, MONDAY + timedelta(minutes=41)) is None


def test_monday_recheck_waits_for_05_30_and_for_late_live_players(
    database_url: str, tmp_path: Path
) -> None:
    with domain_database(database_url) as connection_info, _provider({}) as origin:
        _seed(connection_info)
        assert _check(origin, connection_info, tmp_path, MONDAY + timedelta(minutes=20)) is None
        now = MONDAY + timedelta(minutes=40)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "INSERT INTO players (normalized_tag, active, eligibility_state, next_due_at)"
                " VALUES ('#2PP', true, 'eligible', %s)",
                (now - timedelta(minutes=3),),
            )
        assert _check(origin, connection_info, tmp_path, now) is None
        assert _Provider.asked == []


def test_a_promoted_player_waits_while_the_discovery_queue_is_full(
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
        now = MONDAY + timedelta(minutes=40)
        assert _check(origin, connection_info, tmp_path, now)["queued"] == 0
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT checked_at FROM promotion_candidates WHERE normalized_tag = '#8QQ'"
            ).fetchone()[0] == LAST_WEEK
            connection.execute("UPDATE collector_work SET status = 'cancelled'")
        # Once there is room, the next batch asks again and queues the player.
        assert _check(origin, connection_info, tmp_path, now)["queued"] == 1
