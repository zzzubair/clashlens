"""The Daily board ranks trophies at exactly 05:00:00, as the game's own Reset
rank does: an attack that ends at or after the Reset still counts for the
ended day, but not on that day's board.

On 11 October 2026, 42 battles started before 05:00 and ended between
05:00:00 and 05:03:10. The game's Reset ranks left them out and our board
counted them; taking them off cut order breaks against the game's country
Reset ranks from 400 to 40 of 342,472 pairs.
"""

from __future__ import annotations

import json
from datetime import timedelta

import psycopg
from domain_test_support import domain_database, store_observation, text
from test_reconciliation_postgres import (
    DAY_END,
    DAY_START,
    _processor,
    _profile,
    _seed_reset_collection_identity,
)
from test_snapshot_publication_postgres import _process_snapshot_and_analytics

PLAYERS = ("#2PP", "#8PP", "#9PP", "#YPP")


def _row(*, attack: bool, at, opponent: str, stars: int, destruction: int) -> dict:
    return {
        "battleType": "legend",
        "attack": attack,
        "battleTime": 150,
        "battleTimestamp": at.strftime("%Y%m%dT%H%M%S.000Z"),
        "stars": stars,
        "destructionPercentage": destruction,
        "opponentPlayerTag": opponent,
        "opponentName": f"Opponent {opponent}",
        "opponentTownHallLevel": 18,
        "armyShareCode": "u1x0-2x1",
    }


