"""Which reply each command gets.

Every method here runs off Discord's event loop, reads through the store and
returns a `Reply`; nothing here knows about Discord, so tests drive it with a
fake store. Each command looks up who is typing itself: anyone can look up a
player with /stats, and someone whose Discord is not connected to Clash Lens
is offered sign-up instead of their own linked accounts.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from clashlens.api_db import AccountContext
from clashlens.domain import ranked_day_for

from . import picture, replies
from .replies import Reply, Site

log = logging.getLogger("clashlens.bot.commands")


class StoreReads(Protocol):
    def account(self, discord_id: str) -> AccountContext | None: ...
    def players(self, account: AccountContext, now: datetime) -> list[dict[str, Any]]: ...
    def colours(self, account: AccountContext) -> dict[str, int]: ...
    def default_tag(self, account: AccountContext) -> str | None: ...
    def alert_switches(self, account: AccountContext) -> list[dict[str, Any]]: ...
    def dark_picture(self, account: AccountContext) -> bool: ...
    def player_page(self, tag: str, now: datetime) -> dict[str, Any] | None: ...
    def search(self, query: str, now: datetime) -> list[dict[str, Any]]: ...


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

    def _default(self, account: AccountContext, cards: Sequence[Mapping[str, Any]]) -> str | None:
        """The player commands use when none is named: the only one, or the
        default player chosen on the website, otherwise the first linked one
        Clash Lens tracks."""
        chosen = self.store.default_tag(account)
        return cards[0]["tag"] if len(cards) == 1 else chosen

    def help(self, discord_id: str) -> Reply:
        now = self.now()
        account = self.store.account(discord_id)
        if account is None:
            return replies.help_reply(self.site, None, 0)
        players = self.store.players(account, now)
        return replies.help_reply(self.site, account.username, len(players))

    def link(self, discord_id: str, discord_name: str) -> Reply:
        now = self.now()
        account = self.store.account(discord_id)
        if account is None:
            return replies.not_linked(self.site, discord_name)
        cards = self.store.players(account, now)
        if not cards:
            return replies.no_players(self.site, account.username)
        return replies.link_reply(
            self.site, account.username, cards, self._default(account, cards),
            self.store.colours(account),
        )

    def alerts(self, discord_id: str, discord_name: str) -> Reply:
        account = self.store.account(discord_id)
        if account is None:
            return replies.not_linked(
                self.site, discord_name,
                "get attack alerts and Legend day summaries for it in your Discord DMs",
            )
        switches = self.store.alert_switches(account)
        if not switches:
            return replies.no_players(self.site, account.username)
        return replies.alerts_reply(
            self.site, account.username, switches, self.store.colours(account)
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

    def stats(self, discord_id: str, discord_name: str, player: str | None = None) -> Reply:
        """Today and this Season so far for any tracked player named by tag or
        name, for anyone; or with no player, for the person's default or only
        linked account, which needs their Discord connected."""
        return self._one_moment(
            lambda now: self._stats(self.store.account(discord_id), discord_name, player, now)
        )

    def _stats(
        self, account: AccountContext | None, discord_name: str, player: str | None, now: datetime
    ) -> Reply:
        if player is not None and player.strip():
            tag = self._find(player, now)
            if isinstance(tag, Reply):
                return tag
        elif account is None:
            return replies.name_a_player()
        else:
            cards = self.store.players(account, now)
            if not cards:
                return replies.name_a_player()
            tag = self._default(account, cards)
            if tag is None:
                return replies.which_player(self.site, cards, "stats")
        page = self.store.player_page(tag, now)
        if page is None:
            return replies.not_tracked(self.site, tag)
        else:
            # The person's own linked accounts in their colour, anyone else in
            # Clash Lens yellow.
            colour = (
                replies.GOLD if account is None
                else self.store.colours(account).get(tag, replies.GOLD)
            )
            status = replies.card_status(page)
            has_season = bool((page.get("screen_ready") or {}).get("season_day_starts"))
            if status not in (None, replies.WAITING_FOR_RESET) and not has_season:
                return replies.player_status(self.site, page, status, now, colour)
            # Today's picture, dark when the person's own account chose it,
            # left out while too many pictures wait to be drawn.
            day = picture.legend_day(page, ranked_day_for(now).start)
            drawn = None
            if day is not None:
                dark = account is not None and self.store.dark_picture(account)
                try:
                    drawn = picture.drawing(day, dark).result()
                except picture.Busy:
                    log.warning("/stats picture left out: too many pictures waiting")
            return replies.stats_reply(self.site, page, now, colour, drawn)

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

    def pick(self, discord_id: str, action: str, tag: str) -> Reply:
        """A dropdown choice; which player to look up when this Discord user
        is no longer connected, as for /stats with no player."""
        account = self.store.account(discord_id)
        if account is None:
            return replies.name_a_player()
        if find_own(self.store.players(account, self.now()), tag) is None:
            return replies.not_own()
        return self._one_moment(lambda now: self._stats(account, "", tag, now))

    def player_choices(self, discord_id: str, current: str) -> list[replies.Choice]:
        """Autocomplete for /stats: the person's own players first, then known
        players whose name matches what is typed; a known player whose tag is
        exactly what is typed comes first."""
        account = self.store.account(discord_id)
        now = self.now()
        text = current.strip()
        choices = (
            [] if account is None
            else [replies.choice_for(card) for card in self.store.players(account, now)]
        )
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


