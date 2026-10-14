"""The weekly league record's rule (league_week.decide_season), one Season
at a time: which league each week was in, how each Legend I week began, and
what proved it. See docs/domain.md, Weekly league record."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from clashlens.league_week import (
    LEAGUE_DISPLAY_WINDOW,
    Answer,
    Below,
    Claim,
    Entry,
    League,
    LegendI,
    ProofKind,
    SeasonEvidence,
    Unknown,
    decide_season,
    history_claim,
    week_start,
)

SEASON = datetime(2026, 10, 5, 5, tzinfo=UTC)
W1, W2, W3, W4 = (SEASON + timedelta(days=7 * n) for n in range(4))
L1, L2 = True, False
ACCEPTED, SEASON_0 = True, False


def at(week: datetime, day: int = 0, hours: float = 0) -> datetime:
    """``hours`` into Legend day ``day`` (0 is the Monday) of ``week``."""
    return week + timedelta(days=day, hours=hours)


def answer(read_at: datetime, response: int, legend_i: bool, accepted: bool) -> Answer:
    return Answer(read_at, response, legend_i, accepted)


def battle(day: datetime, battle_id: int) -> Claim:
    return Claim(day, battle_id, ProofKind.BATTLE, League.LEGEND_I)


def claim(answer: Answer, kind: ProofKind, second: int | None = None) -> Claim:
    return Claim(answer.read_at, answer.response_id, kind, answer.league, second)


def carried(week: datetime, league: League) -> Claim:
    """The week before ``week``'s decided league, carried into it."""
    return Claim(week - timedelta(days=7), None, ProofKind.PREVIOUS_WEEK, league)


# A Legend II answer naming Season 0 read hours after the 12 October 2026
# Reset, for a player still in Legend I with battles that day.
GLITCH = answer(at(W2, 0, 6), 21, L2, SEASON_0)
# A Legend I player's Monday: the Reset reading, the settlement check and an
# answer after the display window, all accepted.
STAYED = (answer(at(W2, 0, 0.06), 11, L1, ACCEPTED), answer(at(W2, 0, 0.33), 12, L1, ACCEPTED),
          answer(at(W2, 0, 4), 13, L1, ACCEPTED))
# A dropped player's Monday: the Reset reading still shows Legend I, then
# Legend II naming Season 0, as on 12 October 2026.
DROP = (answer(at(W2, 0, 0.06), 31, L1, ACCEPTED), answer(at(W2, 0, 0.33), 32, L2, SEASON_0),
        answer(at(W2, 0, 4), 33, L2, SEASON_0))
LAST_WEEK = (answer(at(W1, 5, 2), 1, L1, ACCEPTED),)

