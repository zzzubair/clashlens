"""The weekly league record: which league each player was in each week,
Monday Reset to Monday Reset, decided from every piece of evidence about the
week, whatever order it was read or processed in. See docs/domain.md, Weekly
league record.

A Legend day exists only in a week the record says was Legend I, and a
changed decision recalculates the days it affects (``league_days``).

Database triggers (migration 0115) mark a (player, week) in
``league_week_dirty`` in the same transaction as any evidence about it, so
no evidence arrives unmarked, whichever code path saved it. A worker thread
of its own takes the marks (``decide_until``) and decides each marked
player's whole Season again in one pure pass (``decide_season``): a week's
Monday entry and which of its early answers count depend on the weeks before
it. An unchanged decision writes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from threading import Event
from time import monotonic
from typing import Any

from psycopg import errors

from .db import RESET_LOCK_WAIT
from .domain import SEASON_DURATION, ranked_day_for
from .profile import LEGEND_I_TIER_ID, RECOGNIZED_NON_LEGEND_TIERS_V1

WEEK = timedelta(days=7)
# A Monday Reset; every week starts a whole number of weeks from it.
_MONDAY = datetime(2026, 10, 12, 5, tzinfo=UTC)
RULE_VERSION = 1

# The game keeps showing a player's old league for a while after a Monday
# Reset. An answer read earlier than this after the Reset counts only when it
# shows a league other than last week's decided one, because the late display
# only ever shows the old league; when last week is unknown, it does not count.
# Measured on the copy of 14 October 2026 for the 12 October Reset: the 1,898
# players who dropped with an accepted Reset reading were last read in Legend
# I at most 23.5 minutes after it and first in Legend II by 37.6 minutes; one
# hour leaves 60% more, as only one mid-Season Monday has been measured. The
# 62 whose Reset reading named Season 0 were still read in Legend I naming
# Season 0 up to 12 hours later and first in Legend II 12 to 24 hours after
# it; no window short of the whole day covers them, so a Legend I answer
# naming Season 0 read before a lower one the same week is set aside instead
# (decide_season).
LEAGUE_DISPLAY_WINDOW = timedelta(hours=1)

# Every table holding league evidence. Migration 0115 gives each one a
# trigger that marks the (player, week) it is about; a test checks the two
# lists match, so new evidence cannot go unmarked.
EVIDENCE_TABLES = (
    "player_profile_versions",
    "player_profile_effects",
    "legend_battles",
    "player_league_history_entries",
)


class League(StrEnum):
    LEGEND_I = "legend_i"
    BELOW = "below_legend_i"
    UNKNOWN = "unknown"


class Entry(StrEnum):
    """How a Legend I week began at its Monday Reset."""

    SEASON_START = "season_start"  # the Season's first week: everyone starts at 5,000
    STAYED = "stayed"  # Legend I the week before
    PROMOTED = "promoted"  # proven below Legend I the week before: 5,000
    # No Legend I evidence earlier this Season: promoted at this Monday,
    # 5,000 (the owner's rule of 14 October 2026).
    FIRST_SEEN = "first_seen"
    UNKNOWN = "unknown"  # Legend I earlier this Season, then a week not known


class Strength(IntEnum):
    NONE = 0
    # The week before's decided league: a league carries forward until the
    # week's own evidence shows a change (the owner's rule of 14 October
    # 2026), so any claim from the week outweighs it.
    CARRIED = 1
    # A Legend I answer naming Season 0 (not signed up yet), or two lower
    # league answers naming Season 0 from separate responses.
    UNCONFIRMED = 2
    ACCEPTED = 3  # an answer whose Season passed the parser's check
    OFFICIAL = 4  # a Legend battle that week, or the game's league history


class ProofKind(StrEnum):
    BATTLE = "battle"
    LEAGUE_HISTORY = "league_history"
    ACCEPTED_ANSWER = "accepted_answer"
    UNCONFIRMED_ANSWER = "unconfirmed_answer"
    UNCONFIRMED_PAIR = "unconfirmed_pair"
    PREVIOUS_WEEK = "previous_week"


_STRENGTH = {
    ProofKind.BATTLE: Strength.OFFICIAL,
    ProofKind.LEAGUE_HISTORY: Strength.OFFICIAL,
    ProofKind.ACCEPTED_ANSWER: Strength.ACCEPTED,
    ProofKind.UNCONFIRMED_ANSWER: Strength.UNCONFIRMED,
    ProofKind.UNCONFIRMED_PAIR: Strength.UNCONFIRMED,
    ProofKind.PREVIOUS_WEEK: Strength.CARRIED,
}


@dataclass(frozen=True, slots=True, order=True)
class Claim:
    """One piece of evidence naming a week's league. Claims order by time,
    then source, so the same evidence always names the same proof.
    ``source_id`` is the response of an answer, the battle, or the league
    history row, and None for the week before, read at its Monday Reset;
    ``second_id`` the second response of a pair."""

    at: datetime
    source_id: int | None
    kind: ProofKind
    league: League
    second_id: int | None = None

    @property
    def strength(self) -> Strength:
        return _STRENGTH[self.kind]


@dataclass(frozen=True, slots=True, order=True)
class Answer:
    """One saved profile answer naming a recognised league. ``response_id``
    is the API response: one response saved under two parser versions is
    one answer."""

    read_at: datetime
    response_id: int
    legend_i: bool
    accepted: bool

    @property
    def league(self) -> League:
        return League.LEGEND_I if self.legend_i else League.BELOW


@dataclass(frozen=True, slots=True)
class SeasonEvidence:
    """Everything about one player's league in one Season."""

    season_start: datetime
    # The decided league of the week before the Season.
    before: League
    # Answers read in the Season, in any order.
    answers: tuple[Answer, ...] = ()
    # When successful profile responses in the Season that saved no answer
    # were received: skipped as covered by a later one, or not processed yet.
    unsaved: tuple[datetime, ...] = ()
    # Each week's Legend battle and league history claims.
    official: Mapping[datetime, tuple[Claim, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LegendI:
    entry: Entry
    proof: Claim
    against: Claim | None = None


@dataclass(frozen=True, slots=True)
class Below:
    proof: Claim
    against: Claim | None = None


@dataclass(frozen=True, slots=True)
class Unknown:
    """No claim, or ``conflict``: the earliest Legend I claim and the
    earliest below one at the top strength, which disagree."""

    conflict: tuple[Claim, Claim] | None = None


Decision = LegendI | Below | Unknown


def week_start(at: datetime) -> datetime:
    """The Monday Reset starting the week holding ``at``."""
    return _MONDAY + (at - _MONDAY) // WEEK * WEEK


def league_of(decision: Decision) -> League:
    match decision:
        case LegendI():
            return League.LEGEND_I
        case Below():
            return League.BELOW
        case Unknown():
            return League.UNKNOWN


def decide_season(evidence: SeasonEvidence) -> dict[datetime, Decision]:
    """Each week of the Season with any evidence, decided in order. Pure:
    the same evidence gives the same decisions, whatever order it arrived in.

    - An answer counts when read ``LEAGUE_DISPLAY_WINDOW`` or more after its
      week's Monday Reset, or earlier when it shows a league other than the
      week before's decided one, never when that week is unknown.
    - Claims: a Legend battle and a league history row are official; a
      counted accepted answer names its league; a counted Legend I answer
      naming Season 0 is unconfirmed Legend I, unless a lower league answer
      is read after it that week: leagues change only at a Monday Reset, so
      it was the game's display catching up. A counted lower league answer
      naming Season 0 claims below, unconfirmed, only with the week's next or
      previous answer from another response, also a counted lower league
      one, and no successful profile response read between the two that
      saved no answer (it may have shown Legend I). Alone it claims nothing.
    - The week before's decided league, Legend I or below, is carried
      forward as the weakest claim: it decides only a week whose own
      evidence claims nothing.
    - The strongest claims decide. When they name both leagues the week is
      unknown with that conflict.
    """
    answers = sorted(set(evidence.answers))
    decisions: dict[datetime, Decision] = {}
    before = evidence.before
    legend_i_earlier = False
    for index in range(SEASON_DURATION // WEEK):
        week = evidence.season_start + index * WEEK
        in_week = [answer for answer in answers if week <= answer.read_at < week + WEEK]
        official = evidence.official.get(week, ())
        if not in_week and not official:
            before = League.UNKNOWN
            continue
        claims = [*official, *_answer_claims(in_week, week, before, evidence.unsaved)]
        if before is not League.UNKNOWN:
            claims.append(Claim(week - WEEK, None, ProofKind.PREVIOUS_WEEK, before))
        decision = _decide(claims, _entry(index, before, legend_i_earlier))
        decisions[week] = decision
        before = league_of(decision)
        legend_i_earlier = legend_i_earlier or any(
            claim.league is League.LEGEND_I for claim in claims)
    return decisions


def _entry(index: int, before: League, legend_i_earlier: bool) -> Entry:
    if index == 0:
        return Entry.SEASON_START
    match before:
        case League.LEGEND_I:
            return Entry.STAYED
        case League.BELOW:
            return Entry.PROMOTED
        case League.UNKNOWN:
            return Entry.UNKNOWN if legend_i_earlier else Entry.FIRST_SEEN


def _answer_claims(
    answers: list[Answer], week: datetime, before: League, unsaved: Sequence[datetime]
) -> set[Claim]:
    def counts(answer: Answer) -> bool:
        return answer.read_at >= week + LEAGUE_DISPLAY_WINDOW or (
            before is not League.UNKNOWN and answer.league is not before)

    claims = set()
    for index, answer in enumerate(answers):
        if not counts(answer):
            continue
        if answer.accepted:
            claims.add(Claim(answer.read_at, answer.response_id,
                             ProofKind.ACCEPTED_ANSWER, answer.league))
        elif answer.legend_i:
            if all(later.legend_i for later in answers[index + 1:]):
                claims.add(Claim(answer.read_at, answer.response_id,
                                 ProofKind.UNCONFIRMED_ANSWER, League.LEGEND_I))
        else:
            for other in _neighbours(answers, index):
                first, second = sorted((answer, other))
                if not other.legend_i and counts(other) and not any(
                    first.read_at < at < second.read_at for at in unsaved
                ):
                    claims.add(Claim(first.read_at, first.response_id,
                                     ProofKind.UNCONFIRMED_PAIR, League.BELOW,
                                     second.response_id))
    return claims


def _neighbours(answers: list[Answer], index: int) -> list[Answer]:
    """The answers before and after ``answers[index]`` from other responses."""
    response = answers[index].response_id
    before = next((answer for answer in reversed(answers[:index])
                   if answer.response_id != response), None)
    after = next((answer for answer in answers[index + 1:]
                  if answer.response_id != response), None)
    return [answer for answer in (before, after) if answer is not None]


def _decide(claims: Sequence[Claim], entry: Entry) -> Decision:
    if not claims:
        return Unknown()
    top = max(claim.strength for claim in claims)
    strongest = sorted(claim for claim in claims if claim.strength == top)
    proof = strongest[0]
    rival = next((claim for claim in strongest if claim.league is not proof.league), None)
    if rival is not None:
        return Unknown((proof, rival) if proof.league is League.LEGEND_I else (rival, proof))
    against = min((claim for claim in claims if claim.league is not proof.league
                   and claim.kind is not ProofKind.PREVIOUS_WEEK),
                  key=lambda claim: (-claim.strength, claim), default=None)
    if proof.league is League.LEGEND_I:
        return LegendI(entry, proof, against)
    return Below(proof, against)


def history_claim(row_id: int, label: str, tier_id: int | None) -> tuple[datetime, Claim] | None:
    """The week a league history row speaks for, and its claim. A row is
    labelled with the Monday Reset ending its tournament: a week in Legend II
    or lower, the Season in Legend I, of which it proves the last week. A
    tier the parser does not recognise claims nothing."""
    if tier_id == LEGEND_I_TIER_ID:
        league = League.LEGEND_I
    elif tier_id in RECOGNIZED_NON_LEGEND_TIERS_V1:
        league = League.BELOW
    else:
        return None
    ends = datetime.fromtimestamp(int(label), UTC)
    return week_start(ends - WEEK), Claim(ends, row_id, ProofKind.LEAGUE_HISTORY, league)


def load_season(
    connection: Any, player_id: int, season_start: datetime, now: datetime
) -> SeasonEvidence:
    """One player's league evidence for one Season, read with the
    connection's current snapshot. A week with a Legend battle or Legend I
    league history is Legend I whatever its Legend I answers say, so its
    answers are read only when one of them shows a lower league: those can
    be evidence against it. Tracked players' many answers are mostly in such
    weeks."""
    season_end = season_start + SEASON_DURATION
    before = connection.execute(
        "SELECT league FROM player_league_weeks WHERE player_id = %s AND week_start = %s",
        (player_id, season_start - WEEK),
    ).fetchone()
    official: dict[datetime, list[Claim]] = {}
    weeks = [season_start + index * WEEK for index in range(SEASON_DURATION // WEEK)
             if season_start + index * WEEK <= now]
    # Each week's first battle, attack or defense, by day then id. Migration
    # 0115's index finds defenses; without it each day's lookup read every
    # battle of the day.
    for week, day, battle_id in connection.execute(
        """
        SELECT week.start, first.ranked_day_start, first.id
        FROM unnest(%(weeks)s::timestamptz[]) AS week (start)
        CROSS JOIN LATERAL (
            (SELECT ranked_day_start, id FROM legend_battles
             WHERE attacker_player_id = %(player)s AND ranked_day_start >= week.start
               AND ranked_day_start < week.start + interval '7 days'
             ORDER BY ranked_day_start, id LIMIT 1)
            UNION ALL
            (SELECT ranked_day_start, id FROM legend_battles
             WHERE defender_player_id = %(player)s AND ranked_day_start >= week.start
               AND ranked_day_start < week.start + interval '7 days'
             ORDER BY ranked_day_start, id LIMIT 1)
            ORDER BY 1, 2 LIMIT 1
        ) AS first
        """,
        {"weeks": weeks, "player": player_id},
    ).fetchall():
        official.setdefault(week, []).append(
            Claim(day, int(battle_id), ProofKind.BATTLE, League.LEGEND_I))
    for row_id, label, tier_id in connection.execute(
        """
        SELECT id, league_season_id, league_tier_id FROM player_league_history_entries
        WHERE player_id = %s
        """,
        (player_id,),
    ).fetchall():
        claimed = history_claim(int(row_id), str(label), tier_id)
        if claimed is not None and season_start <= claimed[0] < season_end:
            official.setdefault(claimed[0], []).append(claimed[1])
    # Migration 0115's partial index holds only lower league profiles.
    lower = {week_start(at) for (at,) in connection.execute(
        """
        SELECT seen.observed_at
        FROM player_profile_versions AS version
        JOIN player_profile_effects AS seen ON seen.profile_version_id = version.id
        WHERE version.player_id = %(player)s AND version.eligibility_state = 'ineligible'
          AND seen.observed_at >= %(start)s AND seen.observed_at < %(end)s
        """,
        {"player": player_id, "start": season_start, "end": season_end},
    ).fetchall()}
    read = [week for week in weeks if week in lower or all(
        claim.league is not League.LEGEND_I for claim in official.get(week, ()))]
    answers: list[Answer] = []
    unsaved: list[datetime] = []
    for kind, at, response_id, legend_i, accepted in connection.execute(
        """
        WITH observed AS (
            SELECT observed.id, observed.response_completed_at, observed.http_status
            FROM unnest(%(weeks)s::timestamptz[]) AS week (start)
            JOIN collector_observations AS observed
              ON observed.player_id = %(player)s AND observed.endpoint = 'profile'
             AND observed.response_completed_at >= week.start
             AND observed.response_completed_at < week.start + interval '7 days'
        )
        SELECT 'answer', seen.observed_at, seen.observation_id,
               version.eligibility_state = 'eligible', version.source_contract_state = 'accepted'
        FROM observed
        JOIN player_profile_effects AS seen ON seen.observation_id = observed.id
        JOIN player_profile_versions AS version ON version.id = seen.profile_version_id
        WHERE version.eligibility_state IN ('eligible', 'ineligible')
        UNION ALL
        SELECT 'unsaved', response_completed_at, id, NULL, NULL
        FROM observed
        WHERE http_status BETWEEN 200 AND 299
          AND NOT EXISTS (SELECT 1 FROM player_profile_effects AS seen
                          WHERE seen.observation_id = observed.id)
          AND NOT EXISTS (SELECT 1 FROM player_profile_versions AS version
                          WHERE version.observation_id = observed.id)
        """,
        {"player": player_id, "weeks": read},
    ).fetchall() if read else ():
        if kind == "answer":
            answers.append(Answer(at, int(response_id), bool(legend_i), bool(accepted)))
        else:
            unsaved.append(at)
    return SeasonEvidence(
        season_start, League(before[0]) if before else League.UNKNOWN,
        tuple(answers), tuple(unsaved),
        {week: tuple(claims) for week, claims in official.items()},
    )


def _columns(decision: Decision) -> dict[str, Any]:
    entry: Entry | None = None
    reason: str | None = None
    proof: Claim | None
    against: Claim | None
    match decision:
        case LegendI(entry, proof, against):
            pass
        case Below(proof, against):
            pass
        case Unknown(None):
            reason, proof, against = "no_evidence", None, None
        case Unknown((proof, against)):
            reason = "conflicting_evidence"
    return {
        "league": league_of(decision).value,
        "entry": entry.value if entry else None,
        "strength": int(proof.strength) if proof else int(Strength.NONE),
        "unknown_reason": reason,
        "proof_kind": proof.kind.value if proof else None,
        "proof_at": proof.at if proof else None,
        "proof_source_id": proof.source_id if proof else None,
        "proof_second_id": proof.second_id if proof else None,
        "against_kind": against.kind.value if against else None,
        "against_at": against.at if against else None,
        "against_source_id": against.source_id if against else None,
    }


_SAVE = """
INSERT INTO player_league_weeks AS week (
    player_id, week_start, league, entry, strength, unknown_reason,
    proof_kind, proof_at, proof_source_id, proof_second_id,
    against_kind, against_at, against_source_id, rule_version
) VALUES (
    %(player)s, %(week)s, %(league)s, %(entry)s, %(strength)s, %(unknown_reason)s,
    %(proof_kind)s, %(proof_at)s, %(proof_source_id)s, %(proof_second_id)s,
    %(against_kind)s, %(against_at)s, %(against_source_id)s, %(rule_version)s
)
ON CONFLICT (player_id, week_start) DO UPDATE
SET league = EXCLUDED.league, entry = EXCLUDED.entry, strength = EXCLUDED.strength,
    unknown_reason = EXCLUDED.unknown_reason, proof_kind = EXCLUDED.proof_kind,
    proof_at = EXCLUDED.proof_at, proof_source_id = EXCLUDED.proof_source_id,
    proof_second_id = EXCLUDED.proof_second_id, against_kind = EXCLUDED.against_kind,
    against_at = EXCLUDED.against_at, against_source_id = EXCLUDED.against_source_id,
    rule_version = EXCLUDED.rule_version,
    decision_number = week.decision_number + 1, decided_at = clock_timestamp()
WHERE (week.league, week.entry, week.strength, week.unknown_reason, week.proof_kind,
       week.proof_at, week.proof_source_id, week.proof_second_id, week.against_kind,
       week.against_at, week.against_source_id, week.rule_version)
      IS DISTINCT FROM
      (EXCLUDED.league, EXCLUDED.entry, EXCLUDED.strength, EXCLUDED.unknown_reason,
       EXCLUDED.proof_kind, EXCLUDED.proof_at, EXCLUDED.proof_source_id,
       EXCLUDED.proof_second_id, EXCLUDED.against_kind, EXCLUDED.against_at,
       EXCLUDED.against_source_id, EXCLUDED.rule_version)
"""


# A saved week is updated only where its decision changed: an upsert locks
# every row it meets as if its league changed, and a day being calculated
# reads its week's row (league_days.day_league), so it would wait for the
# decider's whole batch. An update locks only the rows it changes, and keeps
# them readable by day calculations while their league stays.
_UPDATE = """
UPDATE player_league_weeks AS week
SET league = %(league)s, entry = %(entry)s, strength = %(strength)s,
    unknown_reason = %(unknown_reason)s, proof_kind = %(proof_kind)s,
    proof_at = %(proof_at)s, proof_source_id = %(proof_source_id)s,
    proof_second_id = %(proof_second_id)s, against_kind = %(against_kind)s,
    against_at = %(against_at)s, against_source_id = %(against_source_id)s,
    rule_version = %(rule_version)s,
    decision_number = week.decision_number + 1, decided_at = clock_timestamp()
WHERE player_id = %(player)s AND week_start = %(week)s
  AND (league, entry, strength, unknown_reason, proof_kind, proof_at, proof_source_id,
       proof_second_id, against_kind, against_at, against_source_id, rule_version)
      IS DISTINCT FROM
      (%(league)s, %(entry)s, %(strength)s, %(unknown_reason)s, %(proof_kind)s,
       %(proof_at)s::timestamptz, %(proof_source_id)s::bigint, %(proof_second_id)s::bigint,
       %(against_kind)s, %(against_at)s::timestamptz, %(against_source_id)s::bigint,
       %(rule_version)s)
"""


def save_season(
    connection: Any, player_id: int, season_start: datetime,
    decisions: Mapping[datetime, Decision],
) -> set[datetime]:
    """Save the Season's decisions; the weeks whose saved record changed. A
    saved week that no longer has any evidence becomes unknown."""
    stored = {row[0] for row in connection.execute(
        """
        SELECT week_start FROM player_league_weeks
        WHERE player_id = %s AND week_start >= %s AND week_start < %s
        """,
        (player_id, season_start, season_start + SEASON_DURATION),
    ).fetchall()}
    return {
        week for week in sorted(stored | set(decisions))
        if connection.execute(_UPDATE if week in stored else _SAVE, {
            "player": player_id, "week": week, "rule_version": RULE_VERSION,
            **_columns(decisions.get(week, Unknown())),
        }).rowcount
    }


# The worker decides on a thread of its own, without pause while marks wait,
# so a Monday Reset's marks are decided within minutes of being saved and
# the Reset publication checks never wait behind it; with nothing marked it
# looks again every IDLE_SECONDS. Each call decides for at most TICK_SECONDS
# so the thread notices the worker stopping.
TICK_SECONDS = 2.0
IDLE_SECONDS = 2.0
BATCH = 200
RETRY_SECONDS = 60
REPORT_SECONDS = 60
_retry_at = float("-inf")
_report_at = float("-inf")
_tally: dict[str, float] = {"decided": 0, "changed": 0, "marks": 0, "skipped": 0, "seconds": 0.0}


def _now() -> datetime:
    return datetime.now(UTC)


def decide_until(database: Any, stop: Event) -> None:
    """The worker's league week thread: decide marked Seasons until ``stop``
    is set."""
    while not stop.is_set():
        if not decide_due(database):
            stop.wait(IDLE_SECONDS)


def decide_due(database: Any) -> bool:
    """Decide the marked players' Seasons for up to ``TICK_SECONDS``; whether
    any mark was found. Only the current and previous Season are decided
    again: raw responses older than that are pruned, so an older week's
    evidence is incomplete. A failure is reported and tried again after
    ``RETRY_SECONDS``; once a minute the work done is reported."""
    global _retry_at, _report_at
    started = monotonic()
    if started < _retry_at:
        return False
    deadline = started + TICK_SECONDS
    found = False
    try:
        while _decide_batch(database, deadline):
            found = True
            if monotonic() >= deadline:
                break
        if monotonic() >= _report_at and _tally["marks"]:
            _report_at = monotonic() + REPORT_SECONDS
            with database.pool.connection() as connection:
                waiting, oldest = connection.execute(
                    "SELECT count(*), min(marked_at) FROM league_week_dirty"
                    " WHERE failed_at IS NULL"
                ).fetchone()
            print(json.dumps({
                "event": "league_week", **_tally, "seconds": round(_tally["seconds"], 1),
                "waiting": waiting, "oldest_mark": oldest.isoformat() if oldest else None,
            }), flush=True)
            _tally.update(decided=0, changed=0, marks=0, skipped=0, seconds=0.0)
    except Exception as error:  # noqa: BLE001 - tried again after RETRY_SECONDS
        _retry_at = monotonic() + RETRY_SECONDS
        print(json.dumps({"event": "league_week", "status": "failed",
                          "error": type(error).__name__}), flush=True)
        return False
    return found


def _decide_batch(database: Any, deadline: float) -> bool:
    """Decide one batch of marked Seasons in one transaction; whether any
    mark was found. Marks are deleted in the transaction reading the
    evidence: evidence committed later leaves a new mark.

    Each player is decided on their own, so one cannot stop the rest. A
    player whose lock is busy, as a Reset's board's while it freezes, keeps
    their marks for the next batch. One whose decision fails any other way
    is set aside: their marks stay, marked failed, until new evidence marks
    them again, and the failed-work alert counts them (collector_metrics)."""
    now = _now()
    oldest = ranked_day_for(now).season_start - SEASON_DURATION
    with database.pool.connection() as connection, connection.transaction():
        if not connection.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended('league-week-decider', 0))"
        ).fetchone()[0]:
            return False
        # A player whose week or Reset a day calculation holds is tried in
        # a later batch, so the batch's locks are not held while it waits.
        connection.execute("SELECT set_config('lock_timeout', %s, true)", (RESET_LOCK_WAIT,))
        marked = connection.execute(
            """
            SELECT DISTINCT player_id, week_start FROM (
                SELECT player_id, week_start FROM league_week_dirty WHERE failed_at IS NULL
                ORDER BY id LIMIT %s
            ) AS mark
            """,
            (BATCH,),
        ).fetchall()
        for player_id, season_start in sorted(
            {(int(player), ranked_day_for(week).season_start) for player, week in marked}
        ):
            try:
                with connection.transaction():
                    _decide_player(database, connection, player_id, season_start, now, oldest)
            except (errors.LockNotAvailable, errors.TransactionRollback, errors.QueryCanceled):
                pass
            except Exception as error:  # noqa: BLE001 - set aside, the rest proceed
                connection.execute(
                    """
                    UPDATE league_week_dirty SET failed_at = clock_timestamp()
                    WHERE player_id = %s AND week_start >= %s AND week_start < %s
                      AND failed_at IS NULL
                    """,
                    (player_id, season_start, season_start + SEASON_DURATION),
                )
                print(json.dumps({"event": "league_week", "status": "set_aside",
                                  "player_id": player_id, "error": type(error).__name__}),
                      flush=True)
            if monotonic() >= deadline:
                break
        return bool(marked)


def _decide_player(
    database: Any, connection: Any, player_id: int, season_start: datetime, now: datetime,
    oldest: datetime,
) -> None:
    """Decide one marked player's Season, deleting its marks."""
    from . import league_days

    sources = [row[0] for row in connection.execute(
        """
        DELETE FROM league_week_dirty
        WHERE player_id = %s AND week_start >= %s AND week_start < %s
        RETURNING source
        """,
        (player_id, season_start, season_start + SEASON_DURATION),
    ).fetchall()]
    _tally["marks"] += len(sources)
    if season_start < oldest:
        _tally["skipped"] += 1
        return
    started = monotonic()
    # Seeded weeks' days are the Season repair's to recalculate, with those
    # of live evidence marked before the seed's first decision: queueing
    # every week of every tracked player at deploy would flood the queue.
    changed = league_days.save_season(
        database, connection, player_id, season_start,
        decide_season(load_season(connection, player_id, season_start, now)),
        now, queue_days="seed" not in sources)
    next_season = season_start + SEASON_DURATION
    if next_season - WEEK in changed and next_season <= now:
        # The next Season's first week counts its early answers
        # against this Season's last.
        connection.execute(
            "INSERT INTO league_week_dirty (player_id, week_start, source)"
            " VALUES (%s, %s, 'decision')",
            (player_id, next_season),
        )
    _tally["decided"] += 1
    _tally["changed"] += len(changed)
    _tally["seconds"] += monotonic() - started


def saves_unchanged_answer(connection: Any, answer: Any, last_saved_id: int) -> bool:
    """Whether the collector saves an answer (a ``collector_db.ResponseHandoff``)
    matching the last saved one: the first profile answer read on a Monday's
    Legend day after ``LEAGUE_DISPLAY_WINDOW``. Unchanged answers are
    otherwise not saved, and the two always saved on a Monday, the Reset
    reading and the settlement check, fall inside the window, so a player who
    does not battle would have no answer counting for their new week until
    Tuesday's Reset. One more saved answer a player a week, from a request
    already made."""
    read_at = answer.response_completed_at
    window_end = week_start(read_at) + LEAGUE_DISPLAY_WINDOW
    if answer.endpoint != "profile" or not 200 <= answer.http_status < 300 or not (
        window_end <= read_at < window_end - LEAGUE_DISPLAY_WINDOW + timedelta(days=1)
    ):
        return False
    saved = connection.execute(
        "SELECT response_completed_at FROM collector_observations WHERE id = %s",
        (last_saved_id,),
    ).fetchone()
    return saved is not None and saved[0] < window_end
