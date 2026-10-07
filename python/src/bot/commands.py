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
from uuid import UUID

from clashlens.api_db import AccountContext
from clashlens.api_groups import GroupTooLarge
from clashlens.domain import ranked_day_for

from . import replies
from .replies import Reply, Site


class StoreReads(Protocol):
    def account(self, discord_id: str) -> AccountContext | None: ...
    def players(self, account: AccountContext, now: datetime) -> list[dict[str, Any]]: ...
    def main_tag(self, account: AccountContext) -> str | None: ...
    def set_main(self, account: AccountContext, tag: str) -> bool: ...
    def player_page(self, tag: str, now: datetime) -> dict[str, Any] | None: ...
    def search(self, query: str, now: datetime) -> list[dict[str, Any]]: ...
    def saved(self, account: AccountContext) -> list[dict[str, Any]]: ...
    def board(self, now: datetime, focus_tag: str | None = None) -> dict[str, Any] | None: ...
    def groups(self, account: AccountContext, now: datetime) -> list[dict[str, Any]]: ...
    def group(
        self, account: AccountContext, group_id: str, days: int, now: datetime
    ) -> dict[str, Any] | None: ...


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

    def me(self, account: AccountContext, player: str | None = None, *, show_all: bool = False) -> Reply:
        return self._one_moment(lambda now: self._me(account, player, now, show_all))

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
            return replies.player_status(self.site, card, status, now, choices)
        return self._day(card["tag"], now, choices) or replies.player_status(
            self.site, card, "Being checked", now, choices
        )

    def _day(self, tag: str, now: datetime, choices: tuple[replies.Choice, ...] = ()) -> Reply | None:
        """A player's full day, or their status word when the numbers do not
        apply; None when Clash Lens has no page for the tag."""
        page = self.store.player_page(tag, now)
        if page is None:
            return None
        status = replies.card_status(page)
        if status not in (None, replies.WAITING_FOR_RESET):
            return replies.player_status(self.site, page, status, now, choices)
        return replies.full_day(self.site, page, now, choices=choices)

    def player(self, account: AccountContext, value: str) -> Reply:
        """Any tracked player's day, by tag (with or without "#") or by name."""
        return self._one_moment(lambda now: self._player(value, now))

    def _player(self, value: str, now: datetime) -> Reply:
        text = value.strip()
        tag = tag_text(text)
        is_tag = _TAG.fullmatch(tag) is not None
        if text.startswith("#") or not text:
            if not is_tag:
                return replies.bad_tag()
            return self._day(tag, now) or replies.not_tracked(self.site, tag, now)
        if is_tag:
            reply = self._day(tag, now)
            if reply is not None:
                return reply
        named = [
            result
            for result in self.store.search(text, now)
            if (result["name"] or "").casefold() == text.casefold()
        ]
        if named:
            found = named[0]["tag"]
            return self._day(found, now) or replies.not_tracked(self.site, found, now)
        if is_tag:
            return replies.not_tracked(self.site, tag, now)
        return replies.no_such_player(text)

    def top(self, account: AccountContext) -> Reply:
        return self._one_moment(lambda now: replies.top_reply(self.site, self.store.board(now), now))

    def _choose(
        self,
        account: AccountContext,
        cards: Sequence[Mapping[str, Any]],
        player: str | None,
        action: str,
    ) -> Mapping[str, Any] | Reply:
        """The player a single-player command is about: the one named, the
        only one, or the main; otherwise a dropdown asking which."""
        if player is not None:
            return find_own(cards, player) or replies.not_own()
        if len(cards) == 1:
            return cards[0]
        main_tag = self.store.main_tag(account)
        main = next((card for card in cards if card["tag"] == main_tag), None)
        return main or replies.which_player(cards, action)

    def rank(self, account: AccountContext, player: str | None = None) -> Reply:
        return self._one_moment(lambda now: self._rank(account, player, now))

    def _rank(self, account: AccountContext, player: str | None, now: datetime) -> Reply:
        cards = self.store.players(account, now)
        if not cards:
            return replies.no_players(self.site, account.username, now)
        card = self._choose(account, cards, player, "rank")
        if isinstance(card, Reply):
            return card
        board = self.store.board(now, card["tag"])
        if board is None or not any(entry["tag"] == card["tag"] for entry in board["entries"]):
            status = replies.card_status(card) or "Unranked: not on the Live Leaderboard"
            return replies.player_status(self.site, card, status, now)
        return replies.rank_reply(self.site, board, card["tag"], now)

    def season(self, account: AccountContext, player: str | None = None) -> Reply:
        return self._one_moment(lambda now: self._season(account, player, now))

    def _season(self, account: AccountContext, player: str | None, now: datetime) -> Reply:
        cards = self.store.players(account, now)
        if not cards:
            return replies.no_players(self.site, account.username, now)
        card = self._choose(account, cards, player, "season")
        if isinstance(card, Reply):
            return card
        page = self.store.player_page(card["tag"], now)
        if page is None:
            return replies.player_status(self.site, card, "Being checked", now)
        status = replies.card_status(page)
        has_season = bool((page.get("screen_ready") or {}).get("season_day_starts"))
        if status not in (None, replies.WAITING_FOR_RESET) and not has_season:
            return replies.player_status(self.site, page, status, now)
        return replies.season_reply(self.site, page, now)

    def group(self, account: AccountContext, group: str | None = None, days: int = 7) -> Reply:
        return self._one_moment(lambda now: self._group(account, group, days, now))

    def _group(self, account: AccountContext, group: str | None, days: int, now: datetime) -> Reply:
        groups = self.store.groups(account, now)
        if group is None:
            return (
                replies.groups_list(self.site, groups, days, now)
                if groups
                else replies.no_groups(self.site, now)
            )
        group_id = next((item["group_id"] for item in groups if item["group_id"] == group), None)
        if group_id is None or not _is_uuid(group_id):
            return replies.not_your_group()
        try:
            comparison = self.store.group(account, group_id, days, now)
        except GroupTooLarge:
            return replies.group_too_large(self.site, group_id)
        if comparison is None:
            return replies.not_your_group()
        return replies.group_reply(self.site, comparison, now)

    def main(self, account: AccountContext, player: str | None = None) -> Reply:
        now = self.now()
        cards = self.store.players(account, now)
        main_tag = self._main(account, cards)
        if not cards:
            return replies.no_players(self.site, account.username, now)
        card = None if player is None else find_own(cards, player)
        if player is not None and card is None:
            return replies.not_own()
        if card is None or len(cards) == 1:
            return replies.main_reply(cards, main_tag)
        if not self.store.set_main(account, card["tag"]):
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
        if action == "rank":
            return self.rank(account, tag)
        if action == "season":
            return self.season(account, tag)
        word, _, days = action.partition(" ")
        if word == replies.GROUP_WORD:
            return self.group(account, tag, int(days))
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

    def group_choices(self, discord_id: str) -> list[replies.Choice]:
        """Autocomplete entries: only the person's own groups."""
        account = self.store.account(discord_id)
        if account is None:
            return []
        return [
            replies.Choice(item["name"][:100], item["group_id"])
            for item in self.store.groups(account, self.now())
        ]

    def player_choices(self, discord_id: str, current: str) -> list[replies.Choice]:
        """Autocomplete for /player: the person's own players and saved
        players first, then known players whose name matches what is typed;
        a known player whose tag is exactly what is typed comes first."""
        account = self.store.account(discord_id)
        if account is None:
            return []
        now = self.now()
        text = current.strip()
        choices = [replies.choice_for(card) for card in self.store.players(account, now)]
        choices += [
            replies.choice_for({**item, "trophies": None}) for item in self.store.saved(account)
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


def _is_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False
