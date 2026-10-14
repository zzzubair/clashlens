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
from clashlens.reconciliation import MAX_DAILY_ATTACKS, MAX_DAILY_DEFENSES

# Discord shows at most 25 dropdown entries, 100 characters per entry label
# and 80 per button label.
MAX_CHOICES = 25
_MAX_CHOICE_LABEL = 100
WAITING_FOR_RESET = "Waiting for Season reset"

# One line per command for /help, also used as each command's description.
COMMANDS = (
    ("help", "What the bot does, its commands and your connection"),
    ("link", "Link your Clash of Clans accounts on the Clash Lens website"),
    ("alerts", "Turn attack alerts on or off on the Clash Lens website"),
    ("stats", "Today and this Season for any tracked player, or your default account"),
)
# Clash Lens yellow, kept for every message that is not about one of the
# person's own linked accounts: settings, help, errors, other players.
GOLD = 0xF2B33D
# One colour per linked account, in the order they were linked, the same in
# every message about it; never yellow. The eighth starts the list again.
PLAYER_COLOURS = (0x4C8DFF, 0x3FB37F, 0xE8618C, 0x9B6DFF, 0xF07C3A, 0x2BB3C0, 0xA3D65C)
# Beside the players a Legend day summary names as not finished yet.
GREY = 0x6D7680

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
class Card:
    """One block of a message with a coloured edge: its parts with a thin line
    between them, and a button beside the last part."""

    parts: tuple[str, ...]
    colour: int = GOLD
    button: Link | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    cards: tuple[Card, ...]
    # A heading above the cards, and small text below them.
    title: str | None = None
    footer: str | None = None
    # Buttons below everything.
    links: tuple[Link, ...] = ()
    # A dropdown of the person's own players; picking one runs `pick`
    # ("stats") for that player.
    choices: tuple[Choice, ...] = ()
    pick: str | None = None
    placeholder: str | None = None
    # Only the person who ran the command sees it: the not-connected pointer
    # and plain errors meant for them. Every other reply is a normal message
    # anyone in the conversation can see and forward.
    private: bool = False

    @property
    def text(self) -> str:
        """All of the reply's words, in order."""
        return "\n\n".join(
            [*filter(None, [self.title]), *(part for card in self.cards for part in card.parts),
             *filter(None, [self.footer])]
        )


def say(text: str, button: Link | None = None, **fields: Any) -> Reply:
    """A reply of one Clash Lens gold card."""
    return Reply((Card((text,), button=button),), **fields)


def colours(tags: Sequence[str]) -> dict[str, int]:
    """Each player's colour, from the account's tags in the order they were
    linked."""
    return {tag: PLAYER_COLOURS[index % len(PLAYER_COLOURS)] for index, tag in enumerate(tags)}


@dataclass(frozen=True, slots=True)
class Site:
    """Links into the Clash Lens website."""

    origin: str

    def url(self, path: str) -> str:
        return f"{self.origin.rstrip('/')}{path}"

    def player(self, tag: str) -> str:
        return self.url(f"/players/{quote(tag, safe='')}")

    def user(self, username: str) -> str:
        """The Clasher's own page, listing their linked accounts."""
        return self.url(f"/users/{quote(username, safe='')}")

    def battle(self, tag: str, day_start: datetime, battle_id: int) -> str:
        """The player's page on that Legend day with the battle highlighted."""
        return f"{self.player(tag)}?day={day_start.date().isoformat()}#battle-{battle_id}"


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
    return say("Clash Lens is unavailable right now, try again in a minute.", private=True)


def slow() -> Reply:
    return say("Clash Lens is taking longer than usual. Try again.", private=True)


def failed() -> Reply:
    return say("Something went wrong on Clash Lens's side. Try again in a minute.", private=True)


def not_own() -> Reply:
    return say("That player isn't one of your linked accounts.", private=True)


def not_your_menu() -> Reply:
    return say("Only the person who ran this command can use these buttons.", private=True)


