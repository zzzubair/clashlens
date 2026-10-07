"""The only part of the bot that talks to Discord.

It registers the slash commands, keeps each person to a few commands at a
time, decides who sees a reply, runs the database reads off the event loop
with a time limit, and turns a `Reply` into a Discord message with buttons.

Who sees a reply: in the bot's own DM every reply is a normal message. In a
server or group DM it is private to the person who typed the command, unless
the command takes `share` and they set it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any, TypeVar

import discord
import psycopg
from discord import app_commands

from . import replies
from .commands import Commands
from .replies import Reply

log = logging.getLogger("clashlens.bot")

T = TypeVar("T")

# Discord waits 3 seconds for the first answer. The link check must finish
# well inside that; later reads may take longer because the bot has already
# shown "thinking".
LINK_CHECK_SECONDS = 2.0
READ_SECONDS = 8.0
# Database reads running at once; the bot's connection pool holds as many.
READ_SLOTS = 4
# Dropdowns and buttons stop working after this, inside Discord's 15 minute
# limit on editing a reply.
VIEW_SECONDS = 600
# A person may run this many commands or clicks in any window of seconds.
COMMANDS_PER_WINDOW = 5
WINDOW_SECONDS = 15.0
# Autocomplete asks on every keystroke, so each person's own players are kept
# this long, for at most this many people.
OWN_CHOICES_SECONDS = 60.0
OWN_CHOICES_PEOPLE = 2000
_COLOUR = discord.Colour(0xF2B33D)


class Slow(Exception):
    pass


class Unavailable(Exception):
    pass


class RateLimiter:
    """At most `limit` actions per person in any `window` seconds."""

    def __init__(
        self,
        limit: int = COMMANDS_PER_WINDOW,
        window: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self._seen: dict[int, deque[float]] = {}

    def retry_after(self, person: int) -> float:
        """0 and the action counts, or the seconds until it would be allowed."""
        now = self.clock()
        if len(self._seen) > 10_000:
            # Forget people with nothing inside the window so memory stays bounded.
            self._seen = {
                key: times for key, times in self._seen.items() if times[-1] > now - self.window
            }
        times = self._seen.setdefault(person, deque())
        while times and times[0] <= now - self.window:
            times.popleft()
        if len(times) >= self.limit:
            return times[0] + self.window - now
        times.append(now)
        return 0.0


class _Recent:
    """A small time-limited memory, oldest entry forgotten first."""

    def __init__(self, seconds: float, size: int, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self.size = size
        self.clock = clock
        self._items: OrderedDict[int, tuple[float, Any]] = OrderedDict()

    def get(self, key: int) -> Any | None:
        item = self._items.get(key)
        if item is None or item[0] < self.clock():
            return None
        return item[1]

    def put(self, key: int, value: Any) -> None:
        self._items[key] = (self.clock() + self.seconds, value)
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)


def in_bot_dm(interaction: discord.Interaction) -> bool:
    return interaction.context.dm_channel


class DiscordApp:
    def __init__(self, commands: Commands, *, limiter: RateLimiter | None = None) -> None:
        self.commands = commands
        self.limiter = limiter or RateLimiter()
        self._slots = asyncio.Semaphore(READ_SLOTS)
        self._own_choices = _Recent(OWN_CHOICES_SECONDS, OWN_CHOICES_PEOPLE)

    async def read(self, work: Callable[..., T], *args: Any, seconds: float | None = None) -> T:
        """Run one blocking read in a worker thread, giving up after `seconds`."""
        try:
            async with asyncio.timeout(READ_SECONDS if seconds is None else seconds):
                async with self._slots:
                    return await asyncio.to_thread(work, *args)
        except TimeoutError as error:
            raise Slow from error
        except psycopg.errors.QueryCanceled as error:
            raise Slow from error
        except psycopg.OperationalError as error:
            raise Unavailable from error

    def render(self, reply: Reply, owner: int, interaction: discord.Interaction) -> dict[str, Any]:
        embed = discord.Embed(title=reply.title, description=reply.body, colour=_COLOUR)
        if reply.footer:
            embed.set_footer(text=reply.footer)
        message: dict[str, Any] = {"embed": embed}
        if reply.links or reply.choices or reply.show_all:
            message["view"] = ReplyView(self, reply, owner, interaction)
        return message

    async def respond(
        self,
        interaction: discord.Interaction,
        name: str,
        work: Callable[..., Reply],
        *args: Any,
        needs_link: bool = True,
        share: bool = False,
        always_private: bool = False,
    ) -> None:
        """Answer one slash command; `work` gets the linked account first when
        the command needs one."""
        started = time.perf_counter()
        outcome = "ok"
        private = not in_bot_dm(interaction) and (always_private or not share)
        owner = interaction.user.id
        try:
            wait = self.limiter.retry_after(owner)
            if wait:
                outcome = "limited"
                await self._send(interaction, replies.limited(wait), private=True)
                return
            if needs_link:
                account = await self.read(
                    self.commands.account, str(owner), seconds=LINK_CHECK_SECONDS
                )
                if account is None:
                    outcome = "not_linked"
                    reply = replies.not_linked(self.commands.site, interaction.user.name)
                    await self._send(interaction, reply, private=not in_bot_dm(interaction))
                    return
                args = (account, *args)
            await interaction.response.defer(ephemeral=private, thinking=True)
            reply = await self.read(work, *args)
            await interaction.followup.send(
                **self.render(reply, owner, interaction), ephemeral=private
            )
        except Slow:
            outcome = "slow"
            await self._send(interaction, replies.slow(), private=True)
        except Unavailable:
            outcome = "unavailable"
            await self._send(interaction, replies.unavailable(), private=True)
        except discord.HTTPException:
            outcome = "discord_error"
            log.warning("command %s could not reach Discord", name, exc_info=True)
        except Exception:
            outcome = "error"
            log.exception("command %s failed", name)
            await self._send(interaction, replies.failed(), private=True)
        finally:
            log.info(
                "command=%s outcome=%s ms=%d",
                name,
                outcome,
                (time.perf_counter() - started) * 1000,
            )

    async def _send(self, interaction: discord.Interaction, reply: Reply, *, private: bool) -> None:
        """Send a reply whether or not the interaction was answered already."""
        message = self.render(reply, interaction.user.id, interaction)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(**message, ephemeral=private)
            else:
                await interaction.response.send_message(**message, ephemeral=private)
        except discord.HTTPException:
            log.warning("could not send a reply to Discord", exc_info=True)

    async def on_component(
        self, interaction: discord.Interaction, view: ReplyView, work: Callable[[], Reply | None]
    ) -> None:
        """A dropdown pick or button click: swap the message for the new reply."""
        started = time.perf_counter()
        outcome = "ok"
        try:
            wait = self.limiter.retry_after(interaction.user.id)
            if wait:
                outcome = "limited"
                await self._send(interaction, replies.limited(wait), private=True)
                return
            await interaction.response.defer()
            reply = await self.read(work)
            if reply is None:
                outcome = "not_linked"
                reply = replies.not_linked(self.commands.site, interaction.user.name)
            await interaction.edit_original_response(
                **self.render(reply, view.owner, view.origin)
            )
            # The old buttons are gone; their timeout must not put them back.
            view.stop()
        except Slow:
            outcome = "slow"
            await self._send(interaction, replies.slow(), private=True)
        except Unavailable:
            outcome = "unavailable"
            await self._send(interaction, replies.unavailable(), private=True)
        except discord.HTTPException:
            outcome = "discord_error"
            log.warning("a button could not reach Discord", exc_info=True)
        except Exception:
            outcome = "error"
            log.exception("a button failed")
            await self._send(interaction, replies.failed(), private=True)
        finally:
            log.info(
                "component outcome=%s ms=%d", outcome, (time.perf_counter() - started) * 1000
            )

    async def own_player_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for options that name one of the person's own players."""
        person = interaction.user.id
        choices = self._own_choices.get(person)
        if choices is None:
            try:
                choices = await self.read(
                    self.commands.own_choices, str(person), seconds=LINK_CHECK_SECONDS
                )
            except Exception:
                # An empty list is the only safe autocomplete answer.
                log.warning("autocomplete read failed", exc_info=True)
                return []
            self._own_choices.put(person, choices)
        wanted = current.casefold().strip()
        return [
            app_commands.Choice(name=choice.label, value=choice.value)
            for choice in choices
            if wanted in choice.label.casefold()
        ][: replies.MAX_CHOICES]

    def register(self, tree: app_commands.CommandTree) -> None:
        app = self
        words = dict(replies.COMMANDS)

        @tree.command(name="help", description=words["help"])
        async def help_command(interaction: discord.Interaction) -> None:
            await app.respond(
                interaction,
                "help",
                app.commands.help,
                str(interaction.user.id),
                needs_link=False,
                always_private=True,
            )

        @tree.command(name="link", description=words["link"])
        async def link_command(interaction: discord.Interaction) -> None:
            await app.respond(
                interaction,
                "link",
                app.commands.link,
                str(interaction.user.id),
                interaction.user.name,
                needs_link=False,
                always_private=True,
            )

        @tree.command(name="me", description=words["me"])
        @app_commands.describe(
            account="One of your players; leave empty for all of them",
            share="Post the reply for everyone in this channel",
        )
        @app_commands.autocomplete(account=app.own_player_choices)
        async def me_command(
            interaction: discord.Interaction, account: str | None = None, share: bool = False
        ) -> None:
            await app.respond(interaction, "me", app.commands.me, account, share=share)

        @tree.command(name="main", description=words["main"])
        @app_commands.describe(account="The player to make your main")
        @app_commands.autocomplete(account=app.own_player_choices)
        async def main_command(interaction: discord.Interaction, account: str | None = None) -> None:
            await app.respond(
                interaction, "main", app.commands.main, account, always_private=True
            )


