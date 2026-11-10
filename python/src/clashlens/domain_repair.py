"""Repairs of a Season's past results.

The Season repair (``season_repair``) brings every saved day and Daily board
of a Season to the rules the running code calculates them by, named by
``rule_revision``. Raise ``DAY_RULES_REVISION`` in any release that changes
how a saved day or board comes out, then run it once:

- ``preview`` writes nothing: the Season's saved days by state and reason,
  its published boards, and how many players are left to recalculate.
- ``queue`` saves that as the repair's receipt the first time, then repairs
  in order, at most ``max_jobs`` per run, at backfill priority: first, once
  the weekly league record has decided every week of the Season with new
  evidence, the public days saved before their weeks were decided
  (``league_days.settle``); then the saved evidence days are built from
  (``battle_day_repair.enqueue_rebuilds`` and
  ``reset_baselines.repair_current_season_reset_baselines``, each for this
  Season only); then, once every such repair has finished and none has
  failed, every day of the Season of each player with a saved day or a
  Legend I week in it (in a Season before the record, each player's saved
  days), oldest first in one job, each later day starting where the day
  before now ends;
  then, once every such job has finished and none has failed, every Reset
  board of the Season whose
  entries the rules now change (``boundary.queue_board_rebuilds``), started
  without the wait between a board's rebuilds outside the quiet window; then,
  once every board correction of the Season requested by the time that step
  began has finished, every saved Season summary stored again from its
  days. Run it again until it reports ``phase`` ``done``.
- ``receipt`` writes nothing: the receipt's before beside the same counts
  now, the jobs that failed, and whether every published view agrees with
  the days: boards the rules would still change, and saved summaries that
  differ from a fresh projection.

With a ``scope`` from ``REPAIR_SCOPES`` it recalculates only the players a
rule change can affect, chosen when its first queue runs
(``_scoped_players``) and kept on its own receipt, so the full repair of the same revision still
recalculates everyone. Every other step is the full repair's.

Recalculating a day that comes out the same saves nothing new.

A dormant campaign design follows. Three fixes change results already
published for a Season: the 2-star/55%
payout (17, not 18, under trophy rule v2), the five-minute battle day move
and saved armies for reports that have none. Each
fix repairing alone would republish every Reset several times from half-fixed inputs, so a
campaign lists everything they change once, for one coordinated rebuild:

- ``source``: one selected report the payout or day fix changes, listed once
  with every reason, including a needed decode. A battle day move counts
  only while a published day does not yet show it.
- ``decode_batch``: up to 100 battles (keyed by battle id / 100) whose
  selected reports need only a saved army (reason ``catalogue``). An army
  saved under any unit list counts: saved armies hold ids, not names.
- ``day``: one player's saved Legend day, from the first their own reports
  change, or whose saved result does not yet show a finished fix or was
  built from an older result of the day before, through every later saved
  day of the Season, plus the next Season's first saved day as a
  ``dependency`` to recalculate, since it starts from day 28's end. Later
  next-Season days are left to the repair, which recalculates days in order
  and lists any further day whose result changes.
- ``publication``: one Reset whose captured population includes a player
  with a listed day or a battle needing a decode on its day. A queued
  correction counts only through these: once its day or decode is repaired,
  it is ordinary work and is not held.

A payout report whose raw response is gone, or an item in a finalized Season
or past its own Season's correction window, is excluded, never done.

The ``republish-current-season`` command's ``preview`` writes nothing;
``register`` saves the list, or replaces a dormant campaign's list, and holds
nothing; ``activate`` refuses until every stage in ``REQUIRED_STAGES`` has a
handler. An active or paused campaign holds each listed Reset until its item
is done: no new publication build, and corrections, even from decode jobs
queued before the campaign, stay queued. From 06:00 it no longer holds a
Reset that has never published its board or its army results
(``board_deadline.overdue``).
At the Season's end plus seven days the command refuses every write for it.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb

from . import day_recalculation, domain, league_week
from .army_decoder import DECODER_VERSION
from .db import PYTHON_BACKFILL_PRIORITY, Database
from .db_rows import text_value
from .domain import (
    BATTLE_DAY_GRACE,
    HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION,
    RANKED_DAY_DURATION,
    SEASON_ANCHOR_RULE_VERSION,
    SEASON_DURATION,
    SEASON_START_TROPHIES,
    TROPHY_ALLOCATION_RULE_VERSION,
)
from .reconciliation import RECONCILIATION_RULE_VERSION
from .season_lifecycle import SEASON_CLOSE_WAIT
from .source_observation_contract import BATTLE_LOG_SOURCE_OBSERVATION_CONTRACT

# The rules saved days and Daily boards come out by; see season_repair.
DAY_RULES_REVISION = "2026-10-19-league-week"
REPAIR_ACTIONS = ("preview", "queue", "receipt")
REPAIR_SCOPES = ("day-1-or-unplayed",)
_UNFINISHED_JOB_STATES = ("pending", "waiting_retry", "waiting_dependency", "leased")


class RepairPhase(StrEnum):
    """The step a Season repair's ``queue`` run stopped at, reported as ``phase``.

    No column stores it. Each run works out the first unfinished step from
    the repair's receipt (``RepairReceipt``) and the jobs it queued, does
    that step's next bit of work, and reports it:

    - ``league``: weeks of the Season not yet decided, or public days saved
      before their weeks were decided still being settled;
    - ``inputs``: repairs of saved evidence (moved battle days, partial
      Reset pairs) queued now, unfinished or failed;
    - ``days``: players' day recalculations being queued, through
      ``days_queued_through``, or still unfinished or failed;
    - ``boards``: board corrections requested by ``boards_queued_at``
      still unfinished;
    - ``summaries``: saved Season summaries being stored again, through
      ``summaries_through``, then any that differ from their days;
    - ``done``.

    The operator moves it on by running ``queue`` again; jobs finishing let
    the next run pass a step. A failed job holds its step until it is
    retried by hand. Every step is safe to repeat, so a run cut short by a
    crash or restart is simply run again.
    """

    LEAGUE = "league"
    INPUTS = "inputs"
    DAYS = "days"
    BOARDS = "boards"
    SUMMARIES = "summaries"
    DONE = "done"


@dataclass(frozen=True)
class RepairReceipt:
    """A Season repair's saved progress: one ``season_repairs`` row, saved
    by its first ``queue`` run with the Season as it was before.

    ``days_queued_through`` is the last player whose day job is queued, 0
    before any; ``boards_queued_at`` is when the ``boards`` step began;
    ``summaries_through`` is the last player whose summary was stored
    again; ``players`` is a scoped repair's players, kept from its first
    run. Only ``season_repair`` writes it.
    """

    before_days: dict[str, Any]
    before_boards: dict[str, Any]
    days_queued_through: int
    boards_queued_at: datetime | None
    summaries_through: int
    players: list[int] | None


def in_days_step(days_queued_through: int, boards_queued_at: datetime | None) -> bool:
    """Whether a receipt with this progress is in its ``days`` step: day
    jobs queued and the boards step not begun."""
    return days_queued_through > 0 and boards_queued_at is None


def _receipt(connection: Any, season_id: str, revision: str) -> RepairReceipt | None:
    """The Season's repair receipt for ``revision``, if one was saved."""
    row = connection.execute(
        """
        SELECT before_days, before_boards, queued_through_player_id,
               boards_queued_at, summaries_through_player_id, players
        FROM season_repairs
        WHERE official_season_id = %s AND rule_revision = %s
        """,
        (season_id, revision),
    ).fetchone()
    if row is None:
        return None
    return RepairReceipt(
        row[0], row[1], int(row[2]), row[3], int(row[4]),
        None if row[5] is None else [int(player) for player in row[5]],
    )


