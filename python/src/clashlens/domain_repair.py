"""Repairs of a Season's past results.

The Season repair (``season_repair``) brings every saved day and Daily board
of a Season to the rules the running code calculates them by, named by
``rule_revision``. Raise ``DAY_RULES_REVISION`` in any release that changes
how a saved day or board comes out, then run it once:

- ``preview`` writes nothing: the Season's saved days by state and reason,
  its published boards, and how many players are left to recalculate.
- ``queue`` saves that as the repair's receipt the first time, then repairs
  in order, at most ``max_jobs`` per run, at backfill priority: first the
  saved evidence days are built from (``battle_day_repair.enqueue_rebuilds``
  and ``reset_baselines.repair_current_season_reset_baselines``, each for
  this Season only); then each player's saved days of the Season, oldest
  first in one job, each later day starting where the day before now ends;
  then, once every such job has finished and none has failed, every Reset
  board of the Season whose
  entries the rules now change (``boundary.queue_board_rebuilds``); then,
  once every board correction of the Season has finished, every saved
  Season summary stored again from its days. Run it again until it reports
  ``phase`` ``done``.
- ``receipt`` writes nothing: the receipt's before beside the same counts
  now, the jobs that failed, and whether every published view agrees with
  the days: boards the rules would still change, and saved summaries that
  differ from a fresh projection.

Recalculating a day that comes out the same saves nothing new.

A dormant campaign design follows. Four fixes change results already
published for a Season: the 2-star/55%
payout (17, not 18, under trophy rule v2), the five-minute battle day move,
unit catalogue v2 decodes and accepted Reset settlements. Each fix repairing
alone would republish every Reset several times from half-fixed inputs, so a
campaign lists everything they change once, for one coordinated rebuild:

- ``source``: one selected report the payout or day fix changes, listed once
  with every reason, including a needed decode. A battle day move counts
  only while a published day does not yet show it.
- ``decode_batch``: up to 100 battles (keyed by battle id / 100) whose
  selected reports need only a catalogue v2 decode.
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
queued before the campaign, stay queued.
At the Season's end plus seven days the command refuses every write for it.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import domain
from .army_decoder import DECODER_VERSION
from .catalog import CATALOG_VERSION
from .db import PYTHON_BACKFILL_PRIORITY, Database, _text_value
from .domain import (
    BATTLE_DAY_GRACE,
    HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION,
    RANKED_DAY_DURATION,
    SEASON_ANCHOR_RULE_VERSION,
    SEASON_DURATION,
    TROPHY_ALLOCATION_RULE_VERSION,
)
from .reconciliation import RECONCILIATION_RULE_VERSION
from .source_observation_contract import BATTLE_LOG_SOURCE_OBSERVATION_CONTRACT

# The rules saved days and Daily boards come out by; see season_repair.
DAY_RULES_REVISION = "2026-10-08-shared-reset-proof"
REPAIR_ACTIONS = ("preview", "queue", "receipt")
_UNFINISHED_JOB_STATES = ("pending", "waiting_retry", "waiting_dependency", "leased")


def season_repair(
    database: Database, season_id: str, action: str, *, max_jobs: int
) -> dict[str, Any]:
    """Run one Season repair action; see the module notes."""
    from . import boundary

    start = datetime.fromtimestamp(int(season_id), UTC)
    if not domain.is_season_boundary(start):
        raise ValueError(f"{season_id} is not a Season's start")
    revision = DAY_RULES_REVISION
    with database.pool.connection() as connection, connection.transaction():
        if connection.execute(
            "SELECT 1 FROM season_detail_retirements WHERE official_season_id = %s",
            (season_id,),
        ).fetchone():
            # Its days can no longer be recalculated.
            return {"season": season_id, "refused": f"season {season_id} is finalized"}
        receipt = connection.execute(
            """
            SELECT before_days, before_boards, queued_through_player_id,
                   boards_queued_at, summaries_through_player_id
            FROM season_repairs
            WHERE official_season_id = %s AND rule_revision = %s
            """,
            (season_id, revision),
        ).fetchone()
        players = [
            int(row[0]) for row in connection.execute(
                """
                SELECT DISTINCT player_id FROM ranked_day_versions
                WHERE ranked_day_start >= %s AND ranked_day_start < %s
                ORDER BY player_id
                """,
                (start, start + SEASON_DURATION),
            ).fetchall()
        ]
        through = int(receipt[2]) if receipt is not None else 0
        report = {
            "season": season_id, "rule_revision": revision,
            "players": len(players),
            "left_to_queue": sum(player > through for player in players),
            **_repair_jobs(connection, revision, season_id,
                           [p for p in players if p <= through], max_jobs),
        }
        if action == "receipt":
            if receipt is None:
                return {**report, "refused": "no repair queued for this revision"}
            return {
                **report,
                "boards_queued_at": receipt[3].isoformat() if receipt[3] else None,
                "days": {"before": receipt[0], "now": _day_counts(connection, start)},
                "boards": {"before": receipt[1], "now": _board_counts(connection, start)},
                # Checks that every published view now agrees with the days.
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
                    official_season_id, rule_revision, before_days, before_boards
                ) VALUES (%s, %s, %s, %s)
                """,
                (season_id, revision, Jsonb(_day_counts(connection, start)),
                 Jsonb(_board_counts(connection, start))),
            )
    inputs = _repair_inputs(database, season_id, max_jobs)
    if inputs:
        return {**report, "phase": "inputs", "queued": inputs}
    if report["left_to_queue"]:
        batch = [player for player in players if player > through][:max_jobs]
        _queue_days(database, season_id, revision, start, batch)
        return {**report, "phase": "days", "queued": len(batch),
                "left_to_queue": report["left_to_queue"] - len(batch)}
    if report["unfinished"] or report["failed"]:
        # A failed recalculation is not retried; it holds every board and
        # summary until it is investigated and retried by hand.
        return {**report, "phase": "days", "queued": 0}
    boards = boundary.queue_board_rebuilds(database, season_id, queue=True)["boards"]
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
        # wait for every board correction to finish.
        rebuilding = connection.execute(
            """
            SELECT count(*) FROM boundary_publication_corrections
            WHERE boundary_at > %s AND boundary_at <= %s
              AND state NOT IN ('finalized', 'terminal')
            """,
            (start, start + SEASON_DURATION),
        ).fetchone()[0]
    if boards or rebuilding:
        return {**report, "phase": "boards", "boards": boards,
                "boards_rebuilding": int(rebuilding)}
    summaries = _refresh_summaries(
        database, season_id, revision,
        int(receipt[4]) if receipt is not None else 0, max_jobs,
    )
    return {**report, "phase": "summaries" if summaries else "done",
            "summaries_refreshed": summaries}


