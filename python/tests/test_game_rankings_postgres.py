"""The daily pulls of the game's ranking lists end in a stored score of the Daily board."""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import json
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import psycopg
from domain_test_support import domain_database, seed_battles, store_observation
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from reconciliation_postgres_support import _processor, _profile
from reset_scenarios_postgres_support import SEASON, _ranked, _story, attacks, day
from reset_settlement_collection_postgres_support import _collector

from clashlens import collector_reset, game_rankings
from clashlens.collector_db import CollectorDatabase
from clashlens.domain import ranked_day_for

RESET = datetime(2026, 10, 11, 5, tzinfo=UTC)
# Each request waits this long for a key before it is sent.
SENT_AFTER = timedelta(seconds=2)
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
    """The global list, in the game's order by trophies, with ``ranks`` as previousRank."""
    board = {tag: value for tag, _position, value, _confidence in BOARD}
    trophies = {**board, **(trophies or {})}
    return _list([(tag, trophies[tag], 40, rank, LEGEND_I) for tag, rank in sorted(
        ranks.items(), key=lambda item: (-trophies[item[0]], item[1]))])


LOCATIONS = json.dumps({"items": [
    {"id": 32000006, "name": "International", "isCountry": False},
    {"id": 32000087, "name": "France", "isCountry": True, "countryCode": "FR"},
    {"id": 32000249, "name": "United States", "isCountry": True, "countryCode": "US"},
], "paging": {"cursors": {}}}).encode()
FRANCE = _list([
    ("#2QQ", 5310, 40, 1, LEGEND_I),
    ("#2PP", 5390, 41, 2, LEGEND_I),  # the game ranks #2QQ above: a break
    ("#2LL", 5000, 0, 3, LEGEND_I),  # the game's stuck 5,000 row; board says 5,200
    ("#2RR", 5050, 12, 0, LEGEND_I),  # an active Legend I player we don't track, rank unknown
    ("#2CC", 5000, 0, 5, LEGEND_I),  # never played this Season, not tracked
])
UNITED_STATES = _list([
    ("#2GG", 5300, 40, 2, LEGEND_I),
    ("#2UU", 5100, 40, 1, LEGEND_I),  # the game ranks uncertain #2UU above: a break
    ("#2VV", 5000, 0, 3, LEGEND_I),  # stuck too, though back at 5,000 on the board
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
    """Tracked players, their published Daily board, and an old pull."""
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
            ) VALUES ('frozen', %s, 3, 'test', 'test', 'published', 0.6, 0, %s)
            RETURNING id
            """,
            (RESET, RESET + timedelta(hours=1)),
        ).fetchone()[0]
        # An army-only correction carried the board into a new publication.
        superseded = connection.execute(
            """
            INSERT INTO boundary_publication_generations (
                boundary_at, generation, ordering_rule_version, freshness_rule_version,
                expected_population_count, expected_population_hash, target_at,
                snapshot_id, snapshot_state, army_state
            ) VALUES (%s, 1, 'test', 'test', 6, repeat('c', 64), %s, %s,
                      'superseded', 'superseded')
            RETURNING id
            """,
            (RESET, RESET, snapshot),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO boundary_publication_generations (
                boundary_at, generation, ordering_rule_version, freshness_rule_version,
                expected_population_count, expected_population_hash, target_at,
                snapshot_id, snapshot_state, supersedes_id
            ) VALUES (%s, 2, 'test', 'test', 6, repeat('c', 64), %s, %s, 'published', %s)
            """,
            (RESET, RESET, snapshot, superseded),
        )
        # Stored battles this Season: #2LL attacked; #2VV defended, so the
        # balanced loss left them at 5,000. Untracked #2CC's was last Season.
        ids["#2CC"] = connection.execute(
            "INSERT INTO players (normalized_tag, active) VALUES ('#2CC', false) RETURNING id"
        ).fetchone()[0]
        for day, attacker, defender in (
            (RESET - timedelta(days=1), "#2LL", "#2PP"),
            (RESET - timedelta(days=2), "#2QQ", "#2VV"),
            (ranked_day_for(RESET).season_start - timedelta(days=1), "#2CC", "#2PP"),
        ):
            connection.execute(
                "INSERT INTO legend_battles (ranked_day_start, attacker_player_id,"
                " defender_player_id) VALUES (%s, %s, %s)",
                (day, ids[attacker], ids[defender]),
            )
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
        # A country directory pulled 29 days ago, which the daily prune removes
        # even when it is the newest one.
        connection.execute(
            """
            INSERT INTO game_ranking_pulls (boundary_at, purpose, requested_at,
                completed_at, http_status)
            VALUES (%s, 'locations', %s, %s, 200)
            """,
            (RESET - timedelta(days=29),) * 3,
        )


