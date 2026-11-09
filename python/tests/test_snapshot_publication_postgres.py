from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import (
    _processor,
    domain_database,
    seed_battles,
    store_observation,
    text,
)
from snapshot_publication_postgres_support import (
    _prepare_snapshot_correction,
    _process_profile,
    _process_snapshot_and_analytics,
    _profile_body,
    _seed_snapshot_job,
)

from clashlens import api_leaderboard, snapshots
from clashlens.analytics import SNAPSHOT_ORDERING_RULE_VERSION
from clashlens.api_db import ApiDatabase
from clashlens.db import Database


def _process_malformed_profile(
    connection_info: str,
    archive_server,
    *,
    occurrence_key: str,
    tag: str,
    observed_at: datetime,
) -> int:
    observation_id, job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=occurrence_key,
        endpoint="profile",
        body=b"not-json",
        observed_at=observed_at,
        normalized_tag=tag,
    )
    database, processor = _processor(connection_info, archive_server)
    try:
        result = processor.process_job(job_id, owner=f"{occurrence_key}-worker")
        assert result is not None and result.outcome == "failed"
        assert result.category == "malformed_json"
    finally:
        database.close()
    return observation_id


def _process_rankings(
    connection_info: str,
    archive_server,
    *,
    occurrence_key: str,
    observed_at: datetime,
    first_tag: str = "#22",
) -> None:
    ranking_fixture = Path(__file__).parents[1] / "testdata" / "global_top_200_v1.json"
    payload = json.loads(ranking_fixture.read_bytes())
    original = payload["items"][0]["tag"]
    payload["items"][0]["tag"] = first_tag
    for item in payload["items"][1:]:
        if item["tag"] == first_tag:
            item["tag"] = original
            break
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    _observation_id, job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=occurrence_key,
        endpoint="global_player_rankings",
        body=body,
        observed_at=observed_at,
        normalized_tag=None,
    )
    database, processor = _processor(connection_info, archive_server)
    try:
        result = processor.process_job(job_id, owner=f"{occurrence_key}-worker")
        assert result is not None and result.outcome == "processed"
    finally:
        database.close()


def _snapshot_insert_calls(connection_info: str) -> int | None:
    try:
        with psycopg.connect(connection_info) as connection:
            return int(
                connection.execute(
                    """
                    SELECT COALESCE(sum(calls), 0)::bigint
                    FROM pg_stat_statements
                    WHERE query ILIKE '%INSERT INTO leaderboard_snapshot_entries%'
                    """
                ).fetchone()[0]
            )
    except (
        psycopg.errors.ObjectNotInPrerequisiteState,
        psycopg.errors.UndefinedTable,
    ):
        return None


