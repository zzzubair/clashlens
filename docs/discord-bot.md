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
  also sends the attack alert DMs Clashers turn on, and the Legend day
  summary; its only writes are their delivery logs, see
  [architecture](architecture.md).
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

## Legend day summary

Once a minute the bot checks whether the Daily board for the latest Reset is
published. Once it is, each Clash Lens account with Discord connected gets
one DM summing up the Legend day that Reset ended, for each verified player
whose summary is on: starting and end-of-day trophies, the trophy change,
attacks and defenses with their trophies, the day's label (Verified,
Calculated, Uncertain) and its place on that Daily board, with a link to the
day on the player page. The numbers come from the player page's own read, so
they match it. The end of day is the one the worker saved with that version
of the day, as the Season page shows it, before any weekly or Season reset at
that Reset, which is shown beside the change; it shows as Unavailable when the
worker saved none. Every place in one round of summaries comes from the same
version of that Daily board. A player whose day is not saved yet, or whom that
board went without, is named as not finished; a player with no Legend day that
day is left out, and an account with no finished day gets no DM. A summary too
long for one message goes as several, in order, each sent as soon as the one
before is delivered.

- Each player's summary is on until turned off on the website's Discord
  alerts page, separately from its attack alerts. Each player turned off is
  one row in `discord_day_summary_off` (migration 0111), deleted when the
  player is unverified or moved to another account, so it is on again if it
  comes back.
- Nothing is read for a Reset until its board is published, so a late board
  makes the summaries late and they never compete with the Reset's own work.
  A day's summaries go out only until the next Reset, at most 10 accounts a
  minute: none is logged, and no DM tried, from the next Reset on.
- Summaries have their own loop beside the attack alert loop, sending through
  the same Discord connection, and their own database connection, so a slow
  summary never holds up an attack alert.
- Each summary is logged before its first DM and never sent twice, across
  restarts too. A DM Discord did not take is tried again as an attack alert
  is, within 2 minutes of that DM's own first try. Before each DM after the
  first, and each try again, the bot checks that every player that DM names
  is still verified to the account with its summary on; if one is not, that
  DM and the ones after it are not sent.

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
8. **Test attack alerts.** Before announcing alerts, turn attack alerts on for
   a test player on the website's Discord alerts page. A DM saying "Attack
   alerts are on for …" should arrive; then, after that player's next real
   defense, the attack alert DM should arrive too.
9. **Test the Legend day summary.** With a test player verified and
   Discord connected, a DM titled "Legend day summary" should arrive
   shortly after the next Reset's Daily board is published.
10. **Launch.** Turn "Public Bot" on when ready, and share the install link.

To turn the bot off, set `CLASHLENS_DISCORD_BOT=off` (or remove the line) and
run `./ops up`; the token file can stay. To stop it at once without `up`:
`systemctl --user stop clashlens-discord-bot.service`.

## Changing the commands

Command names and descriptions are in `replies.COMMANDS`, the linking rule is
`needs_link` in `discord_app.py`, and which replies stay private is
`Reply.private` in `replies.py`. Re-run step 5 after changing names or options.