CASES = {
    "a Legend battle proves Legend I, over a lone lower league answer": (
        SeasonEvidence(SEASON, League.UNKNOWN, (*LAST_WEEK, GLITCH),
                       official={W2: (battle(at(W2), 501),)}),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: LegendI(Entry.STAYED, battle(at(W2), 501))},
    ),
    # The owner's rule of 14 October 2026: a league carries forward until
    # evidence shows a change, and only a guarded pair proves a drop.
    "a lone lower league answer naming Season 0 after a Legend I week: still Legend I": (
        SeasonEvidence(SEASON, League.UNKNOWN, (*LAST_WEEK, GLITCH)),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: LegendI(Entry.STAYED, carried(W2, League.LEGEND_I))},
    ),
    "a lone lower league answer after the window after a below week: still below": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            answer(at(W1, 6, 1), 221, L2, ACCEPTED), answer(at(W2, 2, 1), 222, L2, SEASON_0))),
        {W1: Below(Claim(at(W1, 6, 1), 221, ProofKind.ACCEPTED_ANSWER, League.BELOW)),
         W2: Below(carried(W2, League.BELOW))},
    ),
    # #V02JGRJC: proven below the week of 5 October 2026, then one Legend II
    # answer naming Season 0 at 05:16 on 12 October, inside the window.
    "a lone lower league answer in the window after a below week: still below": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            answer(at(W1, 6, 1), 231, L2, ACCEPTED), answer(at(W2, 0, 0.27), 232, L2, SEASON_0))),
        {W1: Below(Claim(at(W1, 6, 1), 231, ProofKind.ACCEPTED_ANSWER, League.BELOW)),
         W2: Below(carried(W2, League.BELOW))},
    ),
    # Promoted at the Monday Reset, never battles: the Reset reading still
    # shows Legend II; the first Legend I answer after the window decides.
    "a below week, then a Legend I answer naming Season 0 after the window: promoted": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            answer(at(W1, 6, 1), 241, L2, ACCEPTED), answer(at(W2, 0, 0.06), 242, L2, ACCEPTED),
            answer(at(W2, 0, 1.5), 243, L1, SEASON_0))),
        {W1: Below(Claim(at(W1, 6, 1), 241, ProofKind.ACCEPTED_ANSWER, League.BELOW)),
         W2: LegendI(Entry.PROMOTED,
                     Claim(at(W2, 0, 1.5), 243, ProofKind.UNCONFIRMED_ANSWER, League.LEGEND_I))},
    ),
    "a week with no evidence breaks the carry": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            answer(at(W1, 6, 1), 251, L2, ACCEPTED), answer(at(W3, 2, 1), 252, L2, SEASON_0))),
        {W1: Below(Claim(at(W1, 6, 1), 251, ProofKind.ACCEPTED_ANSWER, League.BELOW)),
         W3: Unknown()},
    ),
    "an accepted Legend I answer outweighs a pair naming Season 0": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            *LAST_WEEK, answer(at(W2, 1, 1), 41, L1, ACCEPTED),
            answer(at(W2, 2, 1), 42, L2, SEASON_0), answer(at(W2, 2, 2), 43, L2, SEASON_0))),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: LegendI(Entry.STAYED, Claim(at(W2, 1, 1), 41, ProofKind.ACCEPTED_ANSWER, League.LEGEND_I),
                     Claim(at(W2, 2, 1), 42, ProofKind.UNCONFIRMED_PAIR, League.BELOW, 43))},
    ),
    "a dropped player: two Season 0 Legend II answers next to each other": (
        SeasonEvidence(SEASON, League.UNKNOWN, (*LAST_WEEK, *DROP)),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: Below(claim(DROP[1], ProofKind.UNCONFIRMED_PAIR, 33))},
    ),
    "a response that saved no answer between the two keeps them apart": (
        SeasonEvidence(SEASON, League.UNKNOWN, (*LAST_WEEK, *DROP), (at(W2, 0, 2),)),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: LegendI(Entry.STAYED, carried(W2, League.LEGEND_I))},
    ),
    "a Legend I answer between the two keeps them apart": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            *LAST_WEEK, DROP[1], answer(W2 + LEAGUE_DISPLAY_WINDOW / 2, 34, L1, SEASON_0), DROP[2])),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: LegendI(Entry.STAYED, carried(W2, League.LEGEND_I))},
    ),
    # 62 players dropped on 12 October 2026 were read in Legend I naming
    # Season 0 up to 12 hours after the Reset, then in Legend II.
    "a Season 0 Legend I answer before a lower one is set aside: the pair proves the drop": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            *LAST_WEEK, answer(at(W2, 0, 0.06), 51, L1, SEASON_0),
            answer(at(W2, 0, 8), 52, L1, SEASON_0), answer(at(W2, 0, 14), 53, L2, SEASON_0),
            answer(at(W2, 0, 15), 54, L2, SEASON_0))),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: Below(Claim(at(W2, 0, 14), 53, ProofKind.UNCONFIRMED_PAIR, League.BELOW, 54))},
    ),
    "a set-aside Legend I answer still keeps two lower answers apart": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            answer(at(W2, 1), 261, L2, SEASON_0), answer(at(W2, 2), 262, L1, SEASON_0),
            answer(at(W2, 3), 263, L2, SEASON_0))),
        {W2: Unknown()},
    ),
    "a Season 0 Legend I answer after the week's last lower one counts": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            answer(at(W2, 1), 271, L2, SEASON_0), answer(at(W2, 2), 272, L1, SEASON_0))),
        {W2: LegendI(Entry.FIRST_SEEN,
                     Claim(at(W2, 2), 272, ProofKind.UNCONFIRMED_ANSWER, League.LEGEND_I))},
    ),
    "an accepted Legend I answer is not set aside by a later lower one": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            answer(at(W2, 1), 281, L1, ACCEPTED), answer(at(W2, 2), 282, L2, SEASON_0))),
        {W2: LegendI(Entry.FIRST_SEEN,
                     Claim(at(W2, 1), 281, ProofKind.ACCEPTED_ANSWER, League.LEGEND_I))},
    ),
    "one response saved twice is one answer, not a pair": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            *LAST_WEEK, answer(at(W2, 1, 1), 35, L2, SEASON_0),
            answer(at(W2, 1, 1, ) + timedelta(seconds=1), 35, L2, SEASON_0))),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: LegendI(Entry.STAYED, carried(W2, League.LEGEND_I))},
    ),
    "a Legend I answer naming Season 0, not signed up yet, is Legend I": (
        SeasonEvidence(SEASON, League.UNKNOWN, (answer(at(W1, 2, 1), 51, L1, SEASON_0),)),
        {W1: LegendI(Entry.SEASON_START,
                     Claim(at(W1, 2, 1), 51, ProofKind.UNCONFIRMED_ANSWER, League.LEGEND_I))},
    ),
    "accepted answers for both leagues are a conflict": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            *LAST_WEEK, answer(at(W2, 0, 4), 61, L1, ACCEPTED),
            answer(at(W2, 3, 1), 62, L2, ACCEPTED))),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: Unknown((Claim(at(W2, 0, 4), 61, ProofKind.ACCEPTED_ANSWER, League.LEGEND_I),
                      Claim(at(W2, 3, 1), 62, ProofKind.ACCEPTED_ANSWER, League.BELOW)))},
    ),
    "league history below Legend I against a battle is a conflict": (
        SeasonEvidence(SEASON, League.UNKNOWN, official={W1: (
            battle(at(W1, 3), 71),
            Claim(at(W2), 72, ProofKind.LEAGUE_HISTORY, League.BELOW))}),
        {W1: Unknown((battle(at(W1, 3), 71),
                      Claim(at(W2), 72, ProofKind.LEAGUE_HISTORY, League.BELOW)))},
    ),
    "a battle against an accepted lower league answer is Legend I, the answer kept": (
        SeasonEvidence(SEASON, League.UNKNOWN, (*LAST_WEEK, answer(at(W2, 3, 1), 81, L2, ACCEPTED)),
                       official={W2: (battle(at(W2, 1), 82),)}),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: LegendI(Entry.STAYED, battle(at(W2, 1), 82),
                     Claim(at(W2, 3, 1), 81, ProofKind.ACCEPTED_ANSWER, League.BELOW))},
    ),
    "a week with no evidence has no record": (
        SeasonEvidence(SEASON, League.UNKNOWN, LAST_WEEK),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER))},
    ),
    "Season start: answers after the window, and earlier ones that changed league": (
        SeasonEvidence(SEASON, League.LEGEND_I, (
            answer(at(W1, 0, 0.1), 91, L1, SEASON_0), answer(at(W1, 0, 4), 92, L1, SEASON_0))),
        {W1: LegendI(Entry.SEASON_START,
                     Claim(at(W1, 0, 4), 92, ProofKind.UNCONFIRMED_ANSWER, League.LEGEND_I))},
    ),
    "Season start: dropped at the Season's end, read in the window": (
        SeasonEvidence(SEASON, League.LEGEND_I, (
            answer(at(W1, 0, 0.33), 93, L2, SEASON_0), answer(at(W1, 0, 0.5), 94, L2, SEASON_0))),
        {W1: Below(Claim(at(W1, 0, 0.33), 93, ProofKind.UNCONFIRMED_PAIR, League.BELOW, 94))},
    ),
    "first seen on a Thursday: Legend I, promoted at the Monday by the owner's rule": (
        SeasonEvidence(SEASON, League.UNKNOWN, (answer(at(W2, 3, 1), 101, L1, ACCEPTED),)),
        {W2: LegendI(Entry.FIRST_SEEN,
                     Claim(at(W2, 3, 1), 101, ProofKind.ACCEPTED_ANSWER, League.LEGEND_I))},
    ),
    "below the week before, Legend I now: promoted": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            answer(at(W1, 6, 1), 111, L2, ACCEPTED), answer(at(W2, 1, 1), 112, L1, ACCEPTED))),
        {W1: Below(Claim(at(W1, 6, 1), 111, ProofKind.ACCEPTED_ANSWER, League.BELOW)),
         W2: LegendI(Entry.PROMOTED,
                     Claim(at(W2, 1, 1), 112, ProofKind.ACCEPTED_ANSWER, League.LEGEND_I))},
    ),
    "a failed Reset request: the week's later battles prove it stayed": (
        SeasonEvidence(SEASON, League.UNKNOWN, LAST_WEEK, official={W2: (battle(at(W2, 2), 121),)}),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: LegendI(Entry.STAYED, battle(at(W2, 2), 121))},
    ),
    "Legend I, a week with no evidence, Legend I again: entry unknown": (
        SeasonEvidence(SEASON, League.UNKNOWN, (*LAST_WEEK, answer(at(W3, 1), 131, L1, ACCEPTED))),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W3: LegendI(Entry.UNKNOWN, Claim(at(W3, 1), 131, ProofKind.ACCEPTED_ANSWER, League.LEGEND_I))},
    ),
    "a conflict week's battle still counts as Legend I earlier in the Season": (
        SeasonEvidence(SEASON, League.UNKNOWN, (answer(at(W3, 1), 141, L1, ACCEPTED),), official={W1: (
            battle(at(W1, 3), 142), Claim(at(W2), 143, ProofKind.LEAGUE_HISTORY, League.BELOW))}),
        {W1: Unknown((battle(at(W1, 3), 142),
                      Claim(at(W2), 143, ProofKind.LEAGUE_HISTORY, League.BELOW))),
         W3: LegendI(Entry.UNKNOWN, Claim(at(W3, 1), 141, ProofKind.ACCEPTED_ANSWER, League.LEGEND_I))},
    ),
    "league history proves a Season's last week in Legend I": (
        SeasonEvidence(SEASON, League.UNKNOWN, (*LAST_WEEK, *STAYED), official={
            W4: (Claim(W4 + timedelta(days=7), 151, ProofKind.LEAGUE_HISTORY, League.LEGEND_I),)}),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: LegendI(Entry.STAYED, claim(STAYED[2], ProofKind.ACCEPTED_ANSWER)),
         W4: LegendI(Entry.UNKNOWN,
                     Claim(W4 + timedelta(days=7), 151, ProofKind.LEAGUE_HISTORY, League.LEGEND_I))},
    ),
    "in the window, an answer showing last week's league does not count": (
        SeasonEvidence(SEASON, League.UNKNOWN, (*LAST_WEEK, DROP[0])),
        {W1: LegendI(Entry.SEASON_START, claim(LAST_WEEK[0], ProofKind.ACCEPTED_ANSWER)),
         W2: LegendI(Entry.STAYED, carried(W2, League.LEGEND_I))},
    ),
    "in the window after an unknown week, no answer counts": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            answer(at(W2, 0, 0.33), 161, L2, ACCEPTED),
            answer(W2 + LEAGUE_DISPLAY_WINDOW / 2, 162, L1, ACCEPTED))),
        {W2: Unknown()},
    ),
    "a promoted player read in the window after a week below counts at once": (
        SeasonEvidence(SEASON, League.UNKNOWN, (
            answer(at(W1, 6, 1), 171, L2, ACCEPTED), answer(at(W2, 0, 0.33), 172, L1, ACCEPTED))),
        {W1: Below(Claim(at(W1, 6, 1), 171, ProofKind.ACCEPTED_ANSWER, League.BELOW)),
         W2: LegendI(Entry.PROMOTED,
                     Claim(at(W2, 0, 0.33), 172, ProofKind.ACCEPTED_ANSWER, League.LEGEND_I))},
    ),
    "an answer exactly at the window's end counts": (
        SeasonEvidence(SEASON, League.UNKNOWN, (answer(W2 + LEAGUE_DISPLAY_WINDOW, 181, L2, ACCEPTED),)),
        {W2: Below(Claim(W2 + LEAGUE_DISPLAY_WINDOW, 181, ProofKind.ACCEPTED_ANSWER, League.BELOW))},
    ),
    "the earliest of the strongest claims is the proof": (
        SeasonEvidence(SEASON, League.UNKNOWN, (answer(at(W1, 1), 192, L1, SEASON_0),), official={
            W1: (battle(at(W1, 4), 191), battle(at(W1, 2), 193))}),
        {W1: LegendI(Entry.SEASON_START, battle(at(W1, 2), 193))},
    ),
}


