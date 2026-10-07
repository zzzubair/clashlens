"""Which reply each command gets.

Every method here runs off Discord's event loop, reads through the store and
returns a `Reply`; nothing here knows about Discord, so tests drive it with a
fake store. The Discord side checks the link first, so methods that take an
account only ever see a connected one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from clashlens.api_db import AccountContext
from clashlens.domain import ranked_day_for

from . import replies
from .replies import Reply, Site


class StoreReads(Protocol):
    def account(self, discord_id: str) -> AccountContext | None: ...
    def players(self, account: AccountContext, now: datetime) -> list[dict[str, Any]]: ...
    def main_tag(self, account: AccountContext) -> str | None: ...
    def set_main(self, account: AccountContext, tag: str) -> bool: ...
    def player_page(self, tag: str, now: datetime) -> dict[str, Any] | None: ...
    def live_rank(self, tag: str, now: datetime) -> int | None: ...


def tag_text(value: str) -> str:
    """A typed tag as the website reads it: trimmed, upper case, "#" first."""
    text = value.strip().upper()
    return text if text.startswith("#") else f"#{text}"


def find_own(cards: Sequence[Mapping[str, Any]], value: str) -> Mapping[str, Any] | None:
    """The person's own player named by a picked or typed tag."""
    tag = tag_text(value)
    return next((card for card in cards if card["tag"] == tag), None)


class Commands:
    def __init__(
        self,
        store: StoreReads,
        site: Site,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.site = site
        self.now = now

    def account(self, discord_id: str) -> AccountContext | None:
        return self.store.account(discord_id)

    def _main(self, account: AccountContext, cards: Sequence[Mapping[str, Any]]) -> str | None:
        saved = self.store.main_tag(account)
        return cards[0]["tag"] if len(cards) == 1 else saved

    def help(self, discord_id: str) -> Reply:
        now = self.now()
        account = self.store.account(discord_id)
        if account is None:
            return replies.help_reply(self.site, None, 0, now)
        players = self.store.players(account, now)
        return replies.help_reply(self.site, account.username, len(players), now)

    def link(self, discord_id: str, discord_name: str) -> Reply:
        now = self.now()
        account = self.store.account(discord_id)
        if account is None:
            return replies.not_linked(self.site, discord_name, now)
        cards = self.store.players(account, now)
        return replies.link_reply(
            self.site, account.username, cards, self._main(account, cards), now
        )

    def me(self, account: AccountContext, player: str | None = None, *, show_all: bool = False) -> Reply:
        # One moment for every read and the reply. Data saved after a Reset
        # that passed during the reads may belong to the next Legend day, so
        # then everything is read again for that day.
        now = self.now()
        reply = self._me(account, player, now, show_all)
        later = self.now()
        if ranked_day_for(later).start != ranked_day_for(now).start:
            return self._me(account, player, later, show_all)
        return reply

    def _me(
        self, account: AccountContext, player: str | None, now: datetime, show_all: bool
    ) -> Reply:
        cards = self.store.players(account, now)
        main_tag = self._main(account, cards)
        if not cards:
            return replies.no_players(self.site, account.username, now)
        if player is None and len(cards) > 1:
            return replies.me_overview(self.site, cards, main_tag, now, show_all=show_all)
        card = cards[0] if player is None else find_own(cards, player)
        if card is None:
            return replies.not_own()
        choices = (
            tuple(replies.choice_for(item) for item in replies.ordered(cards, None))
            if len(cards) > 1
            else ()
        )[: replies.MAX_CHOICES]
        status = replies.card_status(card)
        if status not in (None, replies.WAITING_FOR_RESET):
            return replies.player_status(self.site, card, status, choices)
        page = self.store.player_page(card["tag"], now)
        if page is None:
            return replies.player_status(self.site, card, "Being checked", choices)
        status = replies.card_status(page)
        if status not in (None, replies.WAITING_FOR_RESET):
            return replies.player_status(self.site, page, status, choices)
        return replies.full_day(
            self.site, page, self.store.live_rank(card["tag"], now), now, choices=choices
        )

    def main(self, account: AccountContext, player: str | None = None) -> Reply:
        now = self.now()
        cards = self.store.players(account, now)
        main_tag = self._main(account, cards)
        if not cards:
            return replies.no_players(self.site, account.username, now)
        if player is None:
            return replies.main_reply(cards, main_tag)
        card = find_own(cards, player)
        if card is None or not self.store.set_main(account, card["tag"]):
            return replies.not_own()
        return replies.main_reply(cards, card["tag"], changed=True)

    def pick(self, discord_id: str, action: str, tag: str) -> Reply | None:
        """A dropdown choice; None when this Discord user is no longer connected."""
        account = self.store.account(discord_id)
        if account is None:
            return None
        if action == "main":
            return self.main(account, tag)
        if action == "all":
            return self.me(account, show_all=True)
        return self.me(account, tag)

    def keep(self, discord_id: str, reply: Reply) -> Reply | None:
        """`reply` again while this Discord user's account still has every
        player it lists; None when this Discord user is no longer connected."""
        account = self.store.account(discord_id)
        if account is None:
            return None
        own = {card["tag"] for card in self.store.players(account, self.now())}
        return reply if set(reply.shown) <= own else replies.not_own()

    def own_choices(self, discord_id: str) -> list[replies.Choice]:
        """Autocomplete entries: only the person's own verified players."""
        account = self.store.account(discord_id)
        if account is None:
            return []
        cards = self.store.players(account, self.now())
        main_tag = self._main(account, cards)
        return [replies.choice_for(card) for card in replies.ordered(cards, main_tag)]