def season_repair(
    database: Database, season_id: str, action: str, *, max_jobs: int,
    scope: str | None = None,
) -> dict[str, Any]:
    """Run one Season repair action; see the module notes."""
    from . import battle_day_repair, boundary, league_days

    start = datetime.fromtimestamp(int(season_id), UTC)
    if not domain.is_season_boundary(start):
        raise ValueError(f"{season_id} is not a Season's start")
    if scope is not None and scope not in REPAIR_SCOPES:
        raise ValueError(f"{scope} is not a Season repair scope")
    revision = DAY_RULES_REVISION if scope is None else f"{DAY_RULES_REVISION}:{scope}"
    with database.pool.connection() as connection, connection.transaction():
        if connection.execute(
            "SELECT 1 FROM season_detail_retirements WHERE official_season_id = %s",
            (season_id,),
        ).fetchone():
            # Its days can no longer be recalculated.
            return {"season": season_id, "refused": f"season {season_id} is finalized"}
        receipt = _receipt(connection, season_id, revision)
        if scope and receipt is not None:
            # A scoped repair's first run saves its players with the receipt.
            players = list(receipt.players)
        elif scope:
            players = _scoped_players(connection, start)
        else:
            players = [
                int(row[0]) for row in connection.execute(
                    """
                    SELECT player_id FROM ranked_day_versions
                    WHERE ranked_day_start >= %(start)s AND ranked_day_start < %(end)s
                    UNION
                    SELECT player_id FROM player_league_weeks
                    WHERE league = 'legend_i'
                      AND week_start >= %(recorded)s AND week_start < %(end)s
                    ORDER BY player_id
                    """,
                    {"start": start, "end": start + SEASON_DURATION,
                     "recorded": max(start, league_week.RECORDED_FROM)},
                ).fetchall()
            ]
        through = receipt.days_queued_through if receipt is not None else 0
        report = {
            "season": season_id, "rule_revision": revision, "scope": scope,
            "players": len(players),
            "left_to_queue": sum(player > through for player in players),
            **_repair_jobs(connection, revision, season_id,
                           [p for p in players if p <= through], max_jobs),
        }
        if action != "queue":
            inputs = _input_jobs(
                connection, season_id, start, max_jobs,
                battle_day_repair.enqueue_rebuilds(
                    database, max_jobs=max_jobs, season_id=season_id, queue=False
                ),
            )
        if action == "receipt":
            if receipt is None:
                return {**report, "refused": "no repair queued for this revision"}
            return {
                **report,
                "inputs": inputs,
                "boards_queued_at": receipt.boards_queued_at.isoformat()
                if receipt.boards_queued_at else None,
                "days": {"before": receipt.before_days, "now": _day_counts(connection, start)},
                "boards": {"before": receipt.before_boards,
                           "now": _board_counts(connection, start)},
                "boards_disagreeing": [
                    board for board in boundary.queue_board_rebuilds(
                        database, season_id, queue=False
                    )["boards"]
                ],
                "summaries_disagreeing": _stale_summaries(connection, season_id),
            }
        if action == "preview":
            return {
                **report,
                "inputs": inputs,
                "days": _day_counts(connection, start),
                "boards": _board_counts(connection, start),
                "boards_to_rebuild": boundary.queue_board_rebuilds(
                    database, season_id, queue=False
                )["boards"],
            }
        if receipt is None:
            connection.execute(
                """
                INSERT INTO season_repairs (
                    official_season_id, rule_revision, before_days, before_boards,
                    players
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (season_id, revision, Jsonb(_day_counts(connection, start)),
                 Jsonb(_board_counts(connection, start)), players if scope else None),
            )
    with database.pool.connection() as connection, connection.transaction():
        # A Season before the record is calculated without it. Evidence
        # marked since the repair began is new: the decider recalculates the
        # days its decisions change, so only older marks are waited for, and
        # none of a player it set aside (league_decider._decide_batch).
        undecided = connection.execute(
            """
            SELECT count(*) FROM league_week_dirty
            WHERE week_start >= %s AND week_start < %s AND failed_at IS NULL
              AND marked_at < (SELECT started_at FROM season_repairs
                               WHERE official_season_id = %s AND rule_revision = %s)
            """,
            (max(start, league_week.RECORDED_FROM), start + SEASON_DURATION,
             season_id, revision),
        ).fetchone()[0]
        settled = {} if undecided else league_days.settle(database, connection, start, max_jobs)
    if undecided or settled["players_settled"]:
        # Days are recalculated by the decided weeks: a week not Legend I
        # keeps no public day.
        return {**report, "phase": RepairPhase.LEAGUE, "undecided_weeks": int(undecided), **settled}
    inputs = _repair_inputs(database, season_id, start, max_jobs)
    if inputs["queued"] or inputs["unfinished"] or inputs["failed"]:
        # Days wait for every repair of their evidence; a failed one holds
        # them until it is investigated and retried by hand.
        return {**report, "phase": RepairPhase.INPUTS, **inputs}
    if report["left_to_queue"]:
        batch = [player for player in players if player > through][:max_jobs]
        _queue_days(database, season_id, revision, start, batch)
        return {**report, "phase": RepairPhase.DAYS, "queued": len(batch),
                "left_to_queue": report["left_to_queue"] - len(batch)}
    if report["unfinished"] or report["failed"]:
        # A failed recalculation is not retried; it holds every board and
        # summary until it is investigated and retried by hand.
        return {**report, "phase": RepairPhase.DAYS, "queued": 0}
    boards = boundary.queue_board_rebuilds(
        database, season_id, queue=True, repair=True)["boards"]
    with database.pool.connection() as connection, connection.transaction():
        connection.execute(
            """
            UPDATE season_repairs SET boards_queued_at = clock_timestamp()
            WHERE official_season_id = %s AND rule_revision = %s
              AND boards_queued_at IS NULL
            """,
            (season_id, revision),
        )
        # A summary's final rank reads the Season's last board, so summaries
        # wait for every board correction requested by the time this step
        # began. One requested since, as late battles reach a Live board or
        # pacing reaches a past one, runs as ordinary work and is not waited
        # for: one arrives every few minutes while the Season runs.
        rebuilding = connection.execute(
            """
            SELECT count(*) FROM boundary_publication_corrections
            WHERE boundary_at > %s AND boundary_at <= %s
              AND state NOT IN ('finalized', 'terminal')
              AND requested_at <= (
                  SELECT boards_queued_at FROM season_repairs
                  WHERE official_season_id = %s AND rule_revision = %s
              )
            """,
            (start, start + SEASON_DURATION, season_id, revision),
        ).fetchone()[0]
    if rebuilding:
        return {**report, "phase": RepairPhase.BOARDS, "boards": boards,
                "boards_rebuilding": int(rebuilding)}
    summaries = _refresh_summaries(
        database, season_id, revision,
        receipt.summaries_through if receipt is not None else 0, max_jobs,
        None if scope is None else players,
    )
    return {**report, "phase": RepairPhase.SUMMARIES if summaries else RepairPhase.DONE,
            "summaries_refreshed": summaries}


def _scoped_players(connection: Any, start: datetime) -> list[int]:
    """The players with saved days of the Season whose results the 11 October
    2026 day rules can change: those whose Day 1 was saved Inconsistent,
    the only Day 1 results judging a Day 1 reading against the official
    Season-end total can change, and those whose newest profile shows
    Legend I at 5,000, accepted or naming Season 0 (sent before sign-up),
    with no Legend battle of the Season saved, whose days rank at 5,000 with
    no battles."""
    return [
        int(row[0]) for row in connection.execute(
            """
            WITH saved AS (
                SELECT DISTINCT player_id FROM ranked_day_versions
                WHERE ranked_day_start >= %(start)s AND ranked_day_start < %(end)s
            ), played AS (
                SELECT attacker_player_id AS player_id FROM legend_battles
                WHERE ranked_day_start >= %(start)s AND ranked_day_start < %(end)s
                UNION
                SELECT defender_player_id FROM legend_battles
                WHERE ranked_day_start >= %(start)s AND ranked_day_start < %(end)s
            )
            SELECT saved.player_id FROM saved
            JOIN players AS player ON player.id = saved.player_id
            WHERE (
                SELECT day.state FROM ranked_day_versions AS day
                WHERE day.player_id = saved.player_id
                  AND day.ranked_day_start = %(start)s
                ORDER BY day.version DESC, day.id DESC LIMIT 1
            ) = 'Inconsistent'
               OR saved.player_id NOT IN (SELECT player_id FROM played) AND EXISTS (
                SELECT 1 FROM (
                    SELECT eligibility_state, trophies, source_contract_state,
                           current_league_season_id
                    FROM player_profile_versions AS profile
                    WHERE profile.normalized_tag = player.normalized_tag
                    ORDER BY profile.observed_at DESC, profile.id DESC LIMIT 1
                ) AS newest
                WHERE newest.eligibility_state = 'eligible'
                  AND newest.trophies = %(trophies)s
                  AND (newest.source_contract_state = 'accepted'
                       OR newest.current_league_season_id = '0')
            )
            ORDER BY saved.player_id
            """,
            {"start": start, "end": start + SEASON_DURATION,
             "trophies": SEASON_START_TROPHIES},
        ).fetchall()
    ]


def _refresh_summaries(
    database: Database, season_id: str, revision: str, through: int, limit: int,
    only: list[int] | None,
) -> int:
    """Store again up to ``limit`` of the Season's saved summaries, or of
    ``only`` those players', each from its days as they are now, in its own
    transaction; how many it stored.
    Once every one has been, it stores again those that differ from their
    days now, as a board correction since can leave an earlier one, until
    none does. A Season still in progress has none."""
    from .season_summaries import materialize_player_season

    with database.pool.connection() as connection:
        players = [
            int(row[0]) for row in connection.execute(
                """
                SELECT player_id FROM player_season_summaries
                WHERE official_season_id = %s AND player_id > %s
                  AND (%s::bigint[] IS NULL OR player_id = ANY(%s))
                ORDER BY player_id LIMIT %s
                """,
                (season_id, through, only, only, limit),
            ).fetchall()
        ]
        stale = not players
        if stale:
            players = _stale_players(connection, season_id)[1][:limit]
        for player in players:
            with connection.transaction():
                materialize_player_season(connection, player, season_id)
        if players and not stale:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE season_repairs SET summaries_through_player_id = %s
                    WHERE official_season_id = %s AND rule_revision = %s
                    """,
                    (players[-1], season_id, revision),
                )
    return len(players)