def _readings(info: str, archive_server, readings: list[tuple]) -> None:
    """The game's profile answers we saved, (tag, response completed after the
    Reset, trophies), each processed by the real worker a second after it
    arrives, or as long as a fourth item says."""
    database, processor = _processor(info, archive_server)
    try:
        for index, (tag, after, trophies, *saved) in enumerate(readings):
            observation, _ = store_observation(
                info, archive_server, occurrence_key=f"reading-{index}", endpoint="profile",
                body=_profile(trophies, tag), observed_at=RESET + after, normalized_tag=tag,
            )
            processor.process_until_idle(owner="readings")
            with psycopg.connect(info) as connection:
                connection.execute(
                    "UPDATE player_profile_effects SET created_at = %s WHERE observation_id = %s",
                    (RESET + after + (saved or [timedelta(seconds=1)])[0], observation),
                )
    finally:
        database.close()


def _database(info: str) -> CollectorDatabase:
    options = conninfo_to_dict(info)["options"] + " -c role=clashlens_collector"
    return CollectorDatabase(make_conninfo(info, options=options))


def _day(info: str, origin: str, tmp_path, times: list[timedelta],
         reset: datetime = RESET) -> list[Counter[str]]:
    """Run the collector's pulls at each time after the Reset; each is sent SENT_AFTER later."""
    database = _database(info)
    try:
        collector = _collector(origin, database, tmp_path)
        now = [reset]
        client = collector.client
        for fetch in (client.fetch_rankings, client.fetch_locations):
            async def sent_late(*args, fetch=fetch):
                response = await fetch(*args)
                return dataclasses.replace(response, request_started_at=now[0] + SENT_AFTER)
            setattr(client, fetch.__name__, sent_late)

        async def pulls() -> list[Counter[str]]:
            counts = []
            for after in times:
                now[0] = reset + after
                counts.append(await game_rankings.pull_due(
                    collector, game_rankings._Puller(collector, lambda: now[0])
                ))
            return counts

        return asyncio.run(pulls())
    finally:
        database.close()


