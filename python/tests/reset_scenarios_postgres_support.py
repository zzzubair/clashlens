"""Shared setup for reset scenarios postgres tests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import as_api_role, store_observation, text
from reconciliation_postgres_support import BATTLE_FIXTURE, PROFILE_FIXTURE, _processor

from clashlens import (
    api_analytics,
    api_dashboard,
    api_leaderboard,
    army_rank_bands,
    army_types,
    board_deadline,
    boundary_publication,
    league_decider,
    league_week,
    past_reset_pacing,
)
from clashlens.api_db import ApiDatabase
from clashlens.army_analytics import ArmyAnalyticsSelection
from clashlens.db import Database
from clashlens.domain import _ALLOCATION_THRESHOLDS
from clashlens.job_contracts import SUPPORTED_WORK_TYPES
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


# A Thrower army's share code; other battles carry the fixture's Other army.
THROWER = "u9x132-4x7"


@dataclass(frozen=True)
class Battle:
    """One battle on the player's own log: its report time, the trophies it
    moved, positive for an attack's gain, negative for a defense's loss, and
    whether it was an attack. A multiplayer battle is no Legend battle, but
    it takes a row of the log's 50. A Legend row with no opponent is the
    game's "no opponent, no battle" row: a used slot that is no battle.
    ``army`` is the attacker's army share code, None for the fixture's and
    empty when the report shows none."""

    at: datetime
    trophies: int
    attack: bool
    legend: bool = True
    opponent: bool = True
    army: str | None = None
    # The opponent's name as the game shows it, which they can change.
    opponent_name: str = "Opponent"


def attacks(start: datetime, *amounts: int, army: str | None = None) -> list[Battle]:
    """Attacks an hour apart from an hour after ``start``, with ``army``."""
    return [Battle(start + timedelta(hours=n + 1), amount, True, army=army)
            for n, amount in enumerate(amounts)]


def defenses(start: datetime, *amounts: int, army: str | None = None) -> list[Battle]:
    """Defenses an hour apart from 30 minutes after ``start``, by ``army``."""
    return [Battle(start + timedelta(hours=n, minutes=30), -amount, False, army=army)
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
    # Whether the profile names the tier as the game has since 16 November
    # 2026: "Legend League I" for "Legend I".
    renamed: bool = False

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
        if self.renamed:
            tier = payload["leagueTier"]
            tier["name"] = tier["name"].replace("Legend ", "Legend League ")
        return json.dumps(payload).encode()

    def log(self, at: datetime) -> bytes:
        """The battle log as the game shows it at ``at``: the newest 50
        battles reported before then, newest first, each against the same
        opponent in every log that shows it."""
        template = json.loads(BATTLE_FIXTURE.read_bytes())["items"][0]
        shown = sorted((b for b in self.battles if b.at < at), key=lambda b: b.at)
        items = []
        for index, battle in list(enumerate(shown))[-50:]:
            stars, destruction = RESULTS[abs(battle.trophies)]
            items.append({
                **template, "attack": battle.attack, "battleTime": 150,
                "battleType": "legend" if battle.legend else "homeVillage",
                "battleTimestamp": battle.at.strftime("%Y%m%dT%H%M%S.000Z"),
                "stars": stars, "destructionPercentage": destruction,
                "opponentPlayerTag": f"#{_tag(index + 1)}",
                "opponentName": battle.opponent_name,
            })
            if not battle.opponent:
                items[-1].update(opponentPlayerTag=None, battleTime=0)
            if battle.army:
                items[-1]["armyShareCode"] = battle.army
            elif battle.army == "":
                del items[-1]["armyShareCode"]
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
    # Whether the worker decides the weekly league record as it runs, as
    # production's does; a story about the decisions makes them itself.
    decides: bool = True
    # The record's clock: the latest moment the story has read.
    clock: datetime = PREVIOUS_SEASON
    wall_clock: object | None = None

    def __post_init__(self) -> None:
        # The story's clock: the database's, plus the time the story waited.
        # Builds for older Resets wait out 04:30 to 07:00 UTC; a story runs
        # as if outside that window, so CI gets one result at any hour.
        clock = past_reset_pacing._now
        self.monkeypatch.setattr(
            past_reset_pacing, "_now", lambda connection: clock(connection) + self.elapsed)
        self.monkeypatch.setattr(past_reset_pacing, "_in_quiet_window", lambda _now: False)
        # Likewise operator corrections, which wait out 04:00 to 07:00 UTC.
        self.monkeypatch.setattr(past_reset_pacing, "OPERATOR_QUIET_START",
                                 past_reset_pacing.PAST_RESET_QUIET_END)
        # A story runs weeks after its Resets, so each board's 05:30 deadline
        # would pass before its first day saved. Every day saves within
        # minutes of the Reset here, as production's do: each board waits
        # for all of its members.
        self.monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
        # The weekly league record decides every story's Seasons, as it
        # decides each from 5 October 2026, on the story's clock.
        self.monkeypatch.setattr(league_week, "RECORDED_FROM", PREVIOUS_SEASON - timedelta(days=28))
        self.monkeypatch.setattr(league_decider, "_now", lambda: self.clock)
        self.monkeypatch.setattr(league_decider, "_retry_at", float("-inf"))

    def _at(self, at: datetime) -> None:
        self.clock = max(self.clock, at)
        if self.wall_clock is not None:
            self.wall_clock.set(self.clock)

    def wait(self, time: timedelta) -> None:
        """Let ``time`` pass, then run the worker: the newest Reset's board
        rebuilds with corrections at most once an hour."""
        self.elapsed += time
        self.clock += time
        if self.wall_clock is not None:
            self.wall_clock.set(self.clock)
        self.run_worker()

    def reset(self, boundary: datetime,
              readings: dict[str, tuple[int | None, datetime | None]],
              *, run: bool = True, no_log: tuple[str, ...] = ()) -> None:
        """The Reset sweep at ``boundary``: its members are the players in
        ``readings``, and it reads each one's profile, showing (trophies,
        Season or None for Season 0), and battle log 4 minutes after it. A
        profile of None trophies, or the battle log of a player in
        ``no_log``, was never answered: the collector gave it up. Then,
        unless ``run`` is false, the worker runs until idle."""
        at = boundary + READ_AFTER
        self._at(at)
        observations = {}
        with psycopg.connect(self.connection_info) as connection:
            for tag, (trophies, season) in readings.items():
                player = self.players[tag]
                for endpoint, body in (
                    ("profile", None if trophies is None else player.profile(trophies, season)),
                    ("battle_log", None if tag in no_log else player.log(at)),
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
                profile, log = observations[tag, "profile"], observations[tag, "battle_log"]
                connection.execute(
                    """
                    INSERT INTO collector_work (
                        kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
                        coalescing_key, status, profile_status, battle_log_status,
                        profile_observation_id, battle_log_observation_id, completed_at
                    ) VALUES ('reset_baseline', 'reset', 'player', %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (player_id, tag, sweep, boundary, f"reset-{tag}-{boundary}",
                     "complete" if profile and log else "failed",
                     "observed" if profile else "failed", "observed" if log else "failed",
                     profile, log, at if profile and log else None),
                )
        if run:
            self.run_worker()

    def read(self, tag: str, at: datetime, trophies: int | None = None,
             season: datetime | None = SEASON) -> int:
        """A routine read outside a sweep: the profile when ``trophies`` is
        given, else the battle log. Returns its job's ID."""
        player = self.players[tag]
        self._at(at)
        endpoint, body = (
            ("battle_log", player.log(at)) if trophies is None
            else ("profile", player.profile(trophies, season))
        )
        return store_observation(
            self.connection_info, self.archive_server,
            occurrence_key=f"{tag}-{endpoint}-{at.isoformat()}", endpoint=endpoint,
            body=body, observed_at=at, normalized_tag=tag,
        )[1]

    def reads(self, at: datetime, totals: dict[str, int], season: datetime | None = SEASON) -> None:
        """The collector's regular check of each player at ``at``: their
        profile showing ``totals``, then their battle log a minute later. The
        last checks before a Reset run until 04:55; the settlement check is
        at 05:20."""
        for tag, trophies in totals.items():
            self.read(tag, at, trophies, season)
            self.read(tag, at + timedelta(minutes=1))

    def history(self, at: datetime, season_end: datetime, finals: dict[str, int],
                placements: dict[str, int] | None = None, tier_id: int = 105000036) -> None:
        """Each player's league history read at ``at``: the game's official
        final total for the Season ending at ``season_end``, and their final
        rank from ``placements``, else 10,568, in Legend I or ``tier_id``,
        whose row names the Monday Reset starting its week."""
        self._at(at)
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
                    "leagueTrophies": final, "leagueTierId": tier_id,
                    "placement": (placements or {}).get(tag, 10568), "attackWins": 1, "attackLosses": 0,
                    "attackStars": 3, "defenseWins": 0, "defenseLosses": 8,
                    "defenseStars": 16, "maxBattles": 8,
                }]}).encode(),
            )

    def run_worker(self) -> None:
        """Run the worker as production does: the weekly league record's
        decisions and the publication check, then every job due in its claim
        order, until nothing is left to do. A job sent to wait (a retry or a
        dependency) is made due again, as time passing would, for at most 20
        rounds."""
        database, processor = _processor(self.connection_info, self.archive_server)
        try:
            for _ in range(20):
                if self.decides:
                    league_decider.decide_due(database)
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
                    undecided = self.decides and connection.execute(
                        "SELECT count(*) FROM league_week_dirty").fetchone()[0]
                if not processed and not waiting and not undecided:
                    return
        finally:
            database.close()

    def run_round(self, *, skip: tuple[str, ...] = ()) -> None:
        """Run the worker without letting time pass: the publication check
        and every job due now, except those of a work type in ``skip``, until
        nothing is left. A job sent to wait stays waiting."""
        kinds = tuple(kind for kind in SUPPORTED_WORK_TYPES if kind not in skip)
        database, processor = _processor(self.connection_info, self.archive_server)
        try:
            for _ in range(20):
                if self.decides:
                    league_decider.decide_due(database)
                boundary_publication.reevaluate_boundary_publications(database)
                processed = 0
                while processor.process_once(owner="story", work_types=kinds) is not None:
                    processed += 1
                boundary_publication.reevaluate_boundary_publications(database)
                if not processed:
                    return
        finally:
            database.close()

    def run_jobs(self, *job_ids: int) -> list[tuple[str, str | None]]:
        """Process exactly these jobs, in order: each one's outcome and its
        reason, such as ("retrying", "database_lock_busy")."""
        database, processor = _processor(self.connection_info, self.archive_server)
        try:
            return [
                (result.outcome, result.category) if result else None
                for result in (processor.process_job(job_id, owner=f"story-{job_id}")
                               for job_id in job_ids)
            ]
        finally:
            database.close()

    def jobs(self, work_type: str) -> list[int]:
        """The IDs of the pending jobs of ``work_type``, oldest first."""
        with psycopg.connect(self.connection_info) as connection:
            return [row[0] for row in connection.execute(
                "SELECT id FROM python_processing_jobs WHERE work_type = %s"
                " AND status = 'pending' ORDER BY id", (work_type,)).fetchall()]

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

    def served_board(self, boundary: datetime | None = None,
                     offset: int = 0) -> dict[str, object] | None:
        """The page of 100 from ``offset`` of the Daily board the API serves
        now, the newest or the one for the Legend day ending at ``boundary``,
        read as the data service."""
        selector = {}
        if boundary is not None:
            ended = boundary - timedelta(days=1)
            season = SEASON if ended >= SEASON else PREVIOUS_SEASON
            selector = {"official_season_id": str(int(season.timestamp())),
                        "season_day_number": (ended - season).days + 1}
        api = ApiDatabase(as_api_role(self.connection_info))
        try:
            return api_leaderboard.get_frozen_leaderboard(
                api, limit=100, offset=offset, **selector)
        finally:
            api.close()

    def count_armies(self, at: datetime) -> None:
        """The worker's army counts at ``at``, as its maintenance timer runs
        them: each run counts one finished day's rank band totals, then labels
        the saved armies or counts one finished day's attacks or defenses, so
        10 runs count every day of a story. The worker counts at most one
        day's defenses a minute; a story does not wait."""
        self.monkeypatch.setattr(army_types, "DEFENSE_PACE_SECONDS", 0)
        self.monkeypatch.setattr(army_types, "_defense_at", float("-inf"))
        database = Database(self.connection_info)
        try:
            for _ in range(10):
                army_rank_bands.refresh_rank_band_totals(database)
                army_types.refresh_army_types(database, now=at)
        finally:
            database.close()

    def served_armies(self, tag: str, at: datetime) -> dict[str, object] | None:
        """The player's Armies on defense card the API serves at ``at``, for
        the Legend day before, read as the data service."""
        api = ApiDatabase(as_api_role(self.connection_info))
        try:
            return api_dashboard.get_player_armies(
                api, api_dashboard.ArmyDays(), tag, lens="defense", days=1, now=at)
        finally:
            api.close()

    def served_army_types(self, at: datetime) -> dict[str, object] | None:
        """The Armies view the API serves at ``at`` for the current Season's
        top 10, sorted by Avg. trophies, read as the data service."""
        api = ApiDatabase(as_api_role(self.connection_info))
        api_reader = api_analytics.ArmyAnalyticsReader(api.pool)
        try:
            result = api_analytics.get_army_analytics(api_reader, ArmyAnalyticsSelection.parse(
                lens="offense", season="current", start_day=1, end_day=28,
                population="top-10", category="army-types", sort="average-trophies",
            ), now=at)
            assert result["season_bounds"]["day_count"] == 28
            assert result["season_bounds"]["id"] == result["selection"]["season"]
            return result
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


