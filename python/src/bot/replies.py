"""Everything the bot says, as plain data.

Nothing here talks to Discord or the database, so every reply can be checked
from fixture data alone. Player, clan and account names are escaped so their
markup or an @mention in a name cannot ping anyone or break the layout.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from discord.utils import escape_markdown, escape_mentions

from clashlens.domain import ranked_day_for

# Discord shows at most 25 dropdown entries, 100 characters per entry label
# and 80 per button label.
MAX_CHOICES = 25
_MAX_CHOICE_LABEL = 100
WAITING_FOR_RESET = "Waiting for Season reset"

# One line per command for /help, also used as each command's description.
COMMANDS = (
    ("help", "What the bot does, its commands and your connection"),
    ("link", "Connect Discord and choose your default player on the Clash Lens website"),
    ("stats", "This Season so far for any tracked player, or your default player"),
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
    # ("stats") for that player.
    choices: tuple[Choice, ...] = ()
    pick: str | None = None
    placeholder: str | None = None
    # When the oldest data shown was read, or "pending" or "Unavailable"; the
    # bot works out "Updated N min ago" from it as each message goes out.
    updated: datetime | str = "Unavailable"
    # Only the person who ran the command sees it: the not-connected pointer
    # and plain errors meant for them. Every other reply is a normal message
    # anyone in the conversation can see and forward.
    private: bool = False


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



def unavailable() -> Reply:
    return Reply("Clash Lens is unavailable right now, try again in a minute.", private=True)


def slow() -> Reply:
    return Reply("Clash Lens is taking longer than usual. Try again.", private=True)


def failed() -> Reply:
    return Reply("Something went wrong on Clash Lens's side. Try again in a minute.", private=True)


def not_own() -> Reply:
    return Reply("That player isn't linked to your Clash Lens account.", private=True)


def not_your_menu() -> Reply:
    return Reply("Only the person who ran this command can use these buttons.", private=True)


# Where a Clasher connects Discord and chooses the bot's default player.
DISCORD_SETTINGS = "/account/providers"


def not_linked(site: Site, discord_name: str, now: datetime) -> Reply:
    """The one reply for anyone whose Discord account is not connected yet:
    connecting happens on the website, never in the bot."""
    return Reply(
        f"Connect this Discord account (@{safe(discord_name)}) to your Clash Lens "
        "account on the website to use the bot.",
        title="Connect Discord to Clash Lens",
        updated=now,
        private=True,
        links=(
            Link("Connect Discord on Clash Lens", site.url(DISCORD_SETTINGS)),
            Link(
                "New here: create an account with Discord",
                site.url("/auth/discord?returnPath=%2Faccount"),
            ),
        ),
    )


def no_players(site: Site, username: str, now: datetime) -> Reply:
    return Reply(
        f"Connected as @{safe(username)}. Link a Clash of Clans player on Clash "
        "Lens to see your Legend days here.",
        updated=now,
        links=(Link("Link a player on Clash Lens", site.url("/account/verify-player")),),
    )


def help_reply(site: Site, username: str | None, player_count: int, now: datetime) -> Reply:
    lines = [
        "Clash Lens shows how you and other Legend League players are really doing.",
        "",
        *(f"**/{name}** · {text}" for name, text in COMMANDS),
        "",
        (
            "Every command except /help and /link needs this Discord account "
            "connected to Clash Lens. With several players, choose the bot's "
            "default player on Clash Lens."
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


def link_reply(site: Site, username: str, now: datetime) -> Reply:
    """A connected account's pointer: players and the default player are
    managed on the website, never in the bot."""
    return Reply(
        f"Connected as @{safe(username)}. Manage the connection and choose your "
        "default player on Clash Lens.",
        updated=now,
        links=(Link("Manage on Clash Lens", site.url(DISCORD_SETTINGS)),),
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


def choice_for(card: Mapping[str, Any]) -> Choice:
    """A dropdown or autocomplete entry: "Name #TAG · 5,842"."""
    label = f"{card['name'] or 'Unnamed'} {card['tag']}"
    if card.get("trophies") is not None:
        label += f" · {number(card['trophies'])}"
    return Choice(label[:_MAX_CHOICE_LABEL], card["tag"])


def ordered(cards: Sequence[Mapping[str, Any]], first_tag: str | None) -> list[Mapping[str, Any]]:
    """Main first, then the most trophies, then by tag."""
    return sorted(
        cards,
        key=lambda card: (
            card["tag"] != first_tag,
            card["trophies"] is None,
            -(card["trophies"] or 0),
            card["tag"],
        ),
    )


def _player_title(item: Mapping[str, Any]) -> str:
    title = f"{safe(item['name'])} {item['tag']}"
    return f"{title} · {safe(item['clan'])}" if item.get("clan") else title


def player_status(
    site: Site,
    card: Mapping[str, Any],
    status: str,
    now: datetime,
) -> Reply:
    """A player whose numbers do not apply right now: the status word instead,
    from a player page read."""
    return Reply(
        f"{status}\n\n{reset_time(now)}",
        title=_player_title(card),
        updated=oldest([card["observed_at"]]),
        links=(Link("Open on Clash Lens", site.player(card["tag"])),),
    )


def bad_tag() -> Reply:
    return Reply("That doesn't look like a player tag.", private=True)


def no_such_player(name: str) -> Reply:
    return Reply(
        f"Clash Lens doesn't know a player called {safe(name)}. "
        "Pick one from the list or use their tag.",
        private=True,
    )


def not_tracked(site: Site, tag: str, now: datetime) -> Reply:
    return Reply(
        f"Clash Lens hasn't tracked {tag} yet. Open their page to start "
        "tracking and check back in a few minutes.",
        updated=now,
        links=(Link("Open on Clash Lens", site.player(tag)),),
    )


def which_player(site: Site, cards: Sequence[Mapping[str, Any]], action: str) -> Reply:
    """No player named and no default chosen: ask with a dropdown of the
    person's own players."""
    return Reply(
        "Which player? Pick one below, or choose a default player on Clash Lens.",
        links=(Link("Choose a default player", site.url(DISCORD_SETTINGS)),),
        choices=tuple(choice_for(card) for card in ordered(cards, None)[:MAX_CHOICES]),
        updated=oldest([card["observed_at"] for card in cards]),
        pick=action,
        placeholder="Which player?",
    )




