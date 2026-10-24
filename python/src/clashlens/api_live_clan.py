"""The player page's live clan, read from the game with the interactive key.

It is shown only: the answer is never saved. Each player's answer is kept in
memory for a minute, as long as the game's own player cache.
"""

from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from . import api_verification
from .api_db import ApiDatabase
from .verification import (
    KeyAction,
    OfficialVerificationResponse,
    VerificationTransportError,
    classify_official_response,
)

LIVE_CLAN_SECONDS = 60
LIVE_CLAN_TIMEOUT_SECONDS = 1.5
# About 4,096 short entries, well under a megabyte.
MAX_REMEMBERED_PLAYERS = 4096
# As loose as the saved clan tag, which the page already links.
_CLAN_TAG = re.compile(r"#[0-9A-Z]{2,15}")


class PlayerReader(Protocol):
    def player(
        self, normalized_tag: str, *, timeout_seconds: float
    ) -> OfficialVerificationResponse: ...


@dataclass(frozen=True, slots=True)
class LiveClan:
    """``live`` is false when the game gave no answer; the page then shows the saved clan."""

    live: bool
    name: str | None = None
    tag: str | None = None


UNAVAILABLE = LiveClan(False)


def acquire_live_read_permit(database: ApiDatabase, fingerprint: str) -> bool:
    """Take one start from collection's share of the interactive key, never waiting.

    Live reads use collection's share, so verification's reserved start is
    never touched, and only while collection's share is less than half used,
    so Refresh and first-time collection always keep the other half.
    """
    with database.pool.connection() as connection:
        with connection.transaction():
            # Locking the key's row orders this check with every other start.
            budget = connection.execute(
                """
                SELECT collector_budget FROM shared_api_credentials
                WHERE credential_fingerprint = %s FOR UPDATE
                """,
                (fingerprint,),
            ).fetchone()
            if budget is None:
                return False
            used = connection.execute(
                """
                SELECT count(*) FROM shared_api_permits
                WHERE credential_fingerprint = %s AND caller = 'collector'
                  AND permitted_at > clock_timestamp() - interval '1 second'
                """,
                (fingerprint,),
            ).fetchone()
            assert used is not None
            if 2 * (int(used[0]) + 1) > int(budget[0]):
                return False
            granted = connection.execute(
                "SELECT granted FROM clashlens_acquire_shared_api_permit(%s, 'collector')",
                (fingerprint,),
            ).fetchone()
            return granted is not None and bool(granted[0])


class LiveClans:
    def __init__(
        self,
        database: ApiDatabase,
        reader: PlayerReader | None,
        fingerprint: str | None,
        *,
        cooldown_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._database = database
        self._reader = reader
        self._fingerprint = fingerprint
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = Lock()
        self._answers: OrderedDict[str, tuple[float, LiveClan]] = OrderedDict()
        self._reading: dict[str, Future[LiveClan]] = {}

    def read(self, normalized_tag: str) -> LiveClan:
        now = self._clock()
        with self._lock:
            remembered = self._answers.get(normalized_tag)
            if remembered is not None and remembered[0] > now:
                return remembered[1]
            # A view while the same player is being read waits for that answer.
            reading = self._reading.get(normalized_tag)
            if reading is None:
                self._reading[normalized_tag] = Future()
        if reading is not None:
            return reading.result()
        try:
            answer = self._ask(normalized_tag)
        except BaseException as error:
            with self._lock:
                self._reading.pop(normalized_tag).set_exception(error)
            raise
        with self._lock:
            if answer is not None:
                self._answers[normalized_tag] = (now + LIVE_CLAN_SECONDS, answer)
                self._answers.move_to_end(normalized_tag)
                while len(self._answers) > MAX_REMEMBERED_PLAYERS:
                    self._answers.popitem(last=False)
            answer = answer or UNAVAILABLE
            self._reading.pop(normalized_tag).set_result(answer)
        return answer

    def _ask(self, normalized_tag: str) -> LiveClan | None:
        """The game's answer, or None when no request was made, so nothing is remembered."""
        if self._reader is None or self._fingerprint is None:
            return None
        if not acquire_live_read_permit(self._database, self._fingerprint):
            return None
        try:
            response = self._reader.player(
                normalized_tag, timeout_seconds=LIVE_CLAN_TIMEOUT_SECONDS
            )
        except VerificationTransportError:
            return UNAVAILABLE
        action = classify_official_response(response.http_status, response.body).key_action
        if action is not KeyAction.NONE:
            api_verification.apply_official_key_action(
                self._database,
                self._fingerprint,
                action,
                cooldown_seconds=self._cooldown_seconds,
            )
        return _live_clan(response, normalized_tag)


def _live_clan(response: OfficialVerificationResponse, normalized_tag: str) -> LiveClan:
    if response.http_status != 200:
        return UNAVAILABLE
    try:
        profile = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return UNAVAILABLE
    if not isinstance(profile, dict) or profile.get("tag") != normalized_tag:
        return UNAVAILABLE
    clan = profile.get("clan")
    if clan is None:
        return LiveClan(True)
    if not isinstance(clan, dict):
        return UNAVAILABLE
    name, tag = clan.get("name"), clan.get("tag")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(tag, str)
        or not _CLAN_TAG.fullmatch(tag)
    ):
        return UNAVAILABLE
    return LiveClan(True, name, tag)