def _stale_summaries(connection: Any, season_id: str) -> dict[str, Any]:
    """The Season's saved summaries that differ from their days now, as the
    Season's closure checks them."""
    stored, stale = _stale_players(connection, season_id)
    return {"stored": stored, "stale": len(stale), "stale_players": stale[:50]}


def _stale_players(connection: Any, season_id: str) -> tuple[int, list[int]]:
    """How many summaries the Season has saved, and the players whose saved
    summary differs from its days now."""
    from .season_summaries import matches_current_days

    stale = []
    rows = connection.execute(
        """
        SELECT player_id, content_digest FROM player_season_summaries
        WHERE official_season_id = %s ORDER BY player_id
        """,
        (season_id,),
    ).fetchall()
    for player_id, digest in rows:
        if not matches_current_days(connection, int(player_id), season_id, text_value(digest)):
            stale.append(int(player_id))
    return len(rows), stale


def _repair_jobs(
    connection: Any, revision: str, season_id: str, players: list[int], limit: int
) -> dict[str, Any]:
    """How many of the players' day jobs are unfinished, and the failed ones."""
    return _job_states(connection.execute(
        """
        SELECT id, state, failure_category, (input_json ->> 'player_id')::bigint
        FROM python_processing_jobs_worker
        WHERE (input_json ->> 'player_id')::bigint = ANY(%s)
          AND deduplication_key = ANY(%s)
        ORDER BY id
        """,
        (players, [_repair_key(revision, season_id, player) for player in players]),
    ).fetchall(), limit)


