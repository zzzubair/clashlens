from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime

import uvicorn

from .api import create_app
from .archive import S3ArchiveReader
from .db import Database
from .hmac_proof import load_secret_file
from .worker import ObservationProcessor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue 29 Python-layer prototype CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="apply the prototype-only SQL contract")
    _database_argument(init_db)

    seed = subparsers.add_parser("seed", help="insert one collector-compatible observation and job")
    _database_argument(seed)
    seed.add_argument("--occurrence-key", required=True)
    seed.add_argument("--tag", default="#2PP")
    seed.add_argument("--endpoint", default="profile")
    seed.add_argument("--endpoint-version", default="profile-v1")
    seed.add_argument("--schema-version", default="profile-schema-v1")
    seed.add_argument("--observed-at", required=True)
    seed.add_argument("--http-status", type=int, default=200)
    seed.add_argument("--response-hash", required=True)
    seed.add_argument("--archive-reference", required=True)
    seed.add_argument("--collector-version", default="collector-prototype-v1")
    seed.add_argument("--max-attempts", type=int, default=2)

    worker = subparsers.add_parser("worker", help="claim and process prototype observations")
    _database_argument(worker)
    _archive_arguments(worker)
    worker.add_argument("--owner", default="python-prototype-worker")
    worker.add_argument("--max-jobs", type=int, default=1)
    worker.add_argument("--lease-seconds", type=int, default=30)

    serve = subparsers.add_parser("serve", help="run the signed saved-data FastAPI route")
    _database_argument(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--caller",
        default=os.environ.get("CLASHLENS_HMAC_CALLER", "typescript-website"),
    )
    serve.add_argument("--key-id", default=os.environ.get("CLASHLENS_HMAC_KEY_ID", "current"))
    serve.add_argument("--secret-file", default=os.environ.get("CLASHLENS_HMAC_SECRET_FILE", ""))
    serve.add_argument(
        "--previous-key-id",
        default=os.environ.get("CLASHLENS_HMAC_PREVIOUS_KEY_ID", ""),
    )
    serve.add_argument(
        "--previous-secret-file",
        default=os.environ.get("CLASHLENS_HMAC_PREVIOUS_SECRET_FILE", ""),
    )
    serve.add_argument("--log-level", default="warning")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "init-db":
            database = Database(arguments.database_url)
            try:
                database.apply_schema()
            finally:
                database.close()
            print(json.dumps({"status": "schema_ready", "contract": "prototype-only-v2"}))
            return 0
        if arguments.command == "seed":
            database = Database(arguments.database_url)
            try:
                observation_id, job_id = database.insert_observation_and_job(
                    occurrence_key=arguments.occurrence_key,
                    normalized_tag=arguments.tag,
                    endpoint=arguments.endpoint,
                    endpoint_version=arguments.endpoint_version,
                    schema_version=arguments.schema_version,
                    observed_at=_parse_datetime(arguments.observed_at),
                    http_status=arguments.http_status,
                    response_hash=arguments.response_hash,
                    archive_reference=arguments.archive_reference,
                    collector_version=arguments.collector_version,
                    max_attempts=arguments.max_attempts,
                )
            finally:
                database.close()
            print(json.dumps({"status": "seeded", "observation_id": observation_id, "job_id": job_id}))
            return 0
        if arguments.command == "worker":
            database = Database(arguments.database_url)
            try:
                processor = ObservationProcessor(database, _archive(arguments))
                results = processor.process_until_idle(
                    owner=arguments.owner,
                    max_jobs=arguments.max_jobs,
                    lease_seconds=arguments.lease_seconds,
                )
                print(json.dumps({"status": "complete", "results": [asdict(result) for result in results]}))
            finally:
                database.close()
            return 0
        if arguments.command == "serve":
            app = create_app(
                arguments.database_url,
                keys=_load_hmac_keys(arguments),
            )
            uvicorn.run(app, host=arguments.host, port=arguments.port, log_level=arguments.log_level)
            return 0
    except Exception as error:  # noqa: BLE001 - sanitize errors at the CLI boundary
        print(f"prototype command failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    raise AssertionError("unreachable command")


def _database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", default=os.environ.get("CLASHLENS_DATABASE_URL", ""))
    parser.add_argument("--database-url-required", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(_validate_database=True)


def _archive_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--archive-endpoint", default=os.environ.get("CLASHLENS_ARCHIVE_ENDPOINT", ""))
    parser.add_argument("--archive-bucket", default=os.environ.get("CLASHLENS_ARCHIVE_BUCKET", "evidence"))
    parser.add_argument("--archive-access-key", default=os.environ.get("CLASHLENS_ARCHIVE_ACCESS_KEY", ""))
    parser.add_argument("--archive-secret-key", default=os.environ.get("CLASHLENS_ARCHIVE_SECRET_KEY", ""))
    parser.add_argument("--archive-secure", action="store_true")
    parser.add_argument(
        "--archive-max-body-bytes",
        type=int,
        default=int(os.environ.get("CLASHLENS_ARCHIVE_MAX_BODY_BYTES", "2000000")),
    )


def _archive(arguments: argparse.Namespace) -> S3ArchiveReader:
    if not arguments.archive_endpoint:
        raise ValueError("archive endpoint is required")
    return S3ArchiveReader(
        endpoint=arguments.archive_endpoint,
        bucket=arguments.archive_bucket,
        access_key=arguments.archive_access_key or "prototype-access",
        secret_key=arguments.archive_secret_key or "prototype-secret",
        secure=arguments.archive_secure,
        max_body_bytes=arguments.archive_max_body_bytes,
    )


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _load_hmac_keys(arguments: argparse.Namespace) -> dict[tuple[str, str], bytes]:
    if not arguments.caller or not arguments.key_id:
        raise ValueError("HMAC caller and key ID are required")
    if not arguments.secret_file:
        raise ValueError("--secret-file is required")
    previous_present = bool(arguments.previous_key_id or arguments.previous_secret_file)
    if previous_present and not (arguments.previous_key_id and arguments.previous_secret_file):
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


if __name__ == "__main__":
    raise SystemExit(main())