# Where a Clasher connects Discord to an account they have.
DISCORD_SETTINGS = "/account/providers"
# Where a Clasher connects Discord and turns attack alerts on or off; /alerts
# opens it, and a signed-out visitor signs in first and lands there.
DISCORD_ALERTS = "/account/discord-alerts"
# Where a Clasher links a Clash of Clans player to their account.
LINK_PLAYER = "/account/verify-player"
# Signing up with Discord, landing on that page once the account exists.
SIGN_UP = "/auth/discord?returnPath=%2Faccount%2Fverify-player"


def _sign_up(site: Site, text: str) -> Card:
    """The one Clash Lens button someone whose Discord is not connected sees:
    sign up with Discord, then land on the page that links a Clash of Clans
    account."""
    return Card((text,), button=Link("Sign up with Discord", site.url(SIGN_UP)))


def _connect_instead(site: Site) -> str:
    """The smaller way in for someone with a Clash Lens account already, so
    signing up with Discord never makes them a second account."""
    return f"Already have a Clash Lens account? [Connect Discord]({site.url(DISCORD_SETTINGS)})"


def not_linked(
    site: Site, discord_name: str, purpose: str = "see your own Legend days here"
) -> Reply:
    """/link and /alerts for anyone whose Discord account is not connected
    yet, ending with what signing up gets them (`purpose`): signing up and
    connecting happen on the website, never in the bot."""
    return Reply(
        (
            _sign_up(
                site,
                "### Get started with Clash Lens\n"
                f"Sign up with this Discord account (@{safe(discord_name)}), then link "
                f"your Clash of Clans account to {purpose}.\n"
                "-# /stats with a player tag or name works for anyone, no account needed.",
            ),
        ),
        footer=_connect_instead(site),
        private=True,
    )


def name_a_player() -> Reply:
    """/stats with no player from someone with no linked account to show:
    which player to look up, never a sign-up or a link."""
    return say(
        "Type a player tag or name after /stats to look them up, like "
        "`/stats #2PP22` or `/stats Wanderer`.",
        private=True,
    )


def no_players(site: Site, username: str) -> Reply:
    return say(
        f"Connected as @{safe(username)}. Link a Clash of Clans account on Clash "
        "Lens to see your Legend days here.",
        Link("Link an account on Clash Lens", site.url(LINK_PLAYER)),
    )


def help_reply(site: Site, username: str | None, player_count: int) -> Reply:
    page = "your Clash Lens page"
    if username is None:
        you = "You: this Discord account isn't connected to Clash Lens yet."
    else:
        accounts = (
            "1 linked account" if player_count == 1 else f"{player_count} linked accounts"
        )
        you = f"You: connected as @{safe(username)}, {accounts}."
        page = f"[{page}]({site.user(username)})"
    return Reply(
        (
            Card(
                (
                    (
                        "### Clash Lens\nClash Lens shows how you and other Legend "
                        "League players are really doing."
                    ),
                    "\n".join(f"**/{name}** · {text}" for name, text in COMMANDS),
                    (
                        "-# /stats with a player tag or name works for anyone. Your own "
                        "linked accounts need this Discord account connected to Clash "
                        "Lens. With several linked accounts, choose your default account "
                        f"on {page}.\n{you}"
                    ),
                ),
                button=Link("Open Clash Lens", site.url("/"))
                if username is not None
                else Link("Get started on Clash Lens", site.url("/login")),
            ),
        ),
        footer=None if username is not None else _connect_instead(site),
    )


# At most this many linked accounts are listed in /link and /alerts, inside
# Discord's 4,000 characters a message; the rest are counted below them.
_LISTED = 30


def _account_lines(
    accounts: Sequence[Mapping[str, Any]],
    colours: Mapping[str, int],
    detail: Callable[[Mapping[str, Any]], str],
) -> str:
    """A line per linked account, in the order they were linked, each
    followed by its `detail`."""
    order = {tag: index for index, tag in enumerate(colours)}
    ranked = sorted(accounts, key=lambda item: (order.get(item["tag"], len(order)), item["tag"]))
    lines = [
        f"**{safe(item['name'])}** {item['tag']}{detail(item)}"
        for item in ranked[:_LISTED]
    ]
    if len(ranked) > _LISTED:
        lines.append(f"And {number(len(ranked) - _LISTED)} more on Clash Lens.")
    return "\n".join(lines)