def _refresh_summaries(
    database: Database, season_id: str, revision: str, through: int, limit: int
) -> int:
    """Store again up to ``limit`` of the Season's saved summaries, each from
    its days as they are now, in its own transaction; how many it stored.
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
                ORDER BY player_id LIMIT %s
                """,
                (season_id, through, limit),
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
    from .season_summaries import _digest, _project

    stale = []
    rows = connection.execute(
        """
        SELECT player_id, content_digest FROM player_season_summaries
        WHERE official_season_id = %s ORDER BY player_id
        """,
        (season_id,),
    ).fetchall()
    for player_id, digest in rows:
        projected = _project(int(player_id), season_id, connection)
        if projected is None or _digest(projected) != _text_value(digest):
            stale.append(int(player_id))
    return len(rows), stale


def _repair_jobs(
    connection: Any, revision: str, season_id: str, players: list[int], limit: int
) -> dict[str, Any]:
    """How many of the players' day jobs are unfinished, and the failed ones."""
    keys = {_repair_key(revision, season_id, player): player for player in players}
    rows = connection.execute(
        """
        SELECT deduplication_key, id, state, failure_category
        FROM python_processing_jobs_worker
        WHERE deduplication_key = ANY(%s)
        ORDER BY id
        """,
        (list(keys),),
    ).fetchall()
    failed = [row for row in rows if _text_value(row[2]) == "failed"]
    return {
        "unfinished": sum(_text_value(row[2]) in _UNFINISHED_JOB_STATES for row in rows),
        "failed": len(failed),
        "failed_blockers": [
            {"job_id": int(row[1]), "player_id": keys[_text_value(row[0])],
             "failure_category": _text_value(row[3]) if row[3] else None}
            for row in failed[:limit]
        ],
    }