def _publish_snapshot_population(
    connection_info: str,
    archive_server,
    count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, int]:
    observation_id, _job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"snapshot-set-based-{count}",
        endpoint="profile",
        body=_profile_body(trophies=6000),
        observed_at=datetime(2026, 8, 5, 4, tzinfo=UTC),
        normalized_tag="#SEED",
    )
    with psycopg.connect(connection_info) as connection:
        player_ids = [
            int(row[0])
            for row in connection.execute(
                """
                INSERT INTO players (normalized_tag, active)
                SELECT '#S' || %s::text || lpad(g::text, 6, '0'), false
                FROM generate_series(1, %s) AS g
                RETURNING id
                """,
                (count, count),
            ).fetchall()
        ]
        boundary = datetime(2026, 8, 5, 5, tzinfo=UTC) + timedelta(days=count)
        entries = [
            {
                "player_id": player_id,
                "profile_version_id": None,
                "trophies": 6000 - position,
                "observation_id": observation_id,
                "observed_at": boundary,
                "age_seconds": 0,
                "freshness": "fresh",
                "confidence": "confirmed",
                "tie_hash": f"{position:064x}",
                "official": None,
                "tag": f"#S{position:06d}",
            }
            for position, player_id in enumerate(player_ids, start=1)
        ]
        quality = {
            "eligible_population_count": count,
            "included_entry_count": count,
            "stale_entry_count": 0,
            "fresh_entry_count": count,
            "excluded_missing_count": 0,
            "excluded_unavailable_count": 0,
            "excluded_invalid_count": 0,
            "excluded_malformed_count": 0,
            "excluded_partial_count": 0,
            "excluded_inconsistent_count": 0,
            "excluded_conflicting_count": 0,
        }
        calls = [0]
        original_execute = psycopg.Cursor.execute

        def counted_execute(cursor, *args, **kwargs):
            calls[0] += 1
            return original_execute(cursor, *args, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", counted_execute)
        database = Database(connection_info)
        try:
            before_pg = _snapshot_insert_calls(connection_info)
            before_app = calls[0]
            snapshots._publish_snapshot_kind(database,
                connection,
                snapshot_kind="frozen",
                boundary_at=boundary,
                ranked_day_version_id=None,
                entries=entries,
                coverage=1.0,
                quality=quality,
                input_hash=f"{count:064x}",
                ordering_rule_version=SNAPSHOT_ORDERING_RULE_VERSION,
            )
            connection.commit()
        finally:
            database.close()
        after_pg = _snapshot_insert_calls(connection_info)
        return (
            calls[0] - before_app,
            None if before_pg is None or after_pg is None else after_pg - before_pg,
        )


def test_snapshot_entry_statements_are_bounded_as_population_grows(
    database_url: str,
    archive_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with domain_database(database_url) as connection_info:
        small_app, small_postgres = _publish_snapshot_population(
            connection_info, archive_server, 2, monkeypatch
        )
        large_app, large_postgres = _publish_snapshot_population(
            connection_info, archive_server, 5, monkeypatch
        )
        assert small_app <= 10 and large_app <= 10
        assert large_app <= small_app + 1
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM leaderboard_snapshot_entries"
                ).fetchone()[0]
                == 7
            )
        if small_postgres is not None:
            assert small_postgres <= 1
        if large_postgres is not None:
            assert large_postgres <= 1
        if small_postgres is None or large_postgres is None:
            pytest.skip(
                "pg_stat_statements is unavailable; PostgreSQL statement ceilings not asserted"
            )
        assert large_postgres <= small_postgres + 1


def test_snapshot_uses_only_profile_evidence_observed_at_or_before_boundary(
    database_url: str,
    archive_server,
) -> None:
    boundary = datetime(2026, 8, 5, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        old_observation_id, old_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="snapshot-as-of-old",
            endpoint="profile",
            body=_profile_body(trophies=6123),
            observed_at=boundary - timedelta(hours=1),
            normalized_tag="#2PP",
        )
        _future_observation_id, future_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="snapshot-as-of-future",
            endpoint="profile",
            body=_profile_body(trophies=7000),
            observed_at=boundary + timedelta(hours=1),
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert (
                processor.process_job(old_job_id, owner="snapshot-as-of-old")
                is not None
            )
            assert (
                processor.process_job(future_job_id, owner="snapshot-as-of-future")
                is not None
            )
            with database.pool.connection() as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
            snapshot_job_id = _seed_snapshot_job(
                connection_info,
                player_id=player_id,
                boundary_at=boundary,
            )

            _process_snapshot_and_analytics(
                connection_info,
                database,
                processor,
                snapshot_job_id,
                owner_prefix="snapshot-as-of-builder",
            )
            with database.pool.connection() as connection:
                entry = connection.execute(
                    """
                    SELECT e.trophies, e.trophy_observation_id,
                           e.trophy_observed_at, e.observation_age_seconds,
                           e.freshness
                    FROM leaderboard_snapshot_entries AS e
                    JOIN leaderboard_snapshots AS s ON s.id = e.snapshot_id
                    WHERE s.snapshot_kind = 'frozen'
                      AND s.state = 'published'
                    """
                ).fetchone()
            assert entry is not None
            assert entry[0] == 6123
            assert entry[1] == old_observation_id
            assert entry[2] == boundary - timedelta(hours=1)
            assert entry[3] == 3600
            assert text(entry[4]) == "stale"
        finally:
            database.close()


