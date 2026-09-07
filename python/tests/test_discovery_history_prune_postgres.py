from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from psycopg.pq import TransactionStatus
from test_domain_processing_postgres import _processor

from clashlens.domain import SEASON_ANCHOR_RULE_VERSION
from clashlens.history import prune_completed_history

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
BATTLE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"
RANKING_FIXTURE = Path(__file__).parents[1] / "testdata" / "global_top_200_v1.json"

OLD = datetime(2026, 6, 20, 6, tzinfo=UTC)
BOUNDARY = datetime(2026, 7, 15, 5, tzinfo=UTC)
LIVE = datetime(2026, 8, 5, 6, tzinfo=UTC)


def _confirm_anchor(connection_info, archive_server, *, current_start=BOUNDARY):
    observation_id, _job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"anchor-{current_start.isoformat()}",
        endpoint="profile",
        body=PROFILE_FIXTURE.read_bytes(),
        observed_at=current_start,
        normalized_tag="#ANCHOR",
    )
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = '#ANCHOR'"
        ).fetchone()[0]
        profile_id = connection.execute(
            """
            INSERT INTO player_profile_versions (
                player_id, observation_id, normalized_tag, endpoint_version,
                schema_version, parser_version, observed_at, source_http_status,
                name, trophies, league_tier_id, league_tier_name,
                eligibility_state, profile_json
            ) VALUES (
                %s, %s, '#ANCHOR', 'profile-v1', 'profile-schema-v1',
                'profile-parser-v1', %s, 200, 'Anchor', 6000, 105000036,
                'Legend I', 'eligible', '{}'::jsonb
            )
            RETURNING id
            """,
            (player_id, observation_id, current_start),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO legend_season_anchors (
                current_league_season_id, previous_league_season_id,
                current_start, previous_start, anchor_rule_version,
                source_profile_version_id, state
            ) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed')
            """,
            (
                "season-now",
                "season-prev",
                current_start,
                current_start - timedelta(days=28),
                SEASON_ANCHOR_RULE_VERSION,
                profile_id,
            ),
        )
        connection.commit()


def _age_jobs(connection_info):
    with psycopg.connect(connection_info) as connection:
        connection.execute("UPDATE collector_jobs SET updated_at = %s", (OLD,))
        connection.execute(
            "UPDATE python_processing_jobs SET updated_at = %s", (OLD,)
        )
        connection.commit()


def _seed_discovery(connection, observation_id, tag, *, kind="battle_opponent", index=0):
    player_id = connection.execute(
        """
        INSERT INTO players (normalized_tag, active, next_due_at)
        VALUES (%s, false, NULL)
        ON CONFLICT (normalized_tag) DO UPDATE
            SET normalized_tag = EXCLUDED.normalized_tag
        RETURNING id
        """,
        (tag,),
    ).fetchone()[0]
    return connection.execute(
        """
        INSERT INTO known_player_discoveries (
            player_id, observation_id, source_row_index, source_kind, discovered_at
        ) VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (player_id, observation_id, index, kind, OLD),
    ).fetchone()[0]


def _process(connection_info, archive_server, occurrence_key, *, tag="#2PP"):
    observation_id, job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=occurrence_key,
        endpoint="battle_log",
        body=BATTLE_FIXTURE.read_bytes(),
        observed_at=OLD,
        normalized_tag=tag,
    )
    database, processor = _processor(connection_info, archive_server)
    try:
        result = processor.process_job(job_id, owner="discovery-prune")
        assert result is not None and result.outcome == "processed"
    finally:
        database.close()
    return observation_id