def _printed_score(info: str, capsys, after: timedelta = timedelta(hours=2, minutes=1)) -> dict:
    """Score and prune at ``after`` the Reset, then what the operator's game-ranking-score command prints."""
    database = _database(info)
    try:
        now = RESET + after
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
    database_url: str, archive_server, tmp_path, capsys, monkeypatch
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
        # Our readings around the country pulls, sent at 05:07:02.
        _readings(info, archive_server, [
            ("#2PP", timedelta(minutes=5), 5390),  # matches France's 5,390
            ("#2QQ", timedelta(minutes=4), 5300),  # France says 5,310: a miss
            ("#2QQ", timedelta(minutes=8), 5310),  # after the pull, so not compared
            ("#2GG", timedelta(minutes=1), 5250),
            ("#2GG", timedelta(minutes=6), 5300),  # the newest by the pull matches
            ("#2UU", timedelta(minutes=3), 5100),  # matches
            ("#2LL", timedelta(minutes=2), 5200),  # a stuck row, left out
            # Arrived at 05:07:00, before the pull, but saved 10 seconds
            # later: the newest reading by the pull is still 5,100.
            ("#2UU", timedelta(minutes=7), 5130, timedelta(seconds=10)),
        ])
        with psycopg.connect(info) as connection:
            ids = dict(connection.execute(
                "SELECT normalized_tag, id FROM players WHERE normalized_tag IN ('#2QQ', '#2PP')"
            ).fetchall())
            # #2QQ's attack ended at 05:05, before France's 05:07:02 request:
            # France's 5,310 is the board's 5,300 plus its 10. Its defense
            # known only from its own log, stamped 04:58, ended by 05:02, so
            # the board and France both count it. #2PP's defense, also known
            # only from its own log, is stamped 05:04: its attack ended by
            # 05:08, maybe after the request, so France's 5,390 against the
            # board's 5,400 stays unexplained.
            seed_battles(connection, ids["#2QQ"], [(RESET + timedelta(minutes=5), 10),
                                                   (RESET - timedelta(minutes=2), -20)])
            seed_battles(connection, ids["#2PP"], [(RESET + timedelta(minutes=4), -10)])
        # 05:00:40 early pull; 05:03 poll still shows yesterday's ranks.
        early, still_yesterday = _day(
            info, origin, tmp_path, [timedelta(seconds=40), timedelta(minutes=3)]
        )
        assert early == Counter(global_early=1, added_to_discovery=0)
        assert still_yesterday == Counter(global_poll=1, added_to_discovery=0)
        # By 05:07 previousRank has switched: the Reset pull, then the countries.
        answers["/v1/locations/global/rankings/players"] = (200, _global(TODAY_RANKS))
        [switched] = _day(info, origin, tmp_path, [timedelta(minutes=7)])
        assert switched == Counter(global_reset=1, locations=1, country=2, added_to_discovery=2)
        # Nothing more is due, even long after.
        [later] = _day(info, origin, tmp_path, [timedelta(minutes=40)])
        assert later == Counter()
        assert len(_Game.asked) == 6

        # First scored 28 days later, the last day its lists are kept.
        printed = _printed_score(info, capsys, timedelta(days=28, hours=2, minutes=1))
        options = conninfo_to_dict(info)["options"] + " -c role=clashlens_collector"
        with psycopg.connect(make_conninfo(info, options=options)) as connection:
            # The newest reading at or before each time; none for an unknown
            # tag or a time before any reading.
            asked = [("#2QQ", 7), ("#2QQ", 9), ("#2GG", 0), ("#2ZZ", 9), ("#2UU", 7.1)]
            readings, received = (connection.execute(
                f"SELECT * FROM {lookup}(%s, %s)",
                ([tag for tag, _ in asked],
                 [RESET + timedelta(minutes=minute) for _, minute in asked]),
            ).fetchall() for lookup in ("clashlens_game_ranking_processed_readings",
                                        "clashlens_game_ranking_readings"))
        # #2UU's 05:07:00 reading arrived by 05:07:06 but was saved after it.
        assert sorted(readings) == [("#2QQ", 5300), ("#2QQ", 5310), ("#2UU", 5100)]
        assert sorted(received) == [("#2QQ", 5300), ("#2QQ", 5310), ("#2UU", 5130)]
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
        ("global_poll", 1, True, True), ("global_reset", 1, True, True),
        ("locations", 1, True, True),
    ]
    assert printed["unknown_reasons"] == [] and printed["status"] == "scored"
    assert printed["reset"] == RESET.isoformat() and printed["board_version"] == 3
    # Times are when each request was sent, not when it was asked for.
    assert printed["global_early_at"] == (RESET + timedelta(seconds=40) + SENT_AFTER).isoformat()
    assert printed["global_reset_at"] == (RESET + timedelta(minutes=7) + SENT_AFTER).isoformat()
    assert (printed["countries_expected"], printed["countries_pulled"]) == (2, 2)
    # Each list has 4 players with a known rank, too few to tell whether it
    # has switched, so both are scored as they are.
    assert printed["countries_not_switched"] == []
    assert printed["countries_too_small_to_tell"] == 2
    # The stuck rows #2LL and #2VV are left out of every number.
    # France: one pair, and it breaks. US: one pair, and it breaks too.
    assert (printed["country_pairs"], printed["country_order_breaks"]) == (2, 2)
    assert printed["s1_country_order_breaks_per_100k"] == 100000.0
    assert (printed["global_ranked"], printed["s2_global_inside_trophy_group"]) == (4, 4)
    assert printed["global_exact_rank"] == 2
    # Both confirmed entries in France's break; the US break has uncertain
    # #2UU, which may be the wrong one, so confirmed #2GG is not counted.
    assert printed["s3_confirmed_entries_contradicted"] == 2
    assert printed["s4_active_legend_i_missing"] == 1 and printed["s4_tags"] == ["#2RR"]
    # #2PP, #2GG and #2UU match their list's trophies; #2QQ doesn't.
    assert (printed["live_trophies_compared"], printed["live_trophies_matched"]) == (4, 3)
    assert printed["s5_live_trophies_match_percent"] == 75.0
    # By the old definition #2UU's reading saved after the pull counted too.
    assert (printed["received_live_trophies_compared"],
            printed["received_live_trophies_matched"]) == (4, 2)
    assert printed["s5_received_live_trophies_match_percent"] == 50.0
    # One uncertain entry among the four left once the faults are out.
    assert (printed["s6_uncertain_percent"], printed["board_entries"]) == (25.0, 4)
    assert printed["board_measured_coverage"] == 0.6
    assert printed["early_trophies_equal"] == 3
    assert printed["game_faults"] == {
        "stuck_5000": ["#2LL", "#2VV"], "tag_in_two_countries": [],
    }
    # Against the board: #2GG and #2UU equal their list; #2QQ's attack after
    # 05:00 explains its difference; #2PP's does not.
    assert (printed["daily_trophies_compared"], printed["daily_trophies_equal"],
            printed["daily_trophies_equal_after_battles"]) == (4, 2, 3)
    assert printed["daily_trophies_unexplained_tags"] == ["#2PP"]


def test_an_early_pull_already_switched_still_pulls_every_country_once(
    database_url: str, tmp_path, capsys, monkeypatch
) -> None:
    # On 12 Oct 2026 the 05:00:35 pull already held that Reset's ranks, so no
    # poll differed from it, no country list was pulled and 290 Legend I
    # players in them stayed unknown to Clash Lens.
    monkeypatch.setattr(game_rankings, "REQUEST_SECONDS", 0.0)
    answers = {
        "/v1/locations": (200, LOCATIONS),
        "/v1/locations/global/rankings/players": (200, _global(TODAY_RANKS)),
        "/v1/locations/32000087/rankings/players": (200, FRANCE),
        "/v1/locations/32000249/rankings/players": (200, UNITED_STATES),
    }
    with domain_database(database_url) as info, _game(answers) as origin:
        _seed(info)
        early, later = _day(
            info, origin, tmp_path, [timedelta(seconds=40), timedelta(minutes=3)]
        )
        printed = _printed_score(info, capsys)
        with psycopg.connect(info) as connection:
            due = connection.execute(
                "SELECT normalized_tag FROM players"
                " WHERE NOT active AND eligibility_due_at IS NOT NULL ORDER BY normalized_tag"
            ).fetchall()
    assert early == Counter(global_early=1, locations=1, country=2, added_to_discovery=2)
    assert later == Counter()
    # One global list, no poll, and each country once.
    assert [path.split("?")[0] for path in _Game.asked] == [
        "/v1/locations/global/rankings/players", "/v1/locations",
        "/v1/locations/32000087/rankings/players", "/v1/locations/32000249/rankings/players",
    ]
    assert due == [("#2CC",), ("#2RR",)]
    assert printed["status"] == "scored"
    assert printed["global_reset_at"] == printed["global_early_at"]


