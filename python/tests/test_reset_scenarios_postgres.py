"""End-to-end Reset stories: saved game data in, a published Daily board out.

Each test is one Reset as production lives it. It saves the profile
readings and battle logs the collector would have saved, runs the real
worker over them against PostgreSQL, publishes the Reset's Daily board and
reads it back through the API. It then checks each player's saved day, the
board and what the API serves. See docs/testing.md.

On 12 October 2026 two players' Sunday days broke a database check, every
try failed, and the Monday board waited 7 hours for them; these stories are
built so a calculation that cannot be saved fails here first.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import as_api_role, domain_database, store_observation, text
from test_reconciliation_postgres import BATTLE_FIXTURE, PROFILE_FIXTURE, _processor

from clashlens import (
    api_leaderboard,
    board_deadline,
    boundary_publication,
    past_reset_pacing,
)
from clashlens.api_db import ApiDatabase
from clashlens.domain import _ALLOCATION_THRESHOLDS
from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION

# The Season of the 12 October 2026 incident: Day 1 was Monday 5 October.
SEASON = datetime(2026, 10, 5, 5, tzinfo=UTC)
PREVIOUS_SEASON = SEASON - timedelta(days=28)


def day(number: int, season: datetime = SEASON) -> datetime:
    """The Reset starting Legend day ``number`` of the Season."""
    return season + timedelta(days=number - 1)


# A result the game gives for each trophy amount: (stars, destruction).
RESULTS = {
    trophies: (stars, minimum)
    for stars in (3, 2, 1, 0)
    for minimum, trophies in reversed(_ALLOCATION_THRESHOLDS[stars])
}
RESULTS[0] = (0, 0)


# Reset sweeps read profiles from about 05:03:40.
READ_AFTER = timedelta(minutes=4)


@dataclass(frozen=True)
class Battle:
    """One battle on the player's own log: its report time and the trophies
    it moved, positive for an attack's gain, negative for a defense's loss
    (``attack`` says which for a zero). A multiplayer battle is no Legend
    battle, but it takes a row of the log's 50. A Legend row with no
    opponent is the game's "no opponent, no battle" row: a used slot that is
    no battle."""

    at: datetime
    trophies: int
    attack: bool | None = None
    legend: bool = True
    opponent: bool = True


def attacks(start: datetime, *amounts: int) -> list[Battle]:
    """Attacks an hour apart from an hour after ``start``."""
    return [Battle(start + timedelta(hours=n + 1), amount, True)
            for n, amount in enumerate(amounts)]


def defenses(start: datetime, *amounts: int) -> list[Battle]:
    """Defenses an hour apart from 30 minutes after ``start``."""
    return [Battle(start + timedelta(hours=n, minutes=30), -amount, False)
            for n, amount in enumerate(amounts)]


def multiplayer(start: datetime, count: int) -> list[Battle]:
    """``count`` multiplayer battles a minute apart from ``start``."""
    return [Battle(start + timedelta(minutes=n), 0, True, legend=False)
            for n in range(count)]


@dataclass
class Player:
    """A player's side of the story: readings (time, trophies, Season ID,
    whether a Reset sweep took it) and every battle they fought."""

    tag: str
    battles: list[Battle] = field(default_factory=list)
    # A player who drops to Legend II at a Season's end reads Season 0.
    dropped: bool = False

    def profile(self, trophies: int, season: datetime | None) -> bytes:
        payload = json.loads(PROFILE_FIXTURE.read_bytes())
        payload["tag"] = self.tag
        payload["trophies"] = trophies
        # The game names Season 0 until the player's first Legend battle.
        season = None if self.dropped else season
        payload["currentLeagueSeasonId"] = int(season.timestamp()) if season else 0
        payload["previousLeagueSeasonId"] = int(
            ((season or SEASON) - timedelta(days=28)).timestamp())
        if self.dropped:
            payload["leagueTier"] = {"id": 105000035, "name": "Legend II"}
        return json.dumps(payload).encode()

    def log(self, at: datetime) -> bytes:
        """The battle log as the game shows it at ``at``: the newest 50
        battles reported before then, newest first."""
        template = json.loads(BATTLE_FIXTURE.read_bytes())["items"][0]
        shown = sorted((b for b in self.battles if b.at < at), key=lambda b: b.at)
        items = []
        for index, battle in enumerate(shown[-50:]):
            stars, destruction = RESULTS[abs(battle.trophies)]
            attack = battle.trophies > 0 if battle.attack is None else battle.attack
            items.append({
                **template, "attack": attack, "battleTime": 150,
                "battleType": "legend" if battle.legend else "homeVillage",
                "battleTimestamp": battle.at.strftime("%Y%m%dT%H%M%S.000Z"),
                "stars": stars, "destructionPercentage": destruction,
                "opponentPlayerTag": f"#{_tag(index + 1)}",
                "opponentName": "Opponent",
            })
            if not battle.opponent:
                items[-1].update(opponentPlayerTag=None, battleTime=0)
        return json.dumps({"items": items[::-1]}).encode()


def _tag(number: int) -> str:
    alphabet, encoded = "0289PYLQGRJCUV", ""
    while number:
        encoded = alphabet[number % len(alphabet)] + encoded
        number //= len(alphabet)
    return "Q" + encoded.rjust(6, "0")


@dataclass
class Story:
    connection_info: str
    archive_server: object
    monkeypatch: object
    players: dict[str, Player] = field(default_factory=dict)
    elapsed: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        # The story's clock: the database's, plus the time the story waited.
        # Builds for older Resets wait out 04:30 to 07:00 UTC; a story runs
        # as if outside that window, so CI gets one result at any hour.
        clock = past_reset_pacing._now
        self.monkeypatch.setattr(
            past_reset_pacing, "_now", lambda connection: clock(connection) + self.elapsed)
        self.monkeypatch.setattr(past_reset_pacing, "_in_quiet_window", lambda _now: False)
        # A story runs weeks after its Resets, so each board's 05:30 deadline
        # would pass before its first day saved. Every day saves within
        # minutes of the Reset here, as production's do: each board waits
        # for all of its members.
        self.monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))

    def wait(self, time: timedelta) -> None:
        """Let ``time`` pass, then run the worker: the newest Reset's board
        rebuilds with corrections at most once an hour."""
        self.elapsed += time
        self.run_worker()

    def reset(self, boundary: datetime,
              readings: dict[str, tuple[int | None, datetime | None]],
              *, run: bool = True) -> None:
        """The Reset sweep at ``boundary``: its members are the players in
        ``readings``, and it reads each one's profile, showing (trophies,
        Season or None for Season 0), and battle log 4 minutes after it. A
        profile of None trophies was never answered: the collector gave it
        up. Then, unless ``run`` is false, the worker runs until idle."""
        at = boundary + READ_AFTER
        observations = {}
        with psycopg.connect(self.connection_info) as connection:
            for tag, (trophies, season) in readings.items():
                player = self.players[tag]
                for endpoint, body in (
                    ("profile", None if trophies is None else player.profile(trophies, season)),
                    ("battle_log", player.log(at)),
                ):
                    # The worker claims each saved response's job in its own order.
                    observations[tag, endpoint] = None if body is None else store_observation(
                        self.connection_info, self.archive_server,
                        occurrence_key=f"{tag}-{boundary.isoformat()}-{endpoint}",
                        endpoint=endpoint, body=body, observed_at=at, normalized_tag=tag,
                        existing_connection=connection, commit=False,
                    )[0]
            members = {
                tag: connection.execute(
                    "INSERT INTO players (normalized_tag) VALUES (%s)"
                    " ON CONFLICT (normalized_tag) DO UPDATE SET normalized_tag = %s"
                    " RETURNING id", (tag, tag),
                ).fetchone()[0]
                for tag in readings
            }
            sweep = connection.execute(
                "INSERT INTO collector_reset_sweeps (boundary_at, member_ids,"
                " membership_captured_at) VALUES (%s, %s, %s) RETURNING id",
                (boundary, list(members.values()), boundary),
            ).fetchone()[0]
            for tag, player_id in members.items():
                profile = observations[tag, "profile"]
                connection.execute(
                    """
                    INSERT INTO collector_work (
                        kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
                        coalescing_key, status, profile_status, battle_log_status,
                        profile_observation_id, battle_log_observation_id, completed_at
                    ) VALUES ('reset_baseline', 'reset', 'player', %s, %s, %s, %s,
                              %s, %s, %s, 'observed', %s, %s, %s)
                    """,
                    (player_id, tag, sweep, boundary, f"reset-{tag}-{boundary}",
                     "complete" if profile else "failed",
                     "observed" if profile else "failed", profile,
                     observations[tag, "battle_log"], at if profile else None),
                )
        if run:
            self.run_worker()

    def read(self, tag: str, at: datetime, trophies: int | None = None,
             season: datetime | None = SEASON) -> None:
        """A routine read outside a sweep: the profile when ``trophies`` is
        given, else the battle log."""
        player = self.players[tag]
        endpoint, body = (
            ("battle_log", player.log(at)) if trophies is None
            else ("profile", player.profile(trophies, season))
        )
        store_observation(
            self.connection_info, self.archive_server,
            occurrence_key=f"{tag}-{endpoint}-{at.isoformat()}", endpoint=endpoint,
            body=body, observed_at=at, normalized_tag=tag,
        )

    def reads(self, at: datetime, totals: dict[str, int], season: datetime | None = SEASON) -> None:
        """The collector's regular check of each player at ``at``: their
        profile showing ``totals``, then their battle log a minute later. The
        last checks before a Reset run until 04:55; the settlement check is
        at 05:20."""
        for tag, trophies in totals.items():
            self.read(tag, at, trophies, season)
            self.read(tag, at + timedelta(minutes=1))

    def history(self, at: datetime, season_end: datetime, finals: dict[str, int]) -> None:
        """Each player's league history read at ``at``: the game's official
        final total for the Season ending at ``season_end``."""
        for tag, final in finals.items():
            store_observation(
                self.connection_info, self.archive_server,
                occurrence_key=f"{tag}-history-{at.isoformat()}",
                endpoint="league_history", normalized_tag=tag, observed_at=at,
                parser_version=LEAGUE_HISTORY_PARSER_VERSION,
                processing_version="clashlens-domain-processing-v1",
                domain_rule_version="clashlens-domain-rules-v1",
                body=json.dumps({"items": [{
                    "leagueSeasonId": str(int(season_end.timestamp())),
                    "leagueTrophies": final, "leagueTierId": 105000036,
                    "placement": 10568, "attackWins": 1, "attackLosses": 0,
                    "attackStars": 3, "defenseWins": 0, "defenseLosses": 8,
                    "defenseStars": 16, "maxBattles": 8,
                }]}).encode(),
            )

    def run_worker(self, *, rounds: int = 20) -> None:
        """Run the worker as production does: the publication check, then
        every job due in its claim order, until nothing is left to do. A job
        sent to wait (a retry or a dependency) is made due again, as time
        passing would, for at most ``rounds`` rounds."""
        database, processor = _processor(self.connection_info, self.archive_server)
        try:
            for _ in range(rounds):
                boundary_publication.reevaluate_boundary_publications(database)
                processed = processor.process_until_idle(owner="story", max_jobs=10_000)
                boundary_publication.reevaluate_boundary_publications(database)
                with database.pool.connection() as connection:
                    waiting = connection.execute(
                        """
                        UPDATE python_processing_jobs SET due_at = now()
                        WHERE status IN ('pending', 'waiting_retry', 'waiting_dependency')
                          AND due_at > now()
                        """
                    ).rowcount
                if not processed and not waiting:
                    return
        finally:
            database.close()

    def unfinished_jobs(self) -> list[tuple]:
        """Jobs that failed for good or still wait: none should, in a story
        the worker can finish."""
        with psycopg.connect(self.connection_info) as connection:
            return [
                (text(kind), text(status), text(category), text(detail)[:200])
                for kind, status, category, detail in connection.execute(
                    """
                    SELECT work_type, status, failure_category, failure_detail
                    FROM python_processing_jobs
                    WHERE status NOT IN ('complete', 'superseded')
                    ORDER BY id
                    """
                ).fetchall()
            ]

    def saved_day(self, tag: str, start: datetime) -> dict[str, object] | None:
        """The player's newest saved result for the Legend day from ``start``."""
        with psycopg.connect(self.connection_info) as connection:
            row = connection.execute(
                """
                SELECT day.state, day.start_trophies, day.final_trophies_before_reset,
                       day.next_start_trophies, day.automatic_defense_loss,
                       day.boundary_adjustment, day.attack_count, day.defense_count,
                       day.unexplained_residual
                FROM ranked_day_versions AS day
                JOIN players AS player ON player.id = day.player_id
                WHERE player.normalized_tag = %s AND day.ranked_day_start = %s
                ORDER BY day.version DESC LIMIT 1
                """,
                (tag, start),
            ).fetchone()
        if row is None:
            return None
        return dict(zip(
            ("state", "start", "end", "next_start", "automatic_loss", "raise",
             "attacks", "defenses", "residual"),
            (text(row[0]), *row[1:]), strict=True,
        ))

    def board_members(self, boundary: datetime) -> dict[str, str]:
        """Each member of the Reset's newest board and its day's status."""
        with psycopg.connect(self.connection_info) as connection:
            return {
                text(tag): text(status)
                for tag, status in connection.execute(
                    """
                    SELECT player.normalized_tag, member.snapshot_status
                    FROM boundary_publication_generation_members AS member
                    JOIN players AS player ON player.id = member.player_id
                    WHERE member.generation_id = (
                        SELECT id FROM boundary_publication_generations
                        WHERE boundary_at = %s ORDER BY generation DESC LIMIT 1)
                    """,
                    (boundary,),
                ).fetchall()
            }

    def served_board(self, boundary: datetime | None = None) -> dict[str, object] | None:
        """The Daily board the API serves now, the newest or the one for the
        Legend day ending at ``boundary``, read as the data service."""
        selector = {}
        if boundary is not None:
            ended = boundary - timedelta(days=1)
            season = SEASON if ended >= SEASON else PREVIOUS_SEASON
            selector = {"official_season_id": str(int(season.timestamp())),
                        "season_day_number": (ended - season).days + 1}
        api = ApiDatabase(as_api_role(self.connection_info))
        try:
            return api_leaderboard.get_frozen_leaderboard(api, limit=100, **selector)
        finally:
            api.close()


