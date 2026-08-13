from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from domain_test_support import domain_database, store_observation

from clashlens.archive import S3ArchiveReader
from clashlens.db import Database
from clashlens.worker import ObservationProcessor, process_concurrently

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
BATTLE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"


def _tag(index: int) -> str:
    alphabet = "0289PYLQGRJCUV"
    value = index
    encoded = ""
    while value:
        encoded = alphabet[value % len(alphabet)] + encoded
        value //= len(alphabet)
    return "#P" + encoded.rjust(5, "0")


def test_mixed_observation_processing_sustains_one_hundred_per_second(
    database_url: str,
    archive_server,
) -> None:
    enforce_target_rate = os.environ.get("CLASHLENS_ENFORCE_PERFORMANCE_TARGET") == "1"
    observation_count = int(os.environ.get("CLASHLENS_PERFORMANCE_OBSERVATIONS", "100"))
    if observation_count < 100 or observation_count % 2:
        raise ValueError("performance observation count must be an even number >= 100")
    players = observation_count // 2
    observed_at = datetime(2026, 8, 13, 5, 1, tzinfo=UTC)
    profile_template = json.loads(PROFILE_FIXTURE.read_text(encoding="utf-8"))
    battle_template = json.loads(BATTLE_FIXTURE.read_text(encoding="utf-8"))
    battle_template["items"] = battle_template["items"] * 4

    with domain_database(database_url) as connection_info:
        for index in range(players):
            tag = _tag(index + 1)
            profile = {**profile_template, "tag": tag}
            store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"throughput-profile-{index}",
                endpoint="profile",
                body=json.dumps(profile, separators=(",", ":")).encode(),
                observed_at=observed_at,
                normalized_tag=tag,
            )
            store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"throughput-battle-{index}",
                endpoint="battle_log",
                body=json.dumps(battle_template, separators=(",", ":")).encode(),
                observed_at=observed_at,
                normalized_tag=tag,
            )

        database = Database(connection_info, max_size=12)
        archive = S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
            allow_insecure_test_origin=True,
            pool_size=12,
        )
        try:
            started = monotonic()
            results = process_concurrently(
                ObservationProcessor(database, archive),
                concurrency=12,
                owner="throughput-test",
                max_jobs=observation_count,
                lease_seconds=60,
            )
            elapsed = monotonic() - started
        finally:
            database.close()

    assert len(results) == observation_count
    assert all(result.outcome == "processed" for result in results)
    print(
        f"processed {observation_count} mixed observations in {elapsed:.3f}s "
        f"({observation_count / elapsed:.1f}/s)"
    )
    if enforce_target_rate:
        assert elapsed < observation_count / 100, (
            f"processed {observation_count} mixed observations in {elapsed:.3f}s; "
            "the target-host benchmark is at least 100 observations/s"
        )
