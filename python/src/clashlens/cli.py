from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import signal
import sys
import urllib.request
from collections import deque
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from threading import Event
from time import monotonic, time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import uvicorn

from .api import create_app
from .api_db import ApiDatabase
from .archive import MAX_ARCHIVE_POOL_SIZE, S3ArchiveReader
from .db import MAX_POOL_SIZE, Database
from .hmac_proof import SigningInput, load_secret_file, sign
from .profile import normalize_player_tag
from .verification import (
    OfficialVerificationClient,
    VerificationOutcome,
    classify_official_response,
    load_official_api_key_file,
)
from .worker import (
    MAX_CONCURRENCY,
    ObservationProcessor,
    ProcessResult,
    StageMetrics,
    process_concurrently,
)

MAX_REPORTED_RESULTS = 100
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _bounded_int(label: str, minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{label} must be an integer between {minimum} and {maximum}"
            ) from None
        if parsed < minimum or parsed > maximum:
            raise argparse.ArgumentTypeError(
                f"{label} must be an integer between {minimum} and {maximum}"
            )
        return parsed

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clash Lens Python services CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    worker = subparsers.add_parser(
        "worker", help="claim and process production observations"
    )
    _database_argument(worker)
    _archive_arguments(worker)
    worker.add_argument("--owner", default="python-worker")
    worker.add_argument("--max-jobs", type=int, default=1)
    worker.add_argument("--lease-seconds", type=int, default=30)
    worker.add_argument("--run-forever", action="store_true")
    worker.add_argument("--poll-interval-seconds", type=float, default=1.0)
    worker.add_argument(
        "--concurrency",
        type=_bounded_int("concurrency", 1, MAX_CONCURRENCY),
        default=1,
        help=(
            "number of bounded in-process execution lanes (1 to 32; "
            "default 1 preserves sequential behavior)"
        ),
    )
    worker.add_argument(
        "--database-pool-size",
        type=_bounded_int("database pool size", 1, MAX_POOL_SIZE),
        default=None,
        help=(
            "PostgreSQL connection pool size (default: 8 with concurrency, "
            "4 for the sequential worker)"
        ),
    )
    worker.add_argument(
        "--archive-pool-size",
        type=_bounded_int("archive pool size", 1, MAX_ARCHIVE_POOL_SIZE),
        default=None,
        help=(
            "archive HTTP connection pool size (default: max(4, concurrency))"
        ),
    )

    ready = subparsers.add_parser(
        "ready", help="check the production worker database and archive dependencies"
    )
    _database_argument(ready)
    _archive_arguments(ready)
    ready.add_argument("--expected-contract-version", type=int, default=2)

    queue_status = subparsers.add_parser(
        "queue-status", help="report aggregate production worker queue health"
    )
    _database_argument(queue_status)

    republish_current_season = subparsers.add_parser(
        "republish-current-season",
        help="queue a bounded batch of current-season ranked-day republications",
    )
    _database_argument(republish_current_season)
    republish_current_season.add_argument(
        "--max-jobs",
        type=_bounded_int("republication batch size", 1, 1000),
        default=100,
    )

    serve = subparsers.add_parser(
        "serve", help="run the signed saved-data FastAPI route"
    )
    _database_argument(serve)
    serve.add_argument(
        "--host", default=os.environ.get("CLASHLENS_API_HOST", "127.0.0.1")
    )
    serve.add_argument(
        "--port", type=int, default=int(os.environ.get("CLASHLENS_API_PORT", "8000"))
    )
    serve.add_argument(
        "--caller",
        default=os.environ.get("CLASHLENS_HMAC_CALLER", "typescript-website"),
    )
    serve.add_argument(
        "--key-id", default=os.environ.get("CLASHLENS_HMAC_KEY_ID", "current")
    )
    serve.add_argument(
        "--secret-file", default=os.environ.get("CLASHLENS_HMAC_SECRET_FILE", "")
    )
    serve.add_argument(
        "--previous-key-id",
        default=os.environ.get("CLASHLENS_HMAC_PREVIOUS_KEY_ID", ""),
    )
    serve.add_argument(
        "--previous-secret-file",
        default=os.environ.get("CLASHLENS_HMAC_PREVIOUS_SECRET_FILE", ""),
    )
    serve.add_argument(
        "--max-body-bytes",
        type=int,
        default=int(os.environ.get("CLASHLENS_API_MAX_BODY_BYTES", "1048576")),
    )
    serve.add_argument(
        "--official-key-file",
        default=os.environ.get("CLASHLENS_OFFICIAL_KEY_FILE", ""),
    )
    serve.add_argument(
        "--official-proxy-url",
        default=os.environ.get("CLASHLENS_OFFICIAL_PROXY_URL", ""),
    )
    serve.add_argument("--log-level", default="warning")

    probe = subparsers.add_parser(
        "probe", help="make one signed saved-data request for deployment verification"
    )
    probe.add_argument("--url", required=True)
    probe.add_argument("--caller", default="typescript-website")
    probe.add_argument("--key-id", default="current")
    probe.add_argument("--secret-file", required=True)
    probe.add_argument("--timeout-seconds", type=float, default=3.0)

    recover_discord = subparsers.add_parser(
        "recover-discord",
        help=(
            "maintainer-only recovery: verify a linked player's current token, "
            "then attach a free Discord identity to the target account"
        ),
    )
    _database_argument(recover_discord)
    recover_discord.add_argument("--target-account-public-id", required=True)
    recover_discord.add_argument("--player-tag", required=True)
    recover_discord.add_argument("--discord-user-id", required=True)
    recover_discord.add_argument("--operator", required=True)
    recover_discord.add_argument("--reason", required=True)
    recover_discord.add_argument("--official-key-file",
                                 default=os.environ.get("CLASHLENS_OFFICIAL_KEY_FILE", ""))
    recover_discord.add_argument("--official-proxy-url",
                                 default=os.environ.get("CLASHLENS_OFFICIAL_PROXY_URL", ""))

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "worker":
            return _run_worker(arguments)
        if arguments.command == "ready":
            return _run_ready(arguments)
        if arguments.command == "queue-status":
            database = Database(_database_url(arguments))
            try:
                print(json.dumps(database.queue_health(), sort_keys=True))
            finally:
                database.close()
            return 0
        if arguments.command == "republish-current-season":
            database = Database(_database_url(arguments))
            try:
                job_ids = database.enqueue_current_season_republication(
                    max_jobs=arguments.max_jobs
                )
                print(
                    json.dumps(
                        {"enqueued_count": len(job_ids), "job_ids": job_ids},
                        sort_keys=True,
                    )
                )
            finally:
                database.close()
            return 0
        if arguments.command == "serve":
            app, _database = _serve_app(arguments)
            uvicorn.run(
                app,
                host=arguments.host,
                port=arguments.port,
                log_level=arguments.log_level,
            )
            return 0
        if arguments.command == "probe":
            print(_probe(arguments))
            return 0
        if arguments.command == "recover-discord":
            return _run_recover_discord(arguments)
    except (ValueError, OSError) as error:
        # Do not print exception details. Database URLs, request targets, and
        # mounted secret paths can appear in third-party exception messages.
        print(f"service command failed: {type(error).__name__}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - sanitize the CLI boundary
        print("service command failed: internal_error", file=sys.stderr)
        return 1
    raise AssertionError("unreachable command")


def _run_worker(arguments: argparse.Namespace) -> int:
    concurrency = arguments.concurrency
    database_pool_size = (
        arguments.database_pool_size
        if arguments.database_pool_size is not None
        else (8 if concurrency > 1 else 4)
    )
    archive_pool_size = (
        arguments.archive_pool_size
        if arguments.archive_pool_size is not None
        else (max(4, concurrency) if concurrency > 1 else 4)
    )
    database = Database(_database_url(arguments), max_size=database_pool_size)
    stop_requested = Event()
    _install_shutdown_handlers(stop_requested)
    try:
        stage_metrics = StageMetrics()
        archive = _archive(arguments, pool_size=archive_pool_size)
        set_archive_pool_observer = getattr(
            archive, "set_pool_acquire_observer", None
        )
        if callable(set_archive_pool_observer):
            set_archive_pool_observer(
                lambda duration: stage_metrics.record(
                    "python_archive_pool_acquire", duration
                )
            )
        processor = ObservationProcessor(database, archive)
        if isinstance(processor, ObservationProcessor):
            processor.stage_metrics = stage_metrics
            database.stage_metrics = stage_metrics
        next_queue_maintenance_at = float("-inf")

        def process_batch() -> list[ProcessResult]:
            nonlocal next_queue_maintenance_at
            # Archive-backed jobs must remain pending while the object store is
            # unavailable. Check before maintenance as well as before either
            # claim path so an outage cannot consume attempts or lease work.
            if not archive.check_ready():
                return []
            current_time = monotonic()
            if current_time >= next_queue_maintenance_at:
                maintenance_started_at = monotonic()
                database.maintain_queue(max_jobs=100)
                stage_metrics.record(
                    "python_queue_maintenance", monotonic() - maintenance_started_at
                )
                next_queue_maintenance_at = current_time + 10
            if concurrency == 1:
                return processor.process_until_idle(
                    owner=arguments.owner,
                    max_jobs=arguments.max_jobs,
                    lease_seconds=arguments.lease_seconds,
                    stop_requested=stop_requested,
                )
            return process_concurrently(
                processor,
                concurrency=concurrency,
                owner=arguments.owner,
                max_jobs=arguments.max_jobs,
                lease_seconds=arguments.lease_seconds,
                stop_requested=stop_requested,
            )

        if not arguments.run_forever:
            results = process_batch()
            print(
                json.dumps(
                    {
                        "status": "complete",
                        "results": [asdict(result) for result in results],
                    }
                )
            )
            return 0
        recent_results: deque[ProcessResult] = deque(maxlen=MAX_REPORTED_RESULTS)
        processed_count = 0
        last_health_report = float("-inf")
        while not stop_requested.is_set():
            results = process_batch()
            processed_count += len(results)
            recent_results.extend(results)
            for result in results:
                print(
                    json.dumps({"event": "job_result", **asdict(result)}),
                    flush=True,
                )
            current_time = monotonic()
            if current_time - last_health_report >= 60:
                print(
                    json.dumps(
                        {
                            "event": "worker_health",
                            "queue": getattr(database, "queue_health", dict)(),
                            "database_pool": getattr(
                                database, "pool_health", dict
                            )(),
                            "stages": stage_metrics.snapshot(),
                        }
                    ),
                    flush=True,
                )
                last_health_report = current_time
            if not results:
                stop_requested.wait(arguments.poll_interval_seconds)
        print(
            json.dumps(
                {
                    "status": "stopped",
                    "processed_count": processed_count,
                    "results": [asdict(result) for result in recent_results],
                    "database_pool": getattr(database, "pool_health", dict)(),
                    "stages": stage_metrics.snapshot(),
                }
            )
        )
        return 0
    finally:
        database.close()


def _run_ready(arguments: argparse.Namespace) -> int:
    database = Database(_database_url(arguments))
    try:
        archive = _archive(arguments)
        if not database.is_ready(
            expected_contract_version=arguments.expected_contract_version
        ):
            return 1
        if not archive.check_ready():
            return 1
        print(json.dumps({"status": "ready"}))
        return 0
    finally:
        database.close()


def _install_shutdown_handlers(stop_requested: Event) -> None:
    def request_shutdown(_signum: int, _frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)


def _database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database-url", default=os.environ.get("CLASHLENS_DATABASE_URL", "")
    )
    parser.add_argument(
        "--database-url-file",
        default=os.environ.get("CLASHLENS_DATABASE_URL_FILE", ""),
    )