def _rate(part: int | None, whole: int | None) -> str:
    return "Unavailable" if part is None or not whole else f"{round(100 * part / whole)}%"


def _per(total: int | None, count: int | None) -> str:
    return "Unavailable" if total is None or not count else signed(round(total / count))


def _total(days: Sequence[Mapping[str, Any] | None], key: str) -> int | None:
    """A Season total, unknown when a day so far is not published or lacks
    the number."""
    values = [None if day is None else day[key] for day in days]
    return None if None in values else sum(values)


def season_days(page: Mapping[str, Any]) -> list[Mapping[str, Any] | None] | None:
    """Every Legend day of the Season so far from the player page read, None
    where none is published; None when the Season's days are not available."""
    ready = page["screen_ready"]
    season = ready.get("season")
    if season is None:
        return None
    published = {datetime.fromisoformat(start) for start in ready["season_day_starts"]}
    by_start = {
        moment: day
        for day in ready["days"]
        if (moment := datetime.fromisoformat(day["ranked_day_start"])) in published
    }
    season_start = datetime.fromisoformat(season["start"])
    return [
        by_start.get(season_start + timedelta(days=offset))
        for offset in range(season["current_day_number"])
    ]


def hit_rate(days: Sequence[Mapping[str, Any] | None]) -> str:
    """This Season's three-star share of attacks, as /stats and attack alerts
    show it."""
    return _rate(_total(days, "attack_three_star_count"), _total(days, "attack_count"))


def stats_reply(site: Site, page: Mapping[str, Any], now: datetime) -> Reply:
    """This Season so far for one player, from the player page read: one
    compact message anyone in the conversation can forward."""
    season = page["screen_ready"].get("season")
    title = _player_title(page)
    days = season_days(page)
    if season is None or days is None:
        return Reply(
            f"This Season's days are not available yet.\n\n{reset_time(now)}",
            title=title,
            updated=page["observed_at"],
            links=(Link("Open on Clash Lens", site.player(page["tag"])),),
        )
    season_start = datetime.fromisoformat(season["start"])
    start = None if days[0] is None else days[0].get("start_trophies")
    now_trophies = None if page["season_reset_pending"] else page["trophies"]
    change = _known(start, number) + " → " + _known(now_trophies, number)
    if start is not None and now_trophies is not None:
        change += f" ({signed(now_trophies - start)})"
    attack_count = _total(days, "attack_count")
    three_stars = _total(days, "attack_three_star_count")
    gain = _total(days, "attack_gain")
    defense_count = _total(days, "defense_count")
    tripled = _total(days, "defense_three_star_count")
    loss = _total(days, "defense_loss")
    held = None if defense_count is None or tripled is None else defense_count - tripled
    lines = [
        (
            f"Season from {discord_time(season_start, 'D')} · "
            f"Legend day {season['current_day_number']} of 28"
        ),
        change,
        "",
        (
            f"**Attacks** {_known(attack_count, number)} · hit rate "
            f"{hit_rate(days)} ({_known(three_stars, number)} three-stars) · "
            f"{_per(gain, attack_count)} per attack"
        ),
        (
            f"**Defenses** {_known(defense_count, number)} · held {_rate(held, defense_count)} "
            f"({_known(held, number)} not tripled) · "
            f"{_per(None if loss is None else -loss, defense_count)} per defense"
        ),
        "",
        reset_time(now),
    ]
    return Reply(
        "\n".join(lines),
        title=title,
        footer="Totals count the battles Clash Lens recorded this Season.",
        updated=page["observed_at"],
        links=(Link("Season on Clash Lens", site.player(page["tag"])),),
    )


def attack_alert(site: Site, alert: Mapping[str, Any], attacker_hit_rate: str) -> Reply:
    """The DM sent when one of the person's players is attacked, named by the
    defending player since an account can have several."""
    attacker = safe(alert["attacker_name"])
    lost = alert["trophies_lost"]
    return Reply(
        (
            f"{attacker} {alert['attacker_tag']}: {alert['stars']}★ · "
            f"{alert['destruction_percentage']}% · "
            f"{f'−{number(lost)}' if lost else '0'} trophies\n"
            f"{attacker}'s hit rate this Season: {attacker_hit_rate}"
        ),
        title=f"{safe(alert['defender_name'])} {alert['defender_tag']} was attacked",
        updated=alert["saved_at"],
        links=(
            Link("Open on Clash Lens", site.player(alert["defender_tag"])),
            Link("Alert settings", site.url(DISCORD_SETTINGS)),
        ),
    )