def test_board_leaves_out_battles_that_end_after_the_reset(
    database_url: str, archive_server
) -> None:
    """#2PP's attack on #8PP starts at 04:58:30, when #8PP's report is
    stamped, and ends at 05:01:00, when #2PP's is. Both days still count it,
    and so do their end-of-day totals, but the board shows both players
    without it. #9PP's attack on #2PP ended at 04:50, so it stays on both
    players' board values. #YPP's report of #QPP's attack is stamped
    04:57:30 and #QPP's own report is never saved, so the battle stays
    counted, but its attack may have ended after the Reset, so the entry is
    uncertain."""
    late_defense_at = DAY_END - timedelta(seconds=90)
    late_attack_at = DAY_END + timedelta(minutes=1)
    early_attack_at = DAY_END - timedelta(minutes=10)
    early_defense_at = early_attack_at - timedelta(minutes=2)
    unreported_defense_at = DAY_END - timedelta(seconds=150)
    logs = {
        "#2PP": [
            _row(attack=True, at=late_attack_at, opponent="#8PP", stars=3, destruction=100),
            _row(attack=False, at=early_defense_at, opponent="#9PP", stars=1, destruction=60),
        ],
        "#8PP": [
            _row(attack=False, at=late_defense_at, opponent="#2PP", stars=3, destruction=100),
        ],
        "#9PP": [
            _row(attack=True, at=early_attack_at, opponent="#2PP", stars=1, destruction=60),
        ],
        # #QPP isn't tracked, so its report of this attack is never saved.
        "#YPP": [
            _row(attack=False, at=unreported_defense_at, opponent="#QPP", stars=2,
                 destruction=80),
        ],
    }
    with domain_database(database_url, include_coordinator=True) as connection_info:
        # Each Reset sweep captures all three players before collecting them.
        with psycopg.connect(connection_info) as connection:
            for tag in PLAYERS:
                connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES (%s, true)", (tag,)
                )
            for boundary in (DAY_START, DAY_END):
                connection.execute(
                    """
                    WITH sweep AS (
                        INSERT INTO collector_reset_sweeps (
                            boundary_at, member_ids, membership_captured_at
                        )
                        SELECT %s, array_agg(id ORDER BY id), clock_timestamp()
                        FROM players
                        RETURNING id, member_ids
                    )
                    INSERT INTO collector_work (
                        kind, lane, scope, player_id, normalized_tag, sweep_id,
                        due_at, coalescing_key, status
                    )
                    SELECT 'reset_baseline', 'reset', 'player', player.id,
                           player.normalized_tag, sweep.id, %s,
                           'reset-baseline-' || player.normalized_tag || %s, 'pending'
                    FROM sweep JOIN players AS player ON player.id = ANY(sweep.member_ids)
                    """,
                    (boundary, boundary, f"{boundary:%d}"),
                )
        jobs: list[int] = []
        # Read before the automatic defense loss lands: #2PP lost 11 to #9PP
        # and won 40 from #8PP, and #YPP lost 26 to #QPP.
        end_readings = {"#2PP": 6029, "#8PP": 5960, "#9PP": 6011, "#YPP": 5974}
        for boundary, empty in ((DAY_START, True), (DAY_END, False)):
            # The Reset sweep answers 12 minutes after it, once the battles
            # ending after it have landed.
            observed_at = boundary + timedelta(minutes=12)
            for tag in PLAYERS:
                key = f"{tag}-{boundary:%d}"
                profile, profile_job = store_observation(
                    connection_info, archive_server, occurrence_key=f"{key}-profile",
                    endpoint="profile", body=_profile(6000 if empty else end_readings[tag], tag),
                    observed_at=observed_at, normalized_tag=tag,
                )
                battles, battle_job = store_observation(
                    connection_info, archive_server, occurrence_key=f"{key}-battle",
                    endpoint="battle_log",
                    body=json.dumps({"items": [] if empty else logs[tag]}).encode(),
                    observed_at=observed_at, normalized_tag=tag,
                )
                _seed_reset_collection_identity(
                    connection_info, key=key, boundary=boundary,
                    profile_observation_id=profile, battle_observation_id=battles,
                    normalized_tag=tag,
                )
                jobs += [profile_job, battle_job]
        database, processor = _processor(connection_info, archive_server)
        try:
            for job in jobs:
                result = processor.process_job(job, owner=f"source-{job}")
                assert result is not None and result.outcome == "processed"

            def next_job(work_type: str) -> int | None:
                with database.pool.connection() as connection:
                    row = connection.execute(
                        "SELECT id FROM python_processing_jobs"
                        " WHERE work_type = %s AND status = 'pending'"
                        " AND (%s <> 'build_snapshot' OR input_json->>'boundary_at' = %s)"
                        " ORDER BY id LIMIT 1",
                        (work_type, work_type, DAY_END.strftime("%Y-%m-%dT%H:%M:%SZ")),
                    ).fetchone()
                return None if row is None else int(row[0])

            while (reconcile := next_job("reconcile_ranked_day")) is not None:
                processor.process_job(reconcile, owner="reconcile")
            snapshot = next_job("build_snapshot")
            assert snapshot is not None, "the ended day never reached the board"
            _process_snapshot_and_analytics(
                connection_info, database, processor, snapshot, owner_prefix="board"
            )
            with database.pool.connection() as connection:
                # Each ended day's state, its end-of-day total before the
                # automatic defense loss, and the next day's start.
                days = {
                    text(tag): (text(state), before_loss, next_start)
                    for tag, state, before_loss, next_start in connection.execute(
                        """
                        SELECT DISTINCT ON (day.player_id) player.normalized_tag,
                               day.state,
                               day.final_trophies_before_reset
                               + COALESCE(day.automatic_defense_loss, 0),
                               day.expected_next_start_trophies
                        FROM ranked_day_versions AS day
                        JOIN players AS player ON player.id = day.player_id
                        WHERE day.ranked_day_start = %s
                        ORDER BY day.player_id, day.version DESC
                        """,
                        (DAY_START,),
                    ).fetchall()
                }
                board = [
                    (text(tag), trophies, text(confidence))
                    for tag, trophies, confidence in connection.execute(
                        """
                        SELECT player.normalized_tag, entry.trophies, entry.confidence
                        FROM leaderboard_snapshot_entries AS entry
                        JOIN leaderboard_snapshots AS snapshot
                          ON snapshot.id = entry.snapshot_id
                        JOIN players AS player ON player.id = entry.player_id
                        WHERE snapshot.snapshot_kind = 'frozen'
                        ORDER BY entry.position
                        """
                    ).fetchall()
                ]
        finally:
            database.close()

    # The ended days still count every battle, so their totals and the next
    # day's starts are unchanged.
    assert days == {
        "#2PP": ("Complete", 6029, 5952),
        "#8PP": ("Complete", 5960, 5680),
        "#9PP": ("Complete", 6011, 6011),
        "#YPP": ("Complete", 5974, 5792),
    }
    assert board == [
        ("#9PP", 6011, "confirmed"),
        ("#8PP", 6000, "confirmed"),  # 5,960 before this rule
        ("#2PP", 5989, "confirmed"),  # 6,029 before this rule
        ("#YPP", 5974, "uncertain"),
    ]
