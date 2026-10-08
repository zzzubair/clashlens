"""A Season's repair campaign lists each result its fixes change once, writes
nothing on preview, closes with the correction window and, once active,
holds only the Resets it lists."""

from __future__ import annotations

import itertools
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from clashlens import (
    boundary,
    boundary_publication,
    domain_repair,
    past_reset_pacing,
    reset_baselines,
)
from clashlens.analytics import SNAPSHOT_ORDERING_RULE_VERSION
from clashlens.army_decoder import DECODER_VERSION
from clashlens.catalog import CATALOG_VERSION
from clashlens.db import Database
from clashlens.domain import (
    HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION,
    TROPHY_ALLOCATION_RULE_VERSION,
)
from clashlens.reconciliation import RECONCILIATION_RULE_VERSION

SEASON, NEXT_SEASON = "1788757200", "1791176400"
START = datetime(2026, 9, 7, 5, tzinfo=UTC)
DAY = timedelta(days=1)
END = START + 28 * DAY
DEADLINE = datetime(2026, 10, 12, 5, tzinfo=UTC)
NOW = END + DAY
MIGRATION = Path(__file__).parents[2] / "deploy/migrations/0064_domain_repair_campaigns.sql"


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
    rule: str = HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION,
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
         destruction, code, rule, day + DAY),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO battle_perspectives (battle_id, perspective, evidence_id,"
        " source_observed_at) VALUES (%s, %s, %s, %s)",
        (battle_id, perspective, evidence_id, day + DAY),
    )
    return evidence_id


def _saved_day(
    connection, player_id: int, day: datetime, season: str = SEASON, version: int = 1,
    rule: str = RECONCILIATION_RULE_VERSION,
) -> None:
    """Save a day result built, as reconciliation does, from the newest
    saved result of the day before."""
    connection.execute(
        """
        INSERT INTO ranked_day_versions (
            player_id, ranked_day_start, ranked_day_end, official_season_id,
            season_day_number, season_anchor_rule_version,
            reconciliation_rule_version, result_hash, version, state, confidence,
            input_hash, evidence_complete, coverage_complete, input_evidence
        ) VALUES (%s, %s, %s, %s, 1, 'legend-season-anchor-v1', %s, %s, %s,
                  'Complete', 'exact', %s, true, true,
                  jsonb_build_object('previous_day', jsonb_build_object('version_id', (
                      SELECT id FROM ranked_day_versions
                      WHERE player_id = %s AND ranked_day_start = %s
                      ORDER BY version DESC, id DESC LIMIT 1
                  ))))
        """,
        (player_id, day, day + DAY, season, rule,
         f"{version:x}" * 64, version, "a" * 64, player_id, day - DAY),
    )


def _population(connection, boundary_at: datetime, *player_ids: int) -> int:
    """Capture the players a Reset's publication covers."""
    return connection.execute(
        "INSERT INTO collector_reset_sweeps (boundary_at, member_ids,"
        " membership_captured_at) VALUES (%s, %s, clock_timestamp()) RETURNING id",
        (boundary_at, list(player_ids)),
    ).fetchone()[0]


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
            for boundary_at in (START + DAY, START + 2 * DAY):
                _population(connection, boundary_at, kept, gone)
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
                _population(connection, day + DAY, player, opponent)
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


