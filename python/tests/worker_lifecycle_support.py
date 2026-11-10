"""Shared setup for worker lifecycle tests."""

from __future__ import annotations

from argparse import Namespace


def _worker_namespace(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "database_url": "postgresql://prototype@postgres/db",
        "database_url_file": "",
        "archive_endpoint": "archive.example:9000",
        "owner": "cli-worker",
        "max_jobs": 3,
        "lease_seconds": 30,
        "run_forever": False,
        "poll_interval_seconds": 0.01,
        "concurrency": 1,
        "database_pool_size": None,
        "archive_pool_size": None,
        "operating_snapshot_file": "",
        "disable_player_discovery": False,
    }
    values.update(overrides)
    return Namespace(**values)