def test_discovery_prune_preview_apply_and_rerun(database_url: str, archive_server) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        battle_observation, battle_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="prune-battle",
            endpoint="battle_log",
            body=BATTLE_FIXTURE.read_bytes(),
            observed_at=OLD,
            normalized_tag="#2PP",
        )
        store_observation(
            connection_info,
            archive_server,
            occurrence_key="prune-rankings",
            endpoint="global_player_rankings",
            body=RANKING_FIXTURE.read_bytes(),
            observed_at=OLD,
            normalized_tag=None,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_job(battle_job, owner="prune-battle") is not None
            assert processor.process_once(owner="prune-rankings") is not None
        finally:
            database.close()
        _confirm_anchor(connection_info, archive_server)
        _age_jobs(connection_info)
        with psycopg.connect(connection_info) as connection:
            before = connection.execute(
                """
                SELECT (SELECT count(*) FROM known_player_discoveries),
                       (SELECT count(*) FROM collector_jobs),
                       (SELECT count(*) FROM legend_battles),
                       (SELECT count(*) FROM battle_evidence),
                       (SELECT count(*) FROM official_top200_versions)
                """
            ).fetchone()
            assert before[0] == 201
            for invalid in (0, 1001):
                with pytest.raises(ValueError, match="max_discoveries"):
                    prune_completed_history(connection, max_discoveries=invalid)
            preview = prune_completed_history(connection, max_discoveries=2)
            assert preview["eligible_known_player_discoveries"] == 2
            assert preview["deleted_known_player_discoveries"] == 0
            assert preview["eligible_player_discovery_events"] == 0
            assert preview["deleted_player_discovery_events"] == 0
            assert connection.execute(
                "SELECT count(*) FROM known_player_discoveries"
            ).fetchone()[0] == before[0]
            partial = prune_completed_history(connection, max_discoveries=2, apply=True)
            assert partial["deleted_known_player_discoveries"] == 2
            remaining = (
                prune_completed_history(connection, apply=True)[
                    "deleted_known_player_discoveries"
                ]
            )
            assert remaining == before[0] - 2
            assert connection.execute(
                "SELECT count(*) FROM known_player_discoveries"
            ).fetchone()[0] == 0
            # Retained roots and semantic detail survive discovery cleanup.
            assert connection.execute(
                """
                SELECT (SELECT count(*) FROM collector_jobs),
                       (SELECT count(*) FROM legend_battles),
                       (SELECT count(*) FROM battle_evidence),
                       (SELECT count(*) FROM official_top200_versions)
                """
            ).fetchone() == before[1:]
            # Retained roots and semantic detail survive discovery cleanup.
            assert connection.execute(
                "SELECT count(*) FROM collector_observations WHERE id = %s",
                (battle_observation,),
            ).fetchone()[0] == 1
            rerun = prune_completed_history(connection, apply=True)
            assert rerun["eligible_known_player_discoveries"] == 0
            assert rerun["deleted_known_player_discoveries"] == 0


def test_discovery_prune_unknown_boundary_live_season_and_recent(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        old_observation = _process(connection_info, archive_server, "boundary-old")
        _age_jobs(connection_info)
        with psycopg.connect(connection_info) as connection:
            # Unknown season boundary fails closed.
            assert prune_completed_history(connection)[
                "eligible_known_player_discoveries"
            ] == 0
        _confirm_anchor(connection_info, archive_server)
        with psycopg.connect(connection_info) as connection:
            assert prune_completed_history(connection)[
                "eligible_known_player_discoveries"
            ] == 1
            # Recent roots are retained even when pre-season.
            connection.execute(
                "UPDATE collector_jobs SET updated_at = clock_timestamp()"
                " WHERE id = (SELECT collection_job_id FROM collector_observations WHERE id = %s)",
                (old_observation,),
            )
            assert prune_completed_history(connection, apply=True)[
                "eligible_known_player_discoveries"
            ] == 0
            connection.execute(
                "UPDATE collector_jobs SET updated_at = %s"
                " WHERE id = (SELECT collection_job_id FROM collector_observations WHERE id = %s)",
                (OLD, old_observation),
            )
            connection.commit()
        live_body = json.loads(BATTLE_FIXTURE.read_bytes())
        live_body["items"][0]["opponentPlayerTag"] = "#QPP"
        live_observation, live_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="boundary-live",
            endpoint="battle_log",
            body=json.dumps(live_body).encode(),
            observed_at=LIVE,
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            result = processor.process_job(live_job, owner="boundary-live")
            assert result is not None and result.outcome == "processed"
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_jobs SET updated_at = %s", (OLD,)
            )
            connection.execute(
                "UPDATE python_processing_jobs SET updated_at = %s", (OLD,)
            )
            connection.commit()
            preview = prune_completed_history(connection)
            assert preview["eligible_known_player_discoveries"] == 1
            applied = prune_completed_history(connection, apply=True)
            assert applied["deleted_known_player_discoveries"] == 1
            # Live-season discovery history is retained.
            assert connection.execute(
                "SELECT count(*) FROM known_player_discoveries WHERE observation_id = %s",
                (live_observation,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT count(*) FROM known_player_discoveries WHERE observation_id = %s",
                (old_observation,),
            ).fetchone()[0] == 0


def test_discovery_prune_preserves_unfinished_and_protected_work(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        clean = _process(connection_info, archive_server, "protect-clean", tag="#2PP")
        pending_observation, _pending_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="protect-pending",
            endpoint="battle_log",
            body=BATTLE_FIXTURE.read_bytes(),
            observed_at=OLD,
            normalized_tag="#8PP",
        )
        failed_observation, failed_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="protect-failed",
            endpoint="battle_log",
            body=BATTLE_FIXTURE.read_bytes(),
            observed_at=OLD,
            normalized_tag="#9PP",
            http_status=500,
        )
        child = _process(connection_info, archive_server, "protect-child", tag="#QPP")
        transport = _process(
            connection_info, archive_server, "protect-transport", tag="#CPP"
        )
        baseline = _process(
            connection_info, archive_server, "protect-baseline", tag="#VPP"
        )
        replay = _process(connection_info, archive_server, "protect-replay", tag="#GPP")
        recent = _process(connection_info, archive_server, "protect-recent", tag="#JPP")
        recent_job = _process(
            connection_info, archive_server, "protect-recent-job", tag="#LPP"
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            failed = processor.process_job(failed_job, owner="protect-failed")
            assert failed is not None and failed.outcome != "processed"
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            attempt_id = connection.execute(
                "SELECT attempt_id FROM collector_observations WHERE id = %s",
                (child,),
            ).fetchone()[0]
            player_id = connection.execute(
                "SELECT player_id FROM collector_observations WHERE id = %s",
                (child,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO collector_jobs (
                    work_type, player_id, normalized_tag, scope, capacity_pool,
                    priority, due_at, coalescing_key, status, required_endpoint,
                    parent_attempt_id
                ) VALUES (
                    'initial_collection', %s, '#QPP', 'player', 'normal',
                    300, %s, 'protect-child-retry', 'pending', 'battle_log', %s
                )
                """,
                (player_id, OLD, attempt_id),
            )
            transport_attempt = connection.execute(
                "SELECT attempt_id, player_id FROM collector_observations WHERE id = %s",
                (transport,),
            ).fetchone()
            transport_player = transport_attempt[1]
            transport_attempt = transport_attempt[0]
            connection.execute(
                """
                INSERT INTO collector_transport_failures (
                    collection_job_id, attempt_id, player_id, normalized_tag,
                    endpoint, request_started_at, failed_at, failure_category,
                    retry_state, key_label, evidence_key
                ) VALUES (
                    (SELECT collection_job_id FROM collector_observations WHERE id = %s),
                    %s, %s, '#CPP', 'battle_log', %s, %s,
                    'timeout', 'retrying', 'normal-a', 'protect-transport:1'
                )
                """,
                (transport, transport_attempt, transport_player, OLD, OLD),
            )
            baseline_player = connection.execute(
                "SELECT player_id FROM collector_observations WHERE id = %s",
                (baseline,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO reset_baseline_evidence (
                    player_id, boundary_at, battle_log_observation_id
                ) VALUES (%s, %s, %s)
                """,
                (baseline_player, OLD, baseline),
            )
            connection.execute(
                """
                INSERT INTO python_replay_requests (
                    observation_id, operator_identity, reason,
                    target_parser_version, target_domain_rule_version
                ) VALUES (%s, 'test.op', 'retention probe',
                          'supercell-source-parser-v1', 'clashlens-domain-rules-v1')
                """,
                (replay,),
            )
            for observation_id, tag in [
                (pending_observation, "#P200"),
                (failed_observation, "#P201"),
                (child, "#P202"),
                (transport, "#P203"),
                (baseline, "#P204"),
                (replay, "#P205"),
                (recent, "#P206"),
                (recent_job, "#P207"),
            ]:
                _seed_discovery(connection, observation_id, tag, index=0)
            connection.commit()
        _confirm_anchor(connection_info, archive_server)
        _age_jobs(connection_info)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_jobs SET updated_at = clock_timestamp()"
                " WHERE id = (SELECT collection_job_id FROM collector_observations"
                " WHERE occurrence_key = 'protect-recent')"
            )
            connection.execute(
                "UPDATE python_processing_jobs SET updated_at = clock_timestamp()"
                " WHERE observation_id = %s",
                (recent_job,),
            )
            connection.commit()
            report = prune_completed_history(connection, apply=True)
            assert report["deleted_known_player_discoveries"] == 1
            survivors = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT o.occurrence_key FROM known_player_discoveries AS d
                    JOIN collector_observations AS o ON o.id = d.observation_id
                    """
                ).fetchall()
            }
            assert survivors == {
                "protect-pending",
                "protect-failed",
                "protect-child",
                "protect-transport",
                "protect-baseline",
                "protect-replay",
                "protect-recent",
                "protect-recent-job",
            }
            assert connection.execute(
                "SELECT count(*) FROM known_player_discoveries"
                " WHERE observation_id = %s",
                (clean,),
            ).fetchone()[0] == 0


def test_discovery_events_keep_sourceless_history(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observation = _process(connection_info, archive_server, "events-ranked")
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "SELECT player_id FROM collector_observations WHERE id = %s",
                (observation,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO player_discovery_events (
                    player_id, normalized_tag, source, source_observation_id,
                    deduplication_key, discovered_at
                ) VALUES
                    (%s, '#2PP', 'official_global_ranking', %s, 'prune-events:ranked', %s),
                    (%s, '#2PP', 'submitted_tag', NULL, 'prune-events:submitted', %s),
                    (%s, '#2PP', 'account_link', NULL, 'prune-events:linked', %s)
                """,
                (player_id, observation, OLD, player_id, OLD, player_id, OLD),
            )
            connection.commit()
        _confirm_anchor(connection_info, archive_server)
        _age_jobs(connection_info)
        with psycopg.connect(connection_info) as connection:
            preview = prune_completed_history(connection)
            assert preview["eligible_player_discovery_events"] == 1
            assert preview["deleted_player_discovery_events"] == 0
            applied = prune_completed_history(connection, apply=True)
            assert applied["deleted_player_discovery_events"] == 1
            assert [
                row[0]
                for row in connection.execute(
                    "SELECT source FROM player_discovery_events ORDER BY id"
                ).fetchall()
            ] == ["submitted_tag", "account_link"]
            assert prune_completed_history(connection, apply=True)[
                "eligible_player_discovery_events"
            ] == 0


def test_discovery_scheduling_and_replay_survive_cleanup(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observation = _process(connection_info, archive_server, "survive-first")
        _confirm_anchor(connection_info, archive_server)
        _age_jobs(connection_info)
        with psycopg.connect(connection_info) as connection:
            assert prune_completed_history(connection, apply=True)[
                "deleted_known_player_discoveries"
            ] == 1
        # New discoveries still schedule follow-up profiles.
        fresh_observation, fresh_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="survive-fresh",
            endpoint="battle_log",
            body=BATTLE_FIXTURE.read_bytes(),
            observed_at=LIVE,
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            result = processor.process_job(fresh_job, owner="survive-fresh")
            assert result is not None and result.outcome == "processed"
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM known_player_discoveries WHERE observation_id = %s",
                    (fresh_observation,),
                ).fetchone()[0] == 1
                assert connection.execute(
                    "SELECT count(*) FROM collector_jobs WHERE work_type = 'discovery_profile'"
                ).fetchone()[0] > 0
        finally:
            database.close()
        # Replay of a cleaned observation still processes and re-records provenance.
        with psycopg.connect(connection_info) as connection:
            parser_version = connection.execute(
                "SELECT parser_version FROM battle_log_observations WHERE observation_id = %s",
                (observation,),
            ).fetchone()[0]
            replay_job = connection.execute(
                """
                INSERT INTO python_processing_jobs (
                    replay_observation_id, work_type, deduplication_key, input_json,
                    parser_version, processing_version, domain_rule_version,
                    analytics_rule_version
                ) VALUES (
                    %s, 'replay_observation', %s, '{"replay_request_id": 1}'::jsonb,
                    %s, 'clashlens-domain-processing-v1', 'clashlens-domain-rules-v1',
                    'legend-analytics-v1'
                )
                RETURNING id
                """,
                (
                    observation,
                    f"survive-replay:{observation}:{parser_version}",
                    parser_version,
                ),
            ).fetchone()[0]
            connection.commit()
        database, processor = _processor(connection_info, archive_server)
        try:
            replayed = processor.process_job(replay_job, owner="survive-replay")
            assert replayed is not None and replayed.outcome == "processed"
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM known_player_discoveries WHERE observation_id = %s",
                    (observation,),
                ).fetchone()[0] == 1
        finally:
            database.close()


