"""Construct storage clients from runtime settings, independently of commands."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .archive import S3ArchiveReader, SpoolFirstReader


def open_archive(
    arguments: argparse.Namespace,
    *,
    pool_size: int = 4,
    database: Any | None = None,
    validate_archive_instance: bool = True,
) -> S3ArchiveReader | SpoolFirstReader:
    if not arguments.archive_endpoint:
        raise ValueError("archive endpoint is required")
    access_key = file_value(
        arguments.archive_access_key_file,
        arguments.archive_access_key,
        "archive access key",
    )
    secret_key = file_value(
        arguments.archive_secret_key_file,
        arguments.archive_secret_key,
        "archive secret key",
    )
    if not access_key or not secret_key:
        raise ValueError("archive access and secret key are required")
    archive = S3ArchiveReader(
        endpoint=arguments.archive_endpoint,
        bucket=arguments.archive_bucket,
        region=arguments.archive_region,
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
        instance_id=arguments.archive_instance_id,
        marker_key=arguments.archive_marker_key,
        marker_hash=arguments.archive_marker_hash,
        marker_payload_version=arguments.archive_marker_payload_version,
    )
    if arguments.spool_root:
        return SpoolFirstReader(
            archive,
            spool_root=arguments.spool_root,
            max_body_bytes=arguments.archive_max_body_bytes,
            max_bytes=arguments.spool_max_bytes,
            max_objects=arguments.spool_max_objects,
            free_space_floor=arguments.spool_free_space_floor,
            free_inode_floor=arguments.spool_free_inode_floor,
            database=database,
            validate_database=validate_archive_instance,
        )
    return archive


def database_url(arguments: argparse.Namespace) -> str:
    value = file_value(
        arguments.database_url_file, arguments.database_url, "database URL"
    )
    if not value:
        raise ValueError("database URL is required")
    return value


def file_value(path: str, inline_value: str, label: str) -> str:
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
