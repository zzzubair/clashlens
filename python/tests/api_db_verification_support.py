"""Shared setup for api db verification tests."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from clashlens.api_requests import RequestBinding


def support_connection(connection_info: str) -> psycopg.Connection:
    connection = psycopg.connect(connection_info, autocommit=True)
    try:
        connection.execute("SET SESSION AUTHORIZATION clashlens_support_transfer")
    except psycopg.errors.InsufficientPrivilege:
        connection.close()
        pytest.skip("support transfer integration requires SET SESSION AUTHORIZATION")
    return connection


def call_support_transfer(
    connection: psycopg.Connection,
    *,
    verification_request_id: str,
    player_tag: str,
    from_account_public_id: str,
    to_account_public_id: str,
    operator_identity: str,
    reason: str,
) -> tuple[str, str | None]:
    row = connection.execute(
        """
        SELECT status, tag
        FROM clashlens_support_transfer(%s, %s, %s, %s, %s, %s)
        """,
        (
            verification_request_id,
            player_tag,
            from_account_public_id,
            to_account_public_id,
            operator_identity,
            reason,
        ),
    ).fetchone()
    assert row is not None
    status = row[0].decode("utf-8") if isinstance(row[0], bytes) else str(row[0])
    tag = (
        None
        if row[1] is None
        else (row[1].decode("utf-8") if isinstance(row[1], bytes) else str(row[1]))
    )
    return status, tag


def verification_binding(account_id: int, subject: str, tag: str) -> RequestBinding:
    return RequestBinding(
        request_id=str(uuid4()),
        caller="typescript-website",
        provider="google",
        provider_subject=subject,
        account_id=account_id,
        operation="player_links.verify",
        method="POST",
        request_target=f"/v1/players/{tag}/verifytoken",
        identity={"tag": tag},
    )
