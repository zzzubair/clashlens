"""Everything the bot says, as plain data.

Nothing here talks to Discord or the database, so every reply can be checked
from fixture data alone. Game and group names are escaped so their markup or
an @mention in a name cannot ping anyone or break the layout.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from discord.utils import escape_markdown, escape_mentions

from clashlens.domain import ranked_day_for

# The product word for an account's private player lists is still open
# (groups, saved players, crews); renaming it here renames it everywhere.
GROUP_WORD = "group"
# Discord shows at most 25 dropdown entries, 100 characters per entry label
# and 80 per button label, and refuses an embed description over 4,096.
MAX_CHOICES = 25
_MAX_CHOICE_LABEL = 100
_MAX_DESCRIPTION = 4000
# /me lists this many players before a "Show all" button.
COMPACT_LIMIT = 4
_DAILY_BATTLES = 8
WAITING_FOR_RESET = "Waiting for Season reset"

# One line per command for /help, also used as each command's description.
COMMANDS = (
    ("help", "What the bot does, its commands and your connection"),
    ("link", "Connect Discord to Clash Lens, or see what is connected"),
    ("me", "Your Legend day: one line per player, or the full day for one"),
    ("main", "Choose the player commands use when you don't pick one"),
)

_STATE_WORDS = {
    "not_in_legend": "Not in Legend",
    "checking": "Being checked",
    "uncertain": "Being checked",
    "unknown": "Not tracked yet",
    "not_found": "Not found in the game",
    "failed": "Check failed",
}
_REASON_WORDS = {
    "pending": "Being checked",
    "no_legend_battles": "No Legend battles yet",
    "season_unconfirmed": "Season not confirmed yet",
    "unknown_tier": "League not recognised",
    "profile_rejected": "Profile could not be read",
}
# The player page's data notes in the bot's words.
_NOTE_WORDS = {
    "Stale saved profile": "Profile not refreshed recently",
    "Missing current ranked-day data": "Nothing recorded for this Legend day yet",
    "Incomplete ranked-day data": "Some of this Legend day's battles may be missing",
    "Waiting for this player's Season reset": "Waiting for this player's Season reset",
    "Season boundary conflict": "Season days are held back while Clash Lens checks the Season start",
}


@dataclass(frozen=True, slots=True)
class Link:
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class Choice:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class Reply:
    body: str
    title: str | None = None
    footer: str | None = None
    links: tuple[Link, ...] = ()
    # A dropdown of the person's own players; picking one runs `pick`
    # ("day" or "main") for that player.
    choices: tuple[Choice, ...] = ()
    pick: str | None = None
    placeholder: str | None = None
    # How many players a "Show all" button lists, when /me cut its lines.
    show_all: int | None = None
    # When the oldest data shown was read, or "pending" or "Unavailable"; the
    # bot works out "Updated N min ago" from it as each message goes out.
    updated: datetime | str = "Unavailable"
    # The tags of the players the reply lists.
    shown: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Site:
    """Links into the Clash Lens website."""

    origin: str

    def url(self, path: str) -> str:
        return f"{self.origin.rstrip('/')}{path}"

    def player(self, tag: str) -> str:
        return self.url(f"/players/{quote(tag, safe='')}")


def safe(text: str | None) -> str:
    """A game or account name as plain text: no markup, no pings."""
    if not text:
        return "Unnamed"
    return escape_mentions(escape_markdown(" ".join(text.split())))


def number(value: int) -> str:
    return f"{value:,}"


def signed(value: int) -> str:
    return f"+{value:,}" if value >= 0 else f"−{-value:,}"


def oldest(times: Sequence[datetime | None]) -> datetime | str:
    """When the oldest shown profile was seen, so no number looks newer than it is."""
    known = [moment for moment in times if moment is not None]
    return min(known) if known else "pending"


def updated_line(updated: datetime | str, now: datetime) -> str:
    if isinstance(updated, str):
        return f"Updated: {updated}"
    minutes = max(0, int((now - updated).total_seconds())) // 60
    return f"Updated {number(minutes)} min ago"


def discord_time(moment: datetime, style: str) -> str:
    """Discord shows this in each viewer's own time zone."""
    return f"<t:{int(moment.timestamp())}:{style}>"