def _repair_inputs(database: Database, season_id: str, max_jobs: int) -> int:
    """Queue repairs of the Season's saved evidence first: its battles moved
    day, then its Reset pairs left partial, then its Reset settlement checks
    judged under an older proof rule. How many it queued or judged."""
    from . import battle_day_repair, reset_baselines

    moved = battle_day_repair.enqueue_rebuilds(
        database, max_jobs=max_jobs, season_id=season_id
    )
    if moved["job_ids"]:
        return len(moved["job_ids"])
    pairs = reset_baselines.repair_current_season_reset_baselines(
        database, max_works=max_jobs, season_id=season_id
    )
    if pairs["job_ids"] or pairs["evaluated_count"]:
        return max(len(pairs["job_ids"]), pairs["evaluated_count"])
    return _rejudge_checks(database, season_id, max_jobs)


def _rejudge_checks(database: Database, season_id: str, limit: int) -> int:
    """Judge again, under the current proof rule, up to ``limit`` of the
    Season's Reset settlement checks judged under an older one, or never,
    oldest Reset first, each in its own transaction; how many it judged."""
    from . import reset_settlement

    start = datetime.fromtimestamp(int(season_id), UTC)
    with database.pool.connection() as connection:
        if not reset_settlement._has_settlements(database, connection):
            return 0
        rows = connection.execute(
            """
            SELECT player_id, boundary_at FROM reset_boundary_settlements
            WHERE boundary_at > %s AND boundary_at <= %s
              AND delayed_work_id IS NOT NULL
              AND proof_rule_version IS DISTINCT FROM %s
            ORDER BY boundary_at, player_id
            LIMIT %s
            """,
            (start, start + SEASON_DURATION, reset_settlement.PROOF_RULE_VERSION, limit),
        ).fetchall()
        for player_id, boundary_at in rows:
            with connection.transaction():
                reset_settlement.refresh_boundary(
                    database, connection, int(player_id), boundary_at
                )
    return len(rows)


