"""Shared setup for api db organization tests."""

from __future__ import annotations

from uuid import uuid4

from clashlens import (
    api_accounts,
)
from clashlens.api_db import ApiDatabase
from clashlens.api_requests import RequestBinding


def account_binding(
    account_id: int | None,
    operation: str,
    target: str,
    identity: dict[str, object],
    *,
    method: str = "POST",
    subject: str = "group-owner-subject",
) -> RequestBinding:
    return RequestBinding(
        request_id=str(uuid4()),
        caller="typescript-website",
        provider="google",
        provider_subject=subject,
        account_id=account_id,
        operation=operation,
        method=method,
        request_target=target,
        identity=identity,
    )


def create_owner(database: ApiDatabase) -> int:
    created = api_accounts.create_account(database,
        RequestBinding(
            request_id=str(uuid4()),
            caller="typescript-website",
            provider="google",
            provider_subject="group-owner-subject",
            account_id=None,
            operation="account.create",
            method="POST",
            request_target="/v1/account",
            identity={"username": "groupowner"},
        ),
        username="groupowner",
        normalized_username="groupowner",
        display_name="Group Owner",
    )
    assert created.status_code == 201
    account = api_accounts.resolve_account(database, "google", "group-owner-subject")
    assert account is not None
    return account.internal_id
