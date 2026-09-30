"""Opt-in request-count measurement with the existing fake official API.

Admission time is accelerated to finish a week's pass. This measures requests,
key limits and receipt bytes, not the complete stack's live refresh capacity.
"""
from __future__ import annotations

import asyncio
import json
import os
import runpy
import threading
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from time import monotonic

import psycopg
import pytest
from domain_test_support import domain_database
from test_weekly_eligibility_postgres import MONDAY

from clashlens.collector import Collector
from clashlens.collector_db import CollectorDatabase, CollectorIntent
from clashlens.collector_http import ApiKey, KeyPool, OfficialApiClient
from clashlens.spool import Spool


@pytest.mark.skipif(os.environ.get("CLASHLENS_RUN_WEEKLY_LOAD") != "1", reason="opt-in 22,157-tag request measurement")
def test_known_pool_weekly_request_load(database_url, tmp_path):
    fixtures = runpy.run_path(str(Path(__file__).parents[2] / "development/fixtures.py"))
    tags = tuple(fixtures["tag_for"](index) for index in range(22_157))
    indexes = {tag: index for index, tag in enumerate(tags)}

    class Handler(fixtures["ClashHandler"]):
        population = tags
        tag_indexes = indexes

        def send_json(self, status, payload):
            if isinstance(payload, dict) and indexes.get(payload.get("tag"), -1) >= 12_500:
                payload["leagueTier"] = {"id": 105000035, "name": "Legend II"}
            super().send_json(status, payload)

    Handler.reset_trial_requests()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with domain_database(database_url) as info:
            database = CollectorDatabase(info)
            spool = Spool(tmp_path / "spool", max_body_bytes=4096)
            try:
                database.begin_reset(MONDAY, local_regular_inflight=0)
                with psycopg.connect(info) as connection:
                    with connection.cursor().copy("COPY players (normalized_tag, active, eligibility_state, next_due_at) FROM STDIN") as copy:
                        for index, tag in enumerate(tags):
                            copy.write_row((tag, index < 12_500, "eligible" if index < 12_500 else "ineligible", MONDAY + timedelta(days=1) if index < 12_500 else None))
                    connection.execute(
                        """INSERT INTO collector_response_state (
                               scope, identity_key, endpoint, player_id, normalized_tag,
                               last_response_hash, last_content_fingerprint,
                               last_occurrence_key, last_seen_at, last_success_at, last_applied_occurrence_key)
                           SELECT 'player', normalized_tag, 'league_history', id, normalized_tag,
                                  repeat('a', 64), repeat('a', 64), 'initial-history:' || id, %s, %s, 'initial-history:' || id
                           FROM players WHERE NOT active""", (MONDAY - timedelta(days=1),) * 2,
                    )
                collector = Collector(
                    database=database, spool=spool, archive=None,
                    client=OfficialApiClient(f"http://127.0.0.1:{server.server_port}", allow_insecure_test_origin=True, max_body_bytes=4096),
                    regular_keys=KeyPool([ApiKey(f"regular-{i}", "fixture") for i in range(4)], starts_per_second=30, concurrency_per_key=6),
                    interactive_keys=KeyPool([ApiKey("interactive", "fixture")], starts_per_second=30, concurrency_per_key=6),
                    archive_instance_id="fixture", collector_version="weekly-load-test", max_body_bytes=4096,
                )
                started = monotonic()

                async def collect_week():
                    for minute in range(400):
                        now = MONDAY + timedelta(minutes=minute)
                        with psycopg.connect(info) as connection:
                            count = connection.execute("SELECT clashlens_enqueue_weekly_eligibility(%s)", (now,)).fetchone()[0]
                            if not count:
                                return minute
                            rows = connection.execute("SELECT id, player_id, normalized_tag, due_at, league_history_status FROM collector_work WHERE status = 'pending' ORDER BY id").fetchall()
                        assert count <= 30
                        outcomes = await asyncio.gather(*[
                            collector.collect_intent(CollectorIntent(
                                "discovery_profile", row[3], row[1], row[2], work_id=row[0],
                                league_history_required=row[4] == "pending", eligibility_recheck=True,
                            )) for row in rows
                        ])
                        assert outcomes == ["complete"] * count
                    pytest.fail("weekly pass did not drain")

                minutes = asyncio.run(collect_week())
                summary = Handler.trial_summary()
                with psycopg.connect(info) as connection:
                    work_count, players_checked = connection.execute("SELECT count(*), count(DISTINCT player_id) FROM collector_work WHERE eligibility_recheck").fetchone()
                    receipt_bytes = connection.execute("SELECT pg_total_relation_size('collector_work')").fetchone()[0]
                    key_counts = connection.execute("SELECT key_label, count(*) FROM collector_observations GROUP BY key_label ORDER BY key_label").fetchall()
                    assert connection.execute("SELECT count(*) FROM collector_work JOIN players ON players.id = collector_work.player_id WHERE players.active").fetchone()[0] == 0
                assert work_count == players_checked == summary["profile"]["requests"] == 9_657
                assert summary["battle_log"]["requests"] == summary["league_history"]["requests"] == 0
                assert summary["profile"]["revisited_players"] == 0
                assert all(label.startswith("regular-") for label, _count in key_counts)
                print(json.dumps({"known_players": len(tags), "live_players": 12_500, "weekly_profile_requests": work_count,
                    "weekly_batches": minutes, "accelerated_elapsed_seconds": round(monotonic() - started, 3),
                    "collector_work_bytes_including_indexes": receipt_bytes, "requests_by_key": key_counts,
                    "extra_battle_or_history_requests": 0, "repeated_weekly_profiles": 0}, sort_keys=True))
            finally:
                spool.close()
                database.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