def test_a_list_never_seen_to_switch_leaves_the_day_unknown(
    database_url: str, tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(game_rankings, "REQUEST_SECONDS", 0.0)
    answers = {
        "/v1/locations": (200, LOCATIONS),
        "/v1/locations/global/rankings/players": (200, _global(YESTERDAY_RANKS)),
        "/v1/locations/32000087/rankings/players": (200, FRANCE),
        "/v1/locations/32000249/rankings/players": (200, UNITED_STATES),
    }
    with domain_database(database_url) as info, _game(answers) as origin:
        _seed(info)
        _day(info, origin, tmp_path, [timedelta(seconds=40)])
        # The first day collected: until 06:00 the list holds yesterday's ranks,
        # except two that turn unknown, which is no switch.
        answers["/v1/locations/global/rankings/players"] = (
            200, _global({**YESTERDAY_RANKS, "#2PP": 0, "#2QQ": 0}),
        )
        _day(info, origin, tmp_path, [timedelta(minutes=minute) for minute in (3, 20, 59, 60)])
        printed = _printed_score(info, capsys)
        with psycopg.connect(info) as connection:
            [(kept_old,)] = connection.execute(
                "SELECT count(*) FROM game_ranking_pulls WHERE boundary_at < %s",
                (RESET - timedelta(days=28),),
            ).fetchall()
    assert kept_old == 0
    # The early pull and polls at 05:03, 05:20 and 05:59; no country list.
    assert [path.split("?")[0] for path in _Game.asked] == [
        "/v1/locations/global/rankings/players"
    ] * 4
    assert printed["status"] == "unknown"
    assert printed["unknown_reasons"] == ["previous_rank_not_switched", "locations_missing"]
    assert printed["global_reset_at"] is None and printed["countries_pulled"] == 0


def test_a_failed_country_pull_makes_the_day_unknown(
    database_url: str, tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(game_rankings, "REQUEST_SECONDS", 0.0)
    answers = {
        "/v1/locations": (200, LOCATIONS),
        "/v1/locations/global/rankings/players": (200, _global(YESTERDAY_RANKS)),
        "/v1/locations/32000087/rankings/players": (200, FRANCE),
        "/v1/locations/32000249/rankings/players": (503, b'{"reason": "inMaintenance"}'),
    }
    with domain_database(database_url) as info, _game(answers) as origin:
        _seed(info)
        _day(info, origin, tmp_path, [timedelta(seconds=40)])
        # The first day collected switches against its own early pull.
        answers["/v1/locations/global/rankings/players"] = (200, _global(TODAY_RANKS))
        # At 05:03 another job holds #2RR's record, so France's pull can't
        # hand #2RR on to discovery and is tried again 2 minutes later.
        with psycopg.connect(info) as holder:
            holder.execute("INSERT INTO players (normalized_tag, active) VALUES ('#2RR', false)")
            holder.commit()
            holder.execute("SELECT 1 FROM players WHERE normalized_tag = '#2RR' FOR UPDATE")
            _day(info, origin, tmp_path, [timedelta(minutes=3)])
        # By the retry #2PP's trophies have moved; the board is compared
        # with the first list read, the one whose discovery failed.
        answers["/v1/locations/32000087/rankings/players"] = (
            200, FRANCE.replace(b'"trophies": 5390', b'"trophies": 5400'),
        )
        _day(info, origin, tmp_path, [timedelta(minutes=minute) for minute in (4, 5.5)])
        printed = _printed_score(info, capsys)
        with psycopg.connect(info) as connection:
            [(rr_due,)] = connection.execute(
                "SELECT eligibility_due_at IS NOT NULL FROM players WHERE normalized_tag = '#2RR'"
            ).fetchall()
    paths = Counter(path.split("?")[0] for path in _Game.asked)
    # The early pull, then switched at 05:03; each country tried then and 2 minutes later.
    assert paths["/v1/locations/global/rankings/players"] == 2
    assert paths["/v1/locations/32000087/rankings/players"] == 2
    assert paths["/v1/locations/32000249/rankings/players"] == 2
    assert rr_due
    assert printed["status"] == "unknown"
    assert printed["unknown_reasons"] == ["countries_missing:1"]
    assert (printed["countries_expected"], printed["countries_pulled"]) == (2, 1)
    assert (printed["daily_trophies_compared"], printed["daily_trophies_equal"]) == (2, 0)


def test_an_expired_country_list_leaves_an_empty_board_unknown(
    database_url: str, tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(game_rankings, "REQUEST_SECONDS", 0.0)
    answers = {
        "/v1/locations": (503, b'{"reason": "inMaintenance"}'),
        "/v1/locations/global/rankings/players": (200, _global(YESTERDAY_RANKS)),
        "/v1/locations/32000087/rankings/players": (200, FRANCE),
        "/v1/locations/32000249/rankings/players": (200, UNITED_STATES),
    }
    with domain_database(database_url) as info, _game(answers) as origin:
        with psycopg.connect(info) as connection:
            # A published board every member was left out of.
            connection.execute(
                """
                INSERT INTO leaderboard_snapshots (
                    snapshot_kind, boundary_at, version, ordering_rule_version,
                    freshness_rule_version, state, measured_coverage,
                    stale_entry_count, published_at
                ) VALUES ('frozen', %s, 1, 'test', 'test', 'published', 0, 0, %s)
                """,
                (RESET, RESET + timedelta(hours=1)),
            )
            # The only country list was saved 8 days ago.
            pulled = RESET - timedelta(days=8) + timedelta(minutes=5)
            connection.execute(
                """
                INSERT INTO game_ranking_pulls (boundary_at, purpose, requested_at,
                    completed_at, http_status, body_gzip)
                VALUES (%s, 'locations', %s, %s, 200, %s)
                """,
                (RESET - timedelta(days=8), pulled, pulled, gzip.compress(LOCATIONS)),
            )
        _day(info, origin, tmp_path, [timedelta(seconds=40)])
        answers["/v1/locations/global/rankings/players"] = (200, _global(TODAY_RANKS))
        _day(info, origin, tmp_path, [timedelta(minutes=3)])
        printed = _printed_score(info, capsys)
    # Switched at 05:03; the refresh failed, so no country list was pulled.
    assert [path.split("?")[0] for path in _Game.asked] == [
        "/v1/locations/global/rankings/players",
        "/v1/locations/global/rankings/players",
        "/v1/locations",
    ]
    assert printed["status"] == "unknown"
    assert printed["unknown_reasons"] == ["locations_missing"]
    assert (printed["countries_expected"], printed["countries_pulled"]) == (0, 0)
    assert (printed["board_entries"], printed["board_measured_coverage"]) == (0, 0.0)
    assert printed["global_on_board"] == 0


def test_a_resets_ranking_check_steps_from_its_saved_pulls() -> None:
    """Early list, then polls until R's list is seen, then countries until
    R+2 h, then scoring; a list never seen stops the pulls."""
    step, steps = game_rankings.ranking_step, game_rankings.RankingStep

    def pull(purpose: str, after: timedelta, body: bytes | None, failure: str | None = None):
        return game_rankings.Pull(purpose, None, RESET + after, failure, body)

    failed = pull("global_early", timedelta(seconds=30), None, "timeout")
    early = pull("global_early", timedelta(seconds=30), _global(YESTERDAY_RANKS))
    seen = pull("global_reset", timedelta(minutes=6), None)
    already = pull("global_early", timedelta(seconds=30), _global(TODAY_RANKS))
    assert step(RESET, RESET + timedelta(seconds=10), []) is steps.EARLY
    assert step(RESET, RESET + timedelta(minutes=2), [failed]) is steps.EARLY
    assert step(RESET, RESET + timedelta(minutes=6), [failed]) is steps.MISSED
    assert step(RESET, RESET + timedelta(minutes=1), [early]) is steps.POLLING
    assert step(RESET, RESET + timedelta(minutes=10), [early, seen]) is steps.COUNTRIES
    assert step(RESET, RESET + timedelta(minutes=1), [already]) is steps.COUNTRIES
    assert step(RESET, RESET + timedelta(minutes=61), [early]) is steps.MISSED
    assert step(RESET, RESET + timedelta(hours=2), [early]) is steps.SCORING
    assert step(RESET, RESET + timedelta(hours=2), [early, seen]) is steps.SCORING


# Two countries of 10 board players each, below the six of BOARD.
GERMANY = [f"#9{char}{char}" for char in "PYLQGRJCUV"]
AUSTRIA = [f"#8{char}{char}" for char in "PYLQGRJCUV"]
TWO_COUNTRIES = json.dumps({"items": [
    {"id": 32000094, "name": "Germany", "isCountry": True, "countryCode": "DE"},
    {"id": 32000017, "name": "Austria", "isCountry": True, "countryCode": "AT"},
], "paging": {"cursors": {}}}).encode()
# The ranks each list gave at the Reset before: neighbours swapped.
BEFORE_RANKS = [2, 1, 4, 3, 6, 5, 8, 7, 10, 9]


def _country(tags: list[str], first_trophies: int, ranks: list[int]) -> bytes:
    """A country list in the game's order, 10 trophies apart, with ``ranks`` as previousRank."""
    return _list([(tag, first_trophies - 10 * index, 40, rank, LEGEND_I)
                  for index, (tag, rank) in enumerate(zip(tags, ranks, strict=True))])


def test_a_country_list_not_yet_switched_is_pulled_again_and_left_out_of_the_order_score(
    database_url: str, tmp_path, capsys, monkeypatch
) -> None:
    # On 10 Nov 2026 the Austria and Bangladesh lists pulled at 05:00:38 and
    # 05:00:40 still gave every player the 9 November rank: 4,376 of the
    # score's 4,379 order breaks and all 250 contradicted entries.
    monkeypatch.setattr(game_rankings, "REQUEST_SECONDS", 0.0)
    answers = {
        "/v1/locations": (200, TWO_COUNTRIES),
        "/v1/locations/global/rankings/players": (200, _global(TODAY_RANKS)),
        "/v1/locations/32000094/rankings/players": (200, _country(GERMANY, 4990, BEFORE_RANKS)),
        "/v1/locations/32000017/rankings/players": (200, _country(AUSTRIA, 4890, BEFORE_RANKS)),
    }
    with domain_database(database_url) as info, _game(answers) as origin:
        _seed(info)
        with psycopg.connect(info) as connection:
            snapshot, observation = connection.execute(
                "SELECT (SELECT id FROM leaderboard_snapshots),"
                " (SELECT id FROM collector_observations WHERE occurrence_key = 'seed')"
            ).fetchone()
            # Our board agrees with the game's trophies in both countries.
            for position, tag in enumerate(GERMANY + AUSTRIA, len(BOARD) + 1):
                connection.execute(
                    """
                    WITH player AS (
                        INSERT INTO players (normalized_tag, active) VALUES (%s, true) RETURNING id
                    )
                    INSERT INTO leaderboard_snapshot_entries (
                        snapshot_id, position, player_id, trophies, trophy_observation_id,
                        trophy_observed_at, observation_age_seconds, freshness, confidence,
                        tie_hash
                    ) SELECT %s, %s, id, %s, %s, %s, 0, 'fresh', 'confirmed', repeat('c', 64)
                    FROM player
                    """,
                    (tag, snapshot, position, 4990 - 10 * (position - len(BOARD) - 1),
                     observation, RESET),
                )
            # Each country's list from the Reset before, which had switched.
            for location, code, body in ((32000094, "DE", _country(GERMANY, 4950, BEFORE_RANKS)),
                                         (32000017, "AT", _country(AUSTRIA, 4850, BEFORE_RANKS))):
                pulled = RESET - timedelta(days=1) + timedelta(seconds=50)
                connection.execute(
                    """
                    INSERT INTO game_ranking_pulls (boundary_at, purpose, location_id,
                        country_code, requested_at, completed_at, http_status, body_gzip)
                    VALUES (%s, 'country', %s, %s, %s, %s, 200, %s)
                    """,
                    (RESET - timedelta(days=1), location, code, pulled, pulled,
                     gzip.compress(body)),
                )
        # 05:00:40: the global list has switched, both countries not yet.
        [first] = _day(info, origin, tmp_path, [timedelta(seconds=40)])
        # By 05:03 Germany has switched; Austria never does before 06:00.
        answers["/v1/locations/32000094/rankings/players"] = (
            200, _country(GERMANY, 4990, list(range(1, 11))),
        )
        retries = _day(info, origin, tmp_path, [timedelta(minutes=minute)
                                               for minute in (2, 3, 5.5, 59, 61, 90)])
        printed = _printed_score(info, capsys)
        with psycopg.connect(info) as connection:
            saved = connection.execute(
                "SELECT country_code, failure FROM game_ranking_pulls"
                " WHERE purpose = 'country' AND boundary_at = %s ORDER BY requested_at, country_code",
                (RESET,),
            ).fetchall()
    assert first == Counter(global_early=1, locations=1, not_switched=2, added_to_discovery=0)
    # Each not yet switched list again 2 minutes after its last try, Austria
    # until 06:00.
    assert retries == [Counter(), Counter(country=1, not_switched=1, added_to_discovery=0),
                       Counter(not_switched=1, added_to_discovery=0),
                       Counter(not_switched=1, added_to_discovery=0), Counter(), Counter()]
    assert saved == [("AT", "not_switched"), ("DE", "not_switched"), ("AT", "not_switched"),
                     ("DE", None), ("AT", "not_switched"), ("AT", "not_switched")]
    assert printed["status"] == "scored" and printed["unknown_reasons"] == []
    assert (printed["countries_expected"], printed["countries_pulled"]) == (2, 2)
    # Austria is named, and its yesterday's ranks count no pairs; Germany's
    # switched list agrees with the board in all 45 of its pairs.
    assert printed["countries_not_switched"] == ["AT"]
    assert printed["countries_too_small_to_tell"] == 0
    assert (printed["country_pairs"], printed["country_order_breaks"]) == (45, 0)
    assert printed["s3_confirmed_entries_contradicted"] == 0
    # Its trophies were the game's at the Reset all along, so they still count.
    assert (printed["daily_trophies_compared"], printed["daily_trophies_equal"],
            printed["daily_trophies_equal_after_battles"]) == (20, 20, 20)


def test_a_country_list_is_judged_switched_against_the_reset_before() -> None:
    """A list is switched when its ranks moved from both the ranks and the
    order of the same country's list from the Reset before; too few rows to
    compare can't tell."""
    judge = game_rankings.country_switched

    def rows(ranks: list[int], tags: list[str] = GERMANY) -> list[game_rankings.Row]:
        return game_rankings.read_rows(_country(tags, 4990, ranks))

    switched_before = rows(BEFORE_RANKS)
    # The list from the Reset before had not switched either: its order was
    # that Reset's ranks, its ranks the Reset's before.
    in_that_order = [tag for _rank, tag in sorted(zip(BEFORE_RANKS, GERMANY, strict=True))]
    stale_before = rows([3, 4, 1, 2, 7, 8, 5, 6, 9, 10], in_that_order)
    assert judge(rows(BEFORE_RANKS), switched_before) is False
    assert judge(rows(list(range(1, 11))), switched_before) is True
    assert judge(rows([3, 4, 1, 2, 7, 8, 5, 6, 9, 10]), switched_before) is True
    assert judge(rows(BEFORE_RANKS), stale_before) is False
    assert judge(rows([3, 4, 1, 2, 7, 8, 5, 6, 9, 10]), stale_before) is True
    # In its own order already: switched, with nothing to compare.
    assert judge(rows(list(range(1, 11))), None) is True
    assert judge(rows(BEFORE_RANKS), None) is None
    # Nine players with a known rank are too few.
    assert judge(rows([*BEFORE_RANKS[:9], 0]), switched_before) is None
    assert judge(rows(BEFORE_RANKS), rows(BEFORE_RANKS, AUSTRIA)) is None


FRANCE_PATH = "/v1/locations/32000087/rankings/players"
UNITED_STATES_PATH = "/v1/locations/32000249/rankings/players"


def test_a_midday_pull_hands_new_legend_i_players_to_discovery_and_is_never_scored(
    database_url: str, tmp_path, capsys, monkeypatch
) -> None:
    """At 17:00 every country's list is pulled once more, only so a player
    who reached Legend I during the day is due a discovery check before the
    next Reset's sweep. The score reads the morning's lists alone."""
    monkeypatch.setattr(game_rankings, "REQUEST_SECONDS", 0.0)
    answers = {
        "/v1/locations": (200, LOCATIONS),
        "/v1/locations/global/rankings/players": (200, _global(TODAY_RANKS)),
        FRANCE_PATH: (200, FRANCE),
        UNITED_STATES_PATH: (200, UNITED_STATES),
    }
    with domain_database(database_url) as info, _game(answers) as origin:
        _seed(info)
        [morning, before_midday] = _day(
            info, origin, tmp_path, [timedelta(seconds=40), timedelta(hours=11, minutes=59)])
        # By 17:00 #2YY has reached Legend I and France lists them; the
        # United States' list fails once.
        answers[FRANCE_PATH] = (200, _list([
            ("#2QQ", 5350, 41, 1, LEGEND_I), ("#2YY", 5040, 1, 0, LEGEND_I)]))
        answers[UNITED_STATES_PATH] = (503, b"{}")
        [midday] = _day(info, origin, tmp_path, [timedelta(hours=12)])
        answers[UNITED_STATES_PATH] = (200, UNITED_STATES)
        # Each request goes out 2 seconds after its tick, so the retry is
        # due from 17:02:02.
        [too_soon, retried, done, after_window] = _day(info, origin, tmp_path, [
            timedelta(hours=12, minutes=2), timedelta(hours=12, minutes=3),
            timedelta(hours=12, minutes=30), timedelta(hours=13)])
        printed = _printed_score(info, capsys, timedelta(hours=13))
        with psycopg.connect(info) as connection:
            due = connection.execute(
                "SELECT normalized_tag FROM players"
                " WHERE NOT active AND eligibility_due_at IS NOT NULL ORDER BY normalized_tag"
            ).fetchall()
            pulls = connection.execute(
                "SELECT purpose, count(*), count(*) FILTER (WHERE failure IS NULL)"
                " FROM game_ranking_pulls WHERE purpose IN ('country', 'country_midday')"
                " GROUP BY purpose ORDER BY purpose"
            ).fetchall()
    assert morning == Counter(global_early=1, locations=1, country=2, added_to_discovery=2)
    assert before_midday == Counter()
    # #2RR and #2CC are still due from the morning; #2YY is new.
    assert midday == Counter(country_midday=1, failed=1, added_to_discovery=1)
    assert too_soon == Counter()
    assert retried == Counter(country_midday=1, added_to_discovery=0)
    assert done == after_window == Counter()
    assert due == [("#2CC",), ("#2RR",), ("#2YY",)]
    assert pulls == [("country", 2, 2), ("country_midday", 3, 2)]
    # The score is the morning's: #2YY is not missing from the board.
    assert printed["status"] == "scored"
    assert (printed["countries_expected"], printed["countries_pulled"]) == (2, 2)
    assert printed["last_country_at"] == (RESET + timedelta(seconds=40) + SENT_AFTER).isoformat()
    assert printed["s4_tags"] == ["#2RR"]


TRACKED, NEW = "#2PP", "#2YY"


def _discovery_checks(info: str) -> list[str]:
    """The players the collector's next discovery pass checks; it runs on
    the database's clock, as the due times are."""
    with psycopg.connect(info) as connection:
        connection.execute("SELECT clashlens_admit_discovery_profiles(clock_timestamp())")
        return [str(tag) for (tag,) in connection.execute(
            "SELECT normalized_tag FROM collector_work"
            " WHERE kind = 'discovery_profile' AND status = 'pending' ORDER BY normalized_tag")]


def _tracked(info: str) -> list[str]:
    with psycopg.connect(info) as connection:
        return [str(tag) for (tag,) in connection.execute(
            "SELECT normalized_tag FROM players WHERE active ORDER BY normalized_tag")]


def _swept(info: str, boundary: datetime) -> list[str]:
    """The players the collector's Reset sweep at ``boundary`` would take;
    the story then sweeps them itself."""
    with psycopg.connect(info) as connection:
        connection.execute("SELECT 1")
        sweep = collector_reset.begin_reset(connection, boundary)
        members = [str(tag) for (tag,) in connection.execute(
            "SELECT player.normalized_tag FROM collector_reset_sweeps AS sweep"
            " CROSS JOIN unnest(sweep.member_ids) AS member (id)"
            " JOIN players AS player ON player.id = member.id"
            " WHERE sweep.id = %s ORDER BY 1", (sweep,))]
        connection.rollback()
    return members


def test_a_player_first_listed_during_the_day_is_on_the_next_resets_first_board(
    database_url: str, archive_server, tmp_path, monkeypatch
) -> None:
    """On 10 November 2026, 197 Legend I players were first seen in the
    country lists pulled at 05:00 to 05:02, after the Reset had swept its
    players, and joined its board from 05:56 through rebuilds. TRACKED is
    tracked all day. NEW, untracked, attacks for 40 at 07:00 and is first
    listed in France's list at 17:00. The midday pull hands NEW to discovery,
    its check reads NEW in Legend I, so NEW is tracked, swept at the next
    Reset and ranked on that Reset's first board."""
    start, end = day(2), day(3)
    monkeypatch.setattr(game_rankings, "REQUEST_SECONDS", 0.0)
    answers = {
        "/v1/locations": (200, json.dumps({"items": [
            {"id": 32000087, "name": "France", "isCountry": True, "countryCode": "FR"},
        ], "paging": {"cursors": {}}}).encode()),
        "/v1/locations/global/rankings/players": (
            200, _list([(TRACKED, 5100, 40, 1, LEGEND_I)])),
        FRANCE_PATH: (200, _list([(TRACKED, 5100, 40, 1, LEGEND_I)])),
    }
    with domain_database(database_url) as info, _game(answers) as origin:
        story = _story(info, archive_server, monkeypatch, TRACKED, NEW)
        story.players[TRACKED].battles = attacks(start, 40)
        story.players[NEW].battles = attacks(start + timedelta(hours=1), 40)
        story.reset(start, {TRACKED: (5100, SEASON)})
        with psycopg.connect(info) as connection:
            connection.execute("UPDATE players SET active = true WHERE normalized_tag = %s",
                               (TRACKED,))
        [morning] = _day(info, origin, tmp_path, [timedelta(seconds=40)], start)
        answers[FRANCE_PATH] = (200, _list([
            (TRACKED, 5140, 41, 1, LEGEND_I), (NEW, 5040, 1, 0, LEGEND_I)]))
        [midday] = _day(info, origin, tmp_path, [timedelta(hours=12)], start)
        checked = _discovery_checks(info)
        story.reads(start + timedelta(hours=12, minutes=2), dict.fromkeys(checked, 5040))
        story.run_worker()
        # The battle logs' opponents are tracked too; the story plays only
        # its own two players.
        totals = {TRACKED: 5140, NEW: 5040}
        story.reads(end - timedelta(minutes=10),
                    {tag: totals[tag] for tag in _tracked(info) if tag in totals})
        story.run_worker()
        swept = [tag for tag in _swept(info, end) if tag in totals]
        story.reset(end, {tag: (totals[tag], SEASON) for tag in swept})
        unfinished = story.unfinished_jobs()
        members = story.board_members(end)
        board = story.served_board(end)
        with psycopg.connect(info) as connection:
            versions = connection.execute(
                "SELECT count(*) FROM boundary_publication_generations WHERE boundary_at = %s",
                (end,)).fetchone()[0]
    # One board version, and NEW is on it.
    assert set(members) == {TRACKED, NEW}
    assert versions == 1
    assert _ranked(board) == {TRACKED: 5140, NEW: 5040}
    assert unfinished == []
    assert morning == Counter(global_early=1, locations=1, country=1, added_to_discovery=0)
    assert midday == Counter(country_midday=1, added_to_discovery=1)
    assert checked == [NEW]
    assert swept == [TRACKED, NEW]
