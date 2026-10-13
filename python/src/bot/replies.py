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
    ("link", "Connect Discord and turn on attack alerts on the Clash Lens website"),
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


# Where a Clasher chooses the bot's default player.
DISCORD_SETTINGS = "/account/providers"
# Where a Clasher connects Discord and turns attack alerts on or off; /link
# opens it, and a signed-out visitor signs in first and lands there.
DISCORD_ALERTS = "/account/discord-alerts"


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
            Link("Connect Discord on Clash Lens", site.url(DISCORD_ALERTS)),
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
    """A connected account's pointer: attack alerts are managed on the
    website, never in the bot."""
    return Reply(
        f"Connected as @{safe(username)}. Turn attack alerts on or off for your "
        "players on Clash Lens.",
        updated=now,
        links=(Link("Discord alerts on Clash Lens", site.url(DISCORD_ALERTS)),),
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
    """The Season's three-star share of attacks so far, as /stats and attack
    alerts show it."""
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
            f"{attacker}'s hit rate in this attack's Season: {attacker_hit_rate}"
        ),
        title=f"{safe(alert['defender_name'])} {alert['defender_tag']} was attacked",
        updated=alert["saved_at"],
        links=(
            Link("Open on Clash Lens", site.player(alert["defender_tag"])),
            Link("Alert settings", site.url(DISCORD_ALERTS)),
        ),
    )


def alerts_on(site: Site, switch: Mapping[str, Any]) -> Reply:
    """The DM sent when attack alerts go on for a player, and again when
    Discord is connected again, which also proves the bot can reach the
    person."""
    player = safe(switch["name"])
    return Reply(
        f"You'll get a message here as soon as {player} is attacked.",
        title=f"Attack alerts are on for {player} {switch['tag']}",
        updated=switch["confirm_requested_at"],
        links=(Link("Alert settings", site.url(DISCORD_ALERTS)),),
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
# Discord shows at most 4,096 characters in a message's description.
_SUMMARY_LENGTH = 3_900


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


def summary_day(page: Mapping[str, Any], reset: datetime) -> dict[str, Any] | None:
    """The player's Legend day that ended at `reset`, as their page shows it:
    its label, end of day and the place on that Reset's Daily board, with
    "finished" False while its final result is not saved yet. None when the
    page has no such Legend day."""
    ready = page.get("screen_ready") or {}
    days = {datetime.fromisoformat(day["ranked_day_start"]): day for day in ready.get("days", [])}
    day = days.get(reset - timedelta(days=1))
    if day is None:
        return None
    codes = day["uncertainty_reasons"]
    battles = len(day["offense_events"]) + len(day["defense_events"])
    # The page's own rule for a day that is not a Legend day.
    if not battles and ("not_enrolled" in codes or "player_not_eligible" in codes):
        return None
    if day["state"] == "Live":
        return {"tag": page["tag"], "name": page["name"], "finished": False}
    start = day["start_trophies"]
    net = day["net_trophy_change"]
    raised = _adjustment(day, "weekly_reset", "season_reset")
    # dayEnd on the website: the next day's start, else, within a day of
    # the Reset, the start plus the change and any reset at that Reset.
    following = days.get(reset)
    following = None if following is None else following["start_trophies"]
    own = None if start is None or net is None else start + net + (raised or {}).get("amount", 0)
    conflict = None
    if own is not None and following is not None and own != following:
        conflict = f"Battles add up to {number(own)}; next day started at {number(following)}."
    automatic = _adjustment(day, "automatic_defense")
    return {
        "tag": page["tag"],
        "name": page["name"],
        "finished": True,
        "status": "Uncertain" if conflict else _day_status(day),
        "conflict": conflict,
        "start": start,
        "end": following if following is not None else own,
        "net": net,
        "raised": raised,
        "attack_count": day["attack_count"],
        "attack_gain": day["attack_gain"],
        "defense_count": day["defense_count"],
        "defense_loss": day["defense_loss"],
        "automatic_loss": None if automatic is None else -automatic["amount"],
        "place": day.get("reset_rank"),
    }


def _summary_block(site: Site, day: Mapping[str, Any], key: str) -> str:
    lines = [f"**{safe(day['name'])} {day['tag']}** · {day['status']}"]
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
    if day["status"] == "Uncertain":
        lines.append(
            day["conflict"]
            or "Uncertain: some of this day's battles or readings may be missing, so these "
            "numbers may be off. The day on Clash Lens says why."
        )
    lines.append(
        f"Daily board place {_known(day['place'], number)} · "
        f"[Day on Clash Lens]({site.player(day['tag'])}?day={key}#legend-day-{key})"
    )
    return "\n".join(lines)


def day_summary(site: Site, reset: datetime, days: Sequence[Mapping[str, Any]]) -> list[Reply]:
    """The Legend day summary DMs for the day that ended at `reset`: one block
    per finished day from `summary_day`, most trophies first, then the players
    whose day is not finished yet; several DMs when too long for one, and
    none when no day is finished."""
    finished = sorted(
        (day for day in days if day["finished"]),
        key=lambda day: (day["end"] is None, -(day["end"] or 0), day["tag"]),
    )
    if not finished:
        return []
    start = reset - timedelta(days=1)
    key = start.date().isoformat()
    blocks = [_summary_block(site, day, key) for day in finished]
    waiting = [day for day in days if not day["finished"]]
    if waiting:
        blocks.append(
            "Not finished yet: "
            + ", ".join(f"{safe(day['name'])} {day['tag']}" for day in waiting)
            + ". Their day shows on Clash Lens once it is saved."
        )
    blocks.append("Places are from the Daily board at the 05:00 UTC Reset.")
    parts: list[list[str]] = [[]]
    for block in blocks:
        if parts[-1] and len("\n\n".join([*parts[-1], block])) > _SUMMARY_LENGTH:
            parts.append([])
        parts[-1].append(block)
    title = f"Legend day summary · {start:%a} {start.day} {start:%b}"
    return [
        Reply(
            "\n\n".join(part),
            title=title if len(parts) == 1 else f"{title} ({index} of {len(parts)})",
            updated=reset,
            links=(Link("Summary settings", site.url(DISCORD_ALERTS)),)
            if index == len(parts) else (),
        )
        for index, part in enumerate(parts, 1)
    ]
