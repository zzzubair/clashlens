"""Whether a browser login may still be used: the database's half.

The website signs each login cookie and renews it (website
app/server/login-renewal.server.ts, ``LoginCookieStage``); this module keeps
the two records that end a login before its cookie expires.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any


class LoginStanding(StrEnum):
    """What the database says about one login cookie.

    States:
    - ``ACTIVE``: nothing ended it, so the website may use and renew it.
    - ``LOGGED_OUT``: this exact login logged out (``record_logout``).
    - ``IDENTITY_REMOVED``: the sign-in connection it came through was removed
      at or after the millisecond it signed in (``record_identity_removal``),
      which ends that login on every browser.

    Events: logout and removing a sign-in connection move ``ACTIVE`` to an
    ended state; nothing moves an ended login back, and relinking the same
    connection never clears its removal. Both records are rows written in the
    caller's transaction, so a failed or retried request leaves the login as
    it was or ended, never half-ended, and a restart changes nothing. Rows
    older than 31 days are pruned because no login cookie lives that long.
    """

    ACTIVE = "active"
    LOGGED_OUT = "logged_out"
    IDENTITY_REMOVED = "identity_removed"


def login_standing(
    database: Any, session_hash: str, provider: str, provider_subject: str, issued_at_ms: int
) -> LoginStanding:
    """The one place that reads a login's standing from its records."""
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM login_session_revocations WHERE session_hash = %s
            ), EXISTS (
                SELECT 1 FROM provider_identity_removals
                WHERE provider = %s AND provider_subject = %s
                  AND removed_at >= %s
            )
            """,
            (
                session_hash,
                provider,
                provider_subject,
                datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=issued_at_ms),
            ),
        ).fetchone()
    if row is not None and row[0]:
        return LoginStanding.LOGGED_OUT
    if row is not None and row[1]:
        return LoginStanding.IDENTITY_REMOVED
    return LoginStanding.ACTIVE


def revoke_login_session(database: Any, session_hash: str) -> None:
    with database.pool.connection() as connection:
        with connection.transaction():
            record_logout(connection, session_hash)


def record_logout(connection: Any, session_hash: str) -> None:
    # A login cookie lives 30 days and is renewed only while its login has not
    # logged out, so an older logout guards nothing.
    connection.execute(
        "DELETE FROM login_session_revocations"
        " WHERE revoked_at < now() - interval '31 days'"
    )
    connection.execute(
        "INSERT INTO login_session_revocations (session_hash) VALUES (%s)"
        " ON CONFLICT (session_hash) DO NOTHING",
        (session_hash,),
    )


def record_identity_removal(
    connection: Any, provider: str, provider_subject: str
) -> None:
    # Every login cookie issued through this identity up to now stops working,
    # on every browser. Relinking never clears the row, and a login cookie
    # lives 30 days, so an older removal guards nothing.
    connection.execute(
        "DELETE FROM provider_identity_removals"
        " WHERE removed_at < now() - interval '31 days'"
    )
    connection.execute(
        """
        INSERT INTO provider_identity_removals (provider, provider_subject, removed_at)
        VALUES (%s, %s, clock_timestamp())
        ON CONFLICT (provider, provider_subject) DO UPDATE SET removed_at =
            GREATEST(provider_identity_removals.removed_at, EXCLUDED.removed_at)
        """,
        (provider, provider_subject),
    )