def _ranked(board: dict[str, object] | None) -> dict[str, int]:
    """The served board's players and trophies, checked to be in order."""
    assert board is not None, "no Daily board is served"
    entries = board["entries"]
    assert [entry["position"] for entry in entries] == list(range(1, len(entries) + 1))
    trophies = [entry["trophies"] for entry in entries]
    assert trophies == sorted(trophies, reverse=True)
    return {entry["tag"]: entry["trophies"] for entry in entries}


def _story(connection_info: str, archive_server, monkeypatch, *tags: str) -> Story:
    return Story(connection_info, archive_server, monkeypatch,
                 {tag: Player(tag) for tag in tags})


# Players 41426 and 39803153 of 12 October 2026, by their real battles.
P41426, P39803153, STEADY = "#2PP", "#8PP", "#9PP"


def test_monday_reset_of_12_october_2026(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Both players had not played this Season and sat at 5,000, so the
    Season rule started their Sunday (Day 7). Each Sunday ended below 5,000
    only after its automatic defense loss, and the Monday raise took them
    back to 5,000. The Reset reading, taken before the loss, fitted; a 05:15
    reading showed one missing defense's share taken and fitted nothing, so
    the day is Inconsistent. It must still save, and the Monday board must
    publish with both of them on it. A steady player shares the board."""
    sunday, monday = day(7), day(8)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       P41426, P39803153, STEADY)
        # 6 defenses lose 183, so 2 missing cost 30 each; the day ends at
        # 5,001 before the loss and 4,941 after it.
        story.players[P41426].battles = (
            attacks(sunday, 31, 40, 2, 31, 40, 40)
            + defenses(sunday, 40, 40, 40, 40, 0, 23)
        )
        # 3 defenses lose 61, so 5 missing cost 20 each: 5,074 then 4,974.
        story.players[P39803153].battles = (
            attacks(sunday, 26, 40, 29, 40) + defenses(sunday, 30, 31, 0)
        )
        # 8 attacks of 40 and 8 defenses of 30: no automatic loss.
        story.players[STEADY].battles = (
            attacks(sunday + timedelta(hours=8), *[40] * 8)
            + defenses(sunday, *[30] * 8)
        )
        before = {P41426: (5000, None), P39803153: (5000, None),
                  STEADY: (5300, SEASON)}
        story.reset(day(6), before)
        story.reset(sunday, before)
        # The collector reads active players during the day: after their
        # first battle their profiles name the Season.
        for tag, trophies in ((P41426, 5000 + 31 - 40), (P39803153, 5000 + 26 - 30)):
            story.read(tag, sunday + timedelta(hours=1, minutes=20), trophies)
        story.run_worker()
        # The worker was 88,000 jobs behind after this Reset, so it reached
        # these days after the 05:15 readings and the logs covering them.
        story.reset(monday, {
            P41426: (5001, SEASON), P39803153: (5074, SEASON), STEADY: (5380, SEASON),
        }, run=False)
        for tag, later in ((P41426, 4971), (P39803153, 5054)):
            story.read(tag, monday + timedelta(minutes=15), later)
            story.read(tag, monday + timedelta(minutes=20))
        story.run_worker()
        unfinished = story.unfinished_jobs()
        days = {tag: story.saved_day(tag, sunday) for tag in story.players}
        # A day saved after the board's first build joins it at the hourly
        # rebuild.
        story.wait(timedelta(hours=1))
        members = story.board_members(monday)
        board = story.served_board()

    assert unfinished == []
    assert days[P41426] == {
        "state": "Inconsistent", "start": 5000, "end": 4941, "next_start": 5000,
        "automatic_loss": 60, "raise": 59, "attacks": 6, "defenses": 6,
        "residual": 0,
    }
    assert days[P39803153] == {
        "state": "Inconsistent", "start": 5000, "end": 4974, "next_start": 5000,
        "automatic_loss": 100, "raise": 26, "attacks": 4, "defenses": 3,
        "residual": 0,
    }
    assert days[STEADY] == {
        "state": "Complete", "start": 5300, "end": 5380, "next_start": 5380,
        "automatic_loss": None, "raise": 0, "attacks": 8, "defenses": 8,
        "residual": 0,
    }
    assert members == {P41426: "inconsistent", P39803153: "inconsistent",
                       STEADY: "complete"}
    assert board is not None and board["boundary_at"] == monday.isoformat()
    assert [(entry["tag"], entry["trophies"]) for entry in board["entries"]] == [
        (STEADY, 5380), (P39803153, 5074), (P41426, 5001),
    ]


def _saturday(start: datetime) -> list[Battle]:
    """A full Saturday: 2 attacks of 40 and 8 defenses of 30, so Sunday's
    automatic loss averages 30 a missing defense."""
    return attacks(start + timedelta(hours=10), 40, 40) + defenses(start, *[30] * 8)


RAISED, EXACT, UNRAISED, UNSETTLED, IN_FLIGHT, ZERO, DROPPED_WEEKLY = (
    "#2PP", "#8PP", "#9PP", "#PPP", "#YPP", "#LPP", "#QPP")


def test_monday_reset_with_sundays_ending_around_5000(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Each player's Sunday ends near 5,000 a different way, and every one
    must save and rank on the Monday board. The days must have ended by the
    real clock, so this is the previous Season's Monday, 28 September 2026."""
    season = PREVIOUS_SEASON
    saturday, sunday, monday = day(20, season), day(21, season), day(22, season)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       RAISED, EXACT, UNRAISED, UNSETTLED, IN_FLIGHT, ZERO, DROPPED_WEEKLY)
        # From 5,300 each Saturday ends at 5,140; EXACT's at 5,160.
        for player in story.players.values():
            player.battles = _saturday(saturday)
        story.players[EXACT].battles += attacks(saturday + timedelta(hours=13), 20)
        sunday_battles = {
            # 1 attack and 8 defenses: ends at 4,940, raised by 60.
            RAISED: attacks(sunday, 40) + defenses(sunday, *[30] * 8),
            # 8 defenses of 20: ends at exactly 5,000, so no raise.
            EXACT: defenses(sunday, *[20] * 8),
            # As RAISED, but the Reset reading comes before the raise.
            UNRAISED: attacks(sunday, 40) + defenses(sunday, *[30] * 8),
            # 1 attack and 2 defenses: 5,120 before its loss of 6 * 30,
            # then 4,940, raised by 60; the Reset reading comes before both.
            UNSETTLED: attacks(sunday, 40) + defenses(sunday, *[30] * 2),
            # 8 defenses, then an attack ending at 05:04:30, after the Reset
            # reading: it still belongs to Sunday, which ends at 4,940.
            IN_FLIGHT: defenses(sunday, *[30] * 8)
            + [Battle(monday + timedelta(minutes=4, seconds=30), 40, True)],
            # 1 attack, no defense: charged for all 8 (240), ending at
            # 4,940, which the raise hides.
            ZERO: attacks(sunday, 40),
            # As RAISED, but ranked too low to stay in Legend I: no raise.
            DROPPED_WEEKLY: attacks(sunday, 40) + defenses(sunday, *[30] * 8),
        }
        for tag, battles in sunday_battles.items():
            story.players[tag].battles += battles
        story.reset(saturday, {tag: (5300, season) for tag in story.players})
        story.reset(sunday, {tag: (5160 if tag == EXACT else 5140, season)
                             for tag in story.players})
        # The last regular checks, before the in-flight attack and the loss.
        story.reads(monday - timedelta(minutes=10), {
            RAISED: 4940, EXACT: 5000, UNRAISED: 4940, UNSETTLED: 5120,
            IN_FLIGHT: 4900, ZERO: 5180, DROPPED_WEEKLY: 4940,
        }, season)
        story.reset(monday, {
            RAISED: (5000, season), EXACT: (5000, season), UNRAISED: (4940, season),
            UNSETTLED: (5120, season), IN_FLIGHT: (5000, season), ZERO: (5000, season),
            DROPPED_WEEKLY: (4940, season),
        })
        # The 05:20 settlement check: every loss and raise has landed.
        # DROPPED_WEEKLY's profile has shown Legend II since 05:13.
        story.players[DROPPED_WEEKLY].dropped = True
        story.read(DROPPED_WEEKLY, monday + timedelta(minutes=13), 4940)
        story.reads(monday + timedelta(minutes=20), {
            tag: 5000 for tag in story.players if tag != DROPPED_WEEKLY}, season)
        story.run_worker()
        unfinished = story.unfinished_jobs()
        days = {tag: story.saved_day(tag, sunday) for tag in story.players}
        story.wait(timedelta(hours=1))
        members = story.board_members(monday)
        board = story.served_board()
    assert unfinished == []
    sunday_day = {
        "start": 5140, "end": 4940, "next_start": 5000, "automatic_loss": None,
        "raise": 60, "attacks": 1, "defenses": 8, "residual": 0,
    }
    assert days[RAISED] == {"state": "Complete", **sunday_day}
    assert days[EXACT] == {
        "state": "Complete", "start": 5160, "end": 5000, "next_start": 5000,
        "automatic_loss": None, "raise": 0, "attacks": 0, "defenses": 8, "residual": 0,
    }
    assert days[UNSETTLED] == {
        **sunday_day, "state": "Complete", "automatic_loss": 180, "defenses": 2,
    }
    assert days[IN_FLIGHT] == {"state": "Complete", **sunday_day}
    assert days[DROPPED_WEEKLY] == {
        "state": "Complete", **sunday_day, "next_start": 4940, "raise": 0}
    # The reading after the Reset cannot show how much the charge was, as the
    # raise hides it, so none is taken and the day's end is unproven.
    assert days[ZERO] == {
        "state": "Partial", "start": 5140, "end": 5180, "next_start": 5000,
        "automatic_loss": None, "raise": 0, "attacks": 1, "defenses": 0,
        "residual": None,
    }
    # The domain does not say whether the raise can land after the Reset
    # reading; whatever the day's state, it saves and ranks.
    assert days[UNRAISED]["end"] == 4940
    assert members == {
        RAISED: "complete", EXACT: "complete", UNRAISED: "inconsistent",
        UNSETTLED: "complete", IN_FLIGHT: "complete", ZERO: "partial",
        DROPPED_WEEKLY: "complete",
    }
    # Each ranks by their total at 05:00:00, before any automatic loss or
    # raise and without the attack that ended after it.
    assert _ranked(board) == {
        ZERO: 5180, UNSETTLED: 5120, EXACT: 5000, RAISED: 4940, UNRAISED: 4940,
        DROPPED_WEEKLY: 4940, IN_FLIGHT: 4900,
    }