def reset_line(now: datetime) -> str:
    day = ranked_day_for(now)
    return (
        f"Legend day {day.day_number} of 28 · Reset {discord_time(day.end, 't')}"
        f" ({discord_time(day.end, 'R')})"
    )


def unavailable() -> Reply:
    return Reply("Clash Lens is unavailable right now, try again in a minute.")


def slow() -> Reply:
    return Reply("Clash Lens is taking longer than usual. Try again.")


def failed() -> Reply:
    return Reply("Something went wrong on Clash Lens's side. Try again in a minute.")


def not_own() -> Reply:
    return Reply("That player isn't linked to your Clash Lens account.")


def not_your_menu() -> Reply:
    return Reply("Only the person who ran this command can use these buttons.")


def not_linked(site: Site, discord_name: str, now: datetime) -> Reply:
    return Reply(
        f"Clash Lens doesn't know this Discord account (@{safe(discord_name)}) yet.\n\n"
        "**Have a Clash Lens account?** Sign in on the site, open sign-in "
        "connections and connect Discord. This works for Google sign-ins too.\n"
        "**New here?** Create an account with Discord.\n\n"
        "Connect the same Discord account you are chatting from, then run /link again.",
        title="Connect Discord to Clash Lens",
        updated=now,
        links=(
            Link("I have an account: connect Discord", site.url("/account/providers")),
            Link(
                "New here: create an account with Discord",
                site.url("/auth/discord?returnPath=%2Faccount"),
            ),
        ),
    )


def no_players(site: Site, username: str, now: datetime) -> Reply:
    return Reply(
        f"Connected as @{safe(username)}. No player linked yet.\n\n"
        "Link your Clash of Clans player on the site with:\n"
        "1. Your player tag, from your in-game profile.\n"
        "2. Your API token, from the game: Settings → More Settings → API Token.",
        updated=now,
        links=(Link("Link a player", site.url("/account/verify-player")),),
    )


def help_reply(site: Site, username: str | None, player_count: int, now: datetime) -> Reply:
    lines = [
        "Clash Lens shows how you and other Legend League players are really doing.",
        "",
        *(f"**/{name}** · {text}" for name, text in COMMANDS),
        "",
        (
            "Every command except /help and /link needs this Discord account "
            "connected to Clash Lens. In servers, replies are private to you."
        ),
        "",
    ]
    if username is None:
        lines.append("You: not connected yet, use /link.")
    else:
        players = "1 verified player" if player_count == 1 else f"{player_count} verified players"
        lines.append(f"You: connected as @{safe(username)}, {players}.")
    return Reply(
        "\n".join(lines),
        title="Clash Lens",
        updated=now,
        links=(Link("Open Clash Lens", site.url("/")),),
    )


def link_reply(
    site: Site,
    username: str,
    cards: Sequence[Mapping[str, Any]],
    main_tag: str | None,
    now: datetime,
) -> Reply:
    if not cards:
        return no_players(site, username, now)
    main = next((card for card in cards if card["tag"] == main_tag), None)
    lines = [
        f"Connected as @{safe(username)}.",
        "Players:",
        *(f"• {safe(card['name'])} {card['tag']}" for card in cards),
    ]
    if main is not None:
        lines.append(f"Main: {safe(main['name'])}.")
    elif len(cards) > 1:
        lines.append("No main chosen yet: use /main to pick one.")
    return Reply(
        "\n".join(lines),
        updated=oldest([card["observed_at"] for card in cards]),
        shown=tuple(card["tag"] for card in cards),
        links=(Link("Manage on Clash Lens", site.url("/account")),),
    )


def card_status(card: Mapping[str, Any]) -> str | None:
    """Words that replace a player's numbers when they do not apply."""
    if card["state"] != "tracking":
        return _STATE_WORDS.get(card["state"], "Being checked")
    if card.get("reason"):
        return _REASON_WORDS.get(card["reason"], "Being checked")
    if card["season_reset_pending"]:
        return WAITING_FOR_RESET
    if card["trophies"] is None:
        return "Being checked"
    return None


def _known(value: int | None, show: Callable[[int], str]) -> str:
    return "Unavailable" if value is None else show(value)


def _of_eight(count: int) -> str:
    return f"{count}/8"


