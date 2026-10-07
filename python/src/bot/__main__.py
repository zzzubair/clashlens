"""Start the Clash Lens Discord bot.

    python -m bot run         answer slash commands until stopped
    python -m bot register    send the commands to Discord

The bot opens one connection out to Discord and serves nothing itself.
Settings come from the environment:

    CLASHLENS_DISCORD_BOT_TOKEN_FILE  file holding the bot token
    CLASHLENS_DATABASE_URL_FILE       file holding the database address
    CLASHLENS_PUBLIC_ORIGIN           the website, for links in replies
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from clashlens.api_db import ApiDatabase
from clashlens.bootstrap import BootstrapError, read_secret_file

from .commands import Commands
from .discord_app import READ_SLOTS, build_client
from .replies import Site
from .store import Store

log = logging.getLogger("clashlens.bot")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    token_file: str
    database_url_file: str
    origin: str


def load_config(environ: Mapping[str, str]) -> Config:
    values = {
        name: environ.get(name, "").strip()
        for name in (
            "CLASHLENS_DISCORD_BOT_TOKEN_FILE",
            "CLASHLENS_DATABASE_URL_FILE",
            "CLASHLENS_PUBLIC_ORIGIN",
        )
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(f"missing settings: {', '.join(missing)}")
    origin = values["CLASHLENS_PUBLIC_ORIGIN"]
    parts = urlsplit(origin)
    if parts.scheme not in {"https", "http"} or not parts.netloc or parts.path not in {"", "/"}:
        raise ConfigError("CLASHLENS_PUBLIC_ORIGIN must be one website origin, like https://example.com")
    return Config(
        values["CLASHLENS_DISCORD_BOT_TOKEN_FILE"],
        values["CLASHLENS_DATABASE_URL_FILE"],
        origin.rstrip("/"),
    )


def read_token(path: str) -> str:
    """The bot token, checked for shape only; it is never printed or logged."""
    try:
        with open(path, encoding="utf-8") as file:
            token = file.read(512)
    except OSError as error:
        raise ConfigError("the Discord bot token file cannot be read") from error
    token = token.strip()
    if not token or len(token) >= 512 or any(char.isspace() for char in token):
        raise ConfigError("the Discord bot token file must hold one token")
    return token


def _commands(config: Config) -> Commands:
    try:
        database_url = read_secret_file(config.database_url_file)
    except BootstrapError as error:
        raise ConfigError("the database address file cannot be read") from error
    database = ApiDatabase(database_url, min_size=1, max_size=READ_SLOTS)
    return Commands(Store(database), Site(config.origin))


def _offline_commands() -> Commands:
    # Registering commands reads nothing, so it needs no database or website.
    return Commands(store=None, site=Site("https://example.invalid"))  # type: ignore[arg-type]


def run(config: Config) -> None:
    token = read_token(config.token_file)
    client, _tree = build_client(_commands(config))

    @client.event
    async def on_ready() -> None:
        log.info("connected to Discord as application %s", client.application_id)

    # discord.py reconnects on its own after network drops.
    client.run(token, log_handler=None)


def command_payload() -> list[dict]:
    _client, tree = build_client(_offline_commands())
    return [command.to_dict(tree) for command in tree.get_commands()]


async def register(config: Config) -> int:
    token = read_token(config.token_file)
    client, tree = build_client(_offline_commands())
    async with client:
        await client.login(token)
        synced = await tree.sync()
    print(f"registered {len(synced)} commands: {', '.join(sorted(c.name for c in synced))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("run")
    actions.add_parser("register")
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        config = load_config(os.environ)
        if arguments.action == "register":
            return asyncio.run(register(config))
        run(config)
    except ConfigError as error:
        print(f"bot: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
