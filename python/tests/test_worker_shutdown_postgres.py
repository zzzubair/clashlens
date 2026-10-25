"""A stopping worker does not wait on a stuck maintenance step.

On 24 Oct 2026 (and twice on 21 Oct) one worker process's lanes stopped at
once on ``systemctl --user stop``, but it waited for its maintenance timer
until systemd killed it at 70 s. This runs the real worker as its own process
against PostgreSQL, holds a lock its first maintenance step needs, and stops
it with SIGTERM. The locked table is one an empty queue's claims never read,
so the lanes stop at once at any time of day.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from domain_test_support import domain_database

import clashlens

# The worker's maintenance waits are 5 s; lanes stop within a poll interval.
STOP_BOUND_SECONDS = 15


def _waiting_on_lock(connection_info: str) -> bool:
    with psycopg.connect(connection_info) as connection:
        return bool(connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database()"
            " AND wait_event_type = 'Lock' AND query LIKE"
            " '%%SELECT work.id%%FROM collector_reset_sweeps AS sweep%%reset_baseline_evidence%%')"
        ).fetchone()[0])


def test_sigterm_during_a_stuck_maintenance_step_exits_promptly(
    database_url: str, archive_server, tmp_path
) -> None:
    endpoint, *_ = archive_server
    with domain_database(database_url) as connection_info:
        holder = psycopg.connect(connection_info)
        holder.execute("LOCK TABLE reset_baseline_evidence IN ACCESS EXCLUSIVE MODE")
        worker = subprocess.Popen(
            [sys.executable, "-c",
             ("import sys; from clashlens import cli, worker_liveness;"
              f" worker_liveness.PROGRESS_FILE = {str(tmp_path / 'progress')!r};"
              " sys.exit(cli.main(sys.argv[1:]))"),
             "worker", "--database-url", connection_info, "--owner", "shutdown-test",
             "--run-forever", "--concurrency", "2", "--poll-interval-seconds", "0.2",
             "--archive-endpoint", endpoint, "--archive-insecure-test-only",
             "--archive-access-key", "test", "--archive-secret-key", "test"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONPATH": str(Path(clashlens.__file__).parents[1])},
        )
        try:
            deadline = time.monotonic() + 60
            while not _waiting_on_lock(connection_info):
                assert worker.poll() is None, worker.communicate()[0].decode()
                assert time.monotonic() < deadline, "maintenance never reached the lock"
                time.sleep(0.2)
            stopping_at = time.monotonic()
            worker.send_signal(signal.SIGTERM)
            try:
                output, _ = worker.communicate(timeout=STOP_BOUND_SECONDS)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    f"worker still running {STOP_BOUND_SECONDS} s after SIGTERM"
                ) from None
            assert worker.returncode == 0, output.decode()
            assert time.monotonic() - stopping_at < STOP_BOUND_SECONDS
            assert b'"status": "stopped"' in output
        finally:
            holder.close()
            if worker.poll() is None:
                worker.kill()
                worker.wait()
