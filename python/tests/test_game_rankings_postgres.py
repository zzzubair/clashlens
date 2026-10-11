"""The daily pulls of the game's ranking lists end in a stored score of the Daily board."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import psycopg
from domain_test_support import domain_database
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_reset_settlement_collection_postgres import _collector

from clashlens import game_rankings
from clashlens.collector_db import CollectorDatabase

RESET = datetime(2026, 10, 11, 5, tzinfo=UTC)
LEGEND_I = {"id": 105000036, "name": "Legend I"}
LEGEND_II = {"id": 105000035, "name": "Legend II"}
# Our published board: (tag, position, trophies, confidence).
BOARD = [
    ("#2PP", 1, 5400, "confirmed"),
    ("#2QQ", 2, 5300, "confirmed"),
    ("#2GG", 3, 5300, "confirmed"),
    ("#2LL", 4, 5200, "confirmed"),
    ("#2UU", 5, 5100, "uncertain"),
    ("#2VV", 6, 5000, "confirmed"),
]
YESTERDAY_RANKS = {"#2PP": 2, "#2QQ": 1, "#2GG": 3, "#2LL": 5, "#2UU": 4, "#2VV": 6}
TODAY_RANKS = {"#2PP": 1, "#2QQ": 3, "#2GG": 2, "#2LL": 4, "#2UU": 5, "#2VV": 6}


def _list(rows: list[tuple[str, int, int, int, dict]]) -> bytes:
    """A ranking list shaped like the game's: (tag, trophies, attack wins, previousRank, tier)."""
    return json.dumps({
        "items": [
            {"tag": tag, "name": "Player", "expLevel": 250, "trophies": trophies,
             "attackWins": wins, "defenseWins": 3, "rank": rank, "previousRank": previous,
             "league": {"id": 29000000, "name": "Unranked"}, "leagueTier": tier}
            for rank, (tag, trophies, wins, previous, tier) in enumerate(rows, 1)
        ],
        "paging": {"cursors": {}},
    }).encode()


def _global(ranks: dict[str, int], trophies: dict[str, int] | None = None) -> bytes:
    board = {tag: value for tag, _position, value, _confidence in BOARD}
    trophies = {**board, **(trophies or {})}
    return _list([(tag, trophies[tag], 40, rank, LEGEND_I)
                  for tag, rank in sorted(ranks.items(), key=lambda item: item[1])])


LOCATIONS = json.dumps({"items": [
    {"id": 32000006, "name": "International", "isCountry": False},
    {"id": 32000087, "name": "France", "isCountry": True, "countryCode": "FR"},
    {"id": 32000249, "name": "United States", "isCountry": True, "countryCode": "US"},
], "paging": {"cursors": {}}}).encode()
FRANCE = _list([
    ("#2QQ", 5310, 40, 1, LEGEND_I),
    ("#2PP", 5390, 41, 2, LEGEND_I),  # the game ranks #2QQ above: a break
    ("#2LL", 5000, 0, 3, LEGEND_I),  # the game's stuck 5,000 row; board says 5,200
    ("#2RR", 5050, 12, 4, LEGEND_I),  # an active Legend I player we don't track
    ("#2CC", 5000, 0, 5, LEGEND_I),  # never played this Season, not tracked
])
UNITED_STATES = _list([
    ("#2GG", 5300, 40, 1, LEGEND_I),
    ("#2UU", 5100, 40, 2, LEGEND_I),
    ("#2VV", 5000, 0, 3, LEGEND_I),  # really at 5,000 on the board too
    ("#2JJ", 4900, 30, 4, LEGEND_II),
])