def test_campaign_includes_closing_boundary_and_next_season_dependency(
    database_url: str,
) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        with _owner(connection_info) as connection:
            player, opponent = _player(connection, "#LAST"), _player(connection, "#OTHER")
            day28 = END - DAY
            _report(connection, player, opponent, day28)
            _saved_day(connection, player, day28)
            _saved_day(connection, player, END, NEXT_SEASON)
            _saved_day(connection, player, END + DAY, NEXT_SEASON)
            for boundary_at in (END, END + DAY, END + 2 * DAY):
                _population(connection, boundary_at, player)
        domain_repair.register(worker, SEASON, now=NOW)
        # Day 28 ends at the October 5 Reset; the next Season's first day
        # starts from it and is listed to recalculate. Later October days are
        # left to the repair, which recalculates days in order.
        assert [row[0] for row in _items(connection_info, "publication")] == [
            _key("boundary", END), _key("boundary", END + DAY)
        ]
        assert [(row[0], row[1], row[4]) for row in _items(connection_info, "day")] == [
            (_key("day", player, day28), ["payout"], SEASON),
            (_key("day", player, END), ["dependency"], NEXT_SEASON),
        ]
        # After September's window closes, the October day is still open.
        late = domain_repair.preview(worker, SEASON, now=DEADLINE)
        assert not late["open"]
        assert late["excluded"] == {"raw_unavailable": 1, "window_expired": 2}
        assert late["items"] == {"source": 1, "day": 2, "publication": 2}


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
            for boundary_at in (day2 + DAY, day3 + DAY):
                _population(connection, boundary_at, shown, stale, opponent)
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
            for boundary_at in (START + DAY, START + 2 * DAY):
                _population(connection, boundary_at, player, opponent)
        # A queued correction whose Reset reads a battle still needing its
        # decode is held with that decode's Reset.
        _unpublished_correction(connection_info, START + DAY, {"kind": "decode"})
        assert domain_repair.register(worker, SEASON, now=NOW)["items"] == {
            "decode_batch": 1, "publication": 1
        }
        assert [row[:2] for row in _items(connection_info, "publication")] == [
            (_key("boundary", START + DAY), ["catalogue"])
        ]
        # Once the decode queued before the campaign is saved, nothing is left
        # to repair, so registering again lists nothing for it, its still
        # queued correction included.
        _decoded(connection_info, first)
        assert domain_repair.register(worker, SEASON, now=NOW)["items"] == {}
        assert _items(connection_info, "decode_batch") == []
        assert _items(connection_info, "publication") == []

        with _owner(connection_info) as connection:
            second = _report(connection, player, opponent, START + DAY, destruction=56, code="u1x0-2x1")
        domain_repair.register(worker, SEASON, now=NOW)
        monkeypatch.setattr(domain_repair, "HANDLERS", dict.fromkeys(domain_repair.REQUIRED_STAGES))
        domain_repair.activate(worker, SEASON, now=NOW)
        _decoded(connection_info, second)
        with pytest.raises(domain_repair.CampaignRefused, match="campaign is active"):
            domain_repair.register(worker, SEASON, now=NOW)
        assert [row[2] for row in _items(connection_info, "publication")] == ["pending"]