def _archive_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--archive-endpoint", default=os.environ.get("CLASHLENS_ARCHIVE_ENDPOINT", "")
    )
    parser.add_argument(
        "--archive-bucket",
        default=os.environ.get("CLASHLENS_ARCHIVE_BUCKET", "evidence"),
    )
    parser.add_argument(
        "--archive-access-key",
        default=os.environ.get("CLASHLENS_ARCHIVE_ACCESS_KEY", ""),
    )
    parser.add_argument(
        "--archive-secret-key",
        default=os.environ.get("CLASHLENS_ARCHIVE_SECRET_KEY", ""),
    )
    parser.add_argument(
        "--archive-access-key-file",
        default=os.environ.get("CLASHLENS_ARCHIVE_ACCESS_KEY_FILE", ""),
    )
    parser.add_argument(
        "--archive-secret-key-file",
        default=os.environ.get("CLASHLENS_ARCHIVE_SECRET_KEY_FILE", ""),
    )
    parser.add_argument("--archive-secure", action="store_true", default=True)
    parser.add_argument("--archive-insecure-test-only", action="store_true")
    parser.add_argument(
        "--archive-max-body-bytes",
        type=int,
        default=int(os.environ.get("CLASHLENS_ARCHIVE_MAX_BODY_BYTES", "2000000")),
    )
    parser.add_argument(
        "--archive-connect-timeout-seconds",
        type=float,
        default=float(os.environ.get("CLASHLENS_ARCHIVE_CONNECT_TIMEOUT_SECONDS", "5")),
    )
    parser.add_argument(
        "--archive-read-timeout-seconds",
        type=float,
        default=float(os.environ.get("CLASHLENS_ARCHIVE_READ_TIMEOUT_SECONDS", "15")),
    )
    parser.add_argument(
        "--archive-max-retries",
        type=int,
        default=int(os.environ.get("CLASHLENS_ARCHIVE_MAX_RETRIES", "1")),
    )
    parser.add_argument(
        "--archive-retry-backoff-seconds",
        type=float,
        default=float(os.environ.get("CLASHLENS_ARCHIVE_RETRY_BACKOFF_SECONDS", "0.1")),
    )