def _job_states(rows: list[Any], limit: int) -> dict[str, Any]:
    """How many of the jobs are unfinished, and the failed ones."""
    failed = [row for row in rows if text_value(row[1]) == "failed"]
    return {
        "unfinished": sum(text_value(row[1]) in _UNFINISHED_JOB_STATES for row in rows),
        "failed": len(failed),
        "failed_blockers": [
            {"job_id": int(row[0]), "player_id": int(row[3]),
             "failure_category": text_value(row[2]) if row[2] else None}
            for row in failed[:limit]
        ],
    }


def _repair_inputs(
    database: Database, season_id: str, start: datetime, max_jobs: int
) -> dict[str, Any]:
    """Queue repairs of the Season's saved evidence first: its battles moved
    day, then its Reset pairs left partial, each pair at most one job. How
    many it queued or re-checked, and how many of the Season's such repairs
    are unfinished or failed."""
    from . import battle_day_repair, reset_baselines

    moved = battle_day_repair.enqueue_rebuilds(
        database, max_jobs=max_jobs, season_id=season_id
    )
    queued = len(moved["job_ids"])
    if not queued:
        pairs = reset_baselines.repair_current_season_reset_baselines(
            database, max_works=max_jobs, season_id=season_id
        )
        queued = max(len(pairs["job_ids"]), pairs["evaluated_count"])
    with database.pool.connection() as connection:
        return {"queued": queued, **_input_jobs(
            connection, season_id, start, max_jobs, moved
        )}


def _input_jobs(
    connection: Any, season_id: str, start: datetime, limit: int,
    moved: dict[str, Any],
) -> dict[str, Any]:
    """How many of the Season's evidence repairs are unfinished, and the
    failed ones, with ``moved``'s failed blockers holding back its moved
    battles."""
    return _job_states(connection.execute(
        """
        SELECT job.id, job.state, job.failure_category,
               (job.input_json ->> 'player_id')::bigint
        FROM python_processing_jobs_worker AS job
        -- The days a job names: its first through its last, or its one day.
        CROSS JOIN LATERAL (
            SELECT (job.input_json ->> 'ranked_day_start')::timestamptz AS first,
                   coalesce(
                       job.input_json ->> 'last_ranked_day_start',
                       job.input_json ->> 'ranked_day_start'
                   )::timestamptz AS last
        ) AS covered
        WHERE job.work_type = 'reconcile_ranked_day'
          AND job.state::text = ANY(%(states)s)
          AND (
              job.deduplication_key LIKE 'reconcile:battle-day:%%'
              OR job.deduplication_key LIKE 'reconcile:reset-baseline:%%'
              OR job.deduplication_key LIKE 'reconcile:reset-recovery:%%'
          )
          -- Any job reaching a day of the Season, such as one from the
          -- previous Season's last day through this one's first, or a
          -- failed one holding back the Season's moved battles.
          AND (
              job.id = ANY(%(moved)s)
              OR job.input_json ->> 'recalculate_season' = %(season)s
              OR covered.first < %(end)s AND covered.last >= %(start)s
          )
          -- A failed Reset repair queued again counts as its retry; once
          -- the retry has run, and after its finished job is cleaned
          -- up, the days it names show it: one saved since the failure
          -- and no ended one left Live, a day it did not change kept as
          -- saved.
          AND NOT EXISTS (
              SELECT 1 FROM python_processing_jobs_worker AS retry
              WHERE retry.deduplication_key
                    = 'reconcile:reset-recovery:' || job.id::text
          )
          AND NOT (
              job.state::text = 'failed'
              AND job.deduplication_key LIKE 'reconcile:reset-%%'
              AND EXISTS (
                  SELECT 1 FROM ranked_day_versions AS day
                  WHERE day.player_id = (job.input_json ->> 'player_id')::bigint
                    AND day.ranked_day_start >= covered.first
                    AND day.ranked_day_start <= covered.last
                    AND day.created_at > job.updated_at
              )
              AND NOT EXISTS (
                  SELECT 1 FROM (
                      SELECT DISTINCT ON (day.ranked_day_start)
                             day.ranked_day_start, day.state
                      FROM ranked_day_versions AS day
                      WHERE day.player_id = (job.input_json ->> 'player_id')::bigint
                        AND day.ranked_day_start >= covered.first
                        AND day.ranked_day_start <= covered.last
                      ORDER BY day.ranked_day_start, day.version DESC, day.id DESC
                  ) AS latest
                  WHERE latest.state = 'Live'
                    AND latest.ranked_day_start + interval '1 day' <= clock_timestamp()
              )
          )
        ORDER BY job.id
        """,
        {
            "states": [*_UNFINISHED_JOB_STATES, "failed"],
            "moved": moved["failed_job_ids"],
            "season": season_id, "start": start, "end": start + SEASON_DURATION,
        },
    ).fetchall(), limit)


