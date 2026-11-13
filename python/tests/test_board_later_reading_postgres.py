"""End-to-end: a reading after the Reset proves the battles an older
reading left unproven.

On 31 October 2026 the Daily board ranked #CLU28LR2 at 4,924, its 02:54:19
reading, instead of 5,233. A defense stamped 02:51:16, 3 minutes before
that reading, may or may not have shown in it, so the 8 attacks after it,
309 trophies, did not count either. Its Reset reading lacked their credits
too, so its day stayed Partial, but its 05:23:18 reading showed 5,233: the
reading plus the 8 attacks. See docs/testing.md.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import psycopg
from board_later_reading_postgres_support import (
    _hold,
)
from domain_test_support import domain_database
from reconciliation_postgres_support import _processor
from reset_scenarios_postgres_support import (
    SEASON,
    Battle,
    Player,
    _even,
    _ranked,
    _story,
    attacks,
    day,
    defenses,
)

from clashlens import background_pacing, boundary_publication, queue_refresh

# PROVEN is #CLU28LR2's numbers. EARLY's 02:54:19 reading came before that
# defense showed, so the reading plus the attacks is 26 too many: its later
# reading proves the other way of reading it, which the board cannot rank
# from that reading, so it shows the day's own end, unproven. NEW_DAY
# attacks for 30 at 05:10, before its later reading.
# SLOW, read at 03:00 after that defense showed, has its Reset responses
# saved last, as #CLU28LR2's board waited for other players' until 05:43.
PROVEN, EARLY, NEW_DAY, SLOW = "#2PP", "#8PP", "#9PP", "#PPP"
LATE = (PROVEN, EARLY, NEW_DAY)


def _at(hour: int, minute: int, second: int) -> datetime:
    return datetime(2026, 10, 7, hour, minute, second, tzinfo=UTC)


def test_a_later_reading_proves_the_battles_after_an_older_one(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Each player starts the Legend day ending 7 October 2026 at 5,202,
    loses 278 over 8 defenses, the last for 26 at 02:51:16, and gains 309
    over 8 attacks from 04:35:53 to 04:56:53: 5,233. The Reset readings at
    05:04 still lack every attack's credit, 4,924; SLOW's shows 5,233. The
    worker saves the three days, then SLOW's Reset responses and the
    readings at 05:23:18, then builds the board. On 31 October it had not
    yet rechecked #CLU28LR2's day with the evidence saved after the Reset
    when the board froze, nor by 07:36: here it never does."""
    start, end = day(2), day(3)
    battles = [
        *defenses(start, *[40] * 6, 12),
        Battle(_at(2, 51, 16), -26, False),
        *(Battle(_at(4, 35, 53) + timedelta(minutes=3 * n), 40 if n < 7 else 29, True)
          for n in range(8)),
    ]
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *LATE, SLOW)
        for recheck in ("queue_for_reading", "queue_for_battles"):
            monkeypatch.setattr(queue_refresh, recheck, lambda *_: None)
        for tag in story.players:
            story.players[tag].battles = list(battles)
        story.players[NEW_DAY].battles.append(Battle(end + timedelta(minutes=10), 30, True))
        story.reset(start, dict.fromkeys(story.players, (5202, SEASON)))
        story.reads(_at(2, 54, 19), {PROVEN: 4924, EARLY: 4950, NEW_DAY: 4924})
        story.reads(_at(3, 0, 0), {SLOW: 4924})
        story.reset(end, {**dict.fromkeys(LATE, (4924, SEASON)), SLOW: (5233, SEASON)},
                    run=False)
        _hold(connection_info, (SLOW,), end)
        story.reads(_at(5, 23, 18), {PROVEN: 5233, EARLY: 5233, NEW_DAY: 5263})
        _hold(connection_info, LATE, _at(5, 23, 18))
        story.run_worker()
        days = {tag: story.saved_day(tag, start) for tag in LATE}
        unfinished = story.unfinished_jobs()
        board = story.served_board(end)

    assert unfinished == []
    # Each day's own readings leave its end unproven: the board ranks a
    # reading where a later one proves it, else the day's end.
    assert {tag: (days[tag]["state"], days[tag]["end"]) for tag in LATE} == dict.fromkeys(
        LATE, ("Partial", 5233))
    assert _ranked(board) == {SLOW: 5233, PROVEN: 5233, NEW_DAY: 5233, EARLY: 5233}
    labels = {entry["tag"]: entry["confidence"] for entry in board["entries"]}
    # SLOW's 03:00 reading proves the battles after it, but no reading
    # proves the day's end: its Reset reading came while its last attack landed.
    assert labels == {
        SLOW: "uncertain", PROVEN: "confirmed", NEW_DAY: "confirmed", EARLY: "uncertain"}