class _ReplayInterleaveConnection:
    """Proxy that commits replay work mid-cleanup, deterministically.

    The hook fires synchronously when the fencing observation lock is issued:
    strictly after candidate selection, strictly before the post-lock
    eligibility recheck. No threads, sleeps, or timing involved.
    """

    _FENCE_NEEDLE = "FOR UPDATE OF o"

    def __init__(self, real, hook):
        self.__dict__["_real"] = real
        self.__dict__["_hook"] = hook
        self.__dict__["_fired"] = False

    def execute(self, query, params=None, **kwargs):
        if (
            not self.__dict__["_fired"]
            and isinstance(query, str)
            and self._FENCE_NEEDLE in query
        ):
            self.__dict__["_fired"] = True
            self.__dict__["_hook"]()
        return self.__dict__["_real"].execute(query, params, **kwargs)

    def __getattr__(self, name):
        return getattr(self.__dict__["_real"], name)


def test_discovery_prune_replay_committed_before_recheck_preserves_rows(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observation = _process(connection_info, archive_server, "replay-race")
        _confirm_anchor(connection_info, archive_server)
        _age_jobs(connection_info)
        with psycopg.connect(connection_info) as connection:
            # Baseline: the row is an eligible candidate with no replay pending.
            # Commit to release the preview's row locks before the raced call.
            assert prune_completed_history(connection)[
                "eligible_known_player_discoveries"
            ] == 1
            connection.commit()
            fired = []

            def request_replay():
                fired.append(True)
                with psycopg.connect(connection_info) as replay_connection:
                    replay_connection.execute(
                        """
                        INSERT INTO python_replay_requests (
                            observation_id, operator_identity, reason,
                            target_parser_version, target_domain_rule_version
                        ) VALUES (%s, 'test.op', 'retention probe',
                                  'supercell-source-parser-v1', 'clashlens-domain-rules-v1')
                        """,
                        (observation,),
                    )
                    replay_connection.commit()

            with psycopg.connect(connection_info) as cleanup_connection:
                proxy = _ReplayInterleaveConnection(cleanup_connection, request_replay)
                report = prune_completed_history(proxy, apply=True)
            assert fired == [True]
            assert report["eligible_known_player_discoveries"] == 0
            assert report["deleted_known_player_discoveries"] == 0
            assert connection.execute(
                "SELECT count(*) FROM known_player_discoveries WHERE observation_id = %s",
                (observation,),
            ).fetchone()[0] == 1


def test_prune_preview_returns_idle_and_apply_visible_externally(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observation = _process(connection_info, archive_server, "boundaries-idle")
        _confirm_anchor(connection_info, archive_server)
        _age_jobs(connection_info)
        with psycopg.connect(connection_info) as connection:
            assert connection.autocommit is False
            preview = prune_completed_history(connection)
            assert preview["eligible_known_player_discoveries"] == 1
            assert preview["deleted_known_player_discoveries"] == 0
            # Preview leaves no open transaction behind.
            assert connection.info.transaction_status == TransactionStatus.IDLE
            row_id = connection.execute(
                "SELECT id FROM known_player_discoveries WHERE observation_id = %s",
                (observation,),
            ).fetchone()[0]
            # End the setup SELECT transaction: apply must commit on its own.
            connection.commit()
            # Another connection can immediately lock the candidate and its
            # observation while the preview connection remains open.
            with psycopg.connect(connection_info) as other:
                other.execute(
                    "SELECT id FROM known_player_discoveries WHERE id = %s FOR UPDATE NOWAIT",
                    (row_id,),
                ).fetchall()
                other.execute(
                    "SELECT id FROM collector_observations WHERE id = %s FOR UPDATE NOWAIT",
                    (observation,),
                ).fetchall()
            applied = prune_completed_history(connection, apply=True)
            assert applied["deleted_known_player_discoveries"] == 1
            assert connection.info.transaction_status == TransactionStatus.IDLE
            # Deletion is externally visible on return, with no caller commit
            # and while the apply connection remains open.
            with psycopg.connect(connection_info) as other:
                assert other.execute(
                    "SELECT count(*) FROM known_player_discoveries WHERE observation_id = %s",
                    (observation,),
                ).fetchone()[0] == 0


def test_prune_autocommit_connection(database_url: str, archive_server) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observation = _process(connection_info, archive_server, "boundaries-autocommit")
        _confirm_anchor(connection_info, archive_server)
        _age_jobs(connection_info)
        with psycopg.connect(connection_info, autocommit=True) as connection:
            assert prune_completed_history(connection)[
                "eligible_known_player_discoveries"
            ] == 1
            assert prune_completed_history(connection, apply=True)[
                "deleted_known_player_discoveries"
            ] == 1
        with psycopg.connect(connection_info) as other:
            assert other.execute(
                "SELECT count(*) FROM known_player_discoveries WHERE observation_id = %s",
                (observation,),
            ).fetchone()[0] == 0


def test_prune_caller_transaction_not_committed_and_rollback_restores(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observation = _process(connection_info, archive_server, "boundaries-caller")
        _confirm_anchor(connection_info, archive_server)
        _age_jobs(connection_info)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "INSERT INTO players (normalized_tag, active, next_due_at)"
                " VALUES ('#CALLEROWN', false, NULL)"
            )
            report = prune_completed_history(connection, apply=True)
            assert report["deleted_known_player_discoveries"] == 1
            with psycopg.connect(connection_info) as other:
                # Neither the caller's unrelated write nor prune's deletions
                # are committed by prune.
                assert other.execute(
                    "SELECT count(*) FROM players WHERE normalized_tag = '#CALLEROWN'"
                ).fetchone()[0] == 0
                assert other.execute(
                    "SELECT count(*) FROM known_player_discoveries WHERE observation_id = %s",
                    (observation,),
                ).fetchone()[0] == 1
            connection.rollback()
            with psycopg.connect(connection_info) as other:
                assert other.execute(
                    "SELECT count(*) FROM known_player_discoveries WHERE observation_id = %s",
                    (observation,),
                ).fetchone()[0] == 1
                assert other.execute(
                    "SELECT count(*) FROM players WHERE normalized_tag = '#CALLEROWN'"
                ).fetchone()[0] == 0


def test_prune_non_read_committed_rejects_before_changes(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observation = _process(connection_info, archive_server, "boundaries-isolation")
        _confirm_anchor(connection_info, archive_server)
        _age_jobs(connection_info)
        with psycopg.connect(connection_info) as connection:
            connection.execute("SET default_transaction_isolation = 'serializable'")
            connection.commit()
            with pytest.raises(ValueError, match="READ COMMITTED"):
                prune_completed_history(connection, apply=True)
            assert connection.execute(
                "SELECT count(*) FROM known_player_discoveries WHERE observation_id = %s",
                (observation,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT count(*) FROM player_discovery_events"
            ).fetchone()[0] == 0