def _unpublished_correction(connection_info: str, boundary_at: datetime, pending: dict) -> None:
    with _owner(connection_info) as connection:
        connection.execute(
            "INSERT INTO boundary_publication_corrections (boundary_at,"
            " source_generation_id, pending_inputs) VALUES (%s, 0, %s)",
            (boundary_at, Jsonb([pending])),
        )


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
    # Midday, outside the 04:30-07:00 UTC quiet window, so whatever the real
    # time, the two past Resets' builds start once the campaign lets them.
    monkeypatch.setattr(
        past_reset_pacing, "_now", lambda _connection: NOW + timedelta(hours=7)
    )
    # The player's two saved days list the Resets ending them, not the third.
    held, held_next, free = START + 5 * DAY, START + 6 * DAY, START + 7 * DAY
    with _campaign_database(database_url) as (connection_info, worker):
        with _owner(connection_info) as connection:
            player, opponent = _player(connection, "#HELD"), _player(connection, "#FREE")
            _report(connection, player, opponent, held - DAY)
            _saved_day(connection, player, held - DAY)
            _saved_day(connection, player, held)
            sweeps = {
                boundary_at: _population(connection, boundary_at, player, opponent)
                for boundary_at in (held, held_next, free)
            }
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

        for boundary_at, member in itertools.product((held, held_next, free), (player, opponent)):
            with worker.pool.connection() as connection:
                reset_baselines._record_boundary_baseline(
                    worker, connection, boundary_at=boundary_at,
                    reset_sweep_id=sweeps[boundary_at], player_id=member, state="failed",
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


def test_campaign_lists_only_resets_whose_population_uses_the_change(
    database_url: str,
) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        day2, day5 = START + DAY, START + 4 * DAY
        with _owner(connection_info) as connection:
            player, opponent = _player(connection, "#PAY"), _player(connection, "#OPP")
            left, other = _player(connection, "#LEFT"), _player(connection, "#GONE")
            _report(connection, player, opponent, day2)
            _saved_day(connection, player, day2)
            _saved_day(connection, player, day2 + DAY)
            # Both players of a battle needing its decode left before its Reset.
            _report(connection, left, other, day5, destruction=56, code="u1x0-2x1")
            _population(connection, day2 + DAY, opponent)
            _population(connection, day2 + 2 * DAY, player, opponent)
            _population(connection, day5 + DAY, player, opponent)
        report = domain_repair.register(worker, SEASON, now=NOW)
        # The report, decode and both days still need repair; only the Reset
        # whose population includes the player is held.
        assert report["items"] == {"source": 1, "decode_batch": 1, "day": 2, "publication": 1}
        assert [row[0] for row in _items(connection_info, "publication")] == [
            _key("boundary", day2 + 2 * DAY)
        ]


def test_campaign_keeps_days_a_finished_fix_left_stale(database_url: str) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        day2, day3, day4, day5 = (START + n * DAY for n in (1, 2, 3, 4))
        with _owner(connection_info) as connection:
            moved, paid = _player(connection, "#MOVED"), _player(connection, "#PAID")
            opponent = _player(connection, "#OPP")
            evidence_id = _report(connection, moved, opponent, day2, destruction=56)
            connection.execute(
                """
                INSERT INTO battle_day_repairs (
                    from_battle_id, to_battle_id, perspective, evidence_id,
                    attacker_player_id, defender_player_id, from_day, to_day
                ) SELECT 0, battle_id, 'attacker', id, %s, %s, %s, %s
                FROM battle_evidence WHERE id = %s
                """,
                (moved, opponent, day3, day2, evidence_id),
            )
            # Both moved days already show the move, but day 4 was built
            # from day 3's result before the move was rebuilt.
            for day, battles in ((day2, [{"source_evidence_id": evidence_id}]), (day3, [])):
                connection.execute(
                    "INSERT INTO api_player_daily_logs (player_id, ranked_day_start,"
                    " version, state, coverage, battles) VALUES (%s, %s, 2,"
                    " 'Complete', 'complete', %s)",
                    (moved, day, Jsonb(battles)),
                )
            for day in (day2, day3, day4, day5):
                _saved_day(connection, moved, day)
            _saved_day(connection, moved, day3, version=2)
            # A 2-star/55% report already read again under the new payout,
            # whose saved day still counts the old one.
            _report(connection, paid, opponent, day2, rule=TROPHY_ALLOCATION_RULE_VERSION)
            _saved_day(connection, paid, day2)
            # The same, with the day last saved under an older calculation rule.
            older = _player(connection, "#OLDER")
            _report(connection, older, opponent, day4, rule=TROPHY_ALLOCATION_RULE_VERSION)
            _saved_day(connection, older, day4, rule="legend-ranked-day-reconciliation-v2")
            for boundary_at in (day3, day4, day5, day5 + DAY):
                _population(connection, boundary_at, moved, paid, opponent, older)
        # A late battle queues a correction of the moved player's day 3,
        # which needs no repair, so its Reset is not held.
        _unpublished_correction(connection_info, day4, {"player_id": moved})
        report = domain_repair.register(worker, SEASON, now=NOW)
        assert _items(connection_info, "source") == []
        assert [row[:2] for row in _items(connection_info, "day")] == sorted([
            (_key("day", moved, day4), ["dependency"]),
            (_key("day", moved, day5), ["dependency"]),
            (_key("day", paid, day2), ["payout"]),
            (_key("day", older, day4), ["payout"]),
        ])
        assert [row[:2] for row in _items(connection_info, "publication")] == [
            (_key("boundary", day3), ["payout"]),
            (_key("boundary", day5), ["dependency", "payout"]),
            (_key("boundary", day5 + DAY), ["dependency"]),
        ]
        assert report["items"] == {"day": 4, "publication": 3}


def test_campaign_saves_nothing_once_the_window_closes_mid_write(
    database_url: str, monkeypatch
) -> None:
    closing = "1999999999"
    with _campaign_database(database_url) as (connection_info, worker):
        with _owner(connection_info) as connection:
            # A Season whose correction window closes two seconds from now,
            # replacing September as the one confirmed start.
            connection.execute("UPDATE legend_season_anchors SET state = 'superseded'")
            connection.execute(
                """
                INSERT INTO legend_season_anchors (
                    current_league_season_id, previous_league_season_id,
                    current_start, previous_start, anchor_rule_version,
                    source_profile_version_id, state
                ) SELECT %s, '1999999998', start, start - interval '28 days',
                         'legend-season-anchor-v1', 1, 'confirmed'
                FROM (SELECT clock_timestamp() - interval '35 days'
                             + interval '2 seconds' AS start) AS season
                """,
                (closing,),
            )
        inventory = domain_repair._inventory

        def slow_inventory(*arguments):
            time.sleep(2.5)
            return inventory(*arguments)

        monkeypatch.setattr(domain_repair, "_inventory", slow_inventory)
        with pytest.raises(domain_repair.CampaignRefused, match="window closed"):
            domain_repair.register(worker, closing)
        with psycopg.connect(connection_info) as connection:
            assert connection.execute("SELECT count(*) FROM domain_repair_campaigns").fetchone() == (0,)


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


def test_season_repair_recalculates_days_then_rebuilds_boards_with_a_receipt(
    database_url: str,
) -> None:
    """The first queue saves the Season as it was; each player's days are
    queued once; only once they have all run, none failed, are the boards
    the rules now change rebuilt; the receipt shows before beside now."""
    from domain_test_support import repair_season
    from test_boundary_manifest_postgres import (
        DAY_2_RESET,
        _build_board,
        _october,
        _seed_board,
        _seed_days,
    )

    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(
            connection_info, [("#2QCYU8C2G", 4703, _october(7, 4, 37))]
        )
        _seed_days(
            connection_info, generation_id,
            {1: (True, [("offense", 228, _october(7, 4, 30), True)])},
            {1: {"state": "Complete", "failure_reasons": [], "final": 4931,
                 "start": 4703, "end": 4931}},
        )
        database = Database(connection_info)
        try:
            assert _build_board(connection_info, database, generation_id) == [
                ("#2QCYU8C2G", 4931, "confirmed")
            ]
            with database.pool.connection() as connection:
                # As a board built and published before the board rule.
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute("UPDATE leaderboard_snapshot_entries SET trophies = 4902")
                connection.execute(
                    "UPDATE leaderboard_snapshots"
                    " SET state = 'published', published_at = clock_timestamp()"
                    " WHERE snapshot_kind = 'frozen'"
                )
            preview, queued = repair_season(connection_info, NEXT_SEASON)
            waiting = domain_repair.season_repair(
                database, NEXT_SEASON, "queue", max_jobs=100
            )
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET status = 'failed',"
                    " failure_category = 'invalid_work_input'"
                    " WHERE deduplication_key LIKE 'reconcile:season-repair:%'"
                )
            blocked = domain_repair.season_repair(
                database, NEXT_SEASON, "queue", max_jobs=100
            )
            with database.pool.connection() as connection:
                # As once an operator has retried it and it has run.
                connection.execute(
                    "UPDATE python_processing_jobs SET status = 'complete',"
                    " failure_category = NULL"
                    " WHERE deduplication_key LIKE 'reconcile:season-repair:%'"
                )
            boards = domain_repair.season_repair(
                database, NEXT_SEASON, "queue", max_jobs=100
            )
            receipt = domain_repair.season_repair(
                database, NEXT_SEASON, "receipt", max_jobs=100
            )
            with database.pool.connection() as connection:
                # As once the worker has rebuilt the board.
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute(
                    "UPDATE boundary_publication_corrections SET state = 'finalized'"
                )
                connection.execute("UPDATE leaderboard_snapshot_entries SET trophies = 4931")
            done = domain_repair.season_repair(
                database, NEXT_SEASON, "queue", max_jobs=100
            )
        finally:
            database.close()

    day, reset = (DAY_2_RESET - DAY).isoformat(), DAY_2_RESET.isoformat()
    assert (preview["players"], preview["left_to_queue"]) == (1, 1)
    assert [board["late_battles"] for board in preview["boards_to_rebuild"]] == [1]
    assert (queued["phase"], queued["queued"], queued["left_to_queue"]) == ("days", 1, 0)
    # Boards wait for every player's days.
    assert (waiting["phase"], waiting["unfinished"]) == ("days", 1)
    # A failed day job holds the boards and summaries until it is resolved.
    assert (blocked["phase"], blocked["unfinished"], blocked["failed"]) == ("days", 0, 1)
    assert [job["failure_category"] for job in blocked["failed_blockers"]] == [
        "invalid_work_input"
    ]
    assert (boards["phase"], boards["boards_rebuilding"]) == ("boards", 1)
    assert [board["correction"] for board in boards["boards"]] == ["queued"]
    assert receipt["boards_queued_at"] is not None
    assert receipt["days"]["before"] == {day: {"states": {"Complete": 1}, "reasons": {}}}
    assert receipt["days"]["now"] == receipt["days"]["before"]
    before = receipt["boards"]["before"][reset]
    assert (before["confirmed"], before["rule"]) == (1, SNAPSHOT_ORDERING_RULE_VERSION)
    # Until it is rebuilt, the board still disagrees with its days.
    assert [board["late_battles"] for board in receipt["boards_disagreeing"]] == [1]
    assert receipt["summaries_disagreeing"] == {
        "stored": 0, "stale": 0, "stale_players": [],
    }
    assert receipt["rule_revision"] == domain_repair.DAY_RULES_REVISION
    # A Season in progress has no summaries to store again.
    assert (done["phase"], done["summaries_refreshed"]) == ("done", 0)


