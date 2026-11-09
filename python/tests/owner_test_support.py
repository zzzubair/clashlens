"""Operator setup shared by owner tests."""

import psycopg


def _record_owner(connection_info: str, account_id: int) -> None:
    """The one-time production step, run by the operator outside the app."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO site_owner_accounts (account_id) VALUES (%s)", (account_id,)
        )