@pytest.mark.parametrize("evidence, expected", CASES.values(), ids=CASES.keys())
def test_decide_season(evidence: SeasonEvidence, expected: dict) -> None:
    assert decide_season(evidence) == expected


def test_the_order_evidence_arrives_in_does_not_matter() -> None:
    evidence = SeasonEvidence(SEASON, League.UNKNOWN, (
        *LAST_WEEK, *DROP, GLITCH, *STAYED, answer(at(W3, 2), 201, L1, SEASON_0),
        answer(at(W4, 1), 202, L2, SEASON_0), answer(at(W4, 1, 1), 203, L2, SEASON_0),
    ), (at(W3, 5),), {W3: (battle(at(W3, 1), 204),)})
    expected = decide_season(evidence)
    shuffled = list(evidence.answers)
    for seed in range(20):
        random.Random(seed).shuffle(shuffled)
        assert decide_season(SeasonEvidence(
            SEASON, League.UNKNOWN, tuple(shuffled), evidence.unsaved, evidence.official,
        )) == expected
    assert expected[W4] == Below(Claim(at(W4, 1), 202, ProofKind.UNCONFIRMED_PAIR, League.BELOW, 203))


@pytest.mark.parametrize(
    "label, tier, expected",
    [
        # The Season that ended at the 5 October 2026 Reset, in Legend I:
        # its last week.
        ("1791176400", 105000036, (W1 - timedelta(days=7), Claim(
            W1, 1, ProofKind.LEAGUE_HISTORY, League.LEGEND_I))),
        # The Legend II week that ended at the 12 October 2026 Reset.
        ("1791781200", 105000035, (W1, Claim(W2, 1, ProofKind.LEAGUE_HISTORY, League.BELOW))),
        ("1791781200", 999, None),
        ("1791781200", None, None),
    ],
)
def test_a_league_history_row_speaks_for_the_week_before_its_label(label, tier, expected) -> None:
    assert history_claim(1, label, tier) == expected


def test_weeks_start_at_a_monday_reset() -> None:
    assert week_start(W2) == W2
    assert week_start(W2 - timedelta(microseconds=1)) == W1
    assert week_start(at(W3, 6, 23.9)) == W3
    assert week_start(datetime(2026, 9, 7, 4, 59, tzinfo=UTC)) == datetime(2026, 8, 31, 5, tzinfo=UTC)
