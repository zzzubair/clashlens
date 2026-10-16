"""The only part of the bot that talks to Discord.

It registers the slash commands, decides who sees a reply, runs the database
reads off the event loop, and turns a `Reply` into a Discord message laid out
in Discord's cards (its "components v2"), with buttons; an attack alert uses
Discord's older layout instead, the only one that can carry the line a
phone's notification shows.

Who sees a reply: every reply is a normal message anyone in the conversation
can see and forward. Only the not-connected pointer and plain errors meant for
the person who typed the command (`Reply.private`) are shown to them alone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import discord
import psycopg
from discord import app_commands

from . import replies
from .alerts import Refused, Unreachable
from .commands import Commands
from .replies import Reply

log = logging.getLogger("clashlens.bot")

T = TypeVar("T")

# Autocomplete cannot show "thinking", and Discord waits 3 seconds for it.
AUTOCOMPLETE_SECONDS = 2.0
# Database reads running at once; the bot's connection pool holds as many.
READ_SLOTS = 4
# Dropdowns and buttons stop working after this, inside Discord's 15 minute
# limit on editing a reply.
VIEW_SECONDS = 600
# Discord's JSON error codes for a DM it will not deliver
# (https://docs.discord.com/developers/topics/opcodes-and-status-codes):
# 50278 "no mutual guilds", 50007 "Cannot send messages to this user",
# 10013 "Unknown user", 50033 "Invalid Recipient(s)".
_REFUSALS = {
    50278: "no_shared_server",
    50007: "dms_closed",
    10013: "unknown_user",
    50033: "unknown_user",
}


def lay_out(view: discord.ui.LayoutView, reply: Reply) -> None:
    """Put `reply` into `view`: its title, each card with its coloured edge
    and a thin line between its parts, the small print below, then its
    buttons."""
    if reply.title:
        view.add_item(discord.ui.TextDisplay(f"## {reply.title}"))
    for card in reply.cards:
        items: list[discord.ui.Item[Any]] = []
        for index, part in enumerate(card.parts):
            if index:
                items.append(discord.ui.Separator())
            text = discord.ui.TextDisplay(part)
            if card.button is not None and index == len(card.parts) - 1:
                items.append(discord.ui.Section(text, accessory=_button(card.button)))
            else:
                items.append(text)
        view.add_item(discord.ui.Container(*items, accent_colour=card.colour))
    if reply.footer:
        view.add_item(discord.ui.TextDisplay(f"-# {reply.footer}"))
    if reply.links:
        view.add_item(discord.ui.ActionRow(*(_button(link) for link in reply.links)))


def lay_out_notice(reply: Reply) -> dict[str, Any]:
    """`reply` in Discord's older layout, the only one whose message can
    carry the line a phone's notification shows: `notice`, then each card as
    a box with its coloured edge, the first topped by `title` in small bold,
    which keeps a large first line in the box from leaving a gap, and the
    last ending in `footer` as small print, then any buttons."""
    embeds = [
        discord.Embed(description="\n\n".join(card.parts), colour=card.colour)
        for card in reply.cards
    ]
    if reply.title:
        embeds[0].set_author(name=reply.title)
    if reply.footer:
        embeds[-1].description += f"\n-# {reply.footer}"
    view = discord.ui.View(timeout=None)
    for link in (*(card.button for card in reply.cards if card.button), *reply.links):
        view.add_item(_button(link))
    return {"content": reply.notice, "embeds": embeds, **({"view": view} if view.children else {})}


def _button(link: replies.Link) -> discord.ui.Button[Any]:
    # Discord shows at most 80 characters on a button.
    return discord.ui.Button(label=link.label[:80], url=link.url)


class Slow(Exception):
    pass


class Unavailable(Exception):
    pass


def in_bot_dm(interaction: discord.Interaction) -> bool:
    return interaction.context.dm_channel


class DiscordApp:
    def __init__(self, commands: Commands) -> None:
        self.commands = commands
        self._slots = asyncio.Semaphore(READ_SLOTS)

    async def read(
        self, work: Callable[..., T], *args: Any, deadline: float | None = None
    ) -> T:
        """Run blocking database work in a worker thread. The database's own
        time limits end it: a statement over its limit is slow and rolls back,
        no connection within the pool's wait means Clash Lens is unavailable.
        The slot stays taken until the work has ended. Work still waiting for
        a slot at `deadline`, in event loop time, is dropped unrun."""
        try:
            async with self._slots:
                if deadline is not None and asyncio.get_running_loop().time() > deadline:
                    raise Slow
                return await asyncio.to_thread(work, *args)
        except psycopg.errors.QueryCanceled as error:
            raise Slow from error
        except psycopg.OperationalError as error:
            raise Unavailable from error

    def render(
        self, reply: Reply, owner: int, origin: discord.Interaction, *, private: bool
    ) -> dict[str, Any]:
        """The reply as one message."""
        return {"view": ReplyView(self, reply, owner, origin, private)}

    async def respond(
        self,
        interaction: discord.Interaction,
        name: str,
        work: Callable[..., Reply],
        *args: Any,
    ) -> None:
        """Answer one slash command with `work`'s reply."""
        started = time.perf_counter()
        outcome = "ok"
        # Outside the bot's own DM a private reply must first remove the public
        # "thinking"; in the DM every reply is the person's own anyway.
        shared = not in_bot_dm(interaction)
        try:
            # Acknowledge before any database work: Discord waits 3 seconds.
            await interaction.response.defer(thinking=True)
            reply = await self.read(work, *args)
            if reply.private:
                await self._private(interaction, reply, shared)
            else:
                await self._send(interaction, reply, private=False)
        except Slow:
            outcome = "slow"
            await self._private(interaction, replies.slow(), shared)
        except Unavailable:
            outcome = "unavailable"
            await self._private(interaction, replies.unavailable(), shared)
        except discord.HTTPException:
            outcome = "discord_error"
            log.warning("command %s could not reach Discord", name, exc_info=True)
        except Exception:
            outcome = "error"
            log.exception("command %s failed", name)
            await self._private(interaction, replies.failed(), shared)
        finally:
            log.info(
                "command=%s outcome=%s ms=%d",
                name,
                outcome,
                (time.perf_counter() - started) * 1000,
            )

    async def _private(self, interaction: discord.Interaction, reply: Reply, shared: bool) -> None:
        """A reply only the person sees. After a public "thinking", Discord
        would show the next message to everyone, so that goes first."""
        if shared:
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                log.warning(
                    "could not remove a shared reply's placeholder, so the private "
                    "reply was not sent",
                    exc_info=True,
                )
                return
        await self._send(interaction, reply, private=True)

    async def _send(self, interaction: discord.Interaction, reply: Reply, *, private: bool) -> None:
        """Send a reply whether or not the interaction was answered already.
        In the bot's own DM every reply is a normal message."""
        private = private and not in_bot_dm(interaction)
        message = self.render(reply, interaction.user.id, interaction, private=private)
        try:
            await self.deliver(interaction, message, private=private)
        except discord.HTTPException:
            log.warning("could not send a reply to Discord", exc_info=True)

    async def deliver(
        self,
        interaction: discord.Interaction,
        message: dict[str, Any],
        *,
        private: bool,
        replacing: _OwnedView | None = None,
    ) -> None:
        """Send a rendered message for one interaction, in place of
        `replacing`'s message when given."""
        if replacing is not None:
            # Without view=None Discord keeps the old buttons on the message.
            await interaction.edit_original_response(**{"view": None, **message})
            # The old buttons are gone; their timeout must not put them back.
            replacing.stop()
        elif not interaction.response.is_done():
            await interaction.response.send_message(**message, ephemeral=private)
        else:
            await interaction.followup.send(**message, ephemeral=private)

    async def on_component(
        self, interaction: discord.Interaction, view: _OwnedView, work: Callable[[], Reply]
    ) -> None:
        """A dropdown pick: swap the message for the new reply."""
        started = time.perf_counter()
        outcome = "ok"
        try:
            await interaction.response.defer()
            # One click at a time, and none on a menu already replaced or used.
            async with view.lock:
                if view.is_finished():
                    outcome = "already_used"
                    return
                # This click may edit the message for 15 minutes more, the
                # first command no longer can.
                view.origin = interaction
                reply = await self.read(work)
                if reply.private:
                    # A refusal for the clicker never replaces the message.
                    outcome = "refused"
                    await self._send(interaction, reply, private=True)
                    return
                message = self.render(reply, view.owner, interaction, private=view.private)
                await self.deliver(interaction, message, private=view.private, replacing=view)
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

    async def _choices(
        self, work: Callable[..., list[replies.Choice]], *args: Any
    ) -> list[replies.Choice]:
        """One autocomplete read, or no entries when it cannot answer in time."""
        deadline = asyncio.get_running_loop().time() + AUTOCOMPLETE_SECONDS
        task = asyncio.ensure_future(self.read(work, *args, deadline=deadline))
        # A read already running when the answer is due keeps its slot until
        # it ends; one still waiting for a slot then never runs.
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        try:
            async with asyncio.timeout(AUTOCOMPLETE_SECONDS):
                return await asyncio.shield(task)
        except Exception:
            # An empty list is the only safe autocomplete answer.
            log.warning("autocomplete read failed", exc_info=True)
            return []

    @staticmethod
    def _offer(choices: list[replies.Choice], current: str) -> list[app_commands.Choice[str]]:
        wanted = current.casefold().strip()
        return [
            app_commands.Choice(name=choice.label, value=choice.value)
            for choice in choices
            if wanted in choice.label.casefold()
        ][: replies.MAX_CHOICES]

    async def any_player_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for /stats: own players, then name matches."""
        choices = await self._choices(
            self.commands.player_choices, str(interaction.user.id), current
        )
        return self._offer(choices, "")

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
            )

        @tree.command(name="link", description=words["link"])
        async def link_command(interaction: discord.Interaction) -> None:
            await app.respond(
                interaction,
                "link",
                app.commands.link,
                str(interaction.user.id),
                interaction.user.name,
            )

        @tree.command(name="alerts", description=words["alerts"])
        async def alerts_command(interaction: discord.Interaction) -> None:
            await app.respond(
                interaction,
                "alerts",
                app.commands.alerts,
                str(interaction.user.id),
                interaction.user.name,
            )

        @tree.command(name="stats", description=words["stats"])
        @app_commands.describe(
            player=(
                "A player tag, with or without #, or a name; empty for your default "
                "account"
            )
        )
        @app_commands.autocomplete(player=app.any_player_choices)
        async def stats_command(interaction: discord.Interaction, player: str | None = None) -> None:
            await app.respond(
                interaction,
                "stats",
                app.commands.stats,
                str(interaction.user.id),
                interaction.user.name,
                player,
            )


class _OwnedView(discord.ui.LayoutView):
    """Buttons only the person who ran the command can use."""

    def __init__(self, *, timeout: float | None) -> None:
        super().__init__(timeout=timeout)
        self.lock = asyncio.Lock()

    app: DiscordApp
    owner: int
    private: bool
    # The latest interaction that may still edit the message.
    origin: discord.Interaction

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner:
            return True
        await self.app._send(interaction, replies.not_your_menu(), private=True)
        return False


class ReplyView(_OwnedView):
    """The reply's cards and link buttons, plus the dropdown only the person
    who ran the command can use."""

    def __init__(
        self,
        app: DiscordApp,
        reply: Reply,
        owner: int,
        origin: discord.Interaction,
        private: bool,
    ) -> None:
        # Link buttons never expire, so a view with only links needs no timer.
        interactive = bool(reply.choices)
        super().__init__(timeout=VIEW_SECONDS if interactive else None)
        self.app = app
        self.owner = owner
        self.origin = origin
        self.private = private
        lay_out(self, reply)
        self._choices: discord.ui.ActionRow[ReplyView] | None = None
        if reply.choices:
            select: discord.ui.Select[ReplyView] = discord.ui.Select(
                placeholder=reply.placeholder,
                options=[
                    discord.SelectOption(label=choice.label, value=choice.value)
                    for choice in reply.choices
                ],
            )
            action = reply.pick or "stats"

            async def picked(interaction: discord.Interaction) -> None:
                tag = select.values[0]
                await app.on_component(
                    interaction,
                    self,
                    lambda: app.commands.pick(str(interaction.user.id), action, tag),
                )

            select.callback = picked  # type: ignore[method-assign]
            self._choices = discord.ui.ActionRow(select)
            self.add_item(self._choices)

    async def on_timeout(self) -> None:
        # Keep the link buttons; the dropdown would only answer "interaction failed".
        if self._choices is not None:
            self.remove_item(self._choices)
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass


class DiscordMessenger:
    """Sends attack alerts and Legend day summaries as normal DMs from the
    bot. discord.py waits out Discord's rate limits before each request, tries
    a request again itself after a rate limit or a server error, and gives up
    on one after aiohttp's 5 minutes, or at once on a rate limit longer than
    the DM's time left. The alert and summary loops cancel a DM still going at
    the end of its 2 minutes; one cancelled in a global rate limit wait would
    leave that wait shut for every later request, so it is opened again then.
    The two loops can send at once, so the client's own rate limit wait is
    put back only once neither is sending."""

    def __init__(self, client: discord.Client) -> None:
        self.client = client
        self._limit = client.http.max_ratelimit_timeout
        self._sending = 0

    async def send_dm(
        self, discord_user_id: str, reply: Reply, nonce: int | str, time_left: float
    ) -> None:
        if reply.notice:
            message = lay_out_notice(reply)
        else:
            view = discord.ui.LayoutView(timeout=None)
            lay_out(view, reply)
            message = {"view": view}
        http = self.client.http
        http.max_ratelimit_timeout = time_left
        self._sending += 1
        try:
            channel = await self.client.create_dm(discord.Object(int(discord_user_id)))
            await channel.send(**message, nonce=nonce)
        except asyncio.CancelledError:
            http._global_over.set()
            raise
        except discord.RateLimited as error:
            raise Unreachable(error.retry_after) from error
        except discord.DiscordServerError as error:
            raise Unreachable from error
        except discord.HTTPException as error:
            if error.status == 429:
                raise Unreachable(float(error.response.headers.get("Retry-After", 0))) from error
            reason = "unknown_user" if isinstance(error, discord.NotFound) else "other"
            raise Refused(_REFUSALS.get(error.code, reason)) from error
        except Exception as error:
            # A timeout or a dropped connection: the DM may or may not have gone.
            log.warning("a DM could not reach Discord", exc_info=True)
            raise Unreachable from error
        finally:
            self._sending -= 1
            if not self._sending:
                http.max_ratelimit_timeout = self._limit


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