# Days whose Reset reading proves nothing, each as a player's day on the
# board of 8, 9 or 10 November 2026, all ending 7 October here. Each one's
# newest reading before the Reset is taken while a battle may or may not
# show in it, or lags battles its log already lists.
SWEDEN, KOREA, IRAQ, SINGAPORE, NEVER_CREDITED = "#2QQ", "#8QQ", "#9QQ", "#PQQ", "#YQQ"
# Days a Reset reading contradicts: one read 3 minutes after a defense,
# one with no battle near its reading.
CONTRADICTED, CONTRADICTED_FAR = "#LQQ", "#GQQ"


def test_an_unproven_day_shows_its_calculated_end(
    database_url: str, archive_server, monkeypatch, capsys
) -> None:
    """A day nothing contradicts that no reading proves yet shows its own
    calculated end, uncertain, not its newest reading before the Reset,
    until a later reading proves the reading plus the battles after it. A
    day a reading contradicts shows that reading plus every battle stamped
    after it, even when a battle may or may not show in it.

    SWEDEN (#2Q2C2U8UL, 8 November) reads 4,899 at 03:53:36, 3 minutes
    after a defense for 21, and loses 40 at 04:40: 4,859. KOREA
    (#2JLY0P2U0, 9 November) reads 4,807 at 02:44:19, 3 minutes after a
    defense for 20, then gains 278 over 8 attacks: 5,085. IRAQ (#28LQ0992U,
    10 November) reads 5,155 at 04:53:18, 3 minutes after a defense for 40
    and still without one for 28 at 04:20: 5,087. Their Reset readings lag
    a battle (SWEDEN's and IRAQ's) or the attacks' credits (KOREA's).
    SINGAPORE (#20GCYC90Q, 10 November) reads 4,970 at 01:44:31, 3 minutes
    after a defense for 40, then loses 31: 4,939, which its Reset reading
    matches while a new-day defense is in flight, so the day is Complete;
    at 05:44:56 a reading lacks its three new-day attacks' credits and
    leaves the day unproven. NEVER_CREDITED (#UQUQR02Q, 7 November) has an
    attack for 40 at 09:11 its profile never shows: its day ends at 4,680
    by its battles, 4,640 in the game, which its 05:44:56 reading proves."""
    start, end = day(2), day(3)
    evening = [Battle(_at(3, 0, 0) + timedelta(minutes=15 * n), amount, True)
               for n, amount in enumerate((40, 40, 40, 40, 40, 40, 19, 19))]
    battles = {
        SWEDEN: [*attacks(start, 40, 40), *defenses(start, *[10] * 6),
                 Battle(_at(3, 49, 59), -21, False), Battle(_at(4, 40, 0), -40, False)],
        KOREA: [*defenses(start, *[40] * 6, 20), Battle(_at(2, 41, 11), -20, False), *evening],
        IRAQ: [*attacks(start, 40, 40), *defenses(start, *[10] * 6),
               Battle(_at(4, 20, 0), -28, False), Battle(_at(4, 50, 30), -40, False)],
        SINGAPORE: [*attacks(start, 40, 40), *defenses(start, 20, *[10] * 5),
                    Battle(_at(1, 41, 7), -40, False), Battle(_at(4, 55, 0), -31, False),
                    *(Battle(end + timedelta(minutes=minutes), 30, True)
                      for minutes in (20, 25, 30))],
        NEVER_CREDITED: [*attacks(start, 40, 40), Battle(start + timedelta(hours=4, minutes=11),
                                                         40, True),
                         *defenses(start + timedelta(hours=5), *[5] * 7),
                         Battle(_at(4, 55, 0), -5, False)],
        CONTRADICTED: [*defenses(start, *[10] * 7), Battle(_at(2, 41, 11), -30, False),
                       *(Battle(_at(3, 0, 0) + timedelta(minutes=30 * n), 40, True)
                         for n in range(3))],
    }
    battles[CONTRADICTED_FAR] = [
        replace(battle, at=_at(2, 0, 0)) if battle.at == _at(2, 41, 11) else battle
        for battle in battles[CONTRADICTED]]
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *battles)
        for tag, played in battles.items():
            story.players[tag].battles = played
        story.reset(start, {SWEDEN: (4900, SEASON), KOREA: (5087, SEASON), IRAQ: (5135, SEASON),
                            SINGAPORE: (5000, SEASON), NEVER_CREDITED: (4600, SEASON),
                            CONTRADICTED: (5000, SEASON), CONTRADICTED_FAR: (5000, SEASON)})
        story.reads(_at(1, 44, 31), {SINGAPORE: 4970})
        story.reads(_at(2, 44, 19), {KOREA: 4807, CONTRADICTED: 4900, CONTRADICTED_FAR: 4900})
        story.reads(_at(3, 53, 36), {SWEDEN: 4899})
        story.reads(_at(4, 0, 0), {NEVER_CREDITED: 4645})
        story.reads(_at(4, 53, 18), {IRAQ: 5155})
        story.reset(end, {SWEDEN: (4899, SEASON), KOREA: (4807, SEASON), IRAQ: (5127, SEASON),
                          SINGAPORE: (4939, SEASON), NEVER_CREDITED: (4640, SEASON),
                          CONTRADICTED: (5037, SEASON), CONTRADICTED_FAR: (5037, SEASON)})
        first_days = {tag: story.saved_day(tag, start)["state"] for tag in battles}
        first = story.served_board(end)
        stored_first = _stored(connection_info, start, battles)
        story.reads(_at(5, 44, 56), {SINGAPORE: 4939, NEVER_CREDITED: 4640})
        story.wait(timedelta(hours=1))
        later_days = {tag: story.saved_day(tag, start)["state"] for tag in battles}
        with psycopg.connect(connection_info) as connection:
            singapore, never_credited = (connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s", (tag,)).fetchone()[0]
                for tag in (SINGAPORE, NEVER_CREDITED))
        later = story.served_board(end)
        stored_later = _stored(connection_info, start, battles)
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert first_days == {
        **dict.fromkeys((SWEDEN, KOREA, IRAQ, NEVER_CREDITED), "Partial"),
        SINGAPORE: "Complete", CONTRADICTED: "Inconsistent", CONTRADICTED_FAR: "Inconsistent"}
    assert later_days == {**first_days, SINGAPORE: "Partial"}
    # SINGAPORE's proof lost to its 05:44:56 reading is logged once: same
    # end, same battles. Proof only gained, as NEVER_CREDITED's, is not.
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()
              if line.startswith("{")]
    lost = [event for event in events if event.get("event") == "day_proof_lost"]
    assert [(event["player_id"], event["old_state"], event["new_state"], event["end_changed"],
             event["battles_changed"], datetime.fromisoformat(event["reading_at"]))
            for event in lost] == [(singapore, "Complete", "Partial", False, False,
                                    _at(5, 44, 56))]
    shown = {SWEDEN: (4859, "uncertain"), KOREA: (5085, "uncertain"),
             IRAQ: (5087, "uncertain"), SINGAPORE: (4939, "uncertain"),
             NEVER_CREDITED: (4680, "uncertain"), CONTRADICTED: (5020, "uncertain"),
             CONTRADICTED_FAR: (5020, "uncertain")}
    assert _entries(first) == shown
    # The board was rebuilt for the two days the later readings changed.
    assert later["snapshot_id"] != first["snapshot_id"]
    assert _entries(later) == {**shown, NEVER_CREDITED: (4640, "confirmed")}
    # Each player's newest day result stores the Reset total the board shows.
    assert {tag: answer[:2] for tag, answer in stored_first.items()} == _entries(first)
    assert {tag: answer[:2] for tag, answer in stored_later.items()} == _entries(later)
    assert stored_later[NEVER_CREDITED][2:] == ("reading_side", _at(5, 44, 56))
    # The rebuild pinned NEVER_CREDITED's result saved before its 05:44:56
    # reading, whose stored total is its calculated end, unproven; the board
    # judged that reading itself. Its newer result saved after the rebuild.
    assert [(event["generation"], event["player_id"], event["board_trophies"],
             event["board_confirmed"], event["stored_value"], event["stored_proven"])
            for event in events if event.get("event") == "reset_total_shadow"] == [
        (2, never_credited, 4640, True, 4680, False)]
    assert sum(event["different"] for event in events
               if event.get("event") == "reset_total_shadow_summary") == 1


