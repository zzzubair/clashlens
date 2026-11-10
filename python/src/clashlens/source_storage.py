"""Canonical parsed payloads and player identities saved by source ingestion."""

from typing import Any

from psycopg.types.json import Jsonb

from .db_rows import text_value


def record_parsed_payload(
    connection: Any,
    *,
    endpoint: str,
    response_hash: str,
    parser_version: str,
    schema_version: str,
    parse_outcome: str,
    parsed_json: Any,
    representation: str | None = None,
) -> int:
    stored_json = {"representation": representation} if representation else parsed_json
    inserted = connection.execute(
        """
        INSERT INTO parsed_source_payloads (
            endpoint, response_hash, parser_version, schema_version,
            parse_outcome, parsed_json
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (endpoint, response_hash, parser_version) DO NOTHING
        RETURNING id
        """,
        (
            endpoint,
            response_hash,
            parser_version,
            schema_version,
            parse_outcome,
            Jsonb(stored_json),
        ),
    ).fetchone()
    if inserted is not None:
        return int(inserted[0])
    existing = connection.execute(
        """
        SELECT id, schema_version, parse_outcome, parsed_json
        FROM parsed_source_payloads
        WHERE endpoint = %s AND response_hash = %s AND parser_version = %s
        """,
        (endpoint, response_hash, parser_version),
    ).fetchone()
    if existing is None:
        raise RuntimeError("canonical parsed payload disappeared")
    if (
        text_value(existing[1]) != schema_version
        or text_value(existing[2]) != parse_outcome
        or existing[3] not in (parsed_json, stored_json)
    ):
        raise ValueError("canonical parsed payload identity conflict")
    return int(existing[0])


def upsert_player(connection: Any, normalized_tag: str, *, active: bool) -> int:
    row = connection.execute(
        """
        INSERT INTO players (normalized_tag, active, eligibility_state)
        VALUES (%s, %s, 'unknown')
        ON CONFLICT (normalized_tag) DO UPDATE
            SET updated_at = clock_timestamp()
        RETURNING id
        """,
        (normalized_tag, active),
    ).fetchone()
    assert row is not None
    return int(row[0])
