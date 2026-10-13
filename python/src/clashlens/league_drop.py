"""When lower-league profile answers prove a player left Legend I.

On 12 October 2026 one profile answer showing Legend II with Season ID 0,
which the parser marks conflict, stopped tracking 86 players hours after the
Reset; 15 of them had played Legend battles that day. Real Monday demotions
answer Season ID 0 too (1,661 of 1,944 that day), so the conflict mark alone
cannot tell them apart.

A lower-league answer proves the player left Legend I when the parser
accepted it, or when the player's next or previous answer, from another API
response, is a lower league too and the player has no Legend battle after
the earlier of the two. Real demotions are proven by their next saved
answer: at latest the next Reset reading, which is always saved, and that
Reset's Daily board leaves out the day they left by
(``boundary_manifest``). Tracking (``judge``) and the day results' weekly
drop (``reconciliation_db._dropped_from_legend_i``) both follow this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import domain, first_battle_log
from .db import PYTHON_BACKFILL_PRIORITY, _text_value
from .reconciliation import RECONCILIATION_RULE_VERSION

# How many answers before or after a stretch are read to find the next
# answer from another API response: one response can be saved under more
# than one parser version.
_NEIGHBOURS = 5
# Enough of a player's newest answers to reach back past a run of lower-league
# ones to the Legend I answer before them.
_NEWEST = 50


@dataclass(frozen=True, slots=True)
class Reading:
    """One saved profile answer naming a recognised league."""

    eligible: bool
    accepted: bool
    read_at: datetime
    observation_id: int | None


def readings(
    connection: Any, player_id: int, *, since: datetime | None = None,
    until: datetime | None = None, newest_first: bool = False,
    limit: int | None = None,
) -> list[Reading]:
    """The player's answers naming a recognised league read from ``since``
    and before ``until``, oldest first unless ``newest_first``. Each reading
    counts at its own time; a saved profile can be read again."""
    order = "DESC" if newest_first else "ASC"
    return [
        Reading(_text_value(row[0]) == "eligible", _text_value(row[1]) == "accepted",
                row[2], None if row[3] is None else int(row[3]))
        for row in connection.execute(
            f"""
            WITH version AS (
                SELECT version.id, version.observed_at, version.observation_id,
                       version.eligibility_state, version.source_contract_state
                FROM players AS player
                JOIN player_profile_versions AS version
                  ON version.normalized_tag = player.normalized_tag
                WHERE player.id = %(player)s
                  AND version.eligibility_state IN ('eligible', 'ineligible')
            ), reading AS (
                SELECT version.eligibility_state, version.source_contract_state,
                       seen.observed_at AS read_at, seen.observation_id, seen.id
                FROM version
                JOIN player_profile_effects AS seen
                  ON seen.profile_version_id = version.id
                UNION ALL
                SELECT version.eligibility_state, version.source_contract_state,
                       version.observed_at, version.observation_id, 0
                FROM version
                WHERE NOT EXISTS (
                    SELECT 1 FROM player_profile_effects AS seen
                    WHERE seen.profile_version_id = version.id
                )
            )
            SELECT eligibility_state, source_contract_state, read_at, observation_id
            FROM reading
            WHERE (%(since)s::timestamptz IS NULL OR read_at >= %(since)s)
              AND (%(until)s::timestamptz IS NULL OR read_at < %(until)s)
            ORDER BY read_at {order}, id {order}
            LIMIT %(limit)s
            """,
            {"player": player_id, "since": since, "until": until, "limit": limit},
        ).fetchall()
    ]


def battle_after(connection: Any, player_id: int, after: datetime) -> bool:
    """Whether the player has a saved Legend battle, attack or defense, fought
    after ``after``. The battle index holds each day's battles by attacker,
    then defender, so each day is its own lookup: one lookup over every day
    reads the whole table (500 ms a call at the 12 October 2026 Reset)."""
    first_day = domain.ranked_day_for(after).start - timedelta(days=1)
    return bool(connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM generate_series(%(day)s::timestamptz, clock_timestamp(),
                                 interval '24 hours') AS day (start)
            CROSS JOIN LATERAL (
                (SELECT 1 FROM legend_battles AS battle
                 JOIN battle_evidence AS evidence ON evidence.battle_id = battle.id
                 WHERE battle.ranked_day_start = day.start
                   AND battle.attacker_player_id = %(player)s
                   AND evidence.battle_timestamp > %(after)s
                 LIMIT 1)
                UNION ALL
                (SELECT 1 FROM legend_battles AS battle
                 JOIN battle_evidence AS evidence ON evidence.battle_id = battle.id
                 WHERE battle.ranked_day_start = day.start
                   AND battle.defender_player_id = %(player)s
                   AND evidence.battle_timestamp > %(after)s
                 LIMIT 1)
            ) AS battle
        )
        """,
        {"day": first_day, "player": player_id, "after": after},
    ).fetchone()[0])


def _pair(answers: list[Reading], index: int) -> tuple[Reading | None, Reading | None]:
    """The answers before and after ``answers[index]`` from other responses."""
    observation = answers[index].observation_id
    before = next((answer for answer in reversed(answers[:index])
                   if answer.observation_id != observation), None)
    after = next((answer for answer in answers[index + 1:]
                  if answer.observation_id != observation), None)
    return before, after