OFFICIAL, LATE_RESET, NO_DEFENSE, DAY_1_LOSS, SHIELDED, END_LOSS, DISAGREES, DROPPED = (
    "#2PP", "#8PP", "#9PP", "#PPP", "#YPP", "#LPP", "#QPP", "#GPP")


def test_season_end_reset_and_its_first_day(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The Season ending at the 5 October 2026 Reset, then its next Season's
    Day 1. Day 28's end is hidden by the Season reset until the game's
    official final total arrives hours later; Day 1 starts every Legend I
    player at 5,000 and has its own automatic loss rules."""
    day_27, day_28 = day(27, PREVIOUS_SEASON), day(28, PREVIOUS_SEASON)
    day_1, day_2 = day(1), day(2)
    old, new = PREVIOUS_SEASON, SEASON
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, OFFICIAL,
                       LATE_RESET, NO_DEFENSE, DAY_1_LOSS, SHIELDED, END_LOSS,
                       DISAGREES, DROPPED)
        # From 5,600 Day 27 ends at 5,440; SHIELDED sits at 5,400 for both
        # days. Day 28: 3 attacks of 40 and 8 defenses of 30 end at 5,320.
        last_day = attacks(day_28, 40, 40, 40) + defenses(day_28, *[30] * 8)
        for tag, player in story.players.items():
            player.battles = [] if tag == SHIELDED else _saturday(day_27) + last_day
        # LATE_RESET ends at 5,400; END_LOSS at 5,420 before its loss of
        # 6 * 30 and 5,240 after it.
        story.players[LATE_RESET].battles = _saturday(day_27) + attacks(
            day_28, *[40] * 5) + defenses(day_28, *[30] * 8)
        story.players[END_LOSS].battles = _saturday(day_27) + attacks(
            day_28, 40) + defenses(day_28, 30, 30)
        # Day 1: an attack of 40 and 8 defenses of 20 end at 4,880, but for
        # LATE_RESET and DISAGREES, whose first battles wait for their late
        # Season resets (4,960 with defenses of 10), NO_DEFENSE (2 attacks,
        # 5,080) and DAY_1_LOSS (3 attacks and a defense of 30: 5,090 before
        # its Day 1 loss of 2 * 30, 5,030 after).
        for tag in (OFFICIAL, SHIELDED, END_LOSS):
            story.players[tag].battles += attacks(day_1, 40) + defenses(day_1, *[20] * 8)
        for tag in (LATE_RESET, DISAGREES):
            story.players[tag].battles += attacks(day_1 + timedelta(hours=2), 40) + defenses(
                day_1 + timedelta(hours=3), *[10] * 8)
        story.players[NO_DEFENSE].battles += attacks(day_1, 40, 40)
        story.players[DAY_1_LOSS].battles += attacks(day_1, 40, 40, 40) + defenses(day_1, 30)

        story.reset(day_27, {tag: (5400 if tag == SHIELDED else 5600, old)
                             for tag in story.players})
        story.reset(day_28, {tag: (5400 if tag == SHIELDED else 5440, old)
                             for tag in story.players})
        ends = {tag: 5320 for tag in story.players}
        ends.update({LATE_RESET: 5400, SHIELDED: 5400, END_LOSS: 5420})
        story.reads(day_1 - timedelta(minutes=10), ends, old)
        # At the Season's end DROPPED, ranked too low, drops to Legend II.
        # LATE_RESET's and DISAGREES's Season resets land after 05:20; until
        # then DISAGREES reads the game's total, 30 below the calculated end,
        # as 16 players did on 5 October 2026.
        story.players[DROPPED].dropped = True
        opening = {tag: (5000, new) for tag in story.players}
        opening.update({LATE_RESET: (5400, new), DISAGREES: (5290, new),
                        DROPPED: (5320, None)})
        story.reset(day_1, opening)
        story.reads(day_1 + timedelta(minutes=20), {
            tag: trophies for tag, (trophies, _) in opening.items()})
        for tag in (LATE_RESET, DISAGREES):
            story.read(tag, day_1 + timedelta(hours=1), 5000)
        story.run_worker()
        # The official final totals arrive at 11:00 and include each
        # automatic loss; DISAGREES's is 30 below the calculated end.
        finals = {**ends, END_LOSS: 5240, DISAGREES: 5290}
        story.history(day_1 + timedelta(hours=6), day_1, finals)
        story.run_worker()
        season_end = {tag: story.saved_day(tag, day_28) for tag in story.players}
        story.wait(timedelta(hours=1))
        season_end_members = story.board_members(day_1)
        season_end_board = story.served_board(day_1)

        playing = [tag for tag in story.players if tag != DROPPED]
        day_1_ends = {tag: 4880 for tag in playing}
        day_1_ends.update({LATE_RESET: 4960, DISAGREES: 4960, NO_DEFENSE: 5080,
                           DAY_1_LOSS: 5090})
        story.reads(day_2 - timedelta(minutes=10), day_1_ends)
        story.reset(day_2, {tag: (trophies, new) for tag, trophies in day_1_ends.items()})
        story.reads(day_2 + timedelta(minutes=20), {**day_1_ends, DAY_1_LOSS: 5030})
        story.run_worker()
        first_day = {tag: story.saved_day(tag, day_1) for tag in playing}
        unfinished = story.unfinished_jobs()
        story.wait(timedelta(hours=1))
        first_day_members = story.board_members(day_2)
        first_day_board = story.served_board(day_2)
    assert unfinished == []
    # Once the official total arrives it is Day 28's end; how the Season
    # reset is then recorded is pinned by test_reset_settlement_state_postgres.
    ended = ("state", "start", "end", "automatic_loss", "attacks", "defenses")
    last_days = {tag: {key: saved[key] for key in ended} for tag, saved in season_end.items()}
    played = dict(zip(ended, ("Complete", 5440, 5320, None, 3, 8), strict=True))
    assert last_days == {
        OFFICIAL: played, NO_DEFENSE: played, DAY_1_LOSS: played, DROPPED: played,
        LATE_RESET: {**played, "end": 5400, "attacks": 5},
        SHIELDED: {**played, "start": 5400, "end": 5400, "attacks": 0, "defenses": 0},
        END_LOSS: {**played, "end": 5240, "automatic_loss": 180, "attacks": 1, "defenses": 2},
        # The official total is 30 below every battle and reading.
        DISAGREES: {**played, "state": "Inconsistent"},
    }
    assert season_end_members == {
        tag: "inconsistent" if tag == DISAGREES else "complete" for tag in story.players}
    assert _ranked(season_end_board) == {
        END_LOSS: 5420, LATE_RESET: 5400, SHIELDED: 5400, OFFICIAL: 5320,
        NO_DEFENSE: 5320, DAY_1_LOSS: 5320, DISAGREES: 5320, DROPPED: 5320,
    }
    # Day 1 starts at 5,000 by the Season rule, whatever its Reset read.
    day_1_played = {
        "state": "Complete", "start": 5000, "end": 4880, "next_start": 4880,
        "automatic_loss": None, "raise": 0, "attacks": 1, "defenses": 8, "residual": 0,
    }
    late = {**day_1_played, "end": 4960, "next_start": 4960}
    assert first_day == {
        OFFICIAL: day_1_played, SHIELDED: day_1_played, END_LOSS: day_1_played,
        # A reading before the Season reset fits the official total.
        LATE_RESET: late, DISAGREES: late,
        # Day 1 never charges a day with no defense.
        NO_DEFENSE: {**day_1_played, "end": 5080, "next_start": 5080,
                     "attacks": 2, "defenses": 0},
        # Day 1 charges attacks less defenses missing defenses, by its own
        # average; the Reset reading came before it.
        DAY_1_LOSS: {**day_1_played, "end": 5030, "next_start": 5030,
                     "automatic_loss": 60, "attacks": 3, "defenses": 1},
    }
    assert first_day_members == {tag: "complete" for tag in playing}
    assert _ranked(first_day_board) == {
        DAY_1_LOSS: 5090, NO_DEFENSE: 5080, LATE_RESET: 4960, DISAGREES: 4960,
        OFFICIAL: 4880, SHIELDED: 4880, END_LOSS: 4880,
    }


def _even(start: datetime) -> list[Battle]:
    """6 attacks of 40 and 8 defenses of 30: the day ends where it began."""
    return attacks(start, *[40] * 6) + defenses(start, *[30] * 8)


(SHIELD_RUN, GAP_BEFORE, FULL_WITH_GAP, NO_START, NINTH, CHAIN_BROKEN,
 CONTRADICTED, LAGGED, CROWDED, UNSETTLED_LOSS, ZERO_CHARGED, NO_OPPONENT) = (
    "#2PP", "#8PP", "#9PP", "#PPP", "#YPP", "#LPP", "#QPP", "#CPP", "#GPP", "#RPP", "#JPP",
    "#UPP")


def test_wednesday_reset_with_days_that_once_could_not_save(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The Reset of Wednesday 30 September 2026 ends Tuesday, Day 23. Each
    player's Tuesday is a case the reliability reports of 12 October 2026
    found could reject a day or leave it waiting: every one must save, and
    every player must rank on the Wednesday board."""
    season = PREVIOUS_SEASON
    day_20, day_21, day_22, tuesday, wednesday = (
        day(n, season) for n in (20, 21, 22, 23, 24))
    tracked = [SHIELD_RUN, GAP_BEFORE, FULL_WITH_GAP, NINTH, CHAIN_BROKEN, CONTRADICTED,
               LAGGED, CROWDED, UNSETTLED_LOSS, ZERO_CHARGED, NO_OPPONENT]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *tracked, NO_START)
        # From 5,500 Day 20 ends at 5,340, and Days 21 and 22 end where they
        # began; SHIELD_RUN plays nothing after Day 20.
        for tag in tracked:
            story.players[tag].battles = _saturday(day_20) + (
                [] if tag == SHIELD_RUN else _even(day_21) + _even(day_22))
        # The collector missed these players' Day 22 afternoons, and their 42
        # multiplayer battles pushed the morning out of the 50-row log.
        for tag in (GAP_BEFORE, CHAIN_BROKEN):
            story.players[tag].battles += multiplayer(day_22 + timedelta(hours=9), 42)
        tuesday_battles = {
            # SHIELD_RUN plays nothing for 3 days: its third shielded day,
            # which the database refused until 9 October 2026.
            SHIELD_RUN: [],
            # 1 attack and 2 defenses, but the automatic loss needs Day 22's
            # defenses, which the gap hides: 5,320 before it.
            GAP_BEFORE: attacks(tuesday, 40) + defenses(tuesday, 30, 30),
            # 8 attacks and 8 defenses after 34 multiplayer battles: the log
            # does not reach the day's start, but holds all 16: 5,420.
            FULL_WITH_GAP: multiplayer(tuesday + timedelta(minutes=5), 34)
            + attacks(tuesday + timedelta(hours=2), *[40] * 8)
            + defenses(tuesday + timedelta(hours=2), *[30] * 8),
            # First tracked at Tuesday's Reset, whose profile never answered:
            # 16 battles and no start.
            NO_START: attacks(tuesday, *[40] * 8) + defenses(tuesday, *[30] * 8),
            # 9 attacks in the log, one more than the game allows: 5,460.
            NINTH: attacks(tuesday, *[40] * 9) + defenses(tuesday, *[30] * 8),
            # Day 22 has a gap and Tuesday's Reset read Season 0, so Tuesday
            # has no start: 2 attacks and 8 defenses, 160 down.
            CHAIN_BROKEN: attacks(tuesday, 40, 40) + defenses(tuesday, *[30] * 8),
            # The same battles, but a 17:00 reading is 10 below what they
            # add up to by then: 5,180.
            CONTRADICTED: attacks(tuesday, 40, 40) + defenses(tuesday, *[30] * 8),
            # The same battles, read a minute after the second attack, before
            # the game credited it: a delay, not a contradiction.
            LAGGED: attacks(tuesday, 40, 40) + defenses(tuesday, *[30] * 8),
            # 4 attacks and 3 defenses in the 10 minutes before a 23:00
            # reading, too many in flight to judge it: 5,260.
            CROWDED: defenses(tuesday, *[30] * 5) + [
                Battle(tuesday + timedelta(hours=17, minutes=51 + n), 30, False) if n % 2
                else Battle(tuesday + timedelta(hours=17, minutes=51 + n), 40, True)
                for n in range(7)],
            # 1 attack and 2 defenses: 5,320 before a loss of 6 * 30.
            UNSETTLED_LOSS: attacks(tuesday, 40) + defenses(tuesday, 30, 30),
            # 2 attacks and no defense: 5,420 before a charge of 8 * 30.
            ZERO_CHARGED: attacks(tuesday, 40, 40),
            # 1 attack, 6 defenses and a "no opponent, no battle" defense
            # row: 7 used slots, so 1 missing, at floor(420 / 15) = 28 with
            # Day 22's 8. 5,200 before it.
            NO_OPPONENT: attacks(tuesday, 40) + defenses(tuesday, *[30] * 6)
            + [Battle(tuesday + timedelta(hours=10), 0, False, opponent=False)],
        }
        for tag, battles in tuesday_battles.items():
            story.players[tag].battles += battles
        story.reset(day_20, {tag: (5500, season) for tag in tracked})
        story.reset(day_21, {tag: (5340, season) for tag in tracked})
        story.reset(day_22, {tag: (5340, season) for tag in tracked})
        story.reset(tuesday, {
            **{tag: (5340, season) for tag in tracked},
            CHAIN_BROKEN: (5340, None), NO_START: (None, season),
        })
        story.read(CONTRADICTED, tuesday + timedelta(hours=12), 5170, season)
        story.read(CONTRADICTED, tuesday + timedelta(hours=12, minutes=1))
        story.read(LAGGED, tuesday + timedelta(hours=2, minutes=1), 5320, season)
        story.read(LAGGED, tuesday + timedelta(hours=2, minutes=2))
        story.read(CROWDED, tuesday + timedelta(hours=18), 5260, season)
        story.read(CROWDED, tuesday + timedelta(hours=18, minutes=1))
        ends = {
            SHIELD_RUN: 5340, GAP_BEFORE: 5320, FULL_WITH_GAP: 5420, NO_START: 5420,
            NINTH: 5460, CHAIN_BROKEN: 5180, CONTRADICTED: 5180, LAGGED: 5180, CROWDED: 5260,
            UNSETTLED_LOSS: 5320, ZERO_CHARGED: 5420, NO_OPPONENT: 5200,
        }
        story.reads(wednesday - timedelta(minutes=10), ends, season)
        # The Reset reads come before the automatic losses; the 05:20
        # settlement check after them.
        story.reset(wednesday, {tag: (trophies, season) for tag, trophies in ends.items()})
        story.reads(wednesday + timedelta(minutes=20), {
            **ends, GAP_BEFORE: 5140, UNSETTLED_LOSS: 5140, ZERO_CHARGED: 5180,
            NO_OPPONENT: 5172}, season)
        story.run_worker()
        unfinished = story.unfinished_jobs()
        days = {tag: story.saved_day(tag, tuesday) for tag in story.players}
        with psycopg.connect(connection_info) as connection:
            shielded = connection.execute(
                "SELECT shield_duration_days FROM ranked_day_versions AS day"
                " JOIN players AS player ON player.id = day.player_id"
                " WHERE player.normalized_tag = %s AND day.ranked_day_start = %s"
                " ORDER BY day.version DESC LIMIT 1", (SHIELD_RUN, tuesday),
            ).fetchone()
        story.wait(timedelta(hours=1))
        members = story.board_members(wednesday)
        board = story.served_board(wednesday)
    assert unfinished == []
    played = {"state": "Complete", "start": 5340, "automatic_loss": None, "raise": 0,
              "residual": 0}
    assert days[SHIELD_RUN] == {**played, "end": 5340, "next_start": 5340,
                                "attacks": 0, "defenses": 0}
    assert shielded == (3,)
    # Without Day 22's defenses the loss cannot be worked out, so the day's
    # end is unknown and Wednesday starts from the Reset reading.
    assert days[GAP_BEFORE] == {
        **played, "state": "Partial", "end": None, "next_start": 5320, "attacks": 1,
        "defenses": 2, "residual": None,
    }
    assert days[FULL_WITH_GAP] == {**played, "end": 5420, "next_start": 5420,
                                   "attacks": 8, "defenses": 8}
    unproven = {**played, "state": "Partial", "start": None, "end": None,
                "residual": None}
    assert days[NO_START] == {**unproven, "next_start": 5420, "attacks": 8, "defenses": 8}
    assert days[CHAIN_BROKEN] == {**unproven, "next_start": 5180, "attacks": 2,
                                  "defenses": 8}
    # The ninth attack is kept, and marks the day Partial.
    assert days[NINTH] == {**played, "state": "Partial", "end": 5460,
                           "next_start": 5460, "attacks": 9, "defenses": 8}
    assert days[CONTRADICTED] == {**played, "state": "Inconsistent", "end": 5180,
                                  "next_start": 5180, "attacks": 2, "defenses": 8}
    assert days[LAGGED] == {**played, "end": 5180, "next_start": 5180, "attacks": 2,
                            "defenses": 8}
    assert days[CROWDED] == {**played, "end": 5260, "next_start": 5260, "attacks": 4,
                             "defenses": 8}
    assert days[UNSETTLED_LOSS] == {**played, "end": 5140, "next_start": 5140,
                                    "automatic_loss": 180, "attacks": 1, "defenses": 2}
    assert days[NO_OPPONENT] == {**played, "end": 5172, "next_start": 5172,
                                 "automatic_loss": 28, "attacks": 1, "defenses": 6}
    assert days[ZERO_CHARGED] == {**played, "end": 5180, "next_start": 5180,
                                  "automatic_loss": 240, "attacks": 2, "defenses": 0}
    assert members == {
        SHIELD_RUN: "complete", GAP_BEFORE: "partial", FULL_WITH_GAP: "complete",
        NO_START: "partial", NINTH: "partial", CHAIN_BROKEN: "partial",
        CONTRADICTED: "inconsistent", LAGGED: "complete", CROWDED: "complete",
        UNSETTLED_LOSS: "complete",
        ZERO_CHARGED: "complete", NO_OPPONENT: "complete",
    }
    # Each ranks by their total at 05:00:00, before any automatic loss.
    assert _ranked(board) == {
        NINTH: 5460, FULL_WITH_GAP: 5420, NO_START: 5420, ZERO_CHARGED: 5420,
        SHIELD_RUN: 5340, GAP_BEFORE: 5320, UNSETTLED_LOSS: 5320, CROWDED: 5260,
        NO_OPPONENT: 5200,
        CHAIN_BROKEN: 5180, CONTRADICTED: 5180, LAGGED: 5180,
    }


