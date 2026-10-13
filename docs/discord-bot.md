# Discord bot

The Clash Lens Discord bot complements the website with three slash commands:
`/help`, `/link` (a pointer to the website's Discord alerts page, where Discord
is connected and attack alerts are turned on) and `/stats` (this Season so far
for any tracked player, or the person's default player). Its code is in
[`python/src/bot`](../python/src/bot). People use it mostly in a direct
message (DM) with the bot, and also in servers. `/stats` needs the person's
Discord account connected to their Clash Lens account on the website.
Connecting Discord and choosing the default player happen on the
website, never in the bot. Every reply is a normal message anyone can forward;
only the not-connected pointer and plain errors are private to the person.

## How it runs

- One container, `clashlens-discord-bot`, in the Clash Lens pod, from the same
  Python image as the API. It opens one connection out to Discord (the
  "gateway") and listens on no port, so it adds no public endpoint.
- It reads Clash Lens data with the private API's own read code and the API's
  database login, so numbers in Discord match the website. While it runs it
  also sends the attack alert DMs Clashers turn on; its one write is their
  delivery log, see [architecture](architecture.md).
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
  Without it the bot stops at startup and its log names the missing setting.
- Memory is capped at 384 MiB and CPU at half a core.

## Reset time in replies

Only `/stats` replies, which show Legend data, include the next Reset as a
Discord timestamp, which each viewer sees in their own local time. Errors,
instructions, `/help`, `/link` and the "which player?" prompt have no Reset line
on purpose.

## Go-live checklist

Nothing below has been done yet. Do the steps in order.

1. **Discord application.** In the Discord Developer Portal, open the Clash Lens
   application. It must be the one the website's Discord sign-in uses, since
   connecting Discord on the website is what adds the bot for attack alerts.
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
   It prints `registered 3 commands: help, link, stats`. Commands appear only
   where the app is installed, so nobody else sees them yet.
6. **Test in a DM.** Open the install link from the Installation page, choose
   "Add to My Apps", then DM the bot:
   - `/help` before connecting: says "not connected yet".
   - `/link`: shows the website buttons. Connect Discord on the website, run
     `/link` again: says you are connected and points to the website.
   - With two or more players, choose a default player on the website's
     sign-in connections page, then run `/stats` with no player.
   - `/stats` with a tag and with a name: a normal message you can forward.
7. **Test in a test server.** Install the app to a private test server ("Add to
   Server"). Run `/stats`: everyone in the channel sees it. Have a second,
   unconnected account run `/stats`: only they see the connect pointer.
8. **Launch.** Turn "Public Bot" on when ready, and share the install link.

To turn the bot off, set `CLASHLENS_DISCORD_BOT=off` (or remove the line) and
run `./ops up`; the token file can stay. To stop it at once without `up`:
`systemctl --user stop clashlens-discord-bot.service`.

## Changing the commands

Command names and descriptions are in `replies.COMMANDS`, the linking rule is
`needs_link` in `discord_app.py`, and which replies stay private is
`Reply.private` in `replies.py`. Re-run step 5 after changing names or options.