def _queue_days(
    database: Database, season_id: str, revision: str, start: datetime,
    players: list[int],
) -> None:
    """Queue one job per player recalculating their days of the Season from
    their first saved day or Legend I week, oldest first, and note the last
    player queued. From the record on, that is every day through the
    current one, saved or not: a Legend I week has a Legend day each day.
    Before it, the saved days only."""

    with database.pool.connection() as connection, connection.transaction():
        _guard_days(connection, season_id)
        today = domain.ranked_day_for(datetime.now(UTC)).start
        recorded = start >= league_week.RECORDED_FROM
        for player, first_day in connection.execute(
            """
            SELECT player_id, min(day) FROM (
                SELECT player_id, ranked_day_start AS day FROM ranked_day_versions
                WHERE player_id = ANY(%(players)s)
                  AND ranked_day_start >= %(start)s AND ranked_day_start < %(end)s
                UNION ALL
                SELECT player_id, week_start FROM player_league_weeks
                WHERE player_id = ANY(%(players)s) AND league = 'legend_i'
                  AND week_start >= %(start)s AND week_start < %(end)s
            ) AS first GROUP BY player_id ORDER BY player_id
            """,
            {"players": players, "start": start, "end": start + SEASON_DURATION},
        ).fetchall():
            day_recalculation.request(
                connection, int(player), first_day, None,
                key=_repair_key(revision, season_id, int(player)), trigger="season_repair",
                priority=PYTHON_BACKFILL_PRIORITY, every_day=recorded,
                last_day=max(first_day, min(today, start + SEASON_DURATION - RANKED_DAY_DURATION))
                if recorded else None,
            )
        connection.execute(
            """
            UPDATE season_repairs SET queued_through_player_id = %s
            WHERE official_season_id = %s AND rule_revision = %s
            """,
            (players[-1], season_id, revision),
        )


def recalculating_days(connection: Any, season_id: str) -> bool:
    """Whether a Season repair is recalculating the Season's days. Holds the
    Season's guard ``_queue_days`` takes until the caller's transaction ends,
    so no repair starts queueing days before then."""
    _guard_days(connection, season_id)
    return any(
        in_days_step(int(through), boards)
        for through, boards in connection.execute(
            "SELECT queued_through_player_id, boards_queued_at FROM season_repairs"
            " WHERE official_season_id = %s",
            (season_id,),
        )
    )


def _guard_days(connection: Any, season_id: str) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"clashlens:season-repair-days:{season_id}",),
    )


def _repair_key(revision: str, season_id: str, player_id: int) -> str:
    return f"reconcile:season-repair:{revision}:{season_id}:{player_id}"


def _day_counts(connection: Any, start: datetime) -> dict[str, Any]:
    """Each ended day of the Season: its players' latest saved results by
    state, and by each reason they give."""
    days: dict[str, Any] = {}
    for day, state, reasons, count in connection.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (player_id, ranked_day_start)
                   ranked_day_start, state, failure_reasons
            FROM ranked_day_versions
            WHERE ranked_day_start >= %s AND ranked_day_start < %s
              AND ranked_day_start + interval '1 day' <= clock_timestamp()
            ORDER BY player_id, ranked_day_start, version DESC, id DESC
        )
        SELECT ranked_day_start, state, failure_reasons, count(*)
        FROM latest GROUP BY 1, 2, 3
        """,
        (start, start + SEASON_DURATION),
    ).fetchall():
        entry = days.setdefault(
            day.astimezone(UTC).isoformat(), {"states": {}, "reasons": {}}
        )
        entry["states"][text_value(state)] = entry["states"].get(text_value(state), 0) + count
        for reason in reasons:
            entry["reasons"][reason] = entry["reasons"].get(reason, 0) + count
    return dict(sorted(days.items()))


def _board_counts(connection: Any, start: datetime) -> dict[str, Any]:
    """Each Reset of the Season: its published Daily board's version and how
    many of its entries are proven or uncertain, with the board rule that
    built it."""
    return {
        boundary_at.astimezone(UTC).isoformat(): {
            "snapshot_id": int(snapshot_id), "version": int(version),
            "rule": text_value(rule), "confirmed": int(confirmed),
            "uncertain": int(uncertain),
        }
        for boundary_at, snapshot_id, version, rule, confirmed, uncertain
        in connection.execute(
            """
            SELECT DISTINCT ON (snapshot.boundary_at)
                   snapshot.boundary_at, snapshot.id, snapshot.version,
                   snapshot.ordering_rule_version,
                   (SELECT count(*) FILTER (WHERE entry.confidence = 'confirmed')
                    FROM leaderboard_snapshot_entries AS entry
                    WHERE entry.snapshot_id = snapshot.id),
                   (SELECT count(*) FILTER (WHERE entry.confidence <> 'confirmed')
                    FROM leaderboard_snapshot_entries AS entry
                    WHERE entry.snapshot_id = snapshot.id)
            FROM leaderboard_snapshots AS snapshot
            WHERE snapshot.snapshot_kind = 'frozen' AND snapshot.state = 'published'
              AND snapshot.boundary_at > %s AND snapshot.boundary_at <= %s
            ORDER BY snapshot.boundary_at, snapshot.version DESC, snapshot.id DESC
            """,
            (start, start + SEASON_DURATION),
        ).fetchall()
    }


ACTIONS = ("preview", "register", "activate")
# Campaign execution is not installed; preview and registration remain available.
REQUIRED_STAGES = ("battle_reports", "army_decodes", "day_results", "publications")


class CampaignRefused(ValueError):
    """The campaign write is not allowed now."""


class CampaignState(StrEnum):
    """The states of ``domain_repair_campaigns.state``.

    - ``registered``: ``register`` saved its list; it holds nothing, and
      registering again replaces the list with what is still outstanding.
    - ``active``: ``activate`` moved it on; it holds each listed Reset until
      that Reset's item is done. Registering again is refused.
    - ``paused`` and ``complete``: allowed by the database, never written by
      the current code; items ``done`` and ``failed`` likewise.
      ``boundary_held`` still counts a ``paused`` campaign as holding, as the
      schema intends.

    ``activate`` is the only transition, and it refuses until every stage
    in ``REQUIRED_STAGES`` has a handler. None does yet, so no campaign
    becomes active on this code. A refused or failed command writes nothing
    (one transaction); run it again.
    """

    REGISTERED = "registered"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETE = "complete"


def campaign_window(season_start: datetime) -> tuple[datetime, datetime]:
    """The Season's end and the moment its correction window closes."""
    end = season_start + SEASON_DURATION
    return end, end + SEASON_CLOSE_WAIT