def test_season_repair_stores_again_a_summary_a_board_correction_changed(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Summaries are stored again one player per run, oldest player first.
    After the first player's run, a correction of the Season's last board
    moves it from first to second: the repair stores its summary again
    before it reports done, and the receipt finds none differing."""
    from test_player_season_summaries_postgres import (
        _frozen_board,
        _log,
        _player,
        _ranked,
    )

    from clashlens.domain import ranked_day_for
    from clashlens.season_summaries import materialize_player_season

    season = ranked_day_for(datetime(2026, 7, 15, 6, tzinfo=UTC))
    season_id = season.official_season_id
    monkeypatch.setattr(domain_repair, "_repair_inputs", lambda *_: 0)
    # As once every board of the Season has been rebuilt.
    monkeypatch.setattr(boundary, "queue_board_rebuilds", lambda *_, **__: {"boards": []})
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observation_id, _job = store_observation(
            connection_info, archive_server, occurrence_key="final-board",
            endpoint="profile", body=b"{}", observed_at=season.season_end,
            normalized_tag="#2PP",
        )
        with psycopg.connect(connection_info) as connection:
            first = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
            second = _player(connection, "#2QQ")
            for player in (first, second):
                for day in range(1, 29):
                    start = season.season_start + (day - 1) * DAY
                    version_id = _ranked(
                        connection, player, day, start, start + DAY, season=season_id
                    )
                    _log(connection, player, day, version_id, start, season=season_id)
            _frozen_board(
                connection, observation_id, {first: (1, None), second: (2, None)},
                reset=season.season_end,
            )
            connection.execute(
                """
                INSERT INTO season_repairs (
                    official_season_id, rule_revision, before_days, before_boards,
                    queued_through_player_id, boards_queued_at
                ) VALUES (%s, %s, '{}', '{}', %s, clock_timestamp())
                """,
                (season_id, domain_repair.DAY_RULES_REVISION, second),
            )
            connection.commit()
            for player in (first, second):
                materialize_player_season(connection, player, season_id)
                connection.commit()
        database = Database(connection_info)
        try:
            def run(action: str = "queue") -> dict:
                return domain_repair.season_repair(database, season_id, action, max_jobs=1)

            runs = [run()]
            with psycopg.connect(connection_info) as connection:
                # As a recovered last-day battle of the second player moves it up.
                connection.execute(
                    "UPDATE leaderboard_snapshots SET state = 'superseded'"
                    " WHERE snapshot_kind = 'frozen'"
                )
                _frozen_board(
                    connection, observation_id, {first: (2, None), second: (1, None)},
                    version=2, reset=season.season_end,
                )
            runs += [run(), run(), run()]
            receipt = run("receipt")
            with psycopg.connect(connection_info) as connection:
                ranks = connection.execute(
                    "SELECT player_id, final_rank FROM player_season_summaries"
                    " WHERE official_season_id = %s ORDER BY player_id",
                    (season_id,),
                ).fetchall()
        finally:
            database.close()

    assert [(r["phase"], r["summaries_refreshed"]) for r in runs] == [
        ("summaries", 1), ("summaries", 1), ("summaries", 1), ("done", 0),
    ]
    assert ranks == [(first, 2), (second, 1)]
    assert receipt["summaries_disagreeing"] == {
        "stored": 2, "stale": 0, "stale_players": [],
    }