def _archive(arguments: argparse.Namespace, *, pool_size: int = 4) -> S3ArchiveReader:
    if not arguments.archive_endpoint:
        raise ValueError("archive endpoint is required")
    access_key = _file_value(
        arguments.archive_access_key_file,
        arguments.archive_access_key,
        "archive access key",
    )
    secret_key = _file_value(
        arguments.archive_secret_key_file,
        arguments.archive_secret_key,
        "archive secret key",
    )
    if not access_key or not secret_key:
        raise ValueError("archive access and secret key are required")
    return S3ArchiveReader(
        endpoint=arguments.archive_endpoint,
        bucket=arguments.archive_bucket,
        access_key=access_key,
        secret_key=secret_key,
        secure=not arguments.archive_insecure_test_only,
        allow_insecure_test_origin=arguments.archive_insecure_test_only,
        max_body_bytes=arguments.archive_max_body_bytes,
        connect_timeout_seconds=arguments.archive_connect_timeout_seconds,
        read_timeout_seconds=arguments.archive_read_timeout_seconds,
        max_retries=arguments.archive_max_retries,
        retry_backoff_seconds=arguments.archive_retry_backoff_seconds,
        pool_size=pool_size,
    )


def _database_url(arguments: argparse.Namespace) -> str:
    value = _file_value(
        arguments.database_url_file, arguments.database_url, "database URL"
    )
    if not value:
        raise ValueError("database URL is required")
    return value


