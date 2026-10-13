"""The nightly re-check of finished Legend days (``reconcile_pass``), end to
end: a Season's first three days saved by the real worker from saved game
data, errors planted in what it saved, the re-check run over the Season, the
worker recalculating what it queued, and the Daily board rebuilt."""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg
from domain_test_support import domain_database, text
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_reset_scenarios_postgres import (
    SEASON,
    Battle,
    _ranked,
    _story,
    attacks,
    day,
    defenses,
)

from clashlens import alerts, queue_refresh, reconcile_pass
from clashlens.db import Database

# One player per planted error, and one with none.
LATE, JOINED, LEDGER, CHAIN, COPIES, BOARD, STUCK, CLEAN = (
    "#2PP", "#8PP", "#9PP", "#PPP", "#YPP", "#LPP", "#QPP", "#CPP")


def _even(start: datetime) -> list:
    """6 attacks of 40 and 8 defenses of 30: the day ends where it began."""
    return attacks(start, *[40] * 6) + defenses(start, *[30] * 8)


def _as_worker(connection_info: str) -> str:
    options = conninfo_to_dict(connection_info).get("options", "")
    return make_conninfo(connection_info, options=f"{options} -c role=clashlens_python_worker")


def _run_pass(connection_info: str, now: datetime) -> None:
    """One whole pass at ``now``, as the worker's timer runs it after the
    night recheck, a batch a call, with the worker's database role."""
    database = Database(_as_worker(connection_info))
    try:
        checker = reconcile_pass.ReconcilePass(database)
        checker.run_when_due({f"{day_of(now):%Y-%m-%d}:night"}, now)
        while checker.current is not None:
            checker.run_when_due(set(), now)
    finally:
        database.close()


def day_of(now: datetime) -> datetime:
    """The Reset starting the Legend day at ``now``."""
    return now - (now - SEASON) % timedelta(days=1)


def _open(connection_info: str) -> set[tuple[str, str, int]]:
    """Each open finding: its check, player and Season day."""
    with psycopg.connect(connection_info) as connection:
        return {
            (text(check), text(tag), (start - SEASON).days + 1)
            for check, tag, start in connection.execute(
                """
                SELECT finding.check_name, player.normalized_tag, finding.ranked_day_start
                FROM reconcile_findings AS finding
                JOIN players AS player ON player.id = finding.player_id
                WHERE finding.resolved_at IS NULL
                """
            ).fetchall()
        }


def _version(connection: psycopg.Connection, tag: str, start: datetime) -> int:
    """The saved result of the player's day from ``start`` their page shows."""
    return connection.execute(
        """
        SELECT log.ranked_day_version_id FROM api_player_daily_logs AS log
        JOIN players AS player ON player.id = log.player_id
        WHERE player.normalized_tag = %s AND log.ranked_day_start = %s
        ORDER BY log.version DESC LIMIT 1
        """,
        (tag, start),
    ).fetchone()[0]


def _plant(connection_info: str) -> None:
    """One error per check the worker's own recalculation cannot make."""
    with psycopg.connect(connection_info) as connection:
        # A start that does not add up to the day's end.
        connection.execute(
            "UPDATE ranked_day_versions SET start_trophies = start_trophies + 10,"
            " net_trophy_change = net_trophy_change - 10 WHERE id = %s",
            (_version(connection, LEDGER, day(1)),))
        # A next start Day 2 did not start from.
        connection.execute(
            """
            UPDATE ranked_day_versions SET next_start_trophies = next_start_trophies + 10,
                observed_boundary_adjustment = observed_boundary_adjustment + 10,
                unexplained_residual = unexplained_residual + 10
            WHERE id = %s
            """,
            (_version(connection, CHAIN, day(1)),))
        # Two battle logs disagreeing on one of Day 2's attacks.
        connection.execute(
            """
            UPDATE legend_battles SET disagreement_state = 'disagreement',
                disagreement_fields = ARRAY['stars']
            WHERE id = (SELECT min(battle.id) FROM legend_battles AS battle
                        JOIN players AS player ON player.id = battle.attacker_player_id
                        WHERE player.normalized_tag = %s AND battle.ranked_day_start = %s)
            """,
            (COPIES, day(2)))
        # A confirmed Daily board row 7 trophies off its day. Board rows are
        # otherwise never changed.
        connection.execute("SET LOCAL session_replication_role = replica")
        assert connection.execute(
            """
            UPDATE leaderboard_snapshot_entries AS entry SET trophies = trophies + 7
            FROM players AS player
            WHERE player.id = entry.player_id AND player.normalized_tag = %s
              AND entry.snapshot_id = (SELECT id FROM leaderboard_snapshots
                                       WHERE snapshot_kind = 'frozen' AND state = 'published'
                                         AND boundary_at = %s
                                       ORDER BY version DESC LIMIT 1)
            """,
            (BOARD, day(3)),
        ).rowcount == 1
        # A saved attack count no recalculation changes: it saves nothing new.
        connection.execute(
            "UPDATE ranked_day_versions SET attack_count = attack_count + 1 WHERE id = %s",
            (_version(connection, STUCK, day(1)),))


