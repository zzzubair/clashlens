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

from . import replies
from .replies import Choice, Reply, Site


class StoreReads(Protocol):
    def account(self, discord_id: str) -> AccountContext | None: ...
    def players(self, account: AccountContext) -> list[dict[str, Any]]: ...
    def main_tag(self, account: AccountContext) -> str | None: ...
    def set_main(self, account: AccountContext, tag: str) -> bool: ...
    def player_page(self, tag: str) -> dict[str, Any] | None: ...
    def live_rank(self, tag: str) -> int | None: ...


def tag_text(value: str) -> str:
    """A typed tag as the game writes it: "#" first, upper case, no spaces."""
    text = "".join(value.split()).upper().replace("O", "0")
    return text if text.startswith("#") else f"#{text}"


def find_own(cards: Sequence[Mapping[str, Any]], value: str) -> Mapping[str, Any] | None:
    """The person's own player named by a picked tag, a typed tag or a name."""
    tag = tag_text(value)
    for card in cards:
        if card["tag"] == tag:
            return card
    wanted = " ".join(value.split()).casefold()
    named = [card for card in cards if (card["name"] or "").casefold() == wanted]
    return named[0] if len(named) == 1 else None


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
        if len(cards) == 1:
            return cards[0]["tag"]
        return self.store.main_tag(account) if cards else None

    def help(self, discord_id: str) -> Reply:
        account = self.store.account(discord_id)
        if account is None:
            return replies.help_reply(self.site, None, 0)
        return replies.help_reply(self.site, account.username, len(self.store.players(account)))

    def link(self, discord_id: str, discord_name: str) -> Reply:
        account = self.store.account(discord_id)
        if account is None:
            return replies.not_linked(self.site, discord_name)
        cards = self.store.players(account)
        return replies.link_reply(self.site, account.username, cards, self._main(account, cards))

    def me(self, account: AccountContext, player: str | None = None, *, show_all: bool = False) -> Reply:
        cards = self.store.players(account)
        if not cards:
            return replies.no_players(self.site, account.username)
        if player is None and len(cards) > 1:
            return replies.me_overview(
                self.site, cards, self._main(account, cards), self.now(), show_all=show_all
            )
        card = cards[0] if player is None else find_own(cards, player)
        if card is None:
            return replies.not_own()
        choices = (
            tuple(replies.choice_for(item) for item in replies.ordered(cards, None))
            if len(cards) > 1
            else ()
        )
        return self._day(card["tag"], choices[: replies.MAX_CHOICES])

    def _day(self, tag: str, choices: tuple[Choice, ...] = ()) -> Reply:
        page = self.store.player_page(tag)
        if page is None:
            return Reply(
                f"Clash Lens hasn't read {tag} yet. Check back in a few minutes.",
                links=(replies.Link("Open on Clash Lens", self.site.player(tag)),),
            )
        return replies.full_day(
            self.site, page, self.store.live_rank(tag), self.now(), choices=choices
        )

    def main(self, account: AccountContext, player: str | None = None) -> Reply:
        cards = self.store.players(account)
        if not cards:
            return replies.no_players(self.site, account.username)
        if player is None:
            return replies.main_reply(cards, self._main(account, cards))
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

    def own_choices(self, discord_id: str) -> list[Choice]:
        """Autocomplete entries: only the person's own verified players."""
        account = self.store.account(discord_id)
        if account is None:
            return []
        cards = self.store.players(account)
        main_tag = self._main(account, cards)
        return [replies.choice_for(card) for card in replies.ordered(cards, main_tag)]