class ReplyView(discord.ui.View):
    """Link buttons, plus the dropdown and "Show all" button only the person
    who ran the command can use."""

    def __init__(
        self, app: DiscordApp, reply: Reply, owner: int, origin: discord.Interaction
    ) -> None:
        # Link buttons never expire, so a view with only links needs no timer.
        interactive = bool(reply.choices or reply.show_all)
        super().__init__(timeout=VIEW_SECONDS if interactive else None)
        self.app = app
        self.owner = owner
        self.origin = origin
        for link in reply.links:
            self.add_item(discord.ui.Button(label=link.label[:80], url=link.url))
        if reply.choices:
            select: discord.ui.Select[ReplyView] = discord.ui.Select(
                placeholder=reply.placeholder,
                options=[
                    discord.SelectOption(label=choice.label, value=choice.value)
                    for choice in reply.choices
                ],
            )
            action = reply.pick or "day"

            async def picked(interaction: discord.Interaction) -> None:
                tag = select.values[0]
                await app.on_component(
                    interaction,
                    self,
                    lambda: app.commands.pick(str(interaction.user.id), action, tag),
                )

            select.callback = picked  # type: ignore[method-assign]
            self.add_item(select)
        if reply.show_all:
            button: discord.ui.Button[ReplyView] = discord.ui.Button(
                label=f"Show all ({reply.show_all})"
            )

            async def show_all(interaction: discord.Interaction) -> None:
                await app.on_component(
                    interaction, self, lambda: app.commands.pick(
                        str(interaction.user.id), "all", ""
                    )
                )

            button.callback = show_all  # type: ignore[method-assign]
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner:
            return True
        await self.app._send(interaction, replies.not_your_menu(), private=True)
        return False

    async def on_timeout(self) -> None:
        # Keep the link buttons; the rest would only answer "interaction failed".
        for item in list(self.children):
            if not (isinstance(item, discord.ui.Button) and item.url):
                self.remove_item(item)
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass


def build_client(commands: Commands) -> tuple[discord.Client, app_commands.CommandTree]:
    """A client that needs no privileged access, and whose replies can never
    mention anyone."""
    # The bot never joins voice, so the missing voice libraries are expected.
    discord.VoiceClient.warn_nacl = discord.VoiceClient.warn_dave = False
    intents = discord.Intents.none()
    # Not privileged; it only keeps the servers the bot is in, which avoids
    # discord.py's warning about missing server state.
    intents.guilds = True
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
    tree = app_commands.CommandTree(
        client,
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
    )
    DiscordApp(commands).register(tree)
    return client, tree
