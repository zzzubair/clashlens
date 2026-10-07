"""Everything the bot says, as plain data.

Nothing here talks to Discord or the database, so every reply can be checked
from fixture data alone. Game and group names are escaped so their markup or
an @mention in a name cannot ping anyone or break the layout.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    ("player", "Any tracked player's Legend day, by tag or name"),
    ("top", "The top 10 of the Live Leaderboard among tracked players"),
    ("rank", "Where your player stands, with the 5 above and 5 below"),
    (GROUP_WORD, f"One of your {GROUP_WORD}s today and over the last days"),
    ("season", "This Season so far for one of your players"),
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


def reset_time(now: datetime) -> str:
    end = ranked_day_for(now).end
    return f"Reset {discord_time(end, 't')} ({discord_time(end, 'R')})"


def reset_line(now: datetime) -> str:
    return f"Legend day {ranked_day_for(now).day_number} of 28 · {reset_time(now)}"


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
            "connected to Clash Lens. In servers, replies are private to you "
            "unless you add share: True."
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
    site: Site,
    card: Mapping[str, Any],
    status: str,
    now: datetime,
    choices: tuple[Choice, ...] = (),
) -> Reply:
    """A player whose numbers do not apply right now: the status word instead,
    from a card or a player page read."""
    return Reply(
        f"{status}\n\n{reset_time(now)}",
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
    lines += ["", reset_time(now)]
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


def _seen(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def bad_tag() -> Reply:
    return Reply("That doesn't look like a player tag.")


def no_such_player(name: str) -> Reply:
    return Reply(
        f"Clash Lens doesn't know a player called {safe(name)}. "
        "Pick one from the list or use their tag."
    )


def not_tracked(site: Site, tag: str, now: datetime) -> Reply:
    return Reply(
        f"Clash Lens hasn't tracked {tag} yet. Open their page to start "
        "tracking and check back in a few minutes.",
        updated=now,
        links=(Link("Open on Clash Lens", site.player(tag)),),
    )


def which_player(cards: Sequence[Mapping[str, Any]], action: str) -> Reply:
    """No player chosen and no main: ask with a dropdown of the person's own."""
    return Reply(
        "Which player? Pick one below, or use /main to set the one these "
        "commands use when you don't choose.",
        choices=tuple(choice_for(card) for card in ordered(cards, None)[:MAX_CHOICES]),
        updated=oldest([card["observed_at"] for card in cards]),
        pick=action,
        placeholder="Which player?",
    )


_RANK_NOTE = "Clash Lens ranks the players it tracks; this is not the official world ranking."


def top_reply(site: Site, board: Mapping[str, Any] | None, now: datetime) -> Reply:
    link = (Link("Full leaderboard", site.url("/leaderboards/tracked")),)
    if board is None or not board["entries"]:
        return Reply("The Live Leaderboard is empty right now.", updated="pending", links=link)
    lines = [reset_line(now), ""]
    lines += [
        f"#{entry['position']} {safe(entry['name'])} · {number(entry['trophies'])}"
        for entry in board["entries"]
    ]
    return Reply(
        "\n".join(lines),
        title=f"Live Leaderboard · among {number(board['tracked_population'])} tracked players",
        footer=_RANK_NOTE,
        updated=oldest([_seen(entry["observed_at"]) for entry in board["entries"]]),
        links=link,
    )


def rank_reply(
    site: Site, board: Mapping[str, Any], tag: str, now: datetime
) -> Reply:
    """The person's player on the Live Leaderboard with the players around it."""
    entries = board["entries"]
    me = next(entry for entry in entries if entry["tag"] == tag)
    lines = [reset_line(now), ""]
    for entry in entries:
        gap = entry["trophies"] - me["trophies"]
        line = f"#{number(entry['position'])} {safe(entry['name'])} · {number(entry['trophies'])}"
        if entry is me:
            line = f"▶ **{line}**"
        elif gap:
            line += f" ({signed(gap)})"
        lines.append(line)
    return Reply(
        "\n".join(lines),
        title=(
            f"{safe(me['name'])} {tag} · #{number(me['position'])} of "
            f"{number(board['total_entries'])} tracked · {number(me['trophies'])}"
        ),
        footer=_RANK_NOTE,
        updated=oldest([_seen(entry["observed_at"]) for entry in entries]),
        links=(Link("Open on Clash Lens", site.player(tag)),),
    )