def _results(connection_info: str, start: datetime, tag: str) -> int:
    """How many results the player's day from ``start`` has saved."""
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT count(*) FROM ranked_day_versions AS day
            JOIN players AS player ON player.id = day.player_id
            WHERE day.ranked_day_start = %s AND player.normalized_tag = %s
            """,
            (start, tag),
        ).fetchone()[0]


def _story(connection_info: str, archive_server, monkeypatch, *tags: str,
           wall_clock: object | None = None) -> Story:
    return Story(connection_info, archive_server, monkeypatch,
                 {tag: Player(tag) for tag in tags}, wall_clock=wall_clock)


def _saturday(start: datetime) -> list[Battle]:
    """A full Saturday: 2 attacks of 40 and 8 defenses of 30, so Sunday's
    automatic loss averages 30 a missing defense."""
    return attacks(start + timedelta(hours=10), 40, 40) + defenses(start, *[30] * 8)


def _even(start: datetime) -> list[Battle]:
    """6 attacks of 40 and 8 defenses of 30: the day ends where it began."""
    return attacks(start, *[40] * 6) + defenses(start, *[30] * 8)


def board_transcript(connection_info: str, boundary: datetime) -> dict[str, list]:
    """Everything a Reset's Daily board has done so far, in order, with no
    database IDs or times: a board version is named by its number, a frozen
    input list by its kind, version and digest, and a digest by a label in
    order of first sight (m1, m2, ... for frozen lists; h1, h2, ... for the
    other hashes job keys carry), so the same inputs read the same label.

    - versions: (number, board state, army state, rebuild state, artifacts
      rebuilt, number replaced, frozen board list, frozen army list, version
      whose statistics identity it names, version whose army identity it
      names)
    - members: (number, status, board status, army status, how many)
    - rebuilds: (state, artifacts, markers, from version, to version, days)
    - frozen: (frozen list, player, day result number, its class, and for
      a board list the profile reading it ranks by [trophies, minutes from
      the Reset, why it may rank], for an army list the army share codes of
      the battles it read)
    - identities: (kind, version, frozen list)
    - events: (version, version it replaced)
    - jobs: (build job's key, its status)"""
    labels: dict[str, str] = {}

    def label(value: object, prefix: str = "h") -> str | None:
        if value is None:
            return None
        named = [name for name in labels.values() if name[0] == prefix]
        return labels.setdefault(text(value), f"{prefix}{len(named) + 1}")

    with psycopg.connect(connection_info) as connection:
        manifests = {
            manifest_id: (f"{text(kind)}@{number}", label(digest, "m"))
            for manifest_id, kind, number, digest in connection.execute(
                """
                SELECT manifest.id, manifest.artifact_kind, generation.generation, manifest.digest
                FROM boundary_publication_manifests AS manifest
                JOIN boundary_publication_generations AS generation
                  ON generation.id = manifest.generation_id
                WHERE generation.boundary_at = %s ORDER BY manifest.id
                """,
                (boundary,),
            ).fetchall()
        }

        def frozen(manifest_id: int | None) -> str | None:
            return None if manifest_id is None else "{}:{}".format(*manifests[manifest_id])

        frozen_rows = [
            (frozen(manifest_id), text(tag), number, text(classification), detail)
            for manifest_id, tag, number, classification, detail in connection.execute(
                """
                SELECT entry.manifest_id, player.normalized_tag, day.version,
                       entry.classification,
                       CASE WHEN manifest.artifact_kind = 'army' THEN to_jsonb(ARRAY(
                           SELECT coalesce(decode.raw_code, decode.status)
                           FROM battle_army_decodes AS decode
                           WHERE decode.id IN (
                               SELECT jsonb_array_elements_text(
                                   entry.input_identity -> 'decode_ids')::bigint)
                           ORDER BY 1))
                       ELSE jsonb_build_array(
                           entry.input_identity #> '{profile_snapshot,trophies}',
                           (extract(epoch FROM (entry.input_identity
                               #>> '{profile_snapshot,observed_at}')::timestamptz
                               - %(boundary)s) / 60)::int,
                           entry.input_identity ->> 'snapshot_quality') END
                FROM boundary_publication_manifests AS manifest
                CROSS JOIN LATERAL boundary_publication_manifest_entries(manifest.id) AS entry
                JOIN players AS player ON player.id = entry.player_id
                LEFT JOIN ranked_day_versions AS day ON day.id = entry.ranked_day_version_id
                WHERE manifest.id = ANY(%(manifests)s)
                ORDER BY entry.manifest_id, entry.ordinal
                """,
                {"manifests": list(manifests), "boundary": boundary},
            ).fetchall()
        ]

        numbers = dict(connection.execute(
            "SELECT id, generation FROM boundary_publication_generations WHERE boundary_at = %s",
            (boundary,)).fetchall())
        identities = {
            identity_id: (text(kind), numbers[generation_id], frozen(manifest_id))
            for identity_id, kind, generation_id, manifest_id in connection.execute(
                """
                SELECT identity.id, identity.artifact_kind, identity.generation_id,
                       identity.manifest_id
                FROM boundary_publication_artifact_identities AS identity
                WHERE identity.generation_id = ANY(%s) ORDER BY identity.id
                """,
                (list(numbers),),
            ).fetchall()
        }
        versions = [
            (number, text(board), text(army), text(rebuild),
             sorted(text(kind) for kind in affected or []), numbers.get(replaced),
             frozen(board_list), frozen(army_list),
             identities[statistics][1] if statistics else None,
             identities[army_identity][1] if army_identity else None)
            for (number, board, army, rebuild, affected, replaced, board_list, army_list,
                 statistics, army_identity) in connection.execute(
                """
                SELECT generation, snapshot_state, army_state, correction_state,
                       affected_artifacts, supersedes_id, snapshot_manifest_id,
                       army_manifest_id, snapshot_analytics_publication_id, army_publication_id
                FROM boundary_publication_generations
                WHERE boundary_at = %s ORDER BY generation
                """,
                (boundary,),
            ).fetchall()
        ]
        members = [
            (number, text(status), text(board), text(army), count)
            for number, status, board, army, count in connection.execute(
                """
                SELECT generation.generation, member.status, member.snapshot_status,
                       member.army_status, count(*)
                FROM boundary_publication_generation_members AS member
                JOIN boundary_publication_generations AS generation
                  ON generation.id = member.generation_id
                WHERE generation.boundary_at = %s
                GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4
                """,
                (boundary,),
            ).fetchall()
        ]
        rebuilds = []
        for state, affected, inputs, source, target in connection.execute(
            """
            SELECT state, affected_artifacts, clashlens_correction_inputs(correction),
                   source_generation_id, generation_id
            FROM boundary_publication_corrections AS correction
            WHERE boundary_at = %s ORDER BY id
            """,
            (boundary,),
        ).fetchall():
            markers = sorted(
                "arrival" if entry.get("arrival") else text(entry.get("kind"))
                for entry in inputs if "kind" in entry or entry.get("arrival"))
            days = sum("ranked_day_version_id" in entry for entry in inputs)
            rebuilds.append((text(state), sorted(text(kind) for kind in affected or []),
                             markers, numbers.get(source), numbers.get(target), days))
        events = [
            (number, replaced) for number, replaced in connection.execute(
                """
                SELECT generation, superseded_generation FROM boundary_publication_events
                WHERE boundary_at = %s ORDER BY id
                """,
                (boundary,),
            ).fetchall()
        ]
        jobs = []
        for key, status in connection.execute(
            """
            SELECT deduplication_key, status FROM python_processing_jobs
            WHERE work_type IN ('build_snapshot', 'build_analytics', 'build_army_analytics')
              AND input_json ->> 'boundary_at' = %s ORDER BY id
            """,
            (f"{boundary.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}",),
        ).fetchall():
            key = re.sub(r"[0-9a-f]{64}", lambda hashed: label(hashed.group()), text(key))
            key = re.sub(r":freeze:\d+:.*", ":freeze", key)
            key = re.sub(r"(snapshot:|:retry:)\d+", r"\1#", key)
            jobs.append((key, text(status)))
    return {
        "versions": versions, "members": members, "rebuilds": rebuilds,
        "frozen": frozen_rows, "identities": list(identities.values()), "events": events,
        "jobs": jobs,
    }