# The 12 October 2026 Reset swept 13,391 players among 1.2 million saved
# battles. A story's worker takes about 0.2 s a player a Reset, so here the
# other players and their battles are saved directly, and SWEPT players go
# through the worker.
POPULATION, BATTLES_A_DAY, SWEPT = 10_000, 20_000, 200


def _seed_population(connection_info: str) -> None:
    """POPULATION other players, and BATTLES_A_DAY battles among them on each
    of the Season's first 7 days, each with its attacker's report and army,
    as production saves them; then fresh table statistics, as production
    keeps. Battles without armies made the database read the whole battles
    table once for each battle of the board's army build."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        connection.execute("SET session_replication_role = replica")
        connection.execute(
            "INSERT INTO players (id, normalized_tag) OVERRIDING SYSTEM VALUE"
            " SELECT 1000000 + i, '#OTHER' || i FROM generate_series(1, %s) AS i",
            (POPULATION,),
        )
        connection.execute(
            """
            WITH battle AS (
                INSERT INTO legend_battles
                    (ranked_day_start, attacker_player_id, defender_player_id)
                SELECT %(season)s + day * interval '24 hours',
                       1000001 + i %% %(players)s,
                       1000001 + (i %% %(players)s + 1 + i / %(players)s * 37 + day)
                           %% %(players)s
                FROM generate_series(0, 6) AS day,
                     generate_series(0, %(battles)s - 1) AS i
                RETURNING id, ranked_day_start, attacker_player_id
            ), evidence AS (
                INSERT INTO battle_evidence (
                    battle_id, source_row_id, observation_id, reporting_player_id,
                    perspective, battle_timestamp, stars, destruction_percentage,
                    army_share_code, attacker_gain, defender_loss,
                    trophy_rule_version, source_observed_at, parser_version)
                SELECT id, -id, -id, attacker_player_id, 'attacker',
                       ranked_day_start + interval '1 hour', 3, 100, 'u1x0-2x1', 40, 40,
                       'test', ranked_day_start + interval '1 hour', 'test'
                FROM battle
                RETURNING id, battle_id, source_observed_at
            )
            INSERT INTO battle_perspectives
                (battle_id, perspective, evidence_id, source_observed_at)
            SELECT battle_id, 'attacker', id, source_observed_at FROM evidence
            """,
            {"season": SEASON, "players": POPULATION, "battles": BATTLES_A_DAY},
        )
        connection.execute("ANALYZE")


def _battle_reads(connection_info: str) -> tuple[int, int]:
    """Rows of the battles table read by full scans so far, and its size,
    once every closed connection has reported its reads."""
    counts = None
    for _ in range(50):
        with psycopg.connect(connection_info) as connection:
            latest = connection.execute(
                "SELECT seq_tup_read, n_live_tup FROM pg_stat_user_tables"
                " WHERE relname = 'legend_battles'"
            ).fetchone()
        if latest == counts:
            return latest
        counts = latest
        time.sleep(0.2)
    return counts


def test_reset_at_full_size(database_url: str, archive_server, monkeypatch) -> None:
    """The 12 October 2026 Reset among a Season's saved battles. Its
    never-played check read every saved battle once a call, 500 ms each, and
    Legend day results fell 88,000 jobs behind. A Reset's work for each
    player must not grow with the battles saved: no query may read the whole
    battles table for a player, and each player's work keeps to a time
    bound. Half the players never played and sit at 5,000."""
    sunday, monday = day(7), day(8)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _seed_population(connection_info)
        # Tags apart from the battle logs' opponents.
        tags = [f"#P{_tag(n)[1:]}" for n in range(1, SWEPT + 1)]
        played, unplayed = tags[: SWEPT // 2], tags[SWEPT // 2:]
        story = _story(connection_info, archive_server, monkeypatch, *tags)
        for tag in played:
            story.players[tag].battles = _even(sunday) + attacks(
                sunday + timedelta(hours=6), 40, 40)
        story.reset(sunday, {**{tag: (5300, SEASON) for tag in played},
                             **{tag: (5000, None) for tag in unplayed}})
        story.reset(monday, {**{tag: (5380, SEASON) for tag in played},
                             **{tag: (5000, None) for tag in unplayed}}, run=False)
        # The Sunday Reset's rows are new since the last statistics.
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute("ANALYZE")
        read_before, _ = _battle_reads(connection_info)
        started = time.monotonic()
        story.run_worker()
        seconds = time.monotonic() - started
        read_after, battles = _battle_reads(connection_info)
        unfinished = story.unfinished_jobs()
        members = story.board_members(monday)
        board = story.served_board()
    assert unfinished == []
    assert battles >= 7 * BATTLES_A_DAY
    # A board build may read the whole table once or twice; the
    # never-played check of PR 451 read it at least once a player.
    full_reads = (read_after - read_before) / battles
    assert full_reads < SWEPT / 10, full_reads
    # About 0.25 s a player on the owner's server; 1 s allows a slower one.
    assert seconds < SWEPT * 1.0, seconds
    assert sorted(members) == sorted(tags)
    assert {members[tag] for tag in played} == {"complete"}
    assert board is not None and board["total_entries"] == SWEPT
    assert {(entry["tag"] in played, entry["trophies"]) for entry in board["entries"]} == {
        (True, 5380)}