def target_versions() -> dict[str, str]:
    """The versions every listed result must reach, pinned at registration."""
    return {
        "battle_parser": BATTLE_LOG_SOURCE_OBSERVATION_CONTRACT.default_parser_version,
        "trophy_rule": TROPHY_ALLOCATION_RULE_VERSION,
        "battle_day_grace": str(BATTLE_DAY_GRACE),
        "army_decoder": DECODER_VERSION,
        "reconciliation_rule": RECONCILIATION_RULE_VERSION,
    }


_INVENTORY = """
WITH selected AS (
    SELECT p.battle_id, p.evidence_id, b.ranked_day_start, e.reporting_player_id AS player_id,
           b.attacker_player_id, b.defender_player_id, e.stars,
           e.destruction_percentage, e.trophy_rule_version, e.army_share_code,
           date_bin('1 day', e.battle_timestamp - %(grace)s,
                    timestamptz '2000-01-01 05:00:00+00') AS own_day
    FROM legend_battles AS b
    JOIN battle_perspectives AS p ON p.battle_id = b.id
    JOIN battle_evidence AS e ON e.id = p.evidence_id
    WHERE b.ranked_day_start >= %(start)s AND b.ranked_day_start < %(end)s
), changes AS (
    SELECT evidence_id, ranked_day_start AS from_day,
           ranked_day_start AS to_day, 'payout' AS reason
    FROM selected
    WHERE stars = 2 AND destruction_percentage = 55
      AND trophy_rule_version = %(old_rule)s
    UNION ALL
    SELECT evidence_id, ranked_day_start, own_day, 'moved'
    FROM selected WHERE ranked_day_start <> own_day
    UNION ALL
    SELECT evidence_id, from_day, to_day, 'moved'
    FROM ({unfinished_moves}) AS move
    WHERE from_day >= %(start)s AND from_day < %(end)s
       OR to_day >= %(start)s AND to_day < %(end)s
), needs_decode AS (
    SELECT battle_id, evidence_id, ranked_day_start, attacker_player_id,
           defender_player_id
    FROM selected
    WHERE army_share_code IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM battle_army_decodes AS d
        WHERE d.evidence_id = selected.evidence_id AND d.is_active
          AND d.decoder_version = %(decoder)s
    )
), sources AS (
    SELECT c.evidence_id, e.battle_id, e.reporting_player_id AS player_id,
           e.observation_id,
           (array_agg(c.from_day ORDER BY c.reason = 'moved' DESC))[1] AS from_day,
           (array_agg(c.to_day ORDER BY c.reason = 'moved' DESC))[1] AS to_day,
           array_agg(DISTINCT c.reason) AS reasons
    FROM changes AS c JOIN battle_evidence AS e ON e.id = c.evidence_id
    GROUP BY 1, 2, 3, 4
), affected AS (
    -- Players whose saved days these fixes change, repaired yet or not.
    SELECT player_id FROM selected WHERE stars = 2 AND destruction_percentage = 55
    UNION
    SELECT CASE perspective WHEN 'attacker' THEN attacker_player_id
                            ELSE defender_player_id END
    FROM battle_day_repairs
    WHERE from_day >= %(start)s AND from_day < %(end)s
       OR to_day >= %(start)s AND to_day < %(end)s
), newest AS (
    SELECT DISTINCT ON (player_id, ranked_day_start) player_id, ranked_day_start, id
    FROM ranked_day_versions
    WHERE player_id IN (SELECT player_id FROM affected)
      AND ranked_day_start >= %(start)s - interval '1 day'
      AND ranked_day_start <= %(end)s
    ORDER BY player_id, ranked_day_start, id DESC
), saved AS (
    SELECT newest.player_id, newest.ranked_day_start, newest.id,
           v.input_evidence -> 'previous_day' ->> 'version_id' AS built_from,
           v.trophy_allocation_rule_versions ? %(old_rule)s AS old_payout
    FROM newest JOIN ranked_day_versions AS v ON v.id = newest.id
), touched AS (
    SELECT player_id, day, reason
    FROM sources, unnest(ARRAY[from_day, to_day]) AS day, unnest(reasons) AS reason
    UNION ALL
    -- A report read again under the new payout whose saved day still
    -- counts the old one.
    SELECT s.player_id, s.ranked_day_start, 'payout'
    FROM selected AS s
    JOIN saved ON saved.player_id = s.player_id
              AND saved.ranked_day_start = s.ranked_day_start
    WHERE s.stars = 2 AND s.destruction_percentage = 55 AND saved.old_payout
    UNION ALL
    -- A saved day built from an earlier result of the day before it.
    SELECT saved.player_id, saved.ranked_day_start, 'dependency'
    FROM saved
    JOIN newest AS previous
      ON previous.player_id = saved.player_id
     AND previous.ranked_day_start = saved.ranked_day_start - interval '1 day'
    WHERE saved.ranked_day_start >= %(start)s
      AND saved.built_from IS DISTINCT FROM previous.id::text
), days AS (
    SELECT v.player_id, v.ranked_day_start, min(v.official_season_id) AS season,
           coalesce(array_agg(DISTINCT t.reason) FILTER (WHERE t.reason IS NOT NULL),
                    ARRAY['dependency']) AS reasons
    FROM (SELECT player_id, min(day) AS first_day FROM touched GROUP BY 1) AS chain
    JOIN ranked_day_versions AS v
      ON v.player_id = chain.player_id
     AND v.ranked_day_start >= chain.first_day AND v.ranked_day_start <= %(end)s
    LEFT JOIN touched AS t
      ON t.player_id = v.player_id AND t.day = v.ranked_day_start
    GROUP BY 1, 2
), population AS (
    SELECT sweep.boundary_at, member.player_id
    FROM collector_reset_sweeps AS sweep, unnest(sweep.member_ids) AS member(player_id)
    WHERE sweep.boundary_at >= %(start)s
      AND sweep.boundary_at <= %(end)s + interval '1 day'
)
SELECT 'source', 'evidence:' || evidence_id,
       reasons || CASE WHEN evidence_id IN (SELECT evidence_id FROM needs_decode)
                       THEN ARRAY['catalogue'] ELSE ARRAY[]::text[] END,
       player_id, battle_id, evidence_id, observation_id, NULL::timestamptz,
       from_day, to_day, NULL, NULL::timestamptz, NULL::bigint[],
       CASE WHEN 'payout' = ANY(reasons) AND NOT EXISTS (
           SELECT 1 FROM collector_observations AS o
           JOIN archive_catalogue AS a ON a.archive_reference = o.archive_reference
           WHERE o.id = sources.observation_id AND a.availability = 'verified'
       ) THEN 'raw_unavailable' END
FROM sources
UNION ALL
SELECT 'decode_batch', 'battles:' || battle_id / 100, ARRAY['catalogue'], NULL,
       NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       array_agg(DISTINCT battle_id), NULL
FROM needs_decode WHERE evidence_id NOT IN (SELECT evidence_id FROM sources)
GROUP BY battle_id / 100
UNION ALL
SELECT 'day', 'day:' || player_id || ':' || extract(epoch FROM ranked_day_start)::bigint,
       reasons, player_id, NULL, NULL, NULL, ranked_day_start, NULL, NULL,
       season, NULL, NULL, NULL
FROM days
UNION ALL
SELECT 'publication', 'boundary:' || extract(epoch FROM boundary_at)::bigint,
       array_agg(DISTINCT reason), NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       NULL, boundary_at, NULL, NULL
FROM (
    SELECT population.boundary_at, reason
    FROM days, unnest(reasons) AS reason, population
    WHERE population.player_id = days.player_id
      AND population.boundary_at = days.ranked_day_start + interval '1 day'
    UNION ALL
    SELECT DISTINCT population.boundary_at, 'catalogue'
    FROM needs_decode,
         unnest(ARRAY[attacker_player_id, defender_player_id]) AS player(id),
         population
    WHERE population.player_id = player.id
      AND population.boundary_at = needs_decode.ranked_day_start + interval '1 day'
) AS publication
GROUP BY boundary_at
"""
_COLUMNS = (
    "kind", "target_key", "reasons", "player_id", "battle_id", "evidence_id",
    "observation_id", "ranked_day_start", "from_day", "to_day",
    "official_season_id", "boundary_at", "battle_ids", "exclusion",
)
_INSERT = f"""
INSERT INTO domain_repair_items (campaign_id, {", ".join(_COLUMNS)}, state)
VALUES (%s, {", ".join(["%s"] * len(_COLUMNS))},
        CASE WHEN %s::text IS NULL THEN 'pending' ELSE 'excluded' END)
"""