def test_snapshot_orders_with_stable_hash_and_persists_temporal_provenance(
    database_url: str,
    archive_server,
) -> None:
    boundary = datetime(2026, 8, 5, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        old_id = _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-provenance-old",
            tag="#2PP",
            trophies=6123,
            observed_at=boundary - timedelta(hours=1),
        )
        fresh_id = _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-provenance-fresh",
            tag="#28",
            trophies=6123,
            observed_at=boundary - timedelta(seconds=30),
        )
        _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-provenance-post-boundary",
            tag="#2PP",
            trophies=7000,
            observed_at=boundary + timedelta(hours=1),
            tier_id=105000035,
            tier_name="Legend II",
        )
        _process_rankings(
            connection_info,
            archive_server,
            occurrence_key="snapshot-provenance-official",
            observed_at=boundary - timedelta(minutes=2),
            first_tag="#2PP",
        )
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
        snapshot_job_id = _seed_snapshot_job(
            connection_info,
            player_id=player_id,
            boundary_at=boundary,
            end_trophies=6123,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            first_snapshot_id, first_analytics_job_id = _process_snapshot_and_analytics(
                connection_info,
                database,
                processor,
                snapshot_job_id,
                owner_prefix="snapshot-provenance",
            )
            with database.pool.connection() as connection:
                snapshots = connection.execute(
                    """
                    SELECT id, snapshot_kind, boundary_at, version,
                           correction_of_id, ordering_rule_version,
                           freshness_rule_version, input_hash,
                           eligible_population_count, included_entry_count,
                           stale_entry_count, fresh_entry_count,
                           excluded_missing_count, excluded_invalid_count,
                           excluded_malformed_count, excluded_conflicting_count,
                           measured_coverage
                    FROM leaderboard_snapshots
                    WHERE boundary_at = %s
                    ORDER BY snapshot_kind
                    """,
                    (boundary,),
                ).fetchall()
                entries = connection.execute(
                    """
                    SELECT s.snapshot_kind, e.position, p.normalized_tag,
                           e.trophies, e.trophy_observation_id,
                           e.profile_observation_id, e.profile_observed_at,
                           e.profile_age_seconds, e.profile_freshness,
                           e.profile_confidence, e.tie_hash,
                           e.official_rank, e.official_rank_version_id,
                           e.official_rank_observed_at
                    FROM leaderboard_snapshot_entries AS e
                    JOIN leaderboard_snapshots AS s ON s.id = e.snapshot_id
                    JOIN players AS p ON p.id = e.player_id
                    WHERE s.boundary_at = %s AND s.snapshot_kind = 'frozen'
                    ORDER BY e.position
                    """,
                    (boundary,),
                ).fetchall()
            assert len(snapshots) == 2
            assert [text(row[5]) for row in snapshots] == [
                "tracked-player-order-v3",
                "tracked-player-order-v3",
            ]
            for row in snapshots:
                assert text(row[6]) == "profile-freshness-10m-v1"
                assert len(row[7]) == 64
                assert row[8:16] == (2, 2, 1, 1, 0, 0, 0, 0)
                assert row[16] == 1
            assert {text(row[2]) for row in entries} == {"#2PP", "#28"}
            # Independently computed MD5s of the ASCII tags: #2PP sorts first.
            assert [text(row[2]) for row in entries] == ["#2PP", "#28"]
            by_tag = {text(row[2]): row for row in entries}
            assert (
                by_tag["#2PP"][4],
                by_tag["#2PP"][5],
                by_tag["#2PP"][6],
                by_tag["#2PP"][7],
                text(by_tag["#2PP"][8]),
                text(by_tag["#2PP"][9]),
            ) == (
                old_id,
                old_id,
                boundary - timedelta(hours=1),
                3600,
                "stale",
                "confirmed",
            )
            assert (
                by_tag["#28"][4],
                by_tag["#28"][5],
                by_tag["#28"][6],
                by_tag["#28"][7],
                text(by_tag["#28"][8]),
                text(by_tag["#28"][9]),
            ) == (
                fresh_id,
                fresh_id,
                boundary - timedelta(seconds=30),
                30,
                "fresh",
                # No Legend day proves the battles after this reading.
                "uncertain",
            )
            assert text(by_tag["#2PP"][10]) == "5ef05df65a25836932d2187d9ec0da8d"
            assert text(by_tag["#28"][10]) == "f12263d6bada2125758956917e66fe8d"
            assert by_tag["#2PP"][11] == 1
            assert by_tag["#2PP"][13] == boundary - timedelta(minutes=2)

            database.requeue_completed_job(snapshot_job_id)
            replay = processor.process_job(snapshot_job_id, owner="snapshot-replay")
            assert replay is not None and replay.outcome == "processed"
            with database.pool.connection() as connection:
                replay_counts = connection.execute(
                    """
                    SELECT count(*),
                           (SELECT count(*) FROM leaderboard_snapshot_entries)
                    FROM leaderboard_snapshots
                    WHERE boundary_at = %s
                    """,
                    (boundary,),
                ).fetchone()
            assert replay_counts == (2, 4)

            _process_profile(
                connection_info,
                archive_server,
                occurrence_key="snapshot-provenance-after-boundary",
                tag="#28",
                trophies=9000,
                observed_at=boundary + timedelta(hours=2),
            )
            database.requeue_completed_job(snapshot_job_id)
            post_boundary = processor.process_job(
                snapshot_job_id, owner="snapshot-post-boundary-replay"
            )
            assert post_boundary is not None and post_boundary.outcome == "processed"
            with database.pool.connection() as connection:
                post_hashes = connection.execute(
                    """
                    SELECT snapshot_kind, input_hash
                    FROM leaderboard_snapshots
                    WHERE boundary_at = %s
                    ORDER BY snapshot_kind
                    """,
                    (boundary,),
                ).fetchall()
            assert [row[1] for row in post_hashes] == [snapshots[0][7], snapshots[1][7]]

            _process_profile(
                connection_info,
                archive_server,
                occurrence_key="snapshot-provenance-late-correction",
                tag="#28",
                trophies=6500,
                observed_at=boundary - timedelta(seconds=1),
            )
            database.requeue_completed_job(snapshot_job_id)
            corrected_snapshot_id, corrected_analytics_job_id = (
                _process_snapshot_and_analytics(
                    connection_info,
                    database,
                    processor,
                    snapshot_job_id,
                    owner_prefix="snapshot-correction",
                )
            )
            with database.pool.connection() as connection:
                corrected_rows = connection.execute(
                    """
                    SELECT id, snapshot_kind, version, correction_of_id, input_hash
                    FROM leaderboard_snapshots
                    WHERE boundary_at = %s
                    ORDER BY snapshot_kind, version
                    """,
                    (boundary,),
                ).fetchall()
            assert len(corrected_rows) == 4
            assert corrected_rows[0][3] is None
            assert corrected_rows[1][3] == corrected_rows[0][0]
            assert corrected_rows[1][4] != corrected_rows[0][4]
            assert corrected_rows[2][3] is None
            assert corrected_rows[3][3] == corrected_rows[2][0]
            assert corrected_rows[3][4] != corrected_rows[2][4]
            assert all(text(row[1]) in {"frozen", "live"} for row in corrected_rows)
            assert (
                database.scalar(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                )
                == 1
            )
            assert (
                database.scalar(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_analytics'"
                )
                == 2
            )
            assert corrected_snapshot_id != first_snapshot_id
            assert corrected_analytics_job_id != first_analytics_job_id
        finally:
            database.close()


@pytest.mark.parametrize("frozen_under", ["current", "season-attacks", "tag-hash"])
def test_daily_board_orders_equal_trophies_by_latest_trophy_change(
    database_url: str,
    archive_server,
    frozen_under: str,
) -> None:
    """From saved profile responses and battles to the published Daily
    board's order and Tie marks. Inputs frozen under an earlier rule keep it."""
    boundary = datetime(2026, 8, 5, 5, tzinfo=UTC)
    # The MD5 tag hash puts ``first`` ahead. The older rule's SHA-256 tag
    # hash agrees for #28, so only the latest change puts ``second`` ahead;
    # for #8PY it puts ``second`` ahead, so only that hash does.
    first, second = "#2PP", "#8PY" if frozen_under == "tag-hash" else "#28"
    with domain_database(database_url) as connection_info:
        for tag, trophies in ((first, 6123), (second, 6123), ("#2PY", 6200)):
            _process_profile(
                connection_info,
                archive_server,
                occurrence_key=f"tie-order-{tag}",
                tag=tag,
                trophies=trophies,
                observed_at=boundary - timedelta(hours=1),
            )
        with psycopg.connect(connection_info) as connection:
            ids = dict(connection.execute("SELECT normalized_tag, id FROM players").fetchall())
            seed_battles(connection, ids[second], [(boundary - timedelta(hours=2), -20)])
            # Later, but moving no trophies, or ending after the Reset
            # although the ended day counts it.
            seed_battles(
                connection,
                ids[first],
                [
                    (boundary - timedelta(hours=3), 30),
                    (boundary - timedelta(minutes=90), 0),
                    (boundary + timedelta(minutes=1), 40),
                ],
            )
        snapshot_job_id = _seed_snapshot_job(
            connection_info, player_id=ids[first], boundary_at=boundary, end_trophies=6123,
            ordering_rule_version=(
                "tracked-player-order-v1"
                if frozen_under == "tag-hash"
                else SNAPSHOT_ORDERING_RULE_VERSION
            ),
        )
        if frozen_under != "current":
            # An input list frozen before latest trophy changes holds Season
            # attacks, or before those, neither.
            with psycopg.connect(connection_info) as connection:
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute(
                    """
                    UPDATE boundary_publication_manifest_rows
                    SET input_identity = input_identity - 'latest_change_at'
                        || CASE WHEN %s THEN jsonb_build_object(
                               'season_attacks',
                               jsonb_build_object('attacks', 1, 'destruction',
                                                  CASE WHEN player_id = %s THEN 90
                                                       ELSE 50 END))
                           ELSE '{}'::jsonb END
                    """,
                    (frozen_under == "season-attacks", ids[first]),
                )
        database, processor = _processor(connection_info, archive_server)
        try:
            _process_snapshot_and_analytics(
                connection_info, database, processor, snapshot_job_id, owner_prefix="tie-order"
            )
            with database.pool.connection() as connection:
                frozen = dict(
                    connection.execute(
                        """
                        SELECT player_id, input_identity->>'latest_change_at'
                        FROM boundary_publication_manifest_entries((
                            SELECT snapshot_manifest_id
                            FROM boundary_publication_generations
                            WHERE boundary_at = %s
                        ))
                        """,
                        (boundary,),
                    ).fetchall()
                )
            api = ApiDatabase(connection_info)
            try:
                board = api_leaderboard.get_frozen_leaderboard(api, limit=10, now=boundary)
            finally:
                api.close()
        finally:
            database.close()
    assert board is not None
    order = [(entry["tag"], entry["trophies"], entry["tied"]) for entry in board["entries"]]
    if frozen_under == "tag-hash":
        # Ordered and labelled by the older rule, so the board rebuild check
        # rebuilds it.
        assert order == [("#2PY", 6200, False), (second, 6123, True), (first, 6123, True)]
        assert board["ordering_rule_version"] == "tracked-player-order-v1"
        return
    if frozen_under == "season-attacks":
        assert order == [("#2PY", 6200, False), (first, 6123, True), (second, 6123, True)]
        assert board["ordering_rule_version"] == "tracked-player-order-v2"
        return
    assert order == [("#2PY", 6200, False), (second, 6123, True), (first, 6123, True)]
    assert [entry["position"] for entry in board["entries"]] == [1, 2, 3]
    assert board["ordering_rule_version"] == SNAPSHOT_ORDERING_RULE_VERSION
    assert frozen == {
        # A defense seen only in the defender's report ends 155 seconds later.
        ids[second]: (boundary - timedelta(hours=2, seconds=-155)).isoformat(),
        ids[first]: (boundary - timedelta(hours=3)).isoformat(),
        ids["#2PY"]: None,
    }


def test_snapshot_quality_counts_and_a_replacement_published_whole(
    database_url: str,
    archive_server,
) -> None:
    boundary = datetime(2026, 8, 5, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-quality-fresh",
            tag="#2PP",
            trophies=6100,
            observed_at=boundary - timedelta(minutes=1),
        )
        _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-quality-stale",
            tag="#28",
            trophies=6099,
            observed_at=boundary - timedelta(hours=1),
        )
        invalid_id = _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-quality-invalid",
            tag="#29",
            trophies=6098,
            observed_at=boundary - timedelta(minutes=2),
        )
        conflict_id = _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-quality-conflict",
            tag="#2L",
            trophies=6097,
            observed_at=boundary - timedelta(minutes=3),
            tier_id=999999999,
            tier_name="Unexpected Tier",
        )
        malformed_id = _process_malformed_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-quality-malformed",
            tag="#2Y",
            observed_at=boundary - timedelta(minutes=4),
        )
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE player_profile_versions SET eligibility_state = 'uncertain' WHERE observation_id = %s",
                (invalid_id,),
            )
            connection.execute(
                """
                INSERT INTO players (normalized_tag, active, eligibility_state)
                VALUES ('#2U', true, 'eligible')
                """
            )
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
            connection.commit()
        snapshot_job_id = _seed_snapshot_job(
            connection_info,
            player_id=player_id,
            boundary_at=boundary,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            _process_snapshot_and_analytics(
                connection_info,
                database,
                processor,
                snapshot_job_id,
                owner_prefix="snapshot-quality",
            )
            with database.pool.connection() as connection:
                quality = connection.execute(
                    """
                    SELECT eligible_population_count, included_entry_count,
                           stale_entry_count, fresh_entry_count,
                           excluded_missing_count, excluded_invalid_count,
                           excluded_malformed_count, excluded_conflicting_count,
                           measured_coverage
                    FROM leaderboard_snapshots
                    WHERE snapshot_kind = 'frozen' AND state = 'published'
                    """
                ).fetchone()
                assert quality is not None
                assert quality[:8] == (2, 2, 1, 1, 1, 1, 1, 1)
                assert quality[8] == 1
                old_snapshot = connection.execute(
                    """
                    SELECT id, boundary_at, version, state
                    FROM leaderboard_snapshots
                    WHERE snapshot_kind = 'frozen' AND state = 'published'
                    ORDER BY version DESC LIMIT 1
                    """
                ).fetchone()
                assert old_snapshot is not None
                connection.commit()
            _process_profile(
                connection_info,
                archive_server,
                occurrence_key="snapshot-quality-late-correction",
                tag="#28",
                trophies=6101,
                observed_at=boundary - timedelta(seconds=30),
            )
            database.requeue_completed_job(snapshot_job_id)
            _prepare_snapshot_correction(
                connection_info,
                database,
                snapshot_job_id=snapshot_job_id,
            )
            correction = processor.process_job(
                snapshot_job_id,
                owner="snapshot-quality-correction",
            )
            assert correction is not None and correction.outcome == "processed"
            with database.pool.connection() as connection:
                candidates = connection.execute(
                    """
                    SELECT version, state, correction_of_id
                    FROM leaderboard_snapshots
                    WHERE snapshot_kind = 'frozen' AND boundary_at = %s
                    ORDER BY version
                    """,
                    (boundary,),
                ).fetchall()
            # The replacement publishes whole at the end of its build.
            assert len(candidates) == 2
            assert text(candidates[0][1]) == "superseded"
            assert text(candidates[1][1]) == "published"
            assert candidates[1][2] == old_snapshot[0]
            api = ApiDatabase(connection_info)
            try:
                frozen = api_leaderboard.get_frozen_leaderboard(api, limit=10)
            finally:
                api.close()
            assert frozen is not None
            assert frozen["boundary_at"] == boundary.isoformat()
            assert frozen["version"] == old_snapshot[2] + 1
            assert frozen["entries"]
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="published leaderboard snapshots are immutable",
            ):
                with database.pool.connection() as connection:
                    with connection.transaction():
                        connection.execute(
                            "UPDATE leaderboard_snapshots SET boundary_at = boundary_at + interval '1 second' WHERE id = %s",
                            (old_snapshot[0],),
                        )
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="leaderboard snapshot entries are immutable",
            ):
                with database.pool.connection() as connection:
                    with connection.transaction():
                        connection.execute(
                            """
                            UPDATE leaderboard_snapshot_entries
                            SET trophies = trophies + 1
                            WHERE snapshot_id = %s AND position = 1
                            """,
                            (old_snapshot[0],),
                        )
            with psycopg.connect(connection_info) as connection:
                malformed_outcome = connection.execute(
                    """
                    SELECT outcome FROM observation_processing_outcomes
                    WHERE observation_id = %s
                    """,
                    (malformed_id,),
                ).fetchone()[0]
                assert text(malformed_outcome) == "malformed"
                conflict_state = connection.execute(
                    """
                    SELECT source_contract_state FROM player_profile_versions
                    WHERE observation_id = %s
                    """,
                    (conflict_id,),
                ).fetchone()[0]
                assert text(conflict_state) == "conflict"
        finally:
            database.close()


