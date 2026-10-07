"""The only part of the bot that talks to Discord.

It registers the slash commands, decides who sees a reply, runs the database
reads off the event loop, and turns a `Reply` into Discord messages with
buttons.

Who sees a reply: in the bot's own DM every reply is a normal message. In a
server or group DM it is private to the person who typed the command, unless
the command takes `share` and they set it; refusals and failures stay private.
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

from clashlens.api_groups import COMPARISON_DAYS

from . import replies
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
# Discord allows an app installed only to a person 5 follow-up messages per
# interaction.
FOLLOWUPS = 5
_COLOUR = discord.Colour(0xF2B33D)


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
    ) -> list[dict[str, Any]]:
        """One message per page of the reply, the first with the title and
        buttons; `deliver` adds the footer."""
        messages: list[dict[str, Any]] = [
            {
                "embed": discord.Embed(
                    title=reply.title if index == 0 else None, description=text, colour=_COLOUR
                )
            }
            for index, text in enumerate(replies.pages(reply.body))
        ]
        if reply.links or reply.choices or reply.show_all:
            messages[0]["view"] = ReplyView(self, reply, owner, origin, private)
        return messages

    async def respond(
        self,
        interaction: discord.Interaction,
        name: str,
        work: Callable[..., Reply],
        *args: Any,
        needs_link: bool = True,
        share: bool = False,
    ) -> None:
        """Answer one slash command; `work` gets the linked account first when
        the command needs one."""
        started = time.perf_counter()
        outcome = "ok"
        shared = share and not in_bot_dm(interaction)
        try:
            # Acknowledge before any database work: Discord waits 3 seconds.
            await interaction.response.defer(
                ephemeral=not shared and not in_bot_dm(interaction), thinking=True
            )
            if needs_link:
                account = await self.read(self.commands.account, str(interaction.user.id))
                if account is None:
                    outcome = "not_linked"
                    reply = replies.not_linked(
                        self.commands.site, interaction.user.name, self.commands.now()
                    )
                    await self._private(interaction, reply, shared)
                    return
                args = (account, *args)
            reply = await self.read(work, *args)
            await self._send(interaction, reply, private=not shared)
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
        messages = self.render(reply, interaction.user.id, interaction, private=private)
        try:
            await self.deliver(
                interaction, reply, messages, owner=interaction.user.id, private=private
            )
        except discord.HTTPException:
            log.warning("could not send a reply to Discord", exc_info=True)

    async def deliver(
        self,
        interaction: discord.Interaction,
        reply: Reply,
        messages: list[dict[str, Any]],
        *,
        owner: int,
        private: bool,
        replacing: _OwnedView | None = None,
    ) -> None:
        """Send `reply`'s rendered messages for one interaction, the first in
        place of `replacing`'s message when given. What does not fit in
        Discord's follow-up allowance waits behind a "Show more" button. The
        last message sent ends with the footer and how old the data is now."""
        first_free = replacing is not None or not interaction.response.is_done()
        room = FOLLOWUPS + 1 if first_free else FOLLOWUPS
        rest = messages[room:]
        messages = [dict(message) for message in messages[:room]]
        if rest:
            messages[-1]["view"] = MoreView(self, reply, owner, rest, private)
        ending = replies.updated_line(reply.updated, self.commands.now())
        messages[-1]["embed"].set_footer(
            text=f"{reply.footer}\n{ending}" if reply.footer else ending
        )

        if replacing is not None:
            # Without view=None Discord keeps the old buttons on the message.
            await interaction.edit_original_response(**{"view": None, **messages.pop(0)})
            # The old buttons are gone; their timeout must not put them back.
            replacing.stop()
        elif not interaction.response.is_done():
            await interaction.response.send_message(**messages.pop(0), ephemeral=private)
        for message in messages:
            sent = await interaction.followup.send(**message, ephemeral=private, wait=True)
            if isinstance(message.get("view"), MoreView):
                message["view"].message = sent

    async def on_component(
        self, interaction: discord.Interaction, view: _OwnedView, work: Callable[[], Reply | None]
    ) -> None:
        """A dropdown pick or button click: swap the message for the new reply.
        "Show more" sends the rest of its reply only while the person's account
        still has every player it lists."""
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
                if reply is None:
                    # Never on the message itself, which others may see.
                    outcome = "not_linked"
                    reply = replies.not_linked(
                        self.commands.site, interaction.user.name, self.commands.now()
                    )
                    await self._send(interaction, reply, private=True)
                    return
                if isinstance(view, MoreView):
                    if reply is not view.reply:
                        outcome = "not_own"
                        await self._send(interaction, reply, private=True)
                        return
                    await interaction.edit_original_response(view=None)
                    view.stop()
                    await self.deliver(
                        interaction, reply, view.messages, owner=view.owner, private=view.private
                    )
                    return
                messages = self.render(reply, view.owner, interaction, private=view.private)
                await self.deliver(
                    interaction,
                    reply,
                    messages,
                    owner=view.owner,
                    private=view.private,
                    replacing=view,
                )
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

    async def own_player_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for options that name one of the person's own players."""
        choices = await self._choices(self.commands.own_choices, str(interaction.user.id))
        return self._offer(choices, current)

    async def own_group_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for the person's own groups."""
        choices = await self._choices(self.commands.group_choices, str(interaction.user.id))
        return self._offer(choices, current)

    async def any_player_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for /player: own and saved players, then name matches."""
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
                needs_link=False,
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
            await app.respond(interaction, "main", app.commands.main, account)

        share_text = "Post the reply for everyone in this channel"

        @tree.command(name="player", description=words["player"])
        @app_commands.describe(player="A player tag, with or without #, or a name", share=share_text)
        @app_commands.autocomplete(player=app.any_player_choices)
        async def player_command(
            interaction: discord.Interaction, player: str, share: bool = False
        ) -> None:
            await app.respond(interaction, "player", app.commands.player, player, share=share)

        @tree.command(name="top", description=words["top"])
        @app_commands.describe(share=share_text)
        async def top_command(interaction: discord.Interaction, share: bool = False) -> None:
            await app.respond(interaction, "top", app.commands.top, share=share)

        @tree.command(name="rank", description=words["rank"])
        @app_commands.describe(account="One of your players; empty for your main", share=share_text)
        @app_commands.autocomplete(account=app.own_player_choices)
        async def rank_command(
            interaction: discord.Interaction, account: str | None = None, share: bool = False
        ) -> None:
            await app.respond(interaction, "rank", app.commands.rank, account, share=share)

        group_word = replies.GROUP_WORD

        @tree.command(name=group_word, description=words[group_word])
        @app_commands.describe(
            group=f"One of your {group_word}s; empty to list them",
            days="Ended Legend days to compare",
            share=share_text,
        )
        @app_commands.rename(group=group_word)
        @app_commands.choices(
            days=[app_commands.Choice(name=str(days), value=days) for days in COMPARISON_DAYS]
        )
        @app_commands.autocomplete(group=app.own_group_choices)
        async def group_command(
            interaction: discord.Interaction,
            group: str | None = None,
            days: int = 7,
            share: bool = False,
        ) -> None:
            await app.respond(
                interaction, group_word, app.commands.group, group, days, share=share
            )

        @tree.command(name="season", description=words["season"])
        @app_commands.describe(account="One of your players; empty for your main", share=share_text)
        @app_commands.autocomplete(account=app.own_player_choices)
        async def season_command(
            interaction: discord.Interaction, account: str | None = None, share: bool = False
        ) -> None:
            await app.respond(interaction, "season", app.commands.season, account, share=share)


class _OwnedView(discord.ui.View):
    """Buttons only the person who ran the command can use."""

    def __init__(self, *, timeout: float | None) -> None:
        super().__init__(timeout=timeout)
        self.lock = asyncio.Lock()

    app: DiscordApp
    owner: int
    private: bool
    # The latest interaction that may still edit the message.
    origin: discord.Interaction | None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner:
            return True
        await self.app._send(interaction, replies.not_your_menu(), private=True)
        return False


class MoreView(_OwnedView):
    """A "Show more" button for the messages one interaction could not send."""

    def __init__(
        self,
        app: DiscordApp,
        reply: Reply,
        owner: int,
        messages: list[dict[str, Any]],
        private: bool,
    ) -> None:
        super().__init__(timeout=VIEW_SECONDS)
        self.app = app
        self.reply = reply
        self.owner = owner
        self.messages = messages
        self.private = private
        self.origin = None
        self.message: discord.WebhookMessage | None = None
        button: discord.ui.Button[MoreView] = discord.ui.Button(label="Show more")

        async def more(interaction: discord.Interaction) -> None:
            await app.on_component(
                interaction, self, lambda: app.commands.keep(str(interaction.user.id), reply)
            )

        button.callback = more  # type: ignore[method-assign]
        self.add_item(button)

    async def on_timeout(self) -> None:
        try:
            if self.origin is not None:
                await self.origin.edit_original_response(view=None)
            elif self.message is not None:
                await self.message.edit(view=None)
        except discord.HTTPException:
            pass


class ReplyView(_OwnedView):
    """Link buttons, plus the dropdown and "Show all" button only the person
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
        interactive = bool(reply.choices or reply.show_all)
        super().__init__(timeout=VIEW_SECONDS if interactive else None)
        self.app = app
        self.owner = owner
        self.origin = origin
        self.private = private
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
