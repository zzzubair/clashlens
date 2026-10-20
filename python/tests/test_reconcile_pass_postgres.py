"""The nightly re-check of finished Legend days (``reconcile_pass``), end to
end: a Season's first three days saved by the real worker from saved game
data, errors planted in what it saved, the re-check's steps claimed and run
by the real worker over the Season, the worker recalculating what they
queued, and the Daily board rebuilt."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

import psycopg
from domain_test_support import domain_database, text
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_reconciliation_postgres import _processor
from test_reset_scenarios_postgres import (
    SEASON,
    Battle,
    _ranked,
    _story,
    attacks,
    day,
    defenses,
)

from clashlens import alerts, domain_repair, queue_refresh, reconcile_pass
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


def _run_pass(story, monkeypatch, now: datetime, slot: str | None = None,
              *, steps: int | None = None, reads_end: datetime | None = None) -> int:
    """Queue the pass of ``slot``, by default the night recheck's at ``now``,
    and run its waiting steps, or the first ``steps``, at ``now``, or at
    ``reads_end`` once a step's reads end, each claimed by the real worker
    with its own database role; how many ran."""
    clock = [now]
    monkeypatch.setattr(reconcile_pass, "_now", lambda: clock[0])
    database, processor = _processor(_as_worker(story.connection_info), story.archive_server)
    judge = reconcile_pass.judge

    def slow_judge(*args):
        judgment = judge(*args)
        clock[0] = reads_end or clock[0]
        return judgment

    ran = 0
    try:
        with monkeypatch.context() as patch:
            patch.setattr(reconcile_pass, "judge", slow_judge)
            reconcile_pass.queue_pass(database, slot or f"{day_of(now):%Y-%m-%d}:night")
            while ran != steps and (job := _waiting_step(story.connection_info)) is not None:
                assert processor.process_job(job, owner="test").outcome == "processed"
                ran += 1
    finally:
        database.close()
    return ran


def _waiting_step(connection_info: str) -> int | None:
    with psycopg.connect(connection_info) as connection:
        row = connection.execute(
            "SELECT id FROM python_processing_jobs WHERE work_type = 'reconcile_pass'"
            " AND status = 'pending' ORDER BY id LIMIT 1").fetchone()
    return None if row is None else int(row[0])


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


def _plant(connection_info: str) -> str:
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
        # A report of one of Day 2's attacks by its defender, from another
        # battle log, with other stars, under the label saying both reports
        # agree.
        battle = connection.execute(
            """
            SELECT min(battle.id) FROM legend_battles AS battle
            JOIN players AS player ON player.id = battle.attacker_player_id
            WHERE player.normalized_tag = %s AND battle.ranked_day_start = %s
            """, (COPIES, day(2))).fetchone()[0]
        connection.execute(
            """
            WITH copy AS (
                INSERT INTO battle_evidence (battle_id, source_row_id, observation_id,
                    reporting_player_id, perspective, battle_timestamp, stars,
                    destruction_percentage, army_share_code, reporter_trophies,
                    opponent_trophies, attacker_gain, defender_loss, trophy_rule_version,
                    source_observed_at, parser_version)
                SELECT evidence.battle_id, evidence.source_row_id,
                       (SELECT max(id) FROM collector_observations
                        WHERE endpoint = 'battle_log' AND id <> evidence.observation_id),
                       battle.defender_player_id, 'defender', evidence.battle_timestamp,
                       (evidence.stars + 1) %% 4, evidence.destruction_percentage,
                       evidence.army_share_code, evidence.opponent_trophies,
                       evidence.reporter_trophies, evidence.attacker_gain,
                       evidence.defender_loss, evidence.trophy_rule_version,
                       evidence.source_observed_at, evidence.parser_version
                FROM battle_perspectives AS shown
                JOIN battle_evidence AS evidence ON evidence.id = shown.evidence_id
                JOIN legend_battles AS battle ON battle.id = shown.battle_id
                WHERE shown.battle_id = %s AND shown.perspective = 'attacker'
                RETURNING id, battle_id, source_observed_at
            )
            INSERT INTO battle_perspectives (battle_id, perspective, evidence_id,
                                             source_observed_at)
            SELECT battle_id, 'defender', id, source_observed_at FROM copy
            """, (battle,))
        connection.execute(
            "UPDATE legend_battles SET disagreement_state = 'agreed',"
            " disagreement_fields = '{}' WHERE id = %s", (battle,))
        # Its defender's Day 2 is saved too, as they fought Legend battles,
        # and the recount now finds a defense their saved day lacks.
        copied = text(connection.execute(
            "SELECT player.normalized_tag FROM legend_battles AS battle"
            " JOIN players AS player ON player.id = battle.defender_player_id"
            " WHERE battle.id = %s", (battle,)).fetchone()[0])
        # One of Day 3's attacks against a tracked player whose own report of
        # it is missing.
        connection.execute(
            """
            UPDATE legend_battles SET defender_player_id = (
                SELECT id FROM players WHERE normalized_tag = %s)
            WHERE id = (SELECT min(battle.id) FROM legend_battles AS battle
                        JOIN players AS player ON player.id = battle.attacker_player_id
                        WHERE player.normalized_tag = %s AND battle.ranked_day_start = %s)
            """,
            (CLEAN, COPIES, day(3)))
        # A Day 2 whose saved result its page lost.
        connection.execute(
            "DELETE FROM api_player_daily_logs WHERE ranked_day_start = %s AND player_id ="
            " (SELECT id FROM players WHERE normalized_tag = %s)", (day(2), JOINED))
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
    return copied


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


def _record_as_repair_starts(connection_info: str, patch, tag: str) -> dict:
    """Make the step that found ``tag``'s problem, having decided whether it
    may queue recalculations, start a Season repair, its receipt and then
    its first days, and wait 5 seconds before saving what it found; the
    repair's thread."""
    record, repair, season = reconcile_pass.record, {}, str(int(SEASON.timestamp()))
    revision = domain_repair.DAY_RULES_REVISION
    with psycopg.connect(connection_info) as connection:
        player = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (tag,)).fetchone()[0]
        players = [row[0] for row in connection.execute("SELECT id FROM players ORDER BY id")]

    def run() -> None:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                connection.execute(
                    "INSERT INTO season_repairs (official_season_id, rule_revision,"
                    " before_days, before_boards) VALUES (%s, %s, '{}', '{}')",
                    (season, revision))
            domain_repair._queue_days(database, season, revision, SEASON, players)
        finally:
            database.close()

    def record_late(connection, day, judgment, **kwargs):
        if "thread" not in repair and any(f.player_id == player for f in judgment[0]):
            repair["thread"] = threading.Thread(target=run)
            repair["thread"].start()
            repair["thread"].join(timeout=5)
        return record(connection, day, judgment, **kwargs)

    patch.setattr(reconcile_pass, "record", record_late)
    return repair


