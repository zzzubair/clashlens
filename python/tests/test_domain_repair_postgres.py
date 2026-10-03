"""A Season's repair campaign lists each result its fixes change once, writes
nothing on preview, closes with the correction window and, once active,
holds only the Resets it lists."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from clashlens import boundary, boundary_publication, domain_repair, reset_baselines
from clashlens.army_decoder import DECODER_VERSION
from clashlens.catalog import CATALOG_VERSION
from clashlens.db import Database
from clashlens.domain import HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION, ranked_day_for
from clashlens.reconciliation import (
    BattleContribution,
    CoverageObservation,
    PreviousRankedDay,
    ReconciliationInput,
    reconcile_ranked_day,
)

SEASON, NEXT_SEASON = "1788757200", "1791176400"
START = datetime(2026, 9, 7, 5, tzinfo=UTC)
DAY = timedelta(days=1)
END = START + 28 * DAY
DEADLINE = datetime(2026, 10, 12, 5, tzinfo=UTC)
NOW = END + DAY
MIGRATION = Path(__file__).parents[2] / "deploy/migrations/0063_domain_repair_campaigns.sql"


@contextmanager
def _campaign_database(database_url: str) -> Iterator[tuple[str, Database]]:
    """A migrated database with September 2026 confirmed, and a worker-role
    pool, as the command runs in the worker container."""
    with domain_database(database_url) as connection_info:
        with _owner(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO legend_season_anchors (
                    current_league_season_id, previous_league_season_id,
                    current_start, previous_start, anchor_rule_version,
                    source_profile_version_id, state
                ) VALUES (%s, '1786338000', %s, %s, 'legend-season-anchor-v1',
                          1, 'confirmed')
                """,
                (SEASON, START, START - 28 * DAY),
            )
        options = conninfo_to_dict(connection_info).get("options", "")
        worker = Database(
            make_conninfo(connection_info, options=f"{options} -c role=clashlens_python_worker")
        )
        try:
            yield connection_info, worker
        finally:
            worker.close()