OLD_SEASON, NEW_SEASON = 1783918800, 1786338000  # Seasons around August 10.


@pytest.mark.parametrize("boundary_at,season_id,included", [
    # The last day of a Season keeps its pre-Reset values.
    (datetime(2026, 8, 10, 5, tzinfo=UTC), OLD_SEASON, True),
    # Day 1 of the new Season never shows the old Season's trophies.
    (datetime(2026, 8, 11, 5, tzinfo=UTC), OLD_SEASON, False),
    (datetime(2026, 8, 11, 5, tzinfo=UTC), NEW_SEASON, True),
])
def test_daily_snapshot_leaves_out_profiles_naming_another_season(
    database_url: str, archive_server, boundary_at, season_id, included
) -> None:
    payload = json.loads(_profile_body(trophies=6400))
    payload["currentLeagueSeasonId"] = season_id
    payload["previousLeagueSeasonId"] = season_id - 28 * 24 * 60 * 60
    with domain_database(database_url) as connection_info:
        _observation_id, job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="snapshot-season",
            endpoint="profile",
            body=json.dumps(payload).encode(),
            observed_at=boundary_at - timedelta(hours=1),
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_job(job_id, owner="snapshot-season") is not None
            with database.pool.connection() as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
            snapshot_job_id = _seed_snapshot_job(
                connection_info, player_id=player_id, boundary_at=boundary_at
            )
            result = processor.process_job(snapshot_job_id, owner="snapshot-season")
            assert result is not None and result.outcome == "processed"
            with database.pool.connection() as connection:
                entries = connection.execute(
                    """
                    SELECT e.trophies
                    FROM leaderboard_snapshot_entries AS e
                    JOIN leaderboard_snapshots AS s ON s.id = e.snapshot_id
                    WHERE s.snapshot_kind = 'frozen'
                    """
                ).fetchall()
                quality = connection.execute(
                    """
                    SELECT input_identity->>'snapshot_quality'
                    FROM boundary_publication_manifest_rows
                    """
                ).fetchall()
        finally:
            database.close()
    assert entries == ([(6400,)] if included else [])
    assert quality == [("eligible" if included else "season_reset_pending",)]