def card_line(card: Mapping[str, Any], *, main: bool = False) -> str:
    """One player's day so far on one line."""
    head = f"**{safe(card['name'])}** {card['tag']}" + (" (main)" if main else "")
    status = card_status(card)
    if status is not None:
        return f"{head} · {status}"
    parts = [head, f"{number(card['trophies'])} 🏆"]
    parts.append("Unranked" if card["rank"] is None else f"#{number(card['rank'])}")
    today = card["today"]
    if today is None:
        parts += ["no battles recorded today", "net pending"]
    else:
        parts.append(f"⚔ {_known(today['attacks'], _of_eight)}")
        parts.append(f"🛡 {_known(today['defenses'], _of_eight)}")
        net = today["net"]
        parts.append("net pending" if net is None else f"net {signed(net)} so far")
    return " · ".join(parts)


def choice_for(card: Mapping[str, Any]) -> Choice:
    """A dropdown or autocomplete entry: "Name #TAG · 5,842"."""
    label = f"{' '.join((card['name'] or 'Unnamed').split())} {card['tag']}"
    if card.get("trophies") is not None:
        label += f" · {number(card['trophies'])}"
    return Choice(label[:_MAX_CHOICE_LABEL], card["tag"])


def ordered(cards: Sequence[Mapping[str, Any]], main_tag: str | None) -> list[Mapping[str, Any]]:
    """Main first, then the most trophies, then by tag."""
    return sorted(
        cards,
        key=lambda card: (
            card["tag"] != main_tag,
            card["trophies"] is None,
            -(card["trophies"] or 0),
            card["tag"],
        ),
    )


def me_overview(
    site: Site,
    cards: Sequence[Mapping[str, Any]],
    main_tag: str | None,
    now: datetime,
    *,
    show_all: bool = False,
) -> Reply:
    players = ordered(cards, main_tag)
    cut = not show_all and len(players) > COMPACT_LIMIT
    shown = players[:COMPACT_LIMIT] if cut else players
    lines = [reset_line(now), ""]
    lines += [card_line(card, main=card["tag"] == main_tag) for card in shown]
    return Reply(
        "\n".join(lines),
        title="Your Legend day",
        footer="Ranks are among the players Clash Lens tracks, not official world ranks.",
        updated=oldest([card["observed_at"] for card in shown]),
        shown=tuple(card["tag"] for card in shown),
        links=(Link("Open Clash Lens", site.url("/account")),),
        choices=tuple(choice_for(card) for card in players[:MAX_CHOICES]),
        pick="day",
        placeholder="Full day for…",
        show_all=len(players) if cut else None,
    )


def _player_title(item: Mapping[str, Any]) -> str:
    title = f"{safe(item['name'])} {item['tag']}"
    return f"{title} · {safe(item['clan'])}" if item.get("clan") else title


def player_status(
    site: Site, card: Mapping[str, Any], status: str, choices: tuple[Choice, ...] = ()
) -> Reply:
    """A player whose numbers do not apply right now: the status word instead,
    from a card or a player page read."""
    return Reply(
        status,
        title=_player_title(card),
        updated=oldest([card["observed_at"]]),
        links=(Link("Open on Clash Lens", site.player(card["tag"])),),
        choices=choices,
        pick="day" if choices else None,
        placeholder="Full day for…" if choices else None,
    )


def _stars(stars: int) -> str:
    return "⭐" * stars if stars else "0⭐"


def _opponent(event: Mapping[str, Any]) -> str:
    opponent = event["opponent"]
    return safe(opponent.get("name")) if opponent.get("name") else opponent["tag"]


def _battle_lines(events: Sequence[Mapping[str, Any]], word: str) -> list[str]:
    # The page lists the newest battle first; the bot reads in time order.
    return [
        f"{_stars(event['stars'])} {event['destruction_percentage']}% · "
        f"{signed(event['trophy_change'])} · {word} {_opponent(event)}"
        for event in reversed(events)
    ]


