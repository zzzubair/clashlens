"""A Reset's manifest frozen for the whole population at once is the one the
per-player freeze built: the same rows and the same digest, so nothing a
published board froze changes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import psycopg
from boundary_manifest_postgres_support import (
    _ARCHIVE,
    DAY_2_RESET,
    PLAYERS,
    _build_board,
    _october,
    _seed_board,
    _seed_days,
    per_player_inputs,
    seed_population,
)
from domain_test_support import domain_database, store_observation, text

from clashlens import boundary
from clashlens.analytics import SNAPSHOT_ORDERING_RULE_VERSION
from clashlens.db import Database
from clashlens.domain import RANKED_DAY_DURATION, ranked_day_for


def _digest(generation: int, artifact_kind: str, rule_versions: Any, rows: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "generation": generation,
                "artifact_kind": artifact_kind,
                "rule_versions": rule_versions,
                "rows": rows,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def test_whole_population_freeze_matches_the_per_player_freeze(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        generation_id = seed_population(connection_info, PLAYERS)
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                for artifact_kind in ("snapshot", "army"):
                    expected, season_inputs = per_player_inputs(
                        connection,
                        generation_id=generation_id,
                        artifact_kind=artifact_kind,
                    )
                    manifest_id, digest = boundary._freeze_boundary_manifest(
                        connection,
                        generation_id=generation_id,
                        artifact_kind=artifact_kind,
                    )
                    rule_versions = connection.execute(
                        "SELECT rule_versions FROM boundary_publication_manifests WHERE id = %s",
                        (manifest_id,),
                    ).fetchone()[0]
                    assert rule_versions == {
                        **{
                            key: rule_versions[key]
                            for key in (
                                "ordering_rule_version",
                                "freshness_rule_version",
                                "analytics_rule_version",
                            )
                        },
                        **(
                            {"season_inputs": season_inputs}
                            if season_inputs is not None
                            else {}
                        ),
                    }
                    assert digest == _digest(1, artifact_kind, rule_versions, expected)
                    stored = connection.execute(
                        """
                        SELECT ordinal, player_id, ranked_day_version_id,
                               input_hash, classification, unavailable_reason,
                               input_identity
                        FROM boundary_publication_manifest_rows
                        WHERE manifest_id = %s ORDER BY ordinal
                        """,
                        (manifest_id,),
                    ).fetchall()
                    assert [
                        (
                            row[0], row[1], row[2], text(row[3]), text(row[4]),
                            text(row[5]), _canonical(row[6]),
                        )
                        for row in stored
                    ] == [
                        (
                            ordinal,
                            identity["player_id"],
                            identity["ranked_day_version_id"],
                            identity["input_hash"],
                            identity["classification"],
                            "reset_baseline_failed"
                            if identity["classification"] == "Unavailable"
                            else None,
                            _canonical(identity),
                        )
                        for ordinal, identity in enumerate(expected, start=1)
                    ]
                    _assert_every_case_seeded(artifact_kind, expected)
                    if artifact_kind == "army":
                        # Reports on the battles moved sides are on now.
                        assert season_inputs is not None and any(
                            evidence_id >= 2 * 9000000
                            for evidence_id in season_inputs["evidence_ids"]
                        )
        finally:
            database.close()


def _assert_every_case_seeded(
    artifact_kind: str, rows: list[dict[str, Any]]
) -> None:
    classifications = {row["classification"] for row in rows}
    if artifact_kind == "snapshot":
        assert classifications == {
            "Complete", "Partial", "Failed", "Missing", "Unavailable",
            "Inconsistent", "Malformed", "Pending",
        }
        assert {row["snapshot_quality"] for row in rows} >= {
            "eligible", "invalid", "season_reset_pending", "malformed",
            "conflicting", "missing", "unavailable", "partial",
        }
        assert sum(row["official_rank"] is not None for row in rows) > 50
        return
    assert classifications >= {
        "Complete", "Partial", "Failed", "Missing", "Unavailable",
        "Inconsistent", "Malformed", "Pending",
    }
    assert any(
        (9000000 + row["player_id"]) * 2 in row.get("evidence_ids", []) for row in rows
    )
    assert any(
        (9000000 + row["player_id"]) * 2 in row.get("decode_ids", []) for row in rows
    )


def test_board_leaves_out_players_whose_profile_went_missing_before_the_reset(
    database_url: str,
) -> None:
    readings = [
        ("#PJ22PJPQJ", 5280, _october(5, 7, 51)),  # KURDiSTAN, 404 at 08:35
        ("#8LLLG2V99", 5277, _october(6, 6, 19)),  # Eason, 404 later that day
        ("#PPVYC88R", 5274, _october(6, 22, 31)),  # ZOOS Yatta
        ("#RECOVERED", 5250, _october(6, 6)),  # 404, then a success
        ("#AFTERRESET", 5240, _october(6, 8)),  # 404 only after the Reset
        ("#SERVERERROR", 5230, _october(6, 8)),  # a timeout says nothing
        ("#OLDREADING", 5220, _october(6, 4)),  # then only server errors
    ]
    responses = [
        ("#PJ22PJPQJ", 404, _october(5, 8, 35)),
        ("#8LLLG2V99", 404, _october(6, 8, 6)),
        ("#RECOVERED", 404, _october(6, 7)),
        ("#RECOVERED", 200, _october(6, 9)),
        ("#AFTERRESET", 404, _october(7, 6)),
        ("#SERVERERROR", 503, _october(6, 10)),
        ("#OLDREADING", 500, _october(6, 12)),
    ]
    with domain_database(database_url) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        for tag, status, at in responses:
            store_observation(
                connection_info, _ARCHIVE, occurrence_key=f"{tag}-{at.isoformat()}",
                endpoint="profile", body=f"{status} {tag}".encode(),
                observed_at=at, normalized_tag=tag, http_status=status,
            )
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                manifest_id, _ = boundary._freeze_boundary_manifest(
                    connection, generation_id=generation_id,
                    artifact_kind="snapshot",
                )
                rows = connection.execute(
                    """
                    SELECT input_identity->'profile_snapshot'->>'tag',
                           input_identity->>'snapshot_quality'
                    FROM boundary_publication_manifest_rows
                    WHERE manifest_id = %s
                    ORDER BY (input_identity->'profile_snapshot'->>'trophies')::int DESC
                    """,
                    (manifest_id,),
                ).fetchall()
        finally:
            database.close()
    assert [(text(tag), text(quality)) for tag, quality in rows] == [
        ("#PJ22PJPQJ", "profile_not_found"),
        ("#8LLLG2V99", "profile_not_found"),
        ("#PPVYC88R", "eligible"),
        ("#RECOVERED", "eligible"),
        ("#AFTERRESET", "eligible"),
        ("#SERVERERROR", "eligible"),
        ("#OLDREADING", "eligible"),
    ]
    # The board ranks only eligible rows, by trophies, so Yatta is first.


def test_board_rebuild_queues_one_correction_per_board_still_ranking_a_missing_player(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        generation_id = _seed_board(
            connection_info,
            [
                ("#8LLLG2V99", 5277, _october(6, 6, 19)),
                ("#PPVYC88R", 5274, _october(6, 22, 31)),
            ],
        )
        database = Database(connection_info)
        try:
            # Frozen before its "not found" response was saved, as boards
            # built before this rule were.
            with database.pool.connection() as connection:
                boundary._freeze_boundary_manifest(
                    connection, generation_id=generation_id,
                    artifact_kind="snapshot",
                )
            store_observation(
                connection_info, _ARCHIVE, occurrence_key="eason-404",
                endpoint="profile", body=b"404", observed_at=_october(6, 8, 6),
                normalized_tag="#8LLLG2V99", http_status=404,
            )
            season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
            board = {
                "boundary_at": DAY_2_RESET.isoformat(),
                "generation": 1,
                "profile_not_found": 1,
                "late_battles": 0,
                "reordered": False, "members_changed": 0, "official_order": 0,
            }
            reports = [
                boundary.queue_board_rebuilds(database, season, queue=queue)
                for queue in (False, True, True)
            ]
            assert [report["boards"] for report in reports] == [
                [{**board, "correction": "not_queued"}],
                [{**board, "correction": "queued"}],
                [{**board, "correction": "already_queued"}],
            ]
            # The Season before is never read.
            earlier = str(int(season) - 28 * 86400)
            assert boundary.queue_board_rebuilds(database, earlier, queue=True)[
                "boards"
            ] == []
            with database.pool.connection() as connection:
                corrections = connection.execute(
                    """
                    SELECT boundary_at, source_generation_id, affected_artifacts,
                           pending_inputs, state
                    FROM boundary_publication_corrections
                    """
                ).fetchall()
        finally:
            database.close()
    assert [
        (row[0], row[1], sorted(text(value) for value in row[2]), row[3], text(row[4]))
        for row in corrections
    ] == [(DAY_2_RESET, generation_id, ["army", "snapshot"], [], "queued")]


def test_board_adds_the_battles_after_each_reading(database_url: str) -> None:
    """Clash Spot's Day 2 board of 7 October 2026 shows trophies at the Reset
    before the automatic defense loss. Ours missed battles after each player's
    last reading: RAIN showed 5,088, not 5,168, and SnowBBcreaM 5,160, not
    5,128. A day not yet Complete and exact proves none of them."""
    readings = [
        ("#29QUQC8QL", 5088, _october(7, 4, 40)),  # RAIN
        ("#YYPUCVJUP", 5160, _october(6, 22, 22)),  # SnowBBcreaM
        ("#QVCU9PJCR", 5155, _october(7, 4, 50)),  # SUPRA
        ("#2222222", 5150, _october(7, 4, 40)),  # battle logs have a gap
        ("#8888888", 5140, _october(6, 4, 50)),  # read before Day 2 began
    ]
    days = {
        1: (True, [
            ("offense", 40, _october(7, 4, 33), True),
            ("offense", 40, _october(7, 4, 37), True),  # already in the reading
            ("offense", 40, _october(7, 4, 53), True),
            ("offense", 40, _october(7, 4, 57), True),
            ("offense", 40, _october(7, 4, 57), False),  # a second report of it
        ]),
        2: (True, [("defense", 32, _october(7, 4, 57), True)]),
        # Stamped after the Reset, but it finished on Day 2.
        3: (True, [("defense", 40, datetime(2026, 10, 7, 5, 0, 2, tzinfo=UTC), True)]),
        4: (False, [("offense", 40, _october(7, 4, 50), True)]),
        5: (True, [("offense", 40, _october(7, 4, 50), True)]),
    }
    with domain_database(database_url) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(connection_info, generation_id, days)
        database = Database(connection_info)
        try:
            assert _build_board(connection_info, database, generation_id) == [
                ("#29QUQC8QL", 5168, "uncertain"),
                ("#2222222", 5150, "uncertain"),
                ("#8888888", 5140, "uncertain"),
                ("#YYPUCVJUP", 5128, "uncertain"),
                ("#QVCU9PJCR", 5115, "uncertain"),
            ]
            season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
            # A board built under this rule is not rebuilt; one frozen before
            # it, ranking RAIN's reading alone, is.
            assert boundary.queue_board_rebuilds(database, season, queue=False)[
                "boards"
            ] == []
            with database.pool.connection() as connection:
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute(
                    "UPDATE leaderboard_snapshot_entries SET trophies = 5088"
                    " WHERE player_id = 1"
                )
            assert boundary.queue_board_rebuilds(database, season, queue=True)[
                "boards"
            ] == [
                {
                    "boundary_at": DAY_2_RESET.isoformat(),
                    "generation": 1,
                    "profile_not_found": 0,
                    "late_battles": 1,
                    "reordered": False, "members_changed": 0, "official_order": 0,
                    "correction": "queued",
                }
            ]
        finally:
            database.close()


def test_board_keeps_a_reading_its_later_battles_cannot_prove(
    database_url: str,
) -> None:
    """A reading answered at 04:54:59 for a request sent a second earlier may
    or may not hold an attack stamped 04:54:58, and a battle whose trophies
    the two players' logs disagree on proves nothing; each such entry keeps
    its reading and is marked uncertain."""
    readings = [
        ("#PYLQ0", 5088, datetime(2026, 10, 7, 4, 54, 59, tzinfo=UTC)),  # inflight
        ("#PYLQ2", 5100, _october(7, 4, 40)),  # disputed
        ("#PYLQ8", 5090, _october(7, 4, 40)),  # disputedday
    ]
    days = {
        1: (True, [
            ("offense", 40, datetime(2026, 10, 7, 4, 54, 58, tzinfo=UTC), True),
            ("offense", 40, _october(7, 4, 57), True),
        ]),
        2: (True, [("offense", 40, _october(7, 4, 57), True)]),
        3: (True, [("offense", 40, _october(7, 4, 57), True)]),
    }
    with domain_database(database_url) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(connection_info, generation_id, days)
        with psycopg.connect(connection_info) as connection:
            connection.execute("SET LOCAL session_replication_role = replica")
            # One player's log says +40, the other's +0.
            connection.execute(
                """
                UPDATE ranked_day_versions
                SET input_evidence = jsonb_set(
                    input_evidence, '{contributions,0,disagreement}', 'true'
                )
                WHERE id = 2
                """
            )
            connection.execute(
                "UPDATE ranked_day_versions"
                " SET failure_reasons = '[\"duplicate_contribution_disagreement\"]'"
                " WHERE id = 3"
            )
        database = Database(connection_info)
        try:
            assert _build_board(connection_info, database, generation_id) == [
                ("#PYLQ2", 5100, "uncertain"),  # disputed
                ("#PYLQ8", 5090, "uncertain"),  # disputedday
                ("#PYLQ0", 5088, "uncertain"),  # inflight
            ]
            season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
            assert boundary.queue_board_rebuilds(database, season, queue=False)[
                "boards"
            ] == []
        finally:
            database.close()


def test_board_keeps_a_reading_its_timing_or_day_cannot_prove(
    database_url: str,
) -> None:
    """A defender's report can come up to 4 minutes before the attack ends, so
    a reading requested at 04:54:59 may or may not hold a defense stamped
    04:52. Within 15 minutes of the day's Reset, the previous day's last
    reports and automatic defense loss can still land after a reading taken
    at 05:02 or 05:06. Each keeps its reading and is marked uncertain; an
    attack stamped 04:52, a reading taken at 05:16, and a day whose trophies
    don't add up but whose logs are continuous, like #PVVQPGRQL's on 11
    October 2026, still count their battles, as uncertain."""
    readings = [
        ("#GRJC0", 5160, _october(7, 4, 55)),  # defended
        ("#GRJC2", 5150, _october(7, 4, 55)),  # attacked
        ("#GRJC8", 5200, _october(6, 5, 2)),  # early
        ("#GRJC9", 5210, _october(6, 5, 6)),  # refreshed
        ("#GRJCU", 5100, _october(6, 5, 16)),  # settled
        ("#GRJCV", 5120, _october(7, 4, 40)),  # mismatch
        ("#GRJCY", 5130, _october(7, 4, 40)),  # mismatch with a log gap
    ]
    days = {
        1: (True, [("defense", 32, _october(7, 4, 52), True)]),
        2: (True, [
            ("offense", 40, _october(7, 4, 52), True),
            ("offense", 40, _october(7, 4, 58), True),
        ]),
        3: (True, [("offense", 40, _october(6, 6), True)]),
        4: (True, [("offense", 40, _october(6, 6), True)]),
        5: (True, [("offense", 40, _october(6, 6), True)]),
        6: (True, [("defense", 30, _october(7, 4, 50), True)]),
        7: (False, [("offense", 40, _october(7, 4, 50), True)]),
    }
    with domain_database(database_url) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(connection_info, generation_id, days)
        with psycopg.connect(connection_info) as connection:
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(
                "UPDATE ranked_day_versions"
                " SET state = 'Inconsistent', confidence = 'partial',"
                " failure_reasons = '[\"trophy_equation_mismatch\"]'"
                " WHERE id IN (6, 7)"
            )
        database = Database(connection_info)
        try:
            assert _build_board(connection_info, database, generation_id) == [
                ("#GRJC9", 5210, "uncertain"),  # refreshed
                ("#GRJC8", 5200, "uncertain"),  # early
                ("#GRJC2", 5190, "uncertain"),  # attacked
                ("#GRJC0", 5160, "uncertain"),  # defended
                ("#GRJCU", 5140, "uncertain"),  # settled
                ("#GRJCY", 5130, "uncertain"),  # mismatch with a log gap
                ("#GRJCV", 5090, "uncertain"),  # mismatch
            ]
            season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
            assert boundary.queue_board_rebuilds(database, season, queue=False)[
                "boards"
            ] == []
        finally:
            database.close()


def test_board_proves_a_reading_only_by_the_days_reset_readings(
    database_url: str,
) -> None:
    """#2QCYU8C2G read 4,703 at 04:37:05 on 7 October 2026 without its attack
    stamped 04:34:08 for 29, and the board showed 4,902 as proven: a reading
    proves no battle stamped before it. Its Complete day ends at 4,931 from
    its Reset readings at both ends. Without a Complete and exact day, a
    reading plus the battles after it is never proven, even when the day's
    start plus all its battles, or its end Reset reading, comes to it too;
    a Reset that resets trophies proves nothing."""
    readings = [
        ("#2QCYU8C2G", 4703, datetime(2026, 10, 7, 4, 37, 5, tzinfo=UTC)),
        ("#GURYYP99", 4923, _october(7, 4, 54)),  # no end reading
        ("#PL0Q0UVLC", 5100, _october(7, 4, 40)),  # end reading agrees
        ("#P0VPRVPJJ", 5090, _october(7, 4, 40)),  # less the known loss
        ("#P2CC9URVR", 5080, _october(7, 4, 40)),  # less an unknown loss
        ("#Y8V9YYP9C", 5151, _october(7, 4, 40)),  # Season reset, disagrees
        ("#YPG0UY9LU", 5047, _october(7, 4, 40)),  # Season reset, agrees
        ("#2GL8CJL", 5105, _october(7, 4, 40)),  # calculated start
        ("#82RV9CV8C", 5000, _october(7, 4, 59)),  # unproven start
    ]
    days = {
        1: (True, [
            ("defense", 209, _october(7, 3), True),
            ("offense", 29, datetime(2026, 10, 7, 4, 34, 8, tzinfo=UTC), True),
            ("offense", 199, _october(7, 4, 50), True),
        ]),
        2: (True, [
            ("offense", 40, datetime(2026, 10, 7, 4, 52, 3, tzinfo=UTC), True),
            ("offense", 69, _october(7, 4, 58), True),
        ]),
        **{player: (True, [("offense", 40, _october(7, 4, 50), True)])
           for player in range(3, 9)},
        # 8 defenses for 320 and an attack at 04:58 that may still be landing.
        9: (True, [("defense", 40, _october(6, 6 + hour), True) for hour in range(8)]
            + [("offense", 40, _october(7, 4, 58), True)]),
    }
    no_start = {"failure_reasons": ["missing_start_baseline"], "start": None}
    complete = {"state": "Complete", "failure_reasons": []}
    results = {
        1: {**complete, "final": 4931, "start": 4912, "end": 4931},
        2: {"start": 4923},
        3: {**no_start, "end": 5140},
        4: {**no_start, "end": 5100, "automatic_loss": 30},
        5: {**no_start, "end": 5090, "automatic_state": "unknown"},
        6: {
            **complete, "final": 5185, "start": 5145, "boundary_kind": "season",
            "end": 5000,
        },
        7: {**complete, "final": 5087, "boundary_kind": "season", "end": 5000},
        # Complete from its end reading alone, its start no reading matched.
        8: {**complete, "final": 5145, "end": 5145, "confidence": "inferred"},
        9: {"failure_reasons": ["end_reading_unverified"], "start": 5280, "end": 5000},
    }
    with domain_database(database_url) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(connection_info, generation_id, days, results)
        database = Database(connection_info)
        try:
            assert _build_board(connection_info, database, generation_id) == [
                ("#Y8V9YYP9C", 5185, "uncertain"),
                ("#2GL8CJL", 5145, "uncertain"),
                ("#PL0Q0UVLC", 5140, "uncertain"),
                ("#P0VPRVPJJ", 5130, "uncertain"),
                ("#P2CC9URVR", 5120, "uncertain"),
                ("#YPG0UY9LU", 5087, "confirmed"),
                ("#82RV9CV8C", 5000, "uncertain"),
                ("#GURYYP99", 4992, "uncertain"),
                ("#2QCYU8C2G", 4931, "confirmed"),
            ]
            season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
            assert boundary.queue_board_rebuilds(database, season, queue=False)[
                "boards"
            ] == []
            # A board ordered by an older ordering rule is rebuilt too.
            for rule, reordered in (("old", [True]), (SNAPSHOT_ORDERING_RULE_VERSION, [])):
                with database.pool.connection() as connection:
                    connection.execute("SET LOCAL session_replication_role = replica")
                    connection.execute(
                        "UPDATE leaderboard_snapshots SET ordering_rule_version = %s", (rule,)
                    )
                assert [board["reordered"] for board in boundary.queue_board_rebuilds(
                    database, season, queue=False)["boards"]] == reordered
            # A board built before this rule showed the reading plus the
            # battles after it as proven.
            with database.pool.connection() as connection:
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute(
                    "UPDATE leaderboard_snapshot_entries SET trophies = 4902"
                    " WHERE player_id = 1"
                )
            assert boundary.queue_board_rebuilds(database, season, queue=False)[
                "boards"
            ] == [
                {
                    "boundary_at": DAY_2_RESET.isoformat(),
                    "generation": 1,
                    "profile_not_found": 0,
                    "late_battles": 1,
                    "reordered": False, "members_changed": 0, "official_order": 0,
                    "correction": "not_queued",
                }
            ]
        finally:
            database.close()
