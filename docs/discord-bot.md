# Discord bot

The Clash Lens Discord bot answers nine slash commands: `/help`, `/link`, `/me`,
`/main`, `/player`, `/top`, `/rank`, `/group` and `/season`. Its code is in
[`python/src/bot`](../python/src/bot). People use it mostly in a direct message
(DM) with the bot, and also in servers. Every command except `/help` and `/link`
needs the person's Discord account connected to their Clash Lens account on the
website.

## How it runs

- One container, `clashlens-discord-bot`, in the Clash Lens pod, from the same
  Python image as the API. It opens one connection out to Discord (the
  "gateway") and listens on no port, so it adds no public endpoint.
- It reads Clash Lens data with the private API's own read code and the API's
  database login, so numbers in Discord match the website. Its only write is
  each account's main player (migration 0080). Whether the bot should instead go
  through the private API, as [architecture](architecture.md) says outside
  programs should, is an open owner decision.
- It needs no privileged Discord access ("intents"), and its replies can never
  mention or ping anyone.
- `./ops up` always installs the unit
  [`clashlens-discord-bot.container`](../deploy/quadlet/clashlens-discord-bot.container),
  but starts it only in production with `CLASHLENS_DISCORD_BOT=on` in
  `app.env`. Without that setting it stays stopped, across reboots too.
- The bot token lives in the mode-600 file
  `/srv/clashlens-secrets/clashlens-discord-bot.token` (the directory named by
  `CLASHLENS_API_KEY_HOST_DIR`). `./ops up` copies it into Podman's private
  secret store; it is never written to a unit, an environment file or a log.
- Links in replies use the website address in `CLASHLENS_PUBLIC_ORIGIN`.
- Memory is capped at 384 MiB and CPU at half a core.

## Reset time in replies

Only replies that show Legend data end with the next Reset as a Discord
timestamp, which each viewer sees in their own local time: a player's day or
Season (`/me`, `/player`, `/season`, and a player's status word), `/group`
standings, and live numbers (`/top`, `/rank`). Errors, refusals, instructions,
`/help`, `/link`, `/main`, menus and "which one?" prompts (including the
`/group` list of groups), and lists of linked players have no Reset line on
purpose.

## Go-live checklist

Nothing below has been done yet. Do the steps in order.

1. **Discord application.** In the Discord Developer Portal, open the Clash Lens
   application (the same one the website's Discord sign-in uses is fine, or a
   new one).
   - Bot page: name it "Clash Lens", leave the picture empty, turn off "Public
     Bot" until launch, and leave every privileged intent off.
   - General Information: leave "Interactions Endpoint URL" empty, or the bot
     receives nothing.
   - Installation: tick both "User Install" and "Guild Install". User install
     needs the scope `applications.commands`; guild install needs
     `applications.commands` and `bot`, with no permissions.
2. **Token.** Bot page → Reset Token, then on the server:
   ```sh
   install -m 600 /dev/null /srv/clashlens-secrets/clashlens-discord-bot.token
   nano /srv/clashlens-secrets/clashlens-discord-bot.token   # paste, one line
   ```
3. **Turn it on.** Add `CLASHLENS_DISCORD_BOT=on` to `app.env`, check that
   `CLASHLENS_PUBLIC_ORIGIN` is the live website address, then run
   `./ops build` and `./ops up`. `up` refuses to start if the token file is
   missing or not mode 600, before stopping anything.
4. **Check it connected.** `./ops logs discord-bot` should show
   `connected to Discord as application …` and no errors.
5. **Register the commands.** This tells Discord the command list; run it again
   whenever the commands change:
   ```sh
   podman exec clashlens-discord-bot python -m bot register
   ```
   It prints `registered 9 commands: …`. Commands appear only where the app is
   installed, so nobody else sees them yet.
6. **Test in a DM.** Open the install link from the Installation page, choose
   "Add to My Apps", then DM the bot:
   - `/help` before connecting: says "not connected yet".
   - `/link`: shows the two website buttons. Connect Discord on the website, run
     `/link` again: lists your players.
   - `/me`, `/main`, `/player`, `/top`, `/rank`, `/group`, `/season`: replies
     are normal messages in the DM.
7. **Test in a test server.** Install the app to a private test server ("Add to
   Server"). Run `/me`: only you see the reply. Run `/me share: True`: everyone
   sees it. Have a second, unconnected account run `/me`: it gets a private
   connect message and nothing else.
8. **Launch.** Turn "Public Bot" on when ready, and share the install link.

To turn the bot off, set `CLASHLENS_DISCORD_BOT=off` (or remove the line) and
run `./ops up`; the token file can stay. To stop it at once without `up`:
`systemctl --user stop clashlens-discord-bot.service`.

## Changing the commands

Fable's design picks are easy to change before launch: the linking rule is in
`discord_app.py` (`needs_link`), private-by-default replies are in
`DiscordApp.respond`, command names and descriptions are in
`replies.COMMANDS`, and the product word for groups is `replies.GROUP_WORD`.
Re-run step 5 after changing names or options.