def _file_value(path: str, inline_value: str, label: str) -> str:
    if path:
        try:
            value = Path(path).read_bytes()
        except OSError as error:
            raise ValueError(f"{label} file could not be read") from error
        if value.endswith(b"\n"):
            value = value[:-1]
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"{label} file must contain UTF-8 text") from error
        if not decoded or "\n" in decoded or "\r" in decoded:
            raise ValueError(f"{label} file contains invalid line endings")
        return decoded
    return inline_value


def _load_hmac_keys(arguments: argparse.Namespace) -> dict[tuple[str, str], bytes]:
    if not arguments.caller or not arguments.key_id:
        raise ValueError("HMAC caller and key ID are required")
    if not arguments.secret_file:
        raise ValueError("--secret-file is required")
    previous_present = bool(arguments.previous_key_id or arguments.previous_secret_file)
    if previous_present and not (
        arguments.previous_key_id and arguments.previous_secret_file
    ):
        raise ValueError("previous key ID and secret file must be configured together")
    if arguments.previous_key_id == arguments.key_id:
        raise ValueError("current and previous HMAC key IDs must differ")
    keys = {
        (arguments.caller, arguments.key_id): load_secret_file(arguments.secret_file),
    }
    if previous_present:
        keys[(arguments.caller, arguments.previous_key_id)] = load_secret_file(
            arguments.previous_secret_file
        )
    return keys


def _serve_app(arguments: argparse.Namespace) -> tuple[Any, ApiDatabase]:
    """Build the production private API app and its database.

    The returned ApiDatabase is owned by the app lifespan, which closes it on
    shutdown. If any startup step raises after the pool opens, the pool is
    closed here before the error propagates. The official key is required and
    must be file-backed so the shared traffic gate fingerprint is derived from
    the exact ASCII token bytes, never from a command line or environment
    value.
    """
    if not arguments.official_key_file:
        raise ValueError("official API key file is required")
    official_key = load_official_api_key_file(arguments.official_key_file)
    if not arguments.official_proxy_url:
        raise ValueError("fixed-egress proxy URL is required")
    api_database = ApiDatabase(_database_url(arguments))
    try:
        fingerprint = _official_credential_fingerprint(official_key)
        api_database.register_official_credential(fingerprint)
        verification_client = OfficialVerificationClient(
            api_key=official_key,
            proxy_url=arguments.official_proxy_url,
        )
        app = create_app(
            database=api_database,
            keys=_load_hmac_keys(arguments),
            max_body_bytes=arguments.max_body_bytes,
            verification_client=verification_client,
            official_credential_fingerprint=fingerprint,
        )
    except BaseException:
        # Startup failed after the pool opened; the app never reached its
        # lifespan, so ownership never transferred. Close the pool and
        # re-raise without exposing key material.
        api_database.close()
        raise
    return app, api_database