PROVEN_ONCE = "#2RR"


def test_a_reset_total_proven_at_the_reset_stays_proven(
    database_url: str, archive_server, monkeypatch, capsys
) -> None:
    """The day starts at 5,000, gains 80 over 2 attacks and loses 80 over 8
    defenses, which leave no automatic loss: 5,000, which its reading at 04:00,
    after every battle landed, shows, so the board confirms it. At 05:18 a reading 37 lower,
    which nothing explains, contradicts the day. The board, still ranking by
    its own rule, marks the entry uncertain; the day result's stored Reset
    total stays proven by the 04:00 reading, and each build after logs the
    difference in the mark, never in the number."""
    start, end = day(2), day(3)
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, PROVEN_ONCE)
        story.players[PROVEN_ONCE].battles = [*attacks(start, 40, 40),
                                              *defenses(start, *[10] * 8)]
        story.reset(start, {PROVEN_ONCE: (5000, SEASON)})
        story.reads(_at(4, 0, 0), {PROVEN_ONCE: 5000})
        story.reset(end, {PROVEN_ONCE: (5000, SEASON)})
        first = (story.saved_day(PROVEN_ONCE, start)["state"],
                 _entries(story.served_board(end)), _stored(connection_info, start, (PROVEN_ONCE,)))
        capsys.readouterr()
        story.reads(_at(5, 18, 0), {PROVEN_ONCE: 4963})
        story.wait(timedelta(hours=1))
        later = (story.saved_day(PROVEN_ONCE, start)["state"],
                 _entries(story.served_board(end)), _stored(connection_info, start, (PROVEN_ONCE,)))
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    # Its 04:00 reading, after every battle landed, already shows its end.
    proven = {PROVEN_ONCE: (5000, "confirmed", "calculated_end", _at(4, 0, 0))}
    assert first == ("Complete", {PROVEN_ONCE: (5000, "confirmed")}, proven)
    assert later == ("Inconsistent", {PROVEN_ONCE: (5000, "uncertain")}, proven)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()
              if line.startswith("{")]
    shadow = [event for event in events if event.get("event") == "reset_total_shadow"]
    assert shadow and {(event["board_trophies"], event["board_confirmed"],
                        event["stored_value"], event["stored_proven"],
                        event["stored_basis"]) for event in shadow} == {
        (5000, False, 5000, True, "calculated_end")}