def _queue_days(
    database: Database, season_id: str, revision: str, start: datetime,
    players: list[int],
) -> None:
    """Queue one job per player recalculating their saved days of the Season
    from the first, oldest first, and note the last player queued."""
    from . import first_battle_log

    with database.pool.connection() as connection, connection.transaction():
        for player, first_day in connection.execute(
            """
            SELECT player_id, min(ranked_day_start) FROM ranked_day_versions
            WHERE player_id = ANY(%s)
              AND ranked_day_start >= %s AND ranked_day_start < %s
            GROUP BY player_id ORDER BY player_id
            """,
            (players, start, start + SEASON_DURATION),
        ).fetchall():
            first_battle_log._queue(
                connection, int(player), first_day, None,
                key=_repair_key(revision, season_id, int(player)), trigger="season_repair",
                priority=PYTHON_BACKFILL_PRIORITY,
            )
        connection.execute(
            """
            UPDATE season_repairs SET queued_through_player_id = %s
            WHERE official_season_id = %s AND rule_revision = %s
            """,
            (players[-1], season_id, revision),
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
        entry["states"][_text_value(state)] = entry["states"].get(_text_value(state), 0) + count
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
            "rule": _text_value(rule), "confirmed": int(confirmed),
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
# Each later repair change adds its stage; activation refuses until all exist.
REQUIRED_STAGES = ("battle_reports", "army_decodes", "day_results", "publications")
HANDLERS: dict[str, Any] = {}


class CampaignRefused(ValueError):
    """The campaign write is not allowed now."""


def campaign_window(season_start: datetime) -> tuple[datetime, datetime]:
    """The Season's end and the moment its correction window closes."""
    end = season_start + SEASON_DURATION
    return end, end + timedelta(days=7)


def target_versions() -> dict[str, str]:
    """The versions every listed result must reach, pinned at registration."""
    return {
        "battle_parser": BATTLE_LOG_SOURCE_OBSERVATION_CONTRACT.default_parser_version,
        "trophy_rule": TROPHY_ALLOCATION_RULE_VERSION,
        "battle_day_grace": str(BATTLE_DAY_GRACE),
        "army_decoder": DECODER_VERSION,
        "unit_catalog": CATALOG_VERSION,
        "reconciliation_rule": RECONCILIATION_RULE_VERSION,
    }


def boundary_held(connection: Any, boundary_at: datetime) -> bool:
    """Whether an active or paused campaign still holds this Reset."""
    return connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM domain_repair_items AS item
            JOIN domain_repair_campaigns AS campaign ON campaign.id = item.campaign_id
            WHERE item.kind = 'publication' AND item.boundary_at = %s
              AND item.state IN ('pending', 'failed')
              AND campaign.state IN ('active', 'paused')
        )
        """,
        (boundary_at,),
    ).fetchone()[0]


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
          AND d.decoder_version = %(decoder)s AND d.catalog_version = %(catalog)s
    )
), sources AS (
    SELECT c.evidence_id, e.battle_id, e.reporting_player_id AS player_id,
           e.observation_id,
           (array_agg(c.from_day ORDER BY c.reason = 'moved' DESC))[1] AS from_day,
           (array_agg(c.to_day ORDER BY c.reason = 'moved' DESC))[1] AS to_day,
           array_agg(DISTINCT c.reason) AS reasons
    FROM changes AS c JOIN battle_evidence AS e ON e.id = c.evidence_id
    GROUP BY 1, 2, 3, 4
), settled AS (
    SELECT player_id, boundary_at - interval '1 day' AS day
    FROM reset_boundary_settlements
    WHERE state = 'settled' AND boundary_at > %(start)s AND boundary_at <= %(end)s
), affected AS (
    -- Players whose saved days these fixes change, repaired yet or not.
    SELECT player_id FROM selected WHERE stars = 2 AND destruction_percentage = 55
    UNION
    SELECT CASE perspective WHEN 'attacker' THEN attacker_player_id
                            ELSE defender_player_id END
    FROM battle_day_repairs
    WHERE from_day >= %(start)s AND from_day < %(end)s
       OR to_day >= %(start)s AND to_day < %(end)s
    UNION
    SELECT player_id FROM settled
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
    UNION ALL
    SELECT player_id, day, 'settlement' FROM settled
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
        _text_value(row[0])
        for row in connection.execute(
            "SELECT official_season_id FROM season_detail_retirements"
        ).fetchall()
    }
    from .battle_day_repair import UNFINISHED_MOVES

    parameters = {
        "start": start, "end": campaign_window(start)[0], "grace": BATTLE_DAY_GRACE,
        "old_rule": HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION,
        "decoder": DECODER_VERSION, "catalog": CATALOG_VERSION,
    }
    items = []
    query = _INVENTORY.format(unfinished_moves=UNFINISHED_MOVES)
    for row in connection.execute(query, parameters).fetchall():
        item = dict(zip(_COLUMNS, row, strict=True))
        item.update(kind=_text_value(item["kind"]),
                    reasons=sorted({_text_value(value) for value in item["reasons"]}))
        if item["battle_ids"] is not None:
            item["battle_ids"] = sorted(item["battle_ids"])
        # A moved battle can reach the previous Season's last day, and day
        # 28's end the next Season's first: each keeps its own window.
        day = item["ranked_day_start"] or (
            item["boundary_at"] - RANKED_DAY_DURATION if item["boundary_at"] else start
        )
        own_start = start + SEASON_DURATION * ((day - start) // SEASON_DURATION)
        season = _text_value(item["official_season_id"]) or (
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
    from .season_retirement import acquire_season_lock_shared

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
        if _text_value(state) != "registered":
            raise CampaignRefused(f"season {season_id} campaign is {_text_value(state)}")
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
    """Start holding the campaign's Resets, once every stage can run."""
    missing = [stage for stage in REQUIRED_STAGES if stage not in HANDLERS]
    if missing:
        raise CampaignRefused(f"repair stages not installed: {', '.join(missing)}")
    with database.pool.connection() as connection, connection.transaction():
        deadline = _open(connection, season_id, now, write=True)[1]
        row = connection.execute(
            "SELECT id, state, target_versions FROM domain_repair_campaigns"
            " WHERE official_season_id = %s FOR UPDATE",
            (season_id,),
        ).fetchone()
        if row is None or _text_value(row[1]) != "registered":
            raise CampaignRefused(f"season {season_id} has no registered campaign")
        if row[2] != target_versions():
            raise CampaignRefused("pinned versions changed; register again")
        connection.execute(
            "UPDATE domain_repair_campaigns SET state = 'active',"
            " updated_at = clock_timestamp() WHERE id = %s",
            (row[0],),
        )
        _still_open(connection, season_id, deadline, now)
        return {"action": "activate", "season": season_id, "campaign_id": row[0]}


def run_campaign_command(database: Database, action: str, season_id: str) -> dict:
    """Run one campaign action for the CLI, reporting a refusal."""
    try:
        return {"preview": preview, "register": register, "activate": activate}[action](
            database, season_id
        )
    except CampaignRefused as refused:
        return {"action": action, "season": season_id, "refused": str(refused)}