class _Game(BaseHTTPRequestHandler):
    """The game's API: answers by path, recording each request."""

    protocol_version = "HTTP/1.1"
    answers: ClassVar[dict[str, tuple[int, bytes]]] = {}
    asked: ClassVar[list[str]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        type(self).asked.append(self.path)
        status, body = type(self).answers.get(self.path.split("?")[0], (404, b"{}"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _game(answers: dict[str, tuple[int, bytes]]):
    _Game.answers = answers
    _Game.asked = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Game)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _seed(info: str) -> None:
    """Tracked players, their published Daily board, and yesterday's Reset pull."""
    with psycopg.connect(info) as connection:
        ids = {
            tag: connection.execute(
                "INSERT INTO players (normalized_tag, active) VALUES (%s, true) RETURNING id",
                (tag,),
            ).fetchone()[0]
            for tag, *_rest in BOARD
        }
        observation = connection.execute(
            """
            INSERT INTO collector_observations (
                occurrence_key, scope, player_id, normalized_tag, endpoint,
                request_started_at, response_completed_at, http_status,
                response_hash, collector_version, key_label, evidence_headers,
                request_method, request_path, request_query,
                paging_envelope_state, source_adapter_version
            ) VALUES (
                'seed', 'player', %s, '#2PP', 'profile', %s, %s, 200, repeat('a', 64),
                'test', 'normal-test', '{}'::jsonb, 'GET', '/v1/players/%%232PP', '',
                'not_applicable', 'supercell-profile-parser-v3'
            ) RETURNING id
            """,
            (ids["#2PP"], RESET, RESET),
        ).fetchone()[0]
        snapshot = connection.execute(
            """
            INSERT INTO leaderboard_snapshots (
                snapshot_kind, boundary_at, version, ordering_rule_version,
                freshness_rule_version, state, measured_coverage,
                stale_entry_count, published_at
            ) VALUES ('frozen', %s, 3, 'test', 'test', 'published', 1, 0, %s)
            RETURNING id
            """,
            (RESET, RESET + timedelta(hours=1)),
        ).fetchone()[0]
        for tag, position, trophies, confidence in BOARD:
            connection.execute(
                """
                INSERT INTO leaderboard_snapshot_entries (
                    snapshot_id, position, player_id, trophies, trophy_observation_id,
                    trophy_observed_at, observation_age_seconds, freshness, confidence, tie_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, 0, 'fresh', %s, repeat('c', 64))
                """,
                (snapshot, position, ids[tag], trophies, observation, RESET, confidence),
            )
        # A pull 29 days old, which the daily prune removes.
        connection.execute(
            """
            INSERT INTO game_ranking_pulls (boundary_at, purpose, location_id, country_code,
                requested_at, completed_at, http_status)
            VALUES (%s, 'country', 32000087, 'FR', %s, %s, 200)
            """,
            (RESET - timedelta(days=29),) * 3,
        )


def _database(info: str) -> CollectorDatabase:
    options = conninfo_to_dict(info)["options"] + " -c role=clashlens_collector"
    return CollectorDatabase(make_conninfo(info, options=options))


def _day(info: str, origin: str, tmp_path, times: list[timedelta]) -> list[Counter[str]]:
    """Run the collector's pulls at each time after the Reset."""
    database = _database(info)
    try:
        collector = _collector(origin, database, tmp_path)

        async def pulls() -> list[Counter[str]]:
            return [
                await game_rankings.pull_due(
                    collector, game_rankings._Puller(collector, lambda after=after: RESET + after)
                )
                for after in times
            ]

        return asyncio.run(pulls())
    finally:
        database.close()


def _printed_score(info: str, capsys) -> dict:
    """Score and prune at 07:01, then what the operator's game-ranking-score command prints."""
    database = _database(info)
    try:
        now = RESET + timedelta(hours=2, minutes=1)
        for day in game_rankings._days_to_score(database, now):
            game_rankings.score_day(database, day)
        game_rankings._prune(database, now)
    finally:
        database.close()
    options = conninfo_to_dict(info)["options"] + " -c role=clashlens_collector"
    capsys.readouterr()
    game_rankings.run_command(make_conninfo(info, options=options), days=10000)
    [line] = capsys.readouterr().out.splitlines()
    return json.loads(line)


def test_a_reset_morning_of_game_lists_ends_in_a_stored_score(
    database_url: str, tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(game_rankings, "REQUEST_SECONDS", 0.0)
    answers = {
        "/v1/locations": (200, LOCATIONS),
        "/v1/locations/global/rankings/players": (
            200, _global(YESTERDAY_RANKS, {"#2GG": 5290}),
        ),
        "/v1/locations/32000087/rankings/players": (200, FRANCE),
        "/v1/locations/32000249/rankings/players": (200, UNITED_STATES),
    }
    with domain_database(database_url) as info, _game(answers) as origin:
        _seed(info)
        database = _database(info)
        try:
            game_rankings.save_pull(
                database, RESET - timedelta(days=1), "global_reset", None,
                RESET - timedelta(hours=23, minutes=40),
                type("Answer", (), {"body": _global(YESTERDAY_RANKS), "http_status": 200,
                                    "response_completed_at": RESET - timedelta(hours=23)})(),
                None, switch_basis="changed",
            )
        finally:
            database.close()
        # 05:00:40 early pull; 05:03 poll still shows yesterday's ranks.
        early, still_yesterday = _day(
            info, origin, tmp_path, [timedelta(seconds=40), timedelta(minutes=3)]
        )
        assert early == Counter(global_early=1, added_to_discovery=0)
        assert still_yesterday == Counter(global_poll=1, added_to_discovery=0)
        # By 05:06 previousRank has switched: the Reset pull, then the countries.
        answers["/v1/locations/global/rankings/players"] = (200, _global(TODAY_RANKS))
        [switched] = _day(info, origin, tmp_path, [timedelta(minutes=6)])
        assert switched == Counter(global_reset=1, locations=1, country=2, added_to_discovery=2)
        # Nothing more is due, even long after.
        [later] = _day(info, origin, tmp_path, [timedelta(minutes=40)])
        assert later == Counter()
        assert len(_Game.asked) == 6

        printed = _printed_score(info, capsys)
        with psycopg.connect(info) as connection:
            due = connection.execute(
                "SELECT normalized_tag FROM players"
                " WHERE NOT active AND eligibility_due_at IS NOT NULL ORDER BY normalized_tag"
            ).fetchall()
            pulls = connection.execute(
                "SELECT purpose, count(*), sum(body_bytes) > 0, bool_and(failure IS NULL)"
                " FROM game_ranking_pulls GROUP BY purpose ORDER BY purpose"
            ).fetchall()
    # Both untracked Legend I players in the lists are due a discovery check.
    assert due == [("#2CC",), ("#2RR",)]
    # The old pull is pruned; every pull today kept its raw bytes.
    assert pulls == [
        ("country", 2, True, True), ("global_early", 1, True, True),
        ("global_poll", 1, True, True), ("global_reset", 2, True, True),
        ("locations", 1, True, True),
    ]
    assert printed["unknown_reasons"] == [] and printed["status"] == "scored"
    assert printed["reset"] == RESET.isoformat() and printed["board_version"] == 3
    assert printed["switch_basis"] == "changed"
    assert (printed["countries_expected"], printed["countries_pulled"]) == (2, 2)
    # France: #2LL's stuck row is left out, so one pair, and it breaks. US: three pairs.
    assert (printed["country_pairs"], printed["country_order_breaks"]) == (4, 1)
    assert printed["s1_country_order_breaks_per_100k"] == 25000.0
    assert printed["s2_global_inside_trophy_group"] == 6
    assert printed["global_exact_rank"] == 4
    assert printed["s3_confirmed_entries_contradicted"] == 1
    assert printed["s4_active_legend_i_missing"] == 1 and printed["s4_tags"] == ["#2RR"]
    assert printed["s5_legend_i_on_board_percent"] == 75.0
    assert printed["s6_uncertain_percent"] == 16.67
    assert printed["early_trophies_equal"] == 5
    assert printed["game_faults"] == {"stuck_5000": ["#2LL"], "tag_in_two_countries": []}


def test_a_failed_country_pull_makes_the_day_unknown(
    database_url: str, tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(game_rankings, "REQUEST_SECONDS", 0.0)
    answers = {
        "/v1/locations": (200, LOCATIONS),
        "/v1/locations/global/rankings/players": (200, _global(TODAY_RANKS)),
        "/v1/locations/32000087/rankings/players": (200, FRANCE),
        "/v1/locations/32000249/rankings/players": (503, b'{"reason": "inMaintenance"}'),
    }
    with domain_database(database_url) as info, _game(answers) as origin:
        _seed(info)
        # The collector starts at 05:10, after the early pull's window, with
        # no previous day's pull: the clock judges the switch from 05:20.
        _day(info, origin, tmp_path, [timedelta(minutes=minute) for minute in (10, 20, 21, 23)])
        printed = _printed_score(info, capsys)
    paths = Counter(path.split("?")[0] for path in _Game.asked)
    # Polled at 05:10 and 05:20; the US list tried at 05:20 and again 2 minutes later.
    assert paths["/v1/locations/global/rankings/players"] == 2
    assert paths["/v1/locations/32000249/rankings/players"] == 2
    assert printed["status"] == "unknown"
    assert printed["unknown_reasons"] == ["global_early_missing", "countries_missing:1"]
    assert printed["switch_basis"] == "clock"
    assert (printed["countries_expected"], printed["countries_pulled"]) == (2, 1)