def no_groups(site: Site, now: datetime) -> Reply:
    return Reply(
        f"You have no {GROUP_WORD}s yet.",
        updated=now,
        links=(Link(f"Make a {GROUP_WORD}", site.url("/account/groups")),),
    )


def not_your_group() -> Reply:
    return Reply(f"That {GROUP_WORD} isn't on your Clash Lens account.")


def group_too_large(site: Site, group_id: str) -> Reply:
    return Reply(
        f"This {GROUP_WORD} is too large to compare here.",
        links=(Link("Open on Clash Lens", site.url(f"/account/groups/{quote(group_id)}")),),
    )


def groups_list(site: Site, groups: Sequence[Mapping[str, Any]], now: datetime) -> Reply:
    shown = groups[:10]
    lines = [
        f"**{safe(group['name'])}** · {len(group['tags'])} players" for group in shown
    ]
    if len(groups) > len(shown):
        lines.append(f"…and {len(groups) - len(shown)} more on Clash Lens.")
    return Reply(
        "\n".join(lines),
        title=f"Your {GROUP_WORD}s",
        updated=now,
        links=(Link("Open on Clash Lens", site.url("/account/groups")),),
        choices=tuple(
            Choice(" ".join(group["name"].split())[:_MAX_CHOICE_LABEL], group["group_id"])
            for group in groups[:MAX_CHOICES]
        ),
        pick=GROUP_WORD,
        placeholder=f"Open a {GROUP_WORD}…",
    )


def _member_line(player: Mapping[str, Any], days: int) -> str:
    head = f"**{safe(player['name'])}** {player['tag']}" + (" (you)" if player["you"] else "")
    if player["net"] is None:
        window = "pending"
    elif player["counted_days"] < days:
        window = f"{signed(player['net'])} ({player['counted_days']} of {days} days counted)"
    else:
        window = signed(player["net"])
    if player["status"] != "tracking":
        return f"{head} · {_STATE_WORDS.get(player['status'], 'Being checked')}"
    if player["season_reset_pending"]:
        return f"{head} · {WAITING_FOR_RESET} · {days} days: {window}"
    if player["trophies"] is None:
        return f"{head} · Being checked"
    parts = [head, f"{number(player['trophies'])} 🏆"]
    today = player["today"]
    if today is None:
        parts += ["no battles recorded today", "net pending"]
    else:
        parts.append(f"⚔ {_known(today['attacks'], _of_eight)}")
        parts.append(f"🛡 {_known(today['defenses'], _of_eight)}")
        net = today["net"]
        parts.append("net pending" if net is None else f"net {signed(net)} so far")
    parts.append(f"{days} days: {window}")
    return " · ".join(parts)


def group_reply(site: Site, comparison: Mapping[str, Any], now: datetime) -> Reply:
    days = comparison["days"]
    members = sorted(
        (player for player in comparison["players"] if player["in_group"]),
        key=lambda player: (
            player["trophies"] is None,
            -(player["trophies"] or 0),
            player["tag"],
        ),
    )
    lines = [reset_line(now), ""]
    lines += [_member_line(player, days) for player in members] or [
        f"This {GROUP_WORD} has no players yet."
    ]
    return Reply(
        "\n".join(lines),
        title=f"{safe(comparison['name'])} · last {days} ended Legend days",
        updated=oldest([_seen(player["observed_at"]) for player in members]),
        links=(
            Link(
                "Open on Clash Lens",
                site.url(f"/account/groups/{quote(comparison['group_id'])}"),
            ),
        ),
    )


def _rate(part: int | None, whole: int | None) -> str:
    return "Unavailable" if part is None or not whole else f"{round(100 * part / whole)}%"


