"""Server-side visitor counts sent to PostHog's EU cloud, off unless configured.

The website counts page visits in the browser. A few things are better
counted where they really happen, such as an account or a group being
created. Each count is an anonymous total: a fresh random ID per event, no
person profile, no account, name or group details, and no location lookup.
Nothing is sent unless CLASHLENS_POSTHOG_TOKEN is set.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import uuid4

LOGGER = logging.getLogger(__name__)

EVENTS = frozenset({"signup_completed", "group_created", "group_used"})
HOST = "https://eu.i.posthog.com"
_TOKEN = re.compile(r"phc_[A-Za-z0-9]{20,64}")


class _Client(Protocol):
    def capture(self, event: str, **kwargs: Any) -> object: ...

    def shutdown(self) -> None: ...


class UsageEvents:
    def __init__(self, client: _Client | None = None) -> None:
        self._client = client

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] = os.environ) -> UsageEvents:
        token = environ.get("CLASHLENS_POSTHOG_TOKEN", "").strip()
        if not _TOKEN.fullmatch(token):
            return cls()
        from posthog import Posthog

        return cls(Posthog(token, host=HOST, disable_geoip=True))

    def capture(self, event: str, properties: Mapping[str, str] | None = None) -> None:
        """Queue one listed count; sending happens in the background and never fails a request."""
        if self._client is None or event not in EVENTS:
            return
        try:
            self._client.capture(
                event,
                distinct_id=str(uuid4()),
                properties={**(properties or {}), "$process_person_profile": False},
                disable_geoip=True,
            )
        except Exception:  # noqa: BLE001 - a lost count must never fail the request.
            LOGGER.warning("usage count %s was not queued", event)

    def close(self) -> None:
        if self._client is not None:
            self._client.shutdown()
