"""Shared setup for crews postgres tests."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from api_db_public_ops_support import seed_profile
from api_db_verification_support import (
    verification_binding,
)
from domain_test_support import as_api_role

from clashlens import api_accounts, api_crews, api_verification
from clashlens.api_db import ApiDatabase
from clashlens.api_requests import RequestBinding
from clashlens.verification import VerificationOutcome

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


class Site:
    """Clash Lens accounts and their linked game accounts, with crew calls
    made as the production API role."""

    def __init__(self, connection_info: str) -> None:
        self.info = connection_info
        self.seed = ApiDatabase(connection_info)
        self.api = ApiDatabase(as_api_role(connection_info), max_size=8)
        self.ids: dict[str, int] = {}

    def close(self) -> None:
        self.api.close()
        self.seed.close()

    def account(self, username: str) -> str:
        created = api_accounts.create_account(
            self.api,
            RequestBinding(
                request_id=str(uuid4()),
                caller="typescript-website",
                provider="google",
                provider_subject=f"subject-{username}",
                account_id=None,
                operation="account.create",
                method="POST",
                request_target="/v1/account",
                identity={"username": username},
            ),
            username=username,
            normalized_username=username,
            display_name=username.title(),
        )
        assert created.status_code == 201
        context = api_accounts.resolve_account(self.api, "google", f"subject-{username}")
        assert context is not None
        self.ids[username] = context.internal_id
        return username

    def link(self, username: str, tag: str, state: str = "tracking") -> str:
        """Link a game account: tracked in Legend League, out of it, or not
        checked yet."""
        known = self.sql("SELECT 1 FROM players WHERE normalized_tag = %s", (tag,))
        if state != "unchecked" and not known:
            seed_profile(self.seed, tag, 5000)
        binding = verification_binding(self.ids[username], f"subject-{username}", tag)
        assert api_verification.reserve_verification(
            self.api, binding, normalized_tag=tag
        ).fresh
        linked = api_verification.complete_verification(
            self.api,
            binding,
            normalized_tag=tag,
            outcome=VerificationOutcome.VERIFIED,
            account_id=self.ids[username],
            # A support transfer must follow its fresh verification within
            # 15 minutes of the database clock.
            completed_at=datetime.now(UTC),
        )
        assert linked.payload["status"] in {"linked", "support_required"}
        if state == "not_in_legend":
            self.sql(
                """
                UPDATE players SET active = false, eligibility_state = 'ineligible'
                WHERE normalized_tag = %s
                """,
                (tag,),
            )
        return linked.payload.get("verification_request_id")

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self.seed.pool.connection() as connection:
            cursor = connection.execute(query, params)
            return cursor.fetchall() if cursor.description else []

    def call(self, caller: str, function: Any, /, **values: Any) -> Any:
        binding = RequestBinding(
            request_id=values.pop("request_id", None) or str(uuid4()),
            caller="typescript-website",
            provider="google",
            provider_subject=f"subject-{caller}",
            account_id=self.ids[caller],
            operation=f"crews.{function.__name__}",
            method="POST",
            request_target="/v1/account/crews",
            identity={key: str(value) for key, value in values.items()},
        )
        return function(self.api, binding, **values)

    def crew(self, username: str, crew_id: str, now: datetime = NOW) -> Any:
        return api_crews.get_crew(self.api, self.ids[username], crew_id, now=now)

    def make(self, username: str, tags: list[str], size: int = 50) -> str:
        created = self.call(
            username, api_crews.create_crew, name="Night Owls", size=size, tags=tags
        )
        assert created.status_code == 201, created.payload
        return created.payload["crew_id"]

    def invite(self, username: str, crew_id: str, *, new: bool = False, now=NOW) -> str:
        made = self.call(username, api_crews.make_invite, crew_id=crew_id, new=new, now=now)
        assert made.status_code == 200, made.payload
        return made.payload["code"]

    def accept(self, username: str, code: str, tags: list[str], now=NOW) -> Any:
        return self.call(username, api_crews.accept_invite, code=code, tags=tags, now=now)


def _while_held(site: Site, held: psycopg.Connection, hold: Any, call: Any) -> Any:
    """Run ``call`` while ``held`` sits in a transaction that ran ``hold``;
    once ``call`` is seen waiting on that transaction, commit and return
    what ``call`` returned."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        with held.transaction():
            hold(held)
            queued = pool.submit(call)
            deadline = time.monotonic() + 10
            while not site.sql(
                "SELECT 1 FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                (held.info.backend_pid,),
            ):
                if queued.done() or time.monotonic() > deadline:
                    pytest.fail("the second write did not wait for the first")
                time.sleep(0.01)
        return queued.result()