def _per(total: int | None, count: int | None) -> str:
    return "Unavailable" if total is None or not count else signed(round(total / count))


def _total(days: Sequence[Mapping[str, Any]], key: str) -> int | None:
    """A Season total, unknown when no day or any day lacks the number."""
    values = [day[key] for day in days]
    return None if not values or None in values else sum(values)


def _average(values: Sequence[int], unit: str = "") -> str:
    return "Unavailable" if not values else f"{sum(values) / len(values):.1f}{unit}"


def season_reply(site: Site, page: Mapping[str, Any], now: datetime) -> Reply:
    """This Season so far for one player, from the player page read."""
    ready = page["screen_ready"]
    season = ready.get("season")
    title = _player_title(page)
    link = (Link("Open on Clash Lens", site.player(page["tag"])),)
    if season is None:
        return Reply(
            "This Season's days are not available yet.",
            title=title,
            updated=page["observed_at"],
            links=link,
        )
    by_start = {day["ranked_day_start"]: day for day in ready["days"]}
    days = sorted(
        (by_start[start] for start in ready["season_day_starts"] if start in by_start),
        key=lambda day: day["ranked_day_start"],
    )
    first = next((day for day in days if day.get("season_day_number") == 1), None)
    start = None if first is None else first.get("start_trophies")
    now_trophies = None if page["season_reset_pending"] else page["trophies"]
    head = f"Season from {discord_time(datetime.fromisoformat(season['start']), 'D')}"
    current = season["current_day_number"]
    head += f" · Day {current} of 28 · {_known(start, number)} → {_known(now_trophies, number)}"
    if start is not None and now_trophies is not None:
        head += f" ({signed(now_trophies - start)})"
    attacks = [event for day in days for event in day["offense_events"]]
    defenses = [event for day in days for event in day["defense_events"]]
    attack_count = _total(days, "attack_count")
    three_stars = _total(days, "attack_three_star_count")
    gain = _total(days, "attack_gain")
    defense_count = _total(days, "defense_count")
    tripled = _total(days, "defense_three_star_count")
    loss = _total(days, "defense_loss")
    lost = None if loss is None else -loss
    held = None if defense_count is None or tripled is None else defense_count - tripled
    lines = [
        head,
        f"{len(days)} of {current} Legend days recorded.",
        "",
        (
            f"**Attacks** {_known(attack_count, number)} · hit rate "
            f"{_rate(three_stars, attack_count)} ({_known(three_stars, number)} three-stars) · "
            f"{_known(gain, signed)} · {_per(gain, attack_count)} per attack · "
            "average destruction "
            f"{_average([event['destruction_percentage'] for event in attacks], '%')}"
        ),
        (
            f"**Defenses** {_known(defense_count, number)} · held {_known(held, number)} of "
            f"{_known(defense_count, number)} (not tripled) · {_known(lost, signed)} · "
            f"{_per(lost, defense_count)} per defense · average stars given up "
            f"{_average([event['stars'] for event in defenses])}"
        ),
    ]
    by_time = {datetime.fromisoformat(day["ranked_day_start"]): day for day in days}
    season_start = datetime.fromisoformat(season["start"])
    ended = range(max(1, current - 7), current)
    if ended:
        lines += ["", "**Last ended days**"]
        for number_of_day in reversed(ended):
            day = by_time.get(season_start + timedelta(days=number_of_day - 1), {})
            net = day.get("net_trophy_change")
            rank = day.get("reset_rank")
            lines.append(
                f"Day {number_of_day} · net "
                f"{'pending' if net is None else signed(net)} · Reset rank "
                f"{'Unavailable' if rank is None else '#' + number(rank)}"
            )
    lines += ["", reset_time(now)]
    return Reply(
        "\n".join(lines),
        title=title,
        footer="Totals count the battles Clash Lens recorded this Season.",
        updated=page["observed_at"],
        links=(
            Link(
                "Season on Clash Lens",
                site.url(
                    f"/players/{quote(page['tag'], safe='')}?season={quote(season['id'])}"
                ),
            ),
        ),
    )