def _stored(connection_info: str, start: datetime, tags: Iterable[str]
            ) -> dict[str, tuple[object, ...]]:
    """The Reset total stored with each of these players' newest result for
    the day from ``start``: its value, mark, basis and the reading that
    proved it."""
    with psycopg.connect(connection_info) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT ON (day.player_id) player.normalized_tag,
                   day.formula_components -> 'reset_total'
            FROM ranked_day_versions AS day
            JOIN players AS player ON player.id = day.player_id
            WHERE day.ranked_day_start = %s AND player.normalized_tag = ANY(%s)
            ORDER BY day.player_id, day.version DESC
            """,
            (start, list(tags)),
        ).fetchall()
    return {
        tag: (None,) if answer is None else (
            answer["value"], "confirmed" if answer["proven"] else "uncertain", answer["basis"],
            answer["proven_at"] and datetime.fromisoformat(answer["proven_at"]))
        for tag, answer in rows
    }


def _entries(board: dict[str, object] | None) -> dict[str, tuple[int, str]]:
    _ranked(board)
    return {entry["tag"]: (entry["trophies"], entry["confidence"]) for entry in board["entries"]}


UNREAD = "#8RR"


def test_a_day_with_no_reading_of_its_own_stores_the_older_reading(
    database_url: str, archive_server, monkeypatch, capsys
) -> None:
    """The collector answers neither the profile nor the battle log at the
    day's start Reset and reads nothing during the day: the board ranks the
    reading from the day before, 5,040, alone, and the day result stores
    the same."""
    first_day, start, end = day(1), day(2), day(3)
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, UNREAD)
        story.players[UNREAD].battles = [*attacks(first_day, 40), *attacks(start, 40)]
        story.reset(first_day, {UNREAD: (5000, SEASON)})
        story.reads(first_day + timedelta(hours=5), {UNREAD: 5040})
        story.reset(start, {UNREAD: (None, SEASON)}, no_log=(UNREAD,))
        story.reset(end, {UNREAD: (5080, SEASON)})
        board = _entries(story.served_board(end))
        stored = _stored(connection_info, start, (UNREAD,))
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert board == {UNREAD: (5040, "uncertain")}
    assert stored == {UNREAD: (5040, "uncertain", "old_reading", None)}
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()
            if '"reset_total_shadow"' in line] == []


LAGGING = "#2UU"


def test_an_old_reading_that_lags_a_defense_counts_it(
    database_url: str, archive_server, monkeypatch
) -> None:
    """#82PLV802V, Argentina, 12 November 2026: the day starts at 4,955,
    gains 266 over 8 attacks and loses 237 over 8 defenses, the last for 40
    reported at 04:09:22: 4,984. Its readings at 04:41:10 and at the Reset
    still show 5,024, the total before that defense; the Reset one, 55
    minutes after it, is too late to set aside as stale, so it contradicts
    the day. Its readings at 05:19:58, before the board froze, and 05:42:19
    show 4,984, as did the game's country list. The board ranked the
    04:41:10 reading as if it showed every battle reported before it, 5,024,
    on every version; that reading lags the defense, as the day's own end
    shows, so the board counts it: 4,984, uncertain, as no reading proves
    either."""
    start, end = day(2), day(3)
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, LAGGING)
        story.players[LAGGING].battles = [
            *attacks(start, 30, 40, 31, 28, 25, 40, 40, 32),
            *defenses(start, 30, 23, 40, 24, 14, 40, 26),
            Battle(_at(4, 9, 22), -40, False),
        ]
        story.reset(start, {LAGGING: (4955, SEASON)})
        story.reads(_at(3, 21, 34), {LAGGING: 5024})
        story.reads(_at(4, 41, 10), {LAGGING: 5024})
        story.reset(end, {LAGGING: (5024, SEASON)}, run=False)
        story.reads(_at(5, 19, 58), {LAGGING: 4984})
        story.run_worker()
        saved = story.saved_day(LAGGING, start)
        first = _entries(story.served_board(end))
        story.reads(_at(5, 42, 19), {LAGGING: 4984})
        story.wait(timedelta(hours=1))
        later = _entries(story.served_board(end))
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert (saved["state"], saved["end"]) == ("Inconsistent", 4984)
    assert first == later == {LAGGING: (4984, "uncertain")}


DEFENDER, ATTACKER, WAITED_FOR = "#2VV", "#8VV", "#9VV"


@dataclass
class _Opposed(Player):
    """A player whose battles at these times are against these tracked
    players, so both players' reports are of one battle."""

    against: dict[datetime, str] = field(default_factory=dict)

    def log(self, at: datetime) -> bytes:
        body = json.loads(super().log(at))
        against = {when.strftime("%Y%m%dT%H%M%S.000Z"): tag for when, tag in self.against.items()}
        for item in body["items"]:
            item["opponentPlayerTag"] = against.get(
                item["battleTimestamp"], item["opponentPlayerTag"])
        return json.dumps(body).encode()