def _official_credential_fingerprint(key_bytes: bytes) -> str:
    """Full SHA-256 fingerprint of the exact ASCII bearer-token bytes."""
    return hashlib.sha256(key_bytes).hexdigest()


def _run_recover_discord(arguments: argparse.Namespace) -> int:
    """Maintainer-only recovery path; the token is request-only and is never
    echoed, logged, or persisted."""
    if not UUID_PATTERN.fullmatch(arguments.target_account_public_id):
        print(json.dumps({"status": "invalid_request"}))
        return 1
    if not arguments.discord_user_id.isdigit() or not 17 <= len(
        arguments.discord_user_id
    ) <= 20:
        print(json.dumps({"status": "invalid_request"}))
        return 1
    if (
        not 1 <= len(arguments.operator) <= 255
        or any(
            character.isspace() or not character.isprintable()
            for character in arguments.operator
        )
    ):
        print(json.dumps({"status": "invalid_request"}))
        return 1
    if not 8 <= len(arguments.reason) <= 500 or any(
        not character.isprintable() for character in arguments.reason
    ):
        print(json.dumps({"status": "invalid_request"}))
        return 1
    try:
        normalized_tag = normalize_player_tag(arguments.player_tag)
        official_key = load_official_api_key_file(arguments.official_key_file)
        verification_client = OfficialVerificationClient(
            api_key=official_key,
            proxy_url=arguments.official_proxy_url,
        )
    except ValueError:
        print(json.dumps({"status": "invalid_request"}))
        return 1

    # The token crosses stdin only; it never reaches argv, logs, or storage.
    player_token = getpass.getpass("Clash API token: ")
    if not 1 <= len(player_token) <= 512:
        print(json.dumps({"status": "invalid_token"}))
        return 1
    try:
        response = verification_client.verify(normalized_tag, player_token)
        classification = classify_official_response(response.http_status, response.body)
    except Exception:  # noqa: BLE001 - never disclose transport details.
        print(json.dumps({"status": "verification_unavailable"}))
        return 1
    del player_token
    if classification.outcome != VerificationOutcome.VERIFIED:
        print(json.dumps({"status": classification.outcome.value}))
        return 1

    database = ApiDatabase(_database_url(arguments))
    try:
        status, detail = database.support_attach_discord_identity(
            account_public_id=arguments.target_account_public_id,
            normalized_player_tag=normalized_tag,
            discord_subject=arguments.discord_user_id,
            # The wrapper derives the operator from the sudo environment and
            # passes it exactly; Python never rewrites or prefixes it.
            operator_identity=arguments.operator,
            reason=arguments.reason,
        )
    finally:
        database.close()
    print(json.dumps({"status": status, "detail": detail}))
    return 0 if status == "attached" else 1


def _probe(arguments: argparse.Namespace) -> str:
    target = urlsplit(arguments.url)
    raw_target = target.path or "/"
    if target.query:
        raw_target += "?" + target.query
    key = load_secret_file(arguments.secret_file)
    request_id = str(uuid4())
    issued_at = int(time())
    value = SigningInput(
        proof_version="clashlens-hmac-v1",
        caller_b64url=_encode_text(arguments.caller),
        key_id_b64url=_encode_text(arguments.key_id),
        audience="clashlens-python-private-api",
        method="GET",
        target_b64url=_encode_bytes(raw_target.encode("ascii")),
        body_sha256=hashlib.sha256(b"").hexdigest(),
        issued_at=str(issued_at),
        expires_at=str(issued_at + 10),
        request_id=request_id,
        provider_b64url="",
        provider_subject_b64url="",
    )
    headers = {
        "X-ClashLens-Proof-Version": value.proof_version,
        "X-ClashLens-Caller": value.caller_b64url,
        "X-ClashLens-Key-Id": value.key_id_b64url,
        "X-ClashLens-Issued-At": value.issued_at,
        "X-ClashLens-Expires-At": value.expires_at,
        "X-ClashLens-Request-Id": value.request_id,
        "X-ClashLens-Provider": "",
        "X-ClashLens-Provider-Subject": "",
        "X-ClashLens-Signature": sign(key, value),
    }
    request = urllib.request.Request(arguments.url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(
            request, timeout=arguments.timeout_seconds
        ) as response:
            return response.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError("saved-data probe failed") from error


def _encode_text(value: str) -> str:
    return _encode_bytes(value.encode("utf-8"))


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


if __name__ == "__main__":
    raise SystemExit(main())