def _inventory(connection: Any, season_id: str, start: datetime, now: datetime) -> list[dict]:
    finalized = {
        text_value(row[0])
        for row in connection.execute(
            "SELECT official_season_id FROM season_detail_retirements"
        ).fetchall()
    }
    from .battle_day_repair import UNFINISHED_MOVES

    parameters = {
        "start": start, "end": campaign_window(start)[0], "grace": BATTLE_DAY_GRACE,
        "old_rule": HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION,
        "decoder": DECODER_VERSION,
    }
    items = []
    query = _INVENTORY.format(unfinished_moves=UNFINISHED_MOVES)
    for row in connection.execute(query, parameters).fetchall():
        item = dict(zip(_COLUMNS, row, strict=True))
        item.update(kind=text_value(item["kind"]),
                    reasons=sorted({text_value(value) for value in item["reasons"]}))
        if item["battle_ids"] is not None:
            item["battle_ids"] = sorted(item["battle_ids"])
        # A moved battle can reach the previous Season's last day, and day
        # 28's end the next Season's first: each keeps its own window.
        day = item["ranked_day_start"] or (
            item["boundary_at"] - RANKED_DAY_DURATION if item["boundary_at"] else start
        )
        own_start = start + SEASON_DURATION * ((day - start) // SEASON_DURATION)
        season = text_value(item["official_season_id"]) or (
            season_id if own_start == start else None
        )
        if item["exclusion"] is None and season in finalized:
            item["exclusion"] = "season_finalized"
        elif item["exclusion"] is None and now >= campaign_window(own_start)[1]:
            item["exclusion"] = "window_expired"
        items.append(item)
    return items


def _summary(items: list[dict]) -> dict[str, Any]:
    """Counts and a digest that changes whenever the listed plan changes."""
    plan = sorted(
        [item["kind"], item["target_key"], item["reasons"], item["exclusion"],
         item["battle_ids"]]
        for item in items
    )
    return {
        "plan_digest": hashlib.sha256(
            json.dumps([target_versions(), plan], separators=(",", ":")).encode()
        ).hexdigest(),
        "items": dict(Counter(item["kind"] for item in items)),
        "reasons": dict(Counter(
            f"{item['kind']}:{reason}" for item in items for reason in item["reasons"]
        )),
        "excluded": dict(Counter(item["exclusion"] for item in items if item["exclusion"])),
        "decode_battles": sum(len(item["battle_ids"] or []) for item in items),
    }


def _open(connection: Any, season_id: str, now: datetime | None, *, write: bool) -> tuple:
    """The Season's start and window; a write first checks the Season is open."""
    row = connection.execute(
        """
        SELECT season.start FROM legend_season_anchors,
             LATERAL (VALUES (current_league_season_id, current_start),
                             (previous_league_season_id, previous_start))
                 AS season(id, start)
        WHERE state = 'confirmed' AND anchor_rule_version = %s AND season.id = %s
        ORDER BY confirmed_at DESC LIMIT 1
        """,
        (SEASON_ANCHOR_RULE_VERSION, season_id),
    ).fetchone()
    if row is None:
        raise CampaignRefused(f"season {season_id} has no confirmed start")
    start = row[0].astimezone(UTC)
    deadline = campaign_window(start)[1]
    if not write:
        now = now or connection.execute("SELECT clock_timestamp()").fetchone()[0]
        return start, deadline, now
    from .season_lifecycle import acquire_season_lock_shared

    acquire_season_lock_shared(connection, season_id)
    if connection.execute(
        "SELECT 1 FROM season_detail_retirements WHERE official_season_id = %s",
        (season_id,),
    ).fetchone():
        raise CampaignRefused(f"season {season_id} is finalized")
    return start, deadline, _still_open(connection, season_id, deadline, now)


def _still_open(connection: Any, season_id: str, deadline: datetime,
                now: datetime | None) -> datetime:
    """The time now, refusing once the Season's correction window closed."""
    now = now or connection.execute("SELECT clock_timestamp()").fetchone()[0]
    if now >= deadline:
        raise CampaignRefused(
            f"season {season_id} correction window closed at {deadline.isoformat()}"
        )
    return now


def preview(database: Database, season_id: str, *, now: datetime | None = None) -> dict:
    """What a campaign for the Season would list, read without any write."""
    with database.pool.connection() as connection, connection.transaction():
        connection.execute("SET TRANSACTION READ ONLY")
        start, deadline, now = _open(connection, season_id, now, write=False)
        items = _inventory(connection, season_id, start, now)
        return {"action": "preview", "season": season_id, "open": now < deadline,
                "write_deadline": deadline.isoformat(),
                "target_versions": target_versions(), **_summary(items)}


def register(database: Database, season_id: str, *, now: datetime | None = None) -> dict:
    """Save the Season's campaign list, holding nothing until activation.

    Registering again replaces the list with what is still outstanding.
    Refused once the campaign is activated, so no held item is dropped.
    """
    with database.pool.connection() as connection, connection.transaction():
        start, deadline, cutoff = _open(connection, season_id, now, write=True)
        items = _inventory(connection, season_id, start, cutoff)
        connection.execute(
            """
            INSERT INTO domain_repair_campaigns (
                official_season_id, season_start, season_end, write_deadline,
                target_versions, inventory_cutoff, plan_digest
            ) VALUES (%s, %s, %s, %s, %s, %s, repeat('0', 64))
            ON CONFLICT (official_season_id) DO NOTHING
            """,
            (season_id, start, campaign_window(start)[0], deadline,
             Jsonb(target_versions()), cutoff),
        )
        campaign_id, state = connection.execute(
            "SELECT id, state FROM domain_repair_campaigns"
            " WHERE official_season_id = %s FOR UPDATE",
            (season_id,),
        ).fetchone()
        if text_value(state) != CampaignState.REGISTERED:
            raise CampaignRefused(f"season {season_id} campaign is {text_value(state)}")
        connection.execute(
            "DELETE FROM domain_repair_items WHERE campaign_id = %s", (campaign_id,)
        )
        with connection.cursor() as cursor:
            cursor.executemany(_INSERT, [
                (campaign_id, *(item[column] for column in _COLUMNS), item["exclusion"])
                for item in items
            ])
        summary = _summary(items)
        connection.execute(
            """
            UPDATE domain_repair_campaigns
            SET plan_digest = %s, counts = %s, inventory_cutoff = %s,
                target_versions = %s, updated_at = clock_timestamp()
            WHERE id = %s
            """,
            (summary["plan_digest"], Jsonb(summary), cutoff, Jsonb(target_versions()),
             campaign_id),
        )
        _still_open(connection, season_id, deadline, now)
        return {"action": "register", "season": season_id, "campaign_id": campaign_id,
                "write_deadline": deadline.isoformat(), **summary}


def activate(database: Database, season_id: str, *, now: datetime | None = None) -> dict:
    """Refuse activation until campaign execution is implemented."""
    raise CampaignRefused(f"repair stages not installed: {', '.join(REQUIRED_STAGES)}")


def run_campaign_command(database: Database, action: str, season_id: str) -> dict:
    """Run one campaign action for the CLI, reporting a refusal."""
    try:
        return {"preview": preview, "register": register, "activate": activate}[action](
            database, season_id
        )
    except CampaignRefused as refused:
        return {"action": action, "season": season_id, "refused": str(refused)}