def left_legend_i(
    connection: Any, player_id: int, answers: list[Reading], index: int
) -> bool:
    """Whether ``answers[index]`` proves the player left Legend I, judged
    against its neighbours in ``answers`` (see the module notes)."""
    answer = answers[index]
    if answer.eligible:
        return False
    if answer.accepted:
        return True
    before, after = _pair(answers, index)
    return (
        before is not None and not before.eligible
        and not battle_after(connection, player_id, before.read_at)
    ) or (
        after is not None and not after.eligible
        and not battle_after(connection, player_id, answer.read_at)
    )


def dropped_between(
    connection: Any, player_id: int, since: datetime, until: datetime
) -> bool:
    """Whether an answer read from ``since`` and before ``until`` proves the
    player left Legend I, judged with the answers next to it, which may be
    read before or after that window."""
    window = readings(connection, player_id, since=since, until=until)
    if all(answer.eligible for answer in window):
        return False
    before = readings(connection, player_id, until=since, newest_first=True,
                      limit=_NEIGHBOURS)[::-1]
    answers = before + window + readings(
        connection, player_id, since=until, limit=_NEIGHBOURS)
    return any(
        left_legend_i(connection, player_id, answers, len(before) + offset)
        for offset in range(len(window))
    )


def judge(connection: Any, player_id: int) -> None:
    """Track or stop tracking the player by their newest answers, whatever
    order those were saved or processed in. The caller holds the player's row.

    The newest answer decides, unless the current profile was confirmed by a
    later unchanged check. A lower-league answer that proves nothing yet
    leaves a player whose answers before it showed Legend I tracked; one
    never seen in Legend I stays untracked, as before.
    Stopping queues the weekly drop of each Monday the proving answers were
    read on; tracking again a player stopped by lower-league answers
    recalculates their days from the day before the first of them."""
    answers = readings(connection, player_id, newest_first=True, limit=_NEWEST)[::-1]
    if not answers:
        return
    current = connection.execute(
        """
        SELECT player.active, player.eligibility_state,
               player.next_due_at IS NOT NULL,
               player.current_profile_confirmed_at, profile.eligibility_state
        FROM players AS player
        LEFT JOIN player_profile_versions AS profile
          ON profile.id = player.current_profile_version_id
        WHERE player.id = %s
        """,
        (player_id,),
    ).fetchone()
    active, state, due, confirmed_at, current_state = (
        current[0], _text_value(current[1]), current[2], current[3],
        _text_value(current[4]) if current[4] is not None else None,
    )
    newest = answers[-1]
    # The newest stretch of lower-league answers, and whether a Legend I
    # answer came before it.
    end = len(answers)
    while end and answers[end - 1].eligible:
        end -= 1
    start = end
    while start and not answers[start - 1].eligible:
        start -= 1
    stretch = answers[start:end]
    proof: tuple[Reading | None, ...] = ()
    if current_state in {"eligible", "ineligible"} and confirmed_at is not None \
            and confirmed_at > newest.read_at:
        judged = current_state
    elif newest.eligible:
        judged = "eligible"
    elif left_legend_i(connection, player_id, answers, len(answers) - 1):
        judged = "ineligible"
        proof = (newest,) if newest.accepted else (
            _pair(answers, len(answers) - 1)[0], newest)
    elif start > 0 or state == "eligible":
        judged = "eligible"
    else:
        # Never seen in Legend I: not tracked, as before.
        judged = "ineligible"
    for answer in proof:
        if answer is not None:
            first_battle_log.queue_weekly_drop(connection, player_id, answer.read_at)
    eligible = judged == "eligible"
    if state == judged and active == eligible and due == eligible:
        return
    connection.execute(
        """
        UPDATE players
        SET active = %(eligible)s,
            next_due_at = CASE WHEN %(eligible)s
                THEN COALESCE(next_due_at, clock_timestamp()) END,
            eligibility_state = %(state)s,
            updated_at = clock_timestamp()
        WHERE id = %(player)s
        """,
        {"eligible": eligible, "state": judged, "player": player_id},
    )
    if eligible and not active and state == "ineligible" and stretch:
        # The days those lower-league answers made non-Legend days, and every
        # later saved day of their Season.
        day = domain.ranked_day_for(stretch[0].read_at).start - timedelta(days=1)
        day_text = day.strftime("%Y-%m-%dT%H:%M:%SZ")
        first_battle_log._queue(
            connection, player_id, day, None,
            key=(f"reconcile:league-return:{player_id}:{day_text}:"
                 f"{RECONCILIATION_RULE_VERSION}"),
            trigger="league_return", priority=PYTHON_BACKFILL_PRIORITY,
        )


def recheck_battle_players(connection: Any, player_ids: list[int]) -> None:
    """Judge again each untracked player a saved battle log names who was
    stopped by lower-league answers: a Legend battle after them may now show
    they never left. The caller holds no player row; rows are taken in order."""
    for (player_id,) in connection.execute(
        """
        SELECT id FROM players
        WHERE id = ANY(%s::bigint[]) AND NOT active AND eligibility_state = 'ineligible'
        ORDER BY id
        FOR NO KEY UPDATE
        """,
        (sorted(set(player_ids)),),
    ).fetchall():
        judge(connection, int(player_id))