def full_day(
    site: Site,
    page: Mapping[str, Any],
    now: datetime,
    *,
    choices: tuple[Choice, ...] = (),
) -> Reply:
    """One player's whole Legend day so far, from the player page read."""
    ready = page["screen_ready"]
    today = next(
        (day for day in ready["days"] if day["ranked_day_start"] == ready["current_day_start"]),
        None,
    )
    if page["season_reset_pending"]:
        summary = [WAITING_FOR_RESET]
    else:
        summary = [f"{number(page['trophies'])} 🏆"]
    rank = page["rank"]
    summary.append("Unranked" if rank is None else f"#{number(rank)} among tracked")
    day = today or {}
    day_number = day.get("season_day_number") or (ready.get("season") or {}).get(
        "current_day_number"
    )
    summary.append(f"Legend day {day_number} of 28" if day_number else "Legend day Unavailable")
    start = day.get("start_trophies")
    summary.append(
        "start trophies Unavailable" if start is None else f"started today at {number(start)}"
    )
    lines = [" · ".join(summary), ""]
    if today is None:
        lines.append("No battles recorded for this Legend day yet.")
    else:
        loss = today["defense_loss"]
        lines.append(
            f"**Attacks** {_known(today['attack_count'], _of_eight)} · "
            f"{_known(today['attack_gain'], signed)} · "
            f"{_known(today['attack_three_star_count'], str)} three-stars"
        )
        lines += _battle_lines(today["offense_events"], "vs")
        lines.append(
            f"**Defenses** {_known(today['defense_count'], _of_eight)} · "
            f"{_known(None if loss is None else -loss, signed)} · "
            f"{_known(today['defense_three_star_count'], str)} three-stars given up"
        )
        lines += _battle_lines(today["defense_events"], "by")
    lines.append("")
    if today is not None and today["battles_complete"]:
        lines.append(f"Net so far: {signed(today['attack_gain'] - today['defense_loss'])}")
    else:
        lines.append("Net pending: Clash Lens has not recorded every battle yet.")
    defenses = day.get("defense_count")
    if defenses is not None and defenses < _DAILY_BATTLES:
        lines.append(
            "Fewer than 8 defenses so far: the game applies an automatic "
            "defense loss at Reset; the amount shows after Reset."
        )
    notes = [_NOTE_WORDS.get(note["label"], note["label"]) for note in ready["data_quality"]]
    if notes:
        lines += ["", *(f"⚠ {note}" for note in dict.fromkeys(notes))]
    end = ranked_day_for(now).end
    lines += ["", f"Reset {discord_time(end, 't')} ({discord_time(end, 'R')})"]
    return Reply(
        "\n".join(lines),
        title=_player_title(page),
        updated=page["observed_at"],
        links=(Link("Open on Clash Lens", site.player(page["tag"])),),
        choices=choices,
        pick="day" if choices else None,
        placeholder="Full day for…" if choices else None,
    )


def main_reply(
    cards: Sequence[Mapping[str, Any]], main_tag: str | None, *, changed: bool = False
) -> Reply:
    main = next((card for card in cards if card["tag"] == main_tag), None)
    seen = oldest([card["observed_at"] for card in cards])
    if len(cards) == 1:
        only = cards[0]
        return Reply(
            f"Main is {safe(only['name'])} {only['tag']}: your only player, "
            "so every command uses it.",
            updated=seen,
        )
    if main is None:
        return Reply(
            "No main chosen yet. Pick the player commands should use when you "
            "don't choose one.",
            choices=tuple(choice_for(card) for card in ordered(cards, None)[:MAX_CHOICES]),
            updated=seen,
            pick="main",
            placeholder="Make this my main…",
        )
    if changed:
        return Reply(
            f"Main is now {safe(main['name'])} {main['tag']}. Commands that need "
            "one player use it unless you choose another, and /me lists it first.",
            updated=seen,
        )
    return Reply(
        f"Main is {safe(main['name'])} {main['tag']}. Use /main account: to change it.",
        updated=seen,
    )


def pages(text: str) -> list[str]:
    """Text split at line ends into pieces Discord accepts whole, so a long
    list goes out as several messages instead of being cut short."""
    pieces: list[str] = []
    lines: list[str] = []
    size = 0
    for line in text.split("\n"):
        if lines and size + len(line) > _MAX_DESCRIPTION:
            pieces.append("\n".join(lines))
            lines, size = [], 0
        lines.append(line)
        size += len(line) + 1
    pieces.append("\n".join(lines))
    return pieces