def _queued(connection_info: str) -> list[tuple[str, str, int, str]]:
    """The recalculations the re-check queued: player, Season day, priority and state."""
    with psycopg.connect(connection_info) as connection:
        return sorted(
            (text(tag), text(job.get("trigger")), int(priority), text(state))
            for tag, job, priority, state in connection.execute(
                """
                SELECT player.normalized_tag, job.input_json, job.priority, job.status
                FROM python_processing_jobs AS job
                JOIN players AS player ON player.id = (job.input_json ->> 'player_id')::bigint
                WHERE job.deduplication_key LIKE 'reconcile:pass:%%'
                """
            ).fetchall()
        )


def _escalated(connection_info: str) -> set[str]:
    with psycopg.connect(connection_info) as connection:
        return {text(row[0]) for row in connection.execute(
            "SELECT player.normalized_tag FROM reconcile_findings AS finding"
            " JOIN players AS player ON player.id = finding.player_id"
            " WHERE finding.escalated_at IS NOT NULL").fetchall()}


def test_planted_errors_are_found_and_the_fixable_ones_reach_the_board(
    database_url: str, archive_server, monkeypatch, tmp_path, capsys
) -> None:
    """Days 1 to 3 of the Season, saved by the real worker. LATE's last
    Day 2 defense, at 04:58, was missing from the battle log read at the
    Reset, as a log the game cached for a minute can be; the log read at
    05:20 shows it, but no recheck of Day 2 was queued, so Day 2 keeps 7
    defenses and an automatic loss of 30 for the eighth, and the Daily board
    shows 5,030. JOINED was first tracked on Day 3, so no Day 1 was saved.
    Each other player but CLEAN has one error planted in what was saved.
    The re-check must find exactly these, queue the recalculation of LATE's
    Day 2, JOINED's Day 1 and STUCK's Day 1 within its budget, once each,
    and the rebuilt board must then show LATE at 5,000."""
    night = day(4) + timedelta(hours=18)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       LATE, LEDGER, CHAIN, COPIES, BOARD, STUCK, CLEAN, JOINED)
        for player in story.players.values():
            player.battles = _even(day(1)) + _even(day(2)) + _even(day(3))
        story.players[JOINED].battles = _even(day(2)) + _even(day(3))
        # LATE's eighth Day 2 defense comes at 04:58, not 12:30.
        story.players[LATE].battles.remove(Battle(day(2) + timedelta(hours=7, minutes=30),
                                                  -30, False))
        late = Battle(day(3) - timedelta(minutes=2), -30, False)
        story.reset(day(1), {tag: (5000, SEASON) for tag in story.players if tag != JOINED})
        with monkeypatch.context() as patch:
            for number in (2, 3, 4):
                before = [tag for tag in story.players if tag != JOINED or number > 3]
                after = [tag for tag in story.players if tag != JOINED or number > 2]
                # LATE reads 5,030 at 04:50, before Day 2's last defense.
                story.reads(day(number) - timedelta(minutes=10), {
                    tag: 5030 if (tag, number) == (LATE, 3) else 5000 for tag in before})
                story.reset(day(number), {tag: (5000, SEASON) for tag in after})
                if number == 3:
                    # The log read at 05:20 is the first to show it, and no
                    # new evidence queues a recheck of Day 2 from then on.
                    story.players[LATE].battles.append(late)
                    for name in ("queue_for_battles", "queue_for_reading"):
                        patch.setattr(queue_refresh, name, lambda *_args, **_kwargs: None)
                story.reads(day(number) + timedelta(minutes=20), dict.fromkeys(after, 5000))
                story.run_worker()
        story.wait(timedelta(hours=1))
        board_before = story.served_board(day(3))
        late_before = story.saved_day(LATE, day(2))
        with psycopg.connect(connection_info) as connection:
            connection.execute("UPDATE players SET active = true WHERE normalized_tag = ANY(%s)",
                               (list(story.players),))
        _plant(connection_info)

        # Nothing runs from 04:00 to 07:00 UTC, and a pass running at 04:00
        # stops there.
        monkeypatch.setattr(reconcile_pass, "BATCH_DAYS", 3)
        monkeypatch.setattr(reconcile_pass, "PASS_FIX_LIMIT", 2)
        database = Database(_as_worker(connection_info))
        try:
            checker = reconcile_pass.ReconcilePass(database)
            checker.run_when_due({f"{day(4):%Y-%m-%d}:night"}, day(5) + timedelta(minutes=30))
            quiet = (checker.current, _open(connection_info))
            checker.run_when_due({f"{day(4):%Y-%m-%d}:night"}, day(5) - timedelta(hours=1, minutes=1))
            started = checker.current is not None
            checker.run_when_due(set(), day(5) - timedelta(hours=1))
            stopped = checker.current
        finally:
            database.close()

        _run_pass(connection_info, night)
        first = _open(connection_info)
        first_queued = _queued(connection_info)
        # The next pass queues what the budget left, and nothing twice.
        _run_pass(connection_info, night)
        second_queued = _queued(connection_info)
        story.run_worker()
        late_fixed = story.saved_day(LATE, day(2))
        joined_saved = story.saved_day(JOINED, day(1)) is not None
        _run_pass(connection_info, night)
        after_fixes = _open(connection_info)
        escalated = _escalated(connection_info)
        # The Reset's board rebuilds 6 hours after a correction.
        story.wait(timedelta(hours=7))
        board_after = story.served_board(day(3))
        _run_pass(connection_info, night)
        last = _open(connection_info)
        unfinished = story.unfinished_jobs()
        # The daily line's counts, read as the worker reads them.
        url_file = tmp_path / "database-url"
        url_file.write_text(_as_worker(connection_info))
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url_file))
        capsys.readouterr()
        alerts.reconcile_probe()
        line = alerts.reconcile_line([int(n) for n in capsys.readouterr().out.split()])

    assert quiet == (None, set())
    assert started and stopped is None
    assert late_before["defenses"] == 7 and late_before["automatic_loss"] == 30
    assert _ranked(board_before)[LATE] == 5030
    planted = {("ledger", LEDGER, 1), ("chain", CHAIN, 2), ("copies", COPIES, 2),
               ("board", BOARD, 2), ("recount", STUCK, 1)}
    assert first == planted | {("recount", LATE, 2), ("missing_day", JOINED, 1)}
    # Oldest day first, 2 a pass, at backfill priority.
    assert first_queued == [(JOINED, "reconcile_pass", 25, "pending"),
                            (STUCK, "reconcile_pass", 25, "pending")]
    assert [row[:3] for row in second_queued] == [
        (LATE, "reconcile_pass", 25), (JOINED, "reconcile_pass", 25),
        (STUCK, "reconcile_pass", 25)]
    assert late_fixed["defenses"] == 8 and late_fixed["automatic_loss"] is None
    assert joined_saved
    # LATE's old board row now disagrees with its recalculated day; STUCK's
    # count survived its recalculation.
    assert after_fixes == planted | {("board", LATE, 2)}
    assert escalated == {STUCK}
    assert _ranked(board_after)[LATE] == 5000
    assert last == planted - {("board", BOARD, 2)}
    assert unfinished == []
    assert line.startswith(
        "Clash Lens nightly re-check, last 24 hours: 8 new problems (recount 2, chain 1,"
        " ledger 1, battle copies 1, missing days 1, board rows 2); 3 recalculations"
        " queued; 1 still wrong after their recalculation; 4 open.")
