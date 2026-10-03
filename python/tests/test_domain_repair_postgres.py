"""A Season's repair campaign lists each result its fixes change once, writes
nothing on preview, closes with the correction window and, once active,
holds only the Resets it lists."""

from __future__ import annotations

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
from clashlens.domain import HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION

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
    observation_id: int = 0,
) -> int:
    """Save one selected attack report on ``day`` and return its id."""
    battle_id = connection.execute(
        "INSERT INTO legend_battles (ranked_day_start, attacker_player_id,"
        " defender_player_id) VALUES (%s, %s, %s) RETURNING id",
        (day, reporter, opponent),
    ).fetchone()[0]
    evidence_id = connection.execute(
        """
        INSERT INTO battle_evidence (
            battle_id, source_row_id, observation_id, reporting_player_id,
            perspective, battle_timestamp, stars, destruction_percentage,
            army_share_code, attacker_gain, defender_loss, trophy_rule_version,
            source_observed_at, parser_version
        ) VALUES (%s, %s, %s, %s, 'attacker', %s, 2, %s, %s, 18, 18, %s, %s,
                  'supercell-source-parser-v2')
        RETURNING id
        """,
        (battle_id, battle_id, observation_id, reporter, at or day + timedelta(hours=2),
         destruction, code, HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION, day + DAY),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO battle_perspectives (battle_id, perspective, evidence_id,"
        " source_observed_at) VALUES (%s, 'attacker', %s, %s)",
        (battle_id, evidence_id, day + DAY),
    )
    return evidence_id


def _saved_day(
    connection, player_id: int, day: datetime, season: str = SEASON, version: int = 1,
    *, state: str = "Complete", attacks: int = 0, defenses: int = 0,
) -> None:
    connection.execute(
        """
        INSERT INTO ranked_day_versions (
            player_id, ranked_day_start, ranked_day_end, official_season_id,
            season_day_number, season_anchor_rule_version,
            reconciliation_rule_version, result_hash, version, state, confidence,
            input_hash, evidence_complete, coverage_complete, attack_count,
            defense_count
        ) VALUES (%s, %s, %s, %s, 1, 'legend-season-anchor-v1', 'test', %s, %s,
                  %s, 'exact', %s, true, true, %s, %s)
        """,
        (player_id, day, day + DAY, season, f"{version:x}" * 64, version, state,
         "a" * 64, attacks, defenses),
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


def test_campaign_includes_closing_boundary_and_next_season_dependency(
    database_url: str,
) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        with _owner(connection_info) as connection:
            player, opponent = _player(connection, "#LAST"), _player(connection, "#OTHER")
            day28 = END - DAY
            _report(connection, player, opponent, day28)
            _saved_day(connection, player, day28)
            # The next Season's first day only attacked, so its result does not
            # read day 28's and its second day cannot change.
            _saved_day(connection, player, END, NEXT_SEASON, attacks=1)
            _saved_day(connection, player, END + DAY, NEXT_SEASON)
        domain_repair.register(worker, SEASON, now=NOW)
        # Day 28 ends at the October 5 Reset; the next Season's first day
        # starts from it, and only that day of the next Season is listed.
        assert [row[0] for row in _items(connection_info, "publication")] == [
            _key("boundary", END), _key("boundary", END + DAY)
        ]
        days = _items(connection_info, "day")
        assert [(row[0], row[4]) for row in days] == [
            (_key("day", player, day28), SEASON), (_key("day", player, END), NEXT_SEASON)
        ]
        # After September's window closes, the October day is still open.
        late = domain_repair.preview(worker, SEASON, now=DEADLINE)
        assert not late["open"]
        assert late["excluded"] == {"raw_unavailable": 1, "window_expired": 2}
        assert late["items"] == {"source": 1, "day": 2, "publication": 2}


def test_campaign_follows_next_season_days_that_can_change(database_url: str) -> None:
    with _campaign_database(database_url) as (connection_info, worker):
        with _owner(connection_info) as connection:
            player, opponent = _player(connection, "#CHAIN"), _player(connection, "#IDLE")
            day28 = END - DAY
            _report(connection, player, opponent, day28)
            _saved_day(connection, player, day28)
            # Day 1's one defense takes its automatic loss from day 28's
            # defenses, so day 28's 17 can make it Complete, which day 2's
            # automatic loss needs. Day 2 only attacked, so day 3 cannot change.
            _saved_day(connection, player, END, NEXT_SEASON, state="Partial", defenses=1)
            _saved_day(connection, player, END + DAY, NEXT_SEASON, attacks=1)
            _saved_day(connection, player, END + 2 * DAY, NEXT_SEASON)
            # Days with no battles carry the chain, but never past the window.
            _report(connection, opponent, player, day28)
            for day in range(9):
                _saved_day(connection, opponent, day28 + day * DAY,
                           SEASON if day == 0 else NEXT_SEASON)
        domain_repair.register(worker, SEASON, now=NOW)
        days = [row[0] for row in _items(connection_info, "day")]
        assert sorted(days) == sorted(
            [_key("day", player, day) for day in (day28, END, END + DAY)]
            + [_key("day", opponent, day28 + day * DAY) for day in range(8)]
        )
        # The last listed Reset is the window's close.
        assert {row[0] for row in _items(connection_info, "publication")} == {
            _key("boundary", END + day * DAY) for day in range(8)
        }


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