@contextmanager
def _owner(connection_info: str) -> Iterator[psycopg.Connection]:
    """Seed rows directly; references to collection rows are not needed."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        connection.execute("SET session_replication_role = replica")
        yield connection


def _player(connection, tag: str) -> int:
    return connection.execute(
        "INSERT INTO players (normalized_tag, active) VALUES (%s, true) RETURNING id",
        (tag,),
    ).fetchone()[0]


def _report(
    connection, reporter: int, opponent: int, day: datetime, *,
    at: datetime | None = None, destruction: int = 55, code: str | None = None,
    observation_id: int = 0, perspective: str = "attacker",
) -> int:
    """Save one selected report on ``day`` and return its id."""
    sides = (reporter, opponent) if perspective == "attacker" else (opponent, reporter)
    battle_id = connection.execute(
        "INSERT INTO legend_battles (ranked_day_start, attacker_player_id,"
        " defender_player_id) VALUES (%s, %s, %s) RETURNING id",
        (day, *sides),
    ).fetchone()[0]
    evidence_id = connection.execute(
        """
        INSERT INTO battle_evidence (
            battle_id, source_row_id, observation_id, reporting_player_id,
            perspective, battle_timestamp, stars, destruction_percentage,
            army_share_code, attacker_gain, defender_loss, trophy_rule_version,
            source_observed_at, parser_version
        ) VALUES (%s, %s, %s, %s, %s, %s, 2, %s, %s, 18, 18, %s, %s,
                  'supercell-source-parser-v2')
        RETURNING id
        """,
        (battle_id, battle_id, observation_id, reporter, perspective,
         at or day + timedelta(hours=2),
         destruction, code, HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION, day + DAY),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO battle_perspectives (battle_id, perspective, evidence_id,"
        " source_observed_at) VALUES (%s, %s, %s, %s)",
        (battle_id, perspective, evidence_id, day + DAY),
    )
    return evidence_id


def _saved_day(
    connection, player_id: int, day: datetime, season: str = SEASON, version: int = 1
) -> None:
    connection.execute(
        """
        INSERT INTO ranked_day_versions (
            player_id, ranked_day_start, ranked_day_end, official_season_id,
            season_day_number, season_anchor_rule_version,
            reconciliation_rule_version, result_hash, version, state, confidence,
            input_hash, evidence_complete, coverage_complete
        ) VALUES (%s, %s, %s, %s, 1, 'legend-season-anchor-v1', 'test', %s, %s,
                  'Complete', 'exact', %s, true, true)
        """,
        (player_id, day, day + DAY, season, f"{version:x}" * 64, version, "a" * 64),
    )


def _calculated_day(
    connection, player_id: int, day: datetime, *, start: int, end: int,
    attacks=(), defenses=(), previous: PreviousRankedDay | None = None,
) -> PreviousRankedDay:
    """Save a day as the product calculates it from complete battle-log
    coverage and these (report id, trophies) battles, every one 2-star/55%,
    and return what the next day reads of it."""
    result = reconcile_ranked_day(ReconciliationInput(
        ranked_day=ranked_day_for(day), now=DEADLINE + 7 * DAY,
        start_baseline_id=1, end_baseline_id=2, start_trophies=start,
        next_start_trophies=end,
        coverage_observations=(CoverageObservation(
            observed_at=day, row_count=0, battle_identities=(), has_row_gap=False,
            observation_id=1,
        ),),
        contributions=tuple(
            BattleContribution(
                battle_identity=str(report), lens=lens, trophy_amount=trophies,
                source_evidence_id=report, stars=2, destruction_percentage=55,
                attacker_gain=trophies, defender_loss=trophies,
                source_rule_version=HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION,
            )
            for lens, battles in (("offense", attacks), ("defense", defenses))
            for report, trophies in battles
        ),
        previous_day=previous, boundary_kind=None, season_anchor_valid=True,
        start_baseline_battle_log_observation_id=1,
        end_baseline_battle_log_observation_id=1,
    ))
    connection.execute(
        """
        INSERT INTO ranked_day_versions (
            player_id, ranked_day_start, ranked_day_end, official_season_id,
            season_day_number, season_anchor_rule_version,
            reconciliation_rule_version, result_hash, version, state, confidence,
            input_hash, coverage_complete, attack_count, defense_count,
            observed_defense_loss, input_evidence
        ) VALUES (%s, %s, %s, %s, 1, 'legend-season-anchor-v1', 'test', %s, 1, %s,
                  %s, %s, %s, %s, %s, %s, %s)
        """,
        (player_id, day, day + DAY, SEASON if day < END else NEXT_SEASON,
         hashlib.sha256(f"{player_id}:{day}".encode()).hexdigest(), result.state,
         result.confidence, "a" * 64, result.coverage_complete, result.attack_count,
         result.defense_count, result.observed_defense_loss, Jsonb(result.input_evidence)),
    )
    return PreviousRankedDay(
        complete=result.state == "Complete" and result.coverage_complete,
        observed_defense_count=result.defense_count,
        observed_defense_loss=result.observed_defense_loss,
        shield_run_length=0, coverage_complete=result.coverage_complete,
        shield_state=result.shield_state, ranked_day_start=day, state=result.state,
        confidence=result.confidence,
    )


def _items(connection_info: str, kind: str) -> list[tuple]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT target_key, reasons, state, exclusion, official_season_id"
            " FROM domain_repair_items WHERE kind = %s ORDER BY target_key",
            (kind,),
        ).fetchall()


def _key(prefix: str, *parts: object) -> str:
    return ":".join(
        [prefix, *(str(int(p.timestamp())) if isinstance(p, datetime) else str(p) for p in parts)]
    )


def test_campaign_preview_has_no_writes(database_url: str, archive_server) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        observation_id, _ = store_observation(
            connection_info, archive_server, occurrence_key="kept-raw",
            endpoint="battle_log", body=b"{}", observed_at=START + DAY,
            normalized_tag="#2PP",
        )
        with _owner(connection_info) as connection:
            kept = _player(connection, "#KEPT")
            gone = _player(connection, "#GONE")
            _report(connection, kept, gone, START, observation_id=observation_id)
            _report(connection, gone, kept, START + DAY, code="u1x0-2x1")
            tables = ("domain_repair_campaigns", "domain_repair_items", "python_processing_jobs",
                      "battle_evidence", "battle_perspectives", "boundary_publication_generations")
            count = " UNION ALL ".join(f"SELECT '{table}', count(*) FROM {table}" for table in tables)
            before = connection.execute(count).fetchall()

            report = domain_repair.preview(worker, SEASON, now=NOW)

            assert connection.execute(count).fetchall() == before
        assert report["open"] and report["write_deadline"] == DEADLINE.isoformat()
        # Only the decoded report's day is held; the other day was never saved.
        assert report["items"] == {"source": 2, "publication": 1}
        # The second report's raw response is not kept, so it cannot be read again.
        assert report["excluded"] == {"raw_unavailable": 1}
        assert report["reasons"]["source:catalogue"] == 1


def test_campaign_inventory_unions_reasons_once(database_url: str) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        with _owner(connection_info) as connection:
            player, opponent = _player(connection, "#ONE"), _player(connection, "#TWO")
            # A 2-star/55% attack, saved on day 3 although its timestamp, two
            # minutes after day 3's Reset, belongs to day 2, with no decode.
            day2, day3 = START + DAY, START + 2 * DAY
            evidence_id = _report(connection, player, opponent, day3,
                                  at=day3 + timedelta(minutes=2), code="u1x0-2x1")
            _report(connection, opponent, player, day3, destruction=56, code="u1x0-2x1")
            for day in (day2, day3, START + 3 * DAY):
                _saved_day(connection, player, day)
            _saved_day(connection, player, day3, version=2)  # same day, again

        first = domain_repair.register(worker, SEASON, now=NOW)
        assert domain_repair.register(worker, SEASON, now=NOW)["plan_digest"] == first["plan_digest"]

        assert [row[:2] for row in _items(connection_info, "source")] == [
            (f"evidence:{evidence_id}", ["catalogue", "moved", "payout"])
        ]
        assert [row[:2] for row in _items(connection_info, "day")] == [
            (_key("day", player, day2), ["moved", "payout"]),
            (_key("day", player, day3), ["moved", "payout"]),
            (_key("day", player, START + 3 * DAY), ["dependency"]),
        ]
        assert [row[0] for row in _items(connection_info, "publication")] == [
            _key("boundary", day) for day in (day3, START + 3 * DAY, START + 4 * DAY)
        ]
        # The 56% report needs only its decode, in a battle batch.
        assert first["items"]["decode_batch"] == 1 and first["decode_battles"] == 1


def test_campaign_window_is_season_end_plus_seven_days(database_url: str) -> None:
    assert domain_repair.campaign_window(START) == (END, DEADLINE)
    with _campaign_database(database_url) as (connection_info, worker):
        with pytest.raises(domain_repair.CampaignRefused, match="window closed"):
            domain_repair.register(worker, SEASON, now=DEADLINE)
        # The previous Season's window closed on September 14.
        with pytest.raises(domain_repair.CampaignRefused, match="window closed"):
            domain_repair.register(worker, "1786338000", now=NOW)
        assert domain_repair.register(worker, SEASON, now=DEADLINE - timedelta(microseconds=1))
        with _owner(connection_info) as connection:
            connection.execute(
                "INSERT INTO season_detail_retirements (official_season_id) VALUES (%s)",
                (SEASON,),
            )
        with pytest.raises(domain_repair.CampaignRefused, match="finalized"):
            domain_repair.register(worker, SEASON, now=NOW)
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT official_season_id, write_deadline FROM domain_repair_campaigns"
            ).fetchall() == [(SEASON, DEADLINE)]


def test_campaign_leaves_out_next_season_days_it_cannot_change(database_url: str) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        with _owner(connection_info) as connection:
            player, opponent = _player(connection, "#LAST"), _player(connection, "#OTHER")
            day28 = END - DAY
            report = _report(connection, player, opponent, day28)
            previous = _calculated_day(connection, player, day28, start=5000, end=5017,
                                       attacks=[(report, 18)])
            # The next Season's first day only attacked, so it reads nothing
            # of day 28 that its correction changes.
            _calculated_day(connection, player, END, start=5017, end=5047,
                            attacks=[(800_000, 30)], previous=previous)
        domain_repair.register(worker, SEASON, now=NOW)
        assert [row[0] for row in _items(connection_info, "publication")] == [
            _key("boundary", END)
        ]
        assert [(row[0], row[4]) for row in _items(connection_info, "day")] == [
            (_key("day", player, day28), SEASON)
        ]
        late = domain_repair.preview(worker, SEASON, now=DEADLINE)
        assert not late["open"]
        assert late["excluded"] == {"raw_unavailable": 1, "window_expired": 2}
        assert late["items"] == {"source": 1, "day": 1, "publication": 1}


def test_campaign_follows_next_season_days_a_correction_changes(database_url: str) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        day28 = END - DAY
        with _owner(connection_info) as connection:
            chain, short = _player(connection, "#CHAIN"), _player(connection, "#SHORT")
            opponent = _player(connection, "#OPP")
            for player in (chain, short):
                # Day 28's eight defenses lost 227, its 2-star/55% one saved
                # as 18, so day 28 is Inconsistent until corrected to 17.
                report = _report(connection, player, opponent, day28, perspective="defender")
                previous = _calculated_day(
                    connection, player, day28, start=5500, end=5273,
                    defenses=[(report, 18), *((900_000 + n, 30) for n in range(7))],
                )
                # Each next-Season day's one 20-trophy defense takes its
                # automatic loss from the day before, so it is Partial until
                # day 28, then each day after it, turns Complete.
                trophies, losses = 5273, [189, *[140] * 7]
                for number, loss in enumerate(losses):
                    day = END + number * DAY
                    if player == short and number == 2:
                        # Only attacking, day 3 reads nothing that changes.
                        previous = _calculated_day(
                            connection, player, day, start=trophies, end=trophies + 30,
                            attacks=[(800_000, 30)], previous=previous,
                        )
                        break
                    previous = _calculated_day(
                        connection, player, day, start=trophies,
                        end=trophies - 20 - loss, defenses=[(800_000, 20)],
                        previous=previous,
                    )
                    trophies -= 20 + loss
        domain_repair.register(worker, SEASON, now=DEADLINE - timedelta(hours=1))
        assert sorted(row[0] for row in _items(connection_info, "day")) == sorted(
            [_key("day", short, day) for day in (day28, END, END + DAY)]
            # The day starting at the window's close is never listed.
            + [_key("day", chain, day28 + number * DAY) for number in range(8)]
        )
        assert {row[0] for row in _items(connection_info, "publication")} == {
            _key("boundary", END + number * DAY) for number in range(8)
        }


def test_campaign_follows_a_move_out_of_next_season_first_day(database_url: str) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        day28 = END - DAY
        with _owner(connection_info) as connection:
            player, opponent = _player(connection, "#NINE"), _player(connection, "#OPP")
            # A 30-trophy defense two minutes after the Reset belongs to day
            # 28; its report is saved there, but day 1's result still counts it.
            moved = _report(connection, player, opponent, day28, perspective="defender",
                            at=END + timedelta(minutes=2), destruction=56)
            connection.execute(
                """
                INSERT INTO battle_day_repairs (
                    from_battle_id, to_battle_id, perspective, evidence_id,
                    attacker_player_id, defender_player_id, from_day, to_day
                ) SELECT 0, battle_id, 'defender', id, %s, %s, %s, %s
                FROM battle_evidence WHERE id = %s
                """,
                (opponent, player, END, day28, moved),
            )
            connection.execute(
                "INSERT INTO api_player_daily_logs (player_id, ranked_day_start,"
                " version, state, coverage, battles) VALUES (%s, %s, 1, 'Partial',"
                " 'complete', %s)",
                (player, END, Jsonb([{"source_evidence_id": moved}])),
            )
            previous = _calculated_day(connection, player, day28, start=5300, end=5270)
            # Nine defenses make day 1 Inconsistent; without the moved one
            # its eight explain its trophies, and day 2 can take its
            # automatic loss from it: (160 + 20) // 9 * 7 = 140.
            previous = _calculated_day(
                connection, player, END, start=5270, end=5110, previous=previous,
                defenses=[(moved, 30), *((900_000 + n, 20) for n in range(8))],
            )
            previous = _calculated_day(connection, player, END + DAY, start=5110, end=4950,
                                       defenses=[(800_000, 20)], previous=previous)
            _calculated_day(connection, player, END + 2 * DAY, start=4950, end=4980,
                            attacks=[(800_001, 30)], previous=previous)
        domain_repair.register(worker, SEASON, now=END + 3 * DAY)
        assert [row[:2] for row in _items(connection_info, "day")] == [
            (_key("day", player, day28), ["moved"]),
            (_key("day", player, END), ["moved"]),
            (_key("day", player, END + DAY), ["dependency"]),
        ]
        assert [row[0] for row in _items(connection_info, "publication")] == [
            _key("boundary", END + number * DAY) for number in range(3)
        ]


def test_campaign_lists_only_moves_not_yet_published(database_url: str) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        day2, day3 = START + DAY, START + 2 * DAY
        with _owner(connection_info) as connection:
            shown, stale = _player(connection, "#SHOWN"), _player(connection, "#STALE")
            opponent = _player(connection, "#OPP")
            for player in (shown, stale):
                evidence_id = _report(connection, player, opponent, day2, destruction=56)
                connection.execute(
                    """
                    INSERT INTO battle_day_repairs (
                        from_battle_id, to_battle_id, perspective, evidence_id,
                        attacker_player_id, defender_player_id, from_day, to_day
                    ) SELECT 0, battle_id, 'attacker', id, %s, %s, %s, %s
                    FROM battle_evidence WHERE id = %s
                    """,
                    (player, opponent, day3, day2, evidence_id),
                )
                # The moved report is published on its new day for one
                # player; the other's old day still shows it.
                listed = [{"source_evidence_id": evidence_id}]
                for day, battles in ((day2, listed if player == shown else []),
                                     (day3, [] if player == shown else listed)):
                    connection.execute(
                        "INSERT INTO api_player_daily_logs (player_id, ranked_day_start,"
                        " version, state, coverage, battles) VALUES (%s, %s, 1,"
                        " 'Complete', 'complete', %s)",
                        (player, day, Jsonb(battles)),
                    )
                    _saved_day(connection, player, day)
            stale_evidence = evidence_id
        report = domain_repair.register(worker, SEASON, now=NOW)
        assert [row[:2] for row in _items(connection_info, "source")] == [
            (f"evidence:{stale_evidence}", ["moved"])
        ]
        assert [row[0] for row in _items(connection_info, "day")] == [
            _key("day", stale, day2), _key("day", stale, day3)
        ]
        assert report["items"] == {"source": 1, "day": 2, "publication": 2}


def test_campaign_registration_drops_finished_work_until_activated(
    database_url: str, monkeypatch
) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        with _owner(connection_info) as connection:
            player, opponent = _player(connection, "#CODE"), _player(connection, "#OTHER")
            first = _report(connection, player, opponent, START, destruction=56, code="u1x0-2x1")
        assert domain_repair.register(worker, SEASON, now=NOW)["items"] == {
            "decode_batch": 1, "publication": 1
        }
        # The decode queued before the campaign finishes and publishes, so
        # registering again leaves nothing listed or held for it.
        _decoded(connection_info, first)
        assert domain_repair.register(worker, SEASON, now=NOW)["items"] == {}
        assert _items(connection_info, "decode_batch") == []
        assert _items(connection_info, "publication") == []

        with _owner(connection_info) as connection:
            second = _report(connection, player, opponent, START, destruction=56, code="u1x0-2x1")
        domain_repair.register(worker, SEASON, now=NOW)
        monkeypatch.setattr(domain_repair, "HANDLERS", dict.fromkeys(domain_repair.REQUIRED_STAGES))
        domain_repair.activate(worker, SEASON, now=NOW)
        _decoded(connection_info, second)
        with pytest.raises(domain_repair.CampaignRefused, match="campaign is active"):
            domain_repair.register(worker, SEASON, now=NOW)
        assert [row[2] for row in _items(connection_info, "publication")] == ["pending"]


def _decoded(connection_info: str, evidence_id: int) -> None:
    with _owner(connection_info) as connection:
        connection.execute(
            """
            INSERT INTO battle_army_decodes (
                battle_id, evidence_id, perspective, decoder_version,
                catalog_version, catalog_hash, status, failure_category
            ) SELECT battle_id, id, 'attacker', %s, %s, %s, 'failed', 'undecodable'
            FROM battle_evidence WHERE id = %s
            """,
            (DECODER_VERSION, CATALOG_VERSION, "a" * 64, evidence_id),
        )


def test_campaign_hold_defers_only_affected_artifacts(
    database_url: str, monkeypatch
) -> None:
    # The player's two saved days list the Resets ending them, not the third.
    held, held_next, free = START + 5 * DAY, START + 6 * DAY, START + 7 * DAY
    with _campaign_database(database_url) as (connection_info, worker):
        with _owner(connection_info) as connection:
            player, opponent = _player(connection, "#HELD"), _player(connection, "#FREE")
            _report(connection, player, opponent, held - DAY)
            _saved_day(connection, player, held - DAY)
            _saved_day(connection, player, held)
        with pytest.raises(domain_repair.CampaignRefused, match="not installed"):
            domain_repair.activate(worker, SEASON, now=NOW)
        domain_repair.register(worker, SEASON, now=NOW)
        monkeypatch.setattr(domain_repair, "HANDLERS", dict.fromkeys(domain_repair.REQUIRED_STAGES))
        domain_repair.activate(worker, SEASON, now=NOW)

        def builds() -> list[datetime]:
            with psycopg.connect(connection_info) as connection:
                return [row[0] for row in connection.execute(
                    "SELECT DISTINCT boundary_at FROM boundary_publication_generations"
                    " WHERE army_manifest_id IS NOT NULL ORDER BY 1"
                ).fetchall()]

        for boundary_at in (held, held_next, free):
            with _owner(connection_info) as connection:
                sweep_id = connection.execute(
                    "INSERT INTO collector_reset_sweeps (boundary_at, member_ids,"
                    " membership_captured_at) VALUES (%s, %s, clock_timestamp()) RETURNING id",
                    (boundary_at, [opponent]),
                ).fetchone()[0]
            with worker.pool.connection() as connection:
                reset_baselines._record_boundary_baseline(
                    worker, connection, boundary_at=boundary_at, reset_sweep_id=sweep_id,
                    player_id=opponent, state="failed",
                )
        assert builds() == [free]
        boundary_publication.reevaluate_boundary_publications(worker)
        assert builds() == [free]
        _set_publication_items(connection_info, "done")
        boundary_publication.reevaluate_boundary_publications(worker)
        assert builds() == [held, held_next, free]

        # Once published, a rebuilt day result reaches the first Reset, and a
        # decode job queued before the campaign, finishing now, the other two.
        # The free Reset starts its correction at once; each held Reset keeps
        # its publication and the correction waits queued.
        _set_publication_items(connection_info, "pending")
        with _owner(connection_info) as connection:
            _saved_day(connection, opponent, held - DAY)
        with worker.pool.connection() as connection:
            connection.execute(
                "UPDATE boundary_publication_generations"
                " SET snapshot_state = 'published', army_state = 'published'"
            )
            boundary._record_boundary_generation(
                worker, connection, boundary_at=held, player_id=opponent,
                ranked_day_version_id=connection.execute(
                    "SELECT id FROM ranked_day_versions WHERE player_id = %s", (opponent,)
                ).fetchone()[0],
                ranked_day_input_hash="a" * 64,
            )
            for boundary_at in (held_next, free):
                generation_id = connection.execute(
                    "SELECT id FROM boundary_publication_generations WHERE boundary_at = %s",
                    (boundary_at,),
                ).fetchone()[0]
                boundary_publication._queue_boundary_army_correction(
                    worker, connection, boundary_at=boundary_at, generation_id=generation_id
                )
        boundary_publication.reevaluate_boundary_publications(worker)
        with psycopg.connect(connection_info) as connection:
            assert [
                (row[0], str(row[1]), row[2]) for row in connection.execute(
                    """
                    SELECT correction.boundary_at, correction.state,
                           (SELECT count(*) FROM boundary_publication_generations AS g
                            WHERE g.boundary_at = correction.boundary_at)
                    FROM boundary_publication_corrections AS correction ORDER BY 1
                    """
                ).fetchall()
            ] == [(held, "queued", 1), (held_next, "queued", 1), (free, "active", 2)]


def _set_publication_items(connection_info: str, state: str) -> None:
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            "UPDATE domain_repair_items SET state = %s WHERE kind = 'publication'", (state,)
        )


def test_campaign_migration_is_repeatable_and_creates_no_campaign(database_url: str) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(MIGRATION.read_text(encoding="utf-8"))
            assert connection.execute(
                "SELECT (SELECT count(*) FROM domain_repair_campaigns),"
                " (SELECT count(*) FROM domain_repair_items),"
                " (SELECT count(*) FROM python_processing_jobs)"
            ).fetchone() == (0, 0, 0)
        assert domain_repair.preview(worker, SEASON, now=NOW)["items"] == {}