def _repair_after_fix(connection_info: str, tag: str) -> bool:
    """Whether every recalculation the Season repair queued was queued after
    the re-check's recalculation of ``tag``'s day."""
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT max(fix.id) < min(repair.id)
            FROM python_processing_jobs AS fix
            JOIN players AS player ON player.id = (fix.input_json ->> 'player_id')::bigint
            CROSS JOIN python_processing_jobs AS repair
            WHERE fix.deduplication_key LIKE 'reconcile:pass:%%'
              AND player.normalized_tag = %s
              AND repair.deduplication_key LIKE 'reconcile:season-repair:%%'
            """,
            (tag,),
        ).fetchone()[0]


def test_planted_errors_are_found_and_the_fixable_ones_reach_the_board(
    database_url: str, archive_server, monkeypatch, tmp_path, capsys
) -> None:
    """Days 1 to 3 of the Season, saved by the real worker. LATE's last
    Day 2 defense, at 04:58, was missing from the battle log read at the
    Reset, as a log the game cached for a minute can be; the log read at
    05:20 shows it, but no recheck of Day 2 was queued, so Day 2 keeps 7
    defenses and an automatic loss of 30 for the eighth, and the Daily board
    shows 5,030. JOINED was first tracked at the Reset ending Day 2, so it
    has no Day 1, as it should. Each other player but CLEAN has one error
    planted in what was saved, JOINED's Day 2 is lost from its page, and
    COPIES has two battles whose reports do not agree. The re-check must
    find exactly these, queue the recalculation of LATE's Day 2, JOINED's
    Day 2 and STUCK's Day 1 within its budget, once each, and the rebuilt
    board must then show LATE at 5,000."""
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
        copied = _plant(connection_info)

        # No pass starts work from 03:50 to 07:00 UTC, and a step not saved
        # by 03:58 keeps nothing: one queued at 03:49 and run at 03:50, and
        # one run at 03:49 whose reads take until 03:58:30.
        monkeypatch.setattr(reconcile_pass, "BATCH_DAYS", 3)
        monkeypatch.setattr(reconcile_pass, "PASS_FIX_LIMIT", 2)
        morning = f"{day(4):%Y-%m-%d}:morning"
        quiet = _run_pass(story, monkeypatch, day(5) + timedelta(minutes=30), morning)
        _run_pass(story, monkeypatch, day(5) - timedelta(hours=1, minutes=11), morning, steps=0)
        stopped = (_run_pass(story, monkeypatch, day(5) - timedelta(hours=1, minutes=10), morning),
                   _waiting_step(connection_info), _open(connection_info))
        slow = (_run_pass(story, monkeypatch, day(5) - timedelta(hours=1, minutes=11),
                          f"{day(3):%Y-%m-%d}:night",
                          reads_end=day(5) - timedelta(hours=1, minutes=1, seconds=30)),
                _waiting_step(connection_info), _open(connection_info), _queued(connection_info))

        # Four nights' passes: each its own, and none queued twice.
        nights = [day(4 + number) + timedelta(hours=18) for number in range(4)]
        first_steps = _run_pass(story, monkeypatch, nights[0])
        again = _run_pass(story, monkeypatch, nights[0])
        first = _open(connection_info)
        first_queued = _queued(connection_info)
        # The next pass queues what the budget left, and nothing twice.
        _run_pass(story, monkeypatch, nights[1])
        second_queued = _queued(connection_info)
        story.run_worker()
        late_fixed = story.saved_day(LATE, day(2))
        joined_saved = story.saved_day(JOINED, day(2)) is not None
        _run_pass(story, monkeypatch, nights[2])
        after_fixes = _open(connection_info)
        escalated = _escalated(connection_info)
        # The Reset's board rebuilds 6 hours after a correction.
        story.wait(timedelta(hours=7))
        board_after = story.served_board(day(3))
        _run_pass(story, monkeypatch, nights[3])
        last = _open(connection_info)
        unfinished = story.unfinished_jobs()
        # The daily line's counts, read as the worker reads them.
        url_file = tmp_path / "database-url"
        url_file.write_text(_as_worker(connection_info))
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url_file))
        capsys.readouterr()
        alerts.reconcile_probe()
        line = alerts.reconcile_line([int(n) for n in capsys.readouterr().out.split()])

        # A Season repair reaching its days phase while a step saves what it
        # read: the step's recalculation, decided before, is queued before
        # any of the repair's.
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE ranked_day_versions SET attack_count = attack_count + 1 WHERE id = %s",
                (_version(connection, CLEAN, day(1)),))
        with monkeypatch.context() as patch:
            repair = _record_as_repair_starts(connection_info, patch, CLEAN)
            _run_pass(story, monkeypatch, nights[3] + timedelta(days=1))
        repair["thread"].join(timeout=60)
        repaired = (("recount", CLEAN, 1) in _open(connection_info),
                    _repair_after_fix(connection_info, CLEAN))

    assert quiet == 0
    assert stopped == (1, None, set())
    assert slow == (1, None, set(), [])
    assert late_before["defenses"] == 7 and late_before["automatic_loss"] == 30
    assert _ranked(board_before)[LATE] == 5030
    # Days 1 to 3, 3 players a step. The story's 42 opponents fought Legend
    # battles, so the league record puts them in Legend I and their days
    # are saved and judged too: 50 players on Days 1 and 2, then 37.
    assert (first_steps, again) == (47, 0)
    planted = {("ledger", LEDGER, 1), ("chain", CHAIN, 2), ("copies", COPIES, 2),
               ("copies", COPIES, 3), ("board", BOARD, 2), ("recount", STUCK, 1)}
    assert first == planted | {("recount", LATE, 2), ("recount", copied, 2),
                               ("missing_day", JOINED, 2)}
    # Oldest day first, 2 a pass, at backfill priority.
    assert first_queued == [(LATE, "reconcile_pass", 25, "pending"),
                            (STUCK, "reconcile_pass", 25, "pending")]
    assert [row[:3] for row in second_queued] == [
        (LATE, "reconcile_pass", 25), (JOINED, "reconcile_pass", 25),
        (copied, "reconcile_pass", 25), (STUCK, "reconcile_pass", 25)]
    assert late_fixed["defenses"] == 8 and late_fixed["automatic_loss"] is None
    assert joined_saved
    # LATE's old board row now disagrees with its recalculated day; STUCK's
    # count survived its recalculation.
    assert after_fixes == planted | {("board", LATE, 2)}
    assert escalated == {STUCK}
    assert _ranked(board_after)[LATE] == 5000
    # The rebuilt board replaced the one both board problems were about.
    assert last == planted - {("board", BOARD, 2)}
    assert unfinished == []
    assert line.startswith(
        "Clash Lens nightly re-check, last 24 hours: 10 new problems (recount 3, chain 1,"
        " ledger 1, battle copies 2, missing days 1, board rows 2); 4 recalculations"
        " queued; 1 still wrong after their recalculation; 5 open, 0 of them with a"
        " failed recalculation.")
    assert repaired == (True, True)


def test_a_pass_whose_last_step_queues_a_recalculation_finishes(
    database_url: str, archive_server, monkeypatch, capsys
) -> None:
    """Days 1 and 2 of the Season, saved by the real worker, and CLEAN's
    Day 2 lost from its page. The pass's last step, Day 2's, finds the
    missing day and queues its recalculation. Until 20 October 2026 that
    step, having saved what it found, raised TypeError writing its log
    line, and the worker process exited: the dev stack's worker never
    started again, so every lookup stayed "checking". The pass must finish,
    its line counting the 1 recalculation, which gives the page its Day 2
    back."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, CLEAN, LATE)
        for player in story.players.values():
            player.battles = _even(day(1)) + _even(day(2))
        story.reset(day(1), dict.fromkeys(story.players, (5000, SEASON)))
        for number in (2, 3):
            story.reads(day(number) - timedelta(minutes=10), dict.fromkeys(story.players, 5000))
            story.reset(day(number), dict.fromkeys(story.players, (5000, SEASON)))
            story.reads(day(number) + timedelta(minutes=20), dict.fromkeys(story.players, 5000))
            story.run_worker()
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "DELETE FROM api_player_daily_logs WHERE ranked_day_start = %s AND player_id ="
                " (SELECT id FROM players WHERE normalized_tag = %s)", (day(2), CLEAN))
        capsys.readouterr()
        steps = _run_pass(story, monkeypatch, day(3) + timedelta(hours=18))
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()
                 if line.startswith('{"event": "reconcile_pass"')]
        found, queued = _open(connection_info), _queued(connection_info)
        story.run_worker()
        with psycopg.connect(connection_info) as connection:
            restored = connection.execute(
                "SELECT count(*) FROM api_player_daily_logs AS log"
                " JOIN players AS player ON player.id = log.player_id"
                " WHERE player.normalized_tag = %s AND log.ranked_day_start = %s",
                (CLEAN, day(2))).fetchone()[0]

    assert steps == 2
    assert [(line["status"], line["day"], line["queued"]) for line in lines] == [
        ("finished", f"{day(2):%Y-%m-%d}", 1)]
    # Its Daily board row has no day to agree with: listed only.
    assert found == {("missing_day", CLEAN, 2), ("board", CLEAN, 2)}
    assert queued == [(CLEAN, "reconcile_pass", 25, "pending")]
    assert restored == 1