def test_an_attackers_late_report_refreshes_the_stored_reset_total(
    database_url: str, archive_server, monkeypatch, capsys
) -> None:
    """#29Y209RJ2, 13 November 2026: its day result, saved at 05:08:35,
    counted a defense for 26 reported at 04:59:01, whose attacker's report,
    saved at 05:09:49, shows the attack ended at 05:01:27, after the Reset.
    The board, building at 05:34 once other players' days saved, left that
    defense out, 5,089; the stored Reset total stayed 5,063, unproven, as
    its recheck waited behind the Reset until 06:28. Here the day starts at
    5,000, gains 160 over 4 attacks and loses 96 over 8 defenses, that one
    last: 5,064, which its Reset reading and its 05:20 reading show; its
    04:50 reading shows 5,090. The attacker's report is saved at 05:25:59,
    as #Y808CULJJ's was, and WAITED_FOR's Reset responses last, so the board
    waits for them after it."""
    start, end = day(2), day(3)
    defense_at, attack_ended_at = _at(4, 59, 1), _at(5, 1, 27)
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, WAITED_FOR)
        story.players[DEFENDER] = _Opposed(DEFENDER, [
            *attacks(start, 40, 40, 40, 40), *defenses(start, *[10] * 7),
            Battle(defense_at, -26, False)], against={defense_at: ATTACKER})
        story.players[ATTACKER] = _Opposed(ATTACKER, [Battle(attack_ended_at, 26, True)],
                                           against={attack_ended_at: DEFENDER})
        story.players[WAITED_FOR].battles = _even(start)
        story.reset(start, {DEFENDER: (5000, SEASON), WAITED_FOR: (5000, SEASON)})
        story.reads(_at(4, 50, 0), {DEFENDER: 5090, WAITED_FOR: 5000})
        story.reset(end, {DEFENDER: (5064, SEASON), WAITED_FOR: (5000, SEASON)}, run=False)
        _hold(connection_info, (WAITED_FOR,), end)
        # The worker's clock is in the Reset's first minutes, so background
        # work waits until the board publishes, as on 13 November.
        monkeypatch.setattr(background_pacing, "_now", lambda: _at(5, 25, 59))
        monkeypatch.setattr(background_pacing, "_open_window", [])
        database, processor = _processor(connection_info, archive_server)
        try:
            boundary_publication.reevaluate_boundary_publications(database)
            processor.process_until_idle(owner="story", max_jobs=10_000)
        finally:
            database.close()
        saved = story.saved_day(DEFENDER, start)
        before = _stored(connection_info, start, (DEFENDER,))
        capsys.readouterr()
        story.reads(_at(5, 20, 0), {DEFENDER: 5064})
        story.read(ATTACKER, _at(5, 25, 59))
        story.run_worker()
        board = _entries(story.served_board(end))
        after = _stored(connection_info, start, (DEFENDER,))
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert (saved["state"], saved["end"]) == ("Complete", 5064)
    # The defense may have ended after the Reset: unproven.
    assert before == {DEFENDER: (5064, "uncertain", "calculated_end", None)}
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()
              if line.startswith("{")]
    # The board's first build pinned the result the attacker's report refreshed.
    assert [(event["board_trophies"], event["stored_value"], event["stored_proven"])
            for event in events if event.get("event") == "reset_total_shadow"] == []
    assert [event["different"] for event in events
            if event.get("event") == "reset_total_shadow_summary"] == [0]
    assert board == {DEFENDER: (5090, "confirmed"), WAITED_FOR: (5000, "confirmed")}
    assert after == {DEFENDER: (5090, "confirmed", "calculated_end", _at(5, 20, 0))}