def link_reply(
    site: Site,
    username: str,
    accounts: Sequence[Mapping[str, Any]],
    default: str | None,
    colours: Mapping[str, int],
) -> Reply:
    """A connected account's linked Clash of Clans accounts and the one /stats
    uses; they are linked and chosen on the website, never in the bot."""
    lines = _account_lines(
        accounts,
        colours,
        lambda item: " · default account" if item["tag"] == default else "",
    )
    return say(
        f"### Your linked accounts\n{lines}",
        Link("Change linked accounts", site.user(username)),
        footer=(
            f"Connected as @{safe(username)}. Choose your default account, "
            f"the one /stats uses, on your [Clash Lens page]({site.user(username)})."
        ),
    )


def alerts_reply(
    site: Site,
    username: str,
    switches: Sequence[Mapping[str, Any]],
    colours: Mapping[str, int],
) -> Reply:
    """A connected account's alert switches for each linked account, as the
    website's Discord alerts page shows them; they are changed there, never
    in the bot."""

    def word(on: bool) -> str:
        return "**On**" if on else "Off"

    lines = _account_lines(
        switches,
        colours,
        lambda item: (
            f"\n-# Attack alerts {word(item['on'])} · "
            f"Legend day summary {word(item['summary_on'])}"
        ),
    )
    return say(
        f"### Your Discord alerts\n{lines}",
        Link("Change settings", site.url(DISCORD_ALERTS)),
        footer=f"Connected as @{safe(username)}. Turn them on or off on Clash Lens.",
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
    title = f"### {safe(item['name'])} {item['tag']}"
    return f"{title}\n-# {safe(item['clan'])}" if item.get("clan") else title


def player_status(
    site: Site,
    card: Mapping[str, Any],
    status: str,
    now: datetime,
    colour: int = GOLD,
) -> Reply:
    """A player whose numbers do not apply right now: the status word instead,
    from a player page read."""
    return Reply(
        (
            Card(
                (
                    _player_title(card),
                    (
                        f"{status}\n\n{reset_time(now)}\n"
                        f"-# {updated_line(oldest([card['observed_at']]), now)}"
                    ),
                ),
                colour,
                Link("Open on Clash Lens", site.player(card["tag"])),
            ),
        ),
    )


def bad_tag() -> Reply:
    return say("That doesn't look like a player tag.", private=True)


def no_such_player(name: str) -> Reply:
    return say(
        f"Clash Lens doesn't know a player called {safe(name)}. "
        "Pick one from the list or use their tag.",
        private=True,
    )


def not_tracked(site: Site, tag: str) -> Reply:
    return say(
        f"Clash Lens hasn't tracked {tag} yet. Open their page to start "
        "tracking and check back in a few minutes.",
        Link("Open on Clash Lens", site.player(tag)),
    )


def which_player(
    site: Site, username: str, cards: Sequence[Mapping[str, Any]], action: str
) -> Reply:
    """No player named and no default found: ask with a dropdown of the
    person's own players."""
    return say(
        "Which linked account? Pick one below, or choose your default account on your "
        "Clash Lens page.",
        Link("Choose your default account", site.user(username)),
        choices=tuple(choice_for(card) for card in ordered(cards, None)[:MAX_CHOICES]),
        pick=action,
        placeholder="Which linked account?",
    )


def _rate(part: int | None, whole: int | None) -> str:
    return "Unavailable" if part is None or not whole else f"{round(100 * part / whole)}%"


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
    """The Season's three-star share of attacks so far, as /stats and attack
    alerts show it."""
    return _rate(_total(days, "attack_three_star_count"), _total(days, "attack_count"))


# Today's battles as squares in play order, green good for the player and red
# bad, by stars; dark for those still to play out of the game's 8 a day.
_ATTACK_SQUARES = {3: "🟩", 2: "🟨", 1: "🟧", 0: "🟥"}
_DEFENSE_SQUARES = {0: "🟩", 1: "🟨", 2: "🟧", 3: "🟥"}
_UNPLAYED = "⬛"


def _today(page: Mapping[str, Any], now: datetime) -> str:
    """The Legend day so far: trophies gained (attacks) and lost (defenses),
    then a square for each recorded battle and, only while the player is
    tracked in Legend I and every battle so far is recorded, a dark one for
    each still to play of the game's 8 a day."""
    start = ranked_day_for(now).start
    day = next(
        (
            day
            for day in page["screen_ready"]["days"]
            if datetime.fromisoformat(day["ranked_day_start"]) == start
        ),
        None,
    )
    if day is None:
        return "Today's battles are not available yet"
    attacks = day["offense_events"]
    defenses = day["defense_events"]
    complete = day["battles_complete"]
    to_play = complete and page["state"] == "tracking"
    if to_play and not (attacks or defenses):
        lines = ["No battles yet today"]
    else:
        loss = day["defense_loss"]
        gained = f"{_known(day['attack_gain'], signed)} ({_known(day['attack_count'], number)})"
        lost = _known(None if loss is None else -loss, lambda value: f"−{number(-value)}")
        lines = [
            f"## {gained}  {lost} ({_known(day['defense_count'], number)})",
            "-# Today so far",
        ]
    for label, battles, squares, most in (
        ("Attacks ", attacks, _ATTACK_SQUARES, MAX_DAILY_ATTACKS),
        ("Defenses", defenses, _DEFENSE_SQUARES, MAX_DAILY_DEFENSES),
    ):
        shown = "".join(squares.get(battle["stars"], _UNPLAYED) for battle in battles)
        if to_play:
            left = max(0, most - len(battles))
            shown += _UNPLAYED * left + f"  {left} left"
        lines.append(f"`{label}` {shown or '–'}")
    if not complete:
        lines.append("-# Some of today's battles may be missing. The day on Clash Lens says why.")
    return "\n".join(lines)


def stats_reply(
    site: Site, page: Mapping[str, Any], now: datetime, colour: int = GOLD
) -> Reply:
    """Today and this Season for one player, from the player page read: one
    compact message anyone in the conversation can forward."""
    days = season_days(page)
    if days is None:
        season = "This Season's days are not available yet."
    else:
        defense_count = _total(days, "defense_count")
        tripled = _total(days, "defense_three_star_count")
        held = None if defense_count is None or tripled is None else defense_count - tripled
        season = (
            f"Attack hit rate: {hit_rate(days)}\n"
            f"Defenses held: {_rate(held, defense_count)}"
        )
    return Reply(
        (
            Card(
                (
                    f"### {safe(page['name'])} {page['tag']}\n{_today(page, now)}",
                    (
                        f"**Season Stats**\n{season}\n"
                        f"-# {updated_line(oldest([page['observed_at']]), now)}"
                    ),
                ),
                colour,
                Link("View details", site.player(page["tag"])),
            ),
        ),
    )


def attack_alert(
    site: Site, alert: Mapping[str, Any], attacker_hit_rate: str, colour: int
) -> Reply:
    """The DM sent when one of the person's players is attacked, named by the
    defending player since an account can have several."""
    attacker = safe(alert["attacker_name"])
    lost = alert["trophies_lost"]
    stars = "★" * alert["stars"] + "☆" * (3 - alert["stars"])
    return Reply(
        (
            Card(
                (
                    (
                        f"### {safe(alert['defender_name'])} was attacked\n"
                        f"-# {alert['defender_tag']} · {discord_time(alert['saved_at'], 't')}"
                    ),
                    (
                        f"**{attacker}** {alert['attacker_tag']}\n"
                        f"{stars}  ·  {alert['destruction_percentage']}%  ·  "
                        f"**{f'−{number(lost)}' if lost else '0'}** trophies\n"
                        f"-# {attacker}'s hit rate in this attack's Season: {attacker_hit_rate}"
                    ),
                ),
                colour,
                Link(
                    "Open battle",
                    site.battle(
                        alert["defender_tag"], alert["ranked_day_start"], alert["battle_id"]
                    ),
                ),
            ),
        ),
        footer=f"[Alert settings]({site.url(DISCORD_ALERTS)})",
    )


def alerts_on(site: Site, switch: Mapping[str, Any], colour: int) -> Reply:
    """The DM sent when attack alerts go on for a player, and again when
    Discord is connected again, which also proves the bot can reach the
    person."""
    player = safe(switch["name"])
    return Reply(
        (
            Card(
                (
                    (
                        f"### Attack alerts are on\n{player} {switch['tag']}\n"
                        f"-# You'll get a message here as soon as {player} is attacked."
                    ),
                ),
                colour,
                Link("Alert settings", site.url(DISCORD_ALERTS)),
            ),
        ),
    )


# The website's codes that leave a day's 8 attacks and 8 defenses in doubt,
# and that mean a battle may be missing from its logs: BATTLE_DOUBT_CODES and
# GAP_CODES in website/app/lib/player-lookup-text.ts, which labels each day.
_BATTLE_DOUBT_CODES = frozenset({
    "perspective_disagreement",
    "duplicate_contribution_disagreement",
    "trophy_equation_mismatch",
    "ranked_version_mismatch",
    "attack_star_total_mismatch",
    "defense_star_total_mismatch",
    "truncated_reasons",
    "attack_count_exceeds_eight",
    "defense_count_exceeds_eight",
})
_GAP_CODES = frozenset({
    "missing_start_battle_log_baseline",
    "missing_end_battle_log_baseline",
    "battle_log_row_gap",
    "battle_log_overlap_gap",
    "battle_log_stale_window",
    "battle_log_row_count_exceeds_fifty",
    "unclassified_rows",
})
# Discord shows at most 4,000 characters of text and 40 parts (each card,
# text, button and row of buttons counts) in one message; a summary DM's
# heading, small print and button take the rest.
_SUMMARY_LENGTH = 3_700
_SUMMARY_CARDS = 8
# Players named in one "Not finished yet" card, so no card outgrows a DM.
_WAITING_NAMES = 40


def _day_status(day: Mapping[str, Any]) -> str:
    """A finished day's label on the player page: presentDay in
    website/app/lib/player-lookup-text.ts."""
    codes = day["uncertainty_reasons"]
    coverage = (
        day["coverage"] if day["coverage"] in ("complete", "partial")
        else day["completeness"]["state"]
    )
    doubtful = any(code in _BATTLE_DOUBT_CODES for code in codes) or (
        not day["battles_complete"]
        and (coverage != "complete" or any(code in _GAP_CODES for code in codes))
    )
    if day["net_trophy_change"] is None or doubtful:
        return "Uncertain"
    if day["state"] == "Complete" and day["confidence"] == "exact" and not codes:
        return "Verified"
    return "Calculated"


def _adjustment(day: Mapping[str, Any], *kinds: str) -> Mapping[str, Any] | None:
    return next((item for item in day["adjustments"] if item.get("type") in kinds), None)


def summary_day(
    page: Mapping[str, Any], reset: datetime, place: int | None, unfinished: bool
) -> dict[str, Any] | None:
    """The player's Legend day that ended at `reset`, as their page shows it:
    its label, its saved "version", and `place`, its place on that Reset's
    Daily board. Its "end" is None until set to the end of day saved with
    that version, before any weekly or Season reset at `reset`. "finished"
    is False while its final result is not saved yet, as for a player that
    board went without (`unfinished`) with no saved day. None when the page
    has no such Legend day."""
    ready = page.get("screen_ready") or {}
    days = {datetime.fromisoformat(day["ranked_day_start"]): day for day in ready.get("days", [])}
    day = days.get(reset - timedelta(days=1))
    if day is None:
        return {"tag": page["tag"], "name": page["name"], "finished": False} if unfinished else None
    codes = day["uncertainty_reasons"]
    battles = len(day["offense_events"]) + len(day["defense_events"])
    # The page's own rule for a day that is not a Legend day.
    if not battles and ("not_enrolled" in codes or "player_not_eligible" in codes):
        return None
    if day["state"] == "Live":
        return {"tag": page["tag"], "name": page["name"], "finished": False}
    automatic = _adjustment(day, "automatic_defense")
    return {
        "tag": page["tag"],
        "name": page["name"],
        "finished": True,
        "status": _day_status(day),
        "version": day["version"],
        "start": day["start_trophies"],
        "end": None,
        "net": day["net_trophy_change"],
        "raised": _adjustment(day, "weekly_reset", "season_reset"),
        "attack_count": day["attack_count"],
        "attack_gain": day["attack_gain"],
        "defense_count": day["defense_count"],
        "defense_loss": day["defense_loss"],
        "automatic_loss": None if automatic is None else -automatic["amount"],
        "place": place,
    }


def _summary_card(site: Site, day: Mapping[str, Any], key: str, colour: int) -> Card:
    lines = [f"**{safe(day['name'])}** {day['tag']} · {day['status']}"]
    if day["start"] is None and day["end"] is None and day["net"] is None:
        lines.append("Trophies unavailable")
    else:
        change = "change unavailable" if day["net"] is None else signed(day["net"])
        if day["raised"]:
            kind = "weekly" if day["raised"]["type"] == "weekly_reset" else "Season"
            change += f", {signed(day['raised']['amount'])} {kind} reset"
        lines.append(
            f"{_known(day['start'], number)} → {_known(day['end'], number)} ({change})"
        )
    defenses = f"Defenses {_known(day['defense_count'], number)} " + (
        f"({_known(None if day['defense_loss'] is None else -day['defense_loss'], signed)})"
    )
    if day["automatic_loss"]:
        defenses += f" · {signed(-day['automatic_loss'])} automatic loss"
    lines.append(
        f"Attacks {_known(day['attack_count'], number)} "
        f"({_known(day['attack_gain'], signed)}) · {defenses}"
    )
    lines.append(f"Daily board place **{_known(day['place'], number)}**")
    if day["status"] == "Uncertain":
        lines.append(
            "-# Uncertain: some of this day's battles or readings may be missing, so these "
            "numbers may be off. The day on Clash Lens says why."
        )
    return Card(
        ("\n".join(lines),),
        colour,
        Link("Open day", f"{site.player(day['tag'])}?day={key}#legend-day-{key}"),
    )


def day_summary(
    site: Site,
    reset: datetime,
    days: Sequence[Mapping[str, Any]],
    colours: Mapping[str, int],
) -> list[tuple[Reply, tuple[str, ...]]]:
    """The Legend day summary DMs for the day that ended at `reset`, each with
    the tags of the players it names: one card per finished day from
    `summary_day`, in the player's colour, most trophies first, then the
    players whose day is not finished yet, at most `_WAITING_NAMES` a card;
    several DMs when too long for one, and none when no day is finished."""
    finished = sorted(
        (day for day in days if day["finished"]),
        key=lambda day: (day["end"] is None, -(day["end"] or 0), day["tag"]),
    )
    if not finished:
        return []
    start = reset - timedelta(days=1)
    key = start.date().isoformat()
    cards = [
        (_summary_card(site, day, key, colours.get(day["tag"], GOLD)), (day["tag"],))
        for day in finished
    ]
    waiting = [day for day in days if not day["finished"]]
    for first in range(0, len(waiting), _WAITING_NAMES):
        line = waiting[first:first + _WAITING_NAMES]
        cards.append((
            Card((
                "Not finished yet: "
                + ", ".join(f"{safe(day['name'])} {day['tag']}" for day in line)
                + ". Their day shows on Clash Lens once it is saved.",
            ), GREY),
            tuple(day["tag"] for day in line),
        ))
    parts: list[list[tuple[Card, tuple[str, ...]]]] = [[]]
    for card, tags in cards:
        if parts[-1] and (
            len(parts[-1]) == _SUMMARY_CARDS
            or sum(len(text) for item, _ in parts[-1] for text in item.parts)
            + len(card.parts[0]) > _SUMMARY_LENGTH
        ):
            parts.append([])
        parts[-1].append((card, tags))
    title = f"Legend day summary · {start:%a} {start.day} {start:%b}"
    return [
        (
            Reply(
                tuple(card for card, _ in part),
                title=title if len(parts) == 1 else f"{title} ({index} of {len(parts)})",
                footer="Places are from the Daily board at the 05:00 UTC Reset."
                if index == len(parts) else None,
                links=(Link("Summary settings", site.url(DISCORD_ALERTS)),)
                if index == len(parts) else (),
            ),
            tuple(tag for _, tags in part for tag in tags),
        )
        for index, part in enumerate(parts, 1)
    ]
