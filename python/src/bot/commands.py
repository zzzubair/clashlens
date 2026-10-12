"""Which reply each command gets.

Every method here runs off Discord's event loop, reads through the store and
returns a `Reply`; nothing here knows about Discord, so tests drive it with a
fake store. The Discord side checks the link first, so methods that take an
account only ever see a connected one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from clashlens.api_db import AccountContext
from clashlens.domain import ranked_day_for

from . import replies
from .replies import Reply, Site


class StoreReads(Protocol):
    def account(self, discord_id: str) -> AccountContext | None: ...
    def players(self, account: AccountContext, now: datetime) -> list[dict[str, Any]]: ...
    def default_tag(self, account: AccountContext) -> str | None: ...
    def player_page(self, tag: str, now: datetime) -> dict[str, Any] | None: ...
    def search(self, query: str, now: datetime) -> list[dict[str, Any]]: ...
    def groups(self, account: AccountContext, now: datetime) -> list[dict[str, Any]]: ...


T = TypeVar("T")
# The game's player tag letters, as the website checks them.
_TAG = re.compile(r"#[0289PYLQGRJCUV]{3,15}")


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

    def _default(self, account: AccountContext, cards: Sequence[Mapping[str, Any]]) -> str | None:
        """The player commands use when none is named: the only one, or the
        default player chosen on the website."""
        chosen = self.store.default_tag(account)
        return cards[0]["tag"] if len(cards) == 1 else chosen

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
            self.site, account.username, cards, self._default(account, cards), now
        )

    def _one_moment(self, work: Callable[[datetime], T]) -> T:
        """One moment for every read and the reply. Data saved after a Reset
        that passed during the reads may belong to the next Legend day, so
        then everything is read again for that day."""
        now = self.now()
        result = work(now)
        later = self.now()
        if ranked_day_for(later).start != ranked_day_for(now).start:
            return work(later)
        return result

    def stats(self, account: AccountContext, player: str | None = None) -> Reply:
        """This Season so far for any tracked player named by tag or name, or
        with no player for the person's default or only verified player."""
        return self._one_moment(lambda now: self._stats(account, player, now))

    def _stats(self, account: AccountContext, player: str | None, now: datetime) -> Reply:
        if player is not None and player.strip():
            tag = self._find(player, now)
            if isinstance(tag, Reply):
                return tag
        else:
            cards = self.store.players(account, now)
            if not cards:
                return replies.no_players(self.site, account.username, now)
            tag = self._default(account, cards)
            if tag is None:
                return replies.which_player(self.site, cards, "stats")
        page = self.store.player_page(tag, now)
        if page is None:
            return replies.not_tracked(self.site, tag, now)
        status = replies.card_status(page)
        has_season = bool((page.get("screen_ready") or {}).get("season_day_starts"))
        if status not in (None, replies.WAITING_FOR_RESET) and not has_season:
            return replies.player_status(self.site, page, status, now)
        return replies.stats_reply(self.site, page, now)

    def _find(self, value: str, now: datetime) -> str | Reply:
        """The tag of a player typed as a tag (with or without "#") or a name."""
        text = value.strip()
        tag = tag_text(text)
        is_tag = _TAG.fullmatch(tag) is not None
        if text.startswith("#"):
            return tag if is_tag else replies.bad_tag()
        if is_tag and self.store.player_page(tag, now) is not None:
            return tag
        named = [
            result
            for result in self.store.search(text, now)
            if (result["name"] or "").casefold() == text.casefold()
        ]
        if named:
            return named[0]["tag"]
        return tag if is_tag else replies.no_such_player(text)

    def pick(self, discord_id: str, action: str, tag: str) -> Reply | None:
        """A dropdown choice; None when this Discord user is no longer connected."""
        account = self.store.account(discord_id)
        if account is None:
            return None
        if find_own(self.store.players(account, self.now()), tag) is None:
            return replies.not_own()
        return self.stats(account, tag)

    def keep(self, discord_id: str, reply: Reply) -> Reply | None:
        """`reply` again while this Discord user's account still has every
        player it lists; None when this Discord user is no longer connected."""
        account = self.store.account(discord_id)
        if account is None:
            return None
        own = {card["tag"] for card in self.store.players(account, self.now())}
        return reply if set(reply.shown) <= own else replies.not_own()

    def player_choices(self, discord_id: str, current: str) -> list[replies.Choice]:
        """Autocomplete for /stats: the person's own players and the players
        in their groups first, then known players whose name matches what is typed;
        a known player whose tag is exactly what is typed comes first."""
        account = self.store.account(discord_id)
        if account is None:
            return []
        now = self.now()
        text = current.strip()
        choices = [replies.choice_for(card) for card in self.store.players(account, now)]
        choices += [
            replies.choice_for(player)
            for group in self.store.groups(account, now)
            for player in group["players"]
        ]
        if text:
            choices += [replies.choice_for(item) for item in self.store.search(text, now)]
        tag = tag_text(text)
        page = self.store.player_page(tag, now) if text and _TAG.fullmatch(tag) else None
        if page is not None:
            trophies = None if page["season_reset_pending"] else page["trophies"]
            choices.insert(0, replies.choice_for({**page, "trophies": trophies}))
        choices.sort(key=lambda choice: choice.value != tag)
        wanted = text.casefold()
        unique: dict[str, replies.Choice] = {}
        for choice in choices:
            if wanted in choice.label.casefold() and choice.value not in unique:
                unique[choice.value] = choice
        return list(unique.values())[: replies.MAX_CHOICES]


