# Discord bot

The Clash Lens Discord bot complements the website with four slash commands:
`/help`, `/link` (the person's linked accounts and their default account),
`/alerts` (each linked account's attack alert and Legend day summary switch)
and `/stats` (today and this Season so far for any tracked player, or the
person's default account). Its code is in
[`python/src/bot`](../python/src/bot). People use it mostly in a direct
message (DM) with the bot, and also in servers. `/stats` with a player tag or
name works for anyone. `/stats` with no player shows the person's default
account when their Discord account is connected to Clash Lens and has a linked
account; otherwise it asks for a player tag or name, never offering sign-up or
linking. `/link` and `/alerts` need the person's Discord account connected to
their Clash Lens account on the website, and otherwise offer "Sign up with
Discord" (or, for an existing account, "Connect Discord"). Signing up,
connecting Discord, linking accounts, choosing the default account and
turning alerts on or off happen on the website, never
in the bot. The default account is the one marked Default on
the Clasher's Settings page (`/account/settings`); until they choose, it is
their first linked account Clash Lens tracks. With none tracked there is no
default, and `/stats` asks which account unless they have only one. Every
reply is a normal message anyone can forward; only the not-connected pointer
and plain errors are private to the person.

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

## How messages look

Every message but an attack alert is laid out in Discord's cards ("components
v2"): text in one or more blocks with a coloured edge, with link buttons. A
message in that layout cannot carry the line a phone's notification shows, so
the notification would be blank. An attack alert therefore uses Discord's
older layout: one box with nothing above it, so the notification shows the
box's top. That is the defending player and the trophies lost as the box's
bold title ("Wanderer  −24"), then the attacker, stars and destruction
("Drift  ★★☆  87%"). Under "Attacker stats", which the notification leaves
out, come the attacker's trophies at the Reset (🏆), live rank (🌍) and hit
rate in the battle's Season (🎯), each left out when unknown, or "Not tracked
by Clash Lens yet", then their clan and Open battle and Alert settings as
small links. Each name in it is wrapped in Unicode isolation marks, so a name
written right to left cannot move the numbers beside it.

The box ends with a picture of the defending player's Legend day so far: its
Season day, trophies and rank from the Reset to now, then each attack and
defense with their totals, the new defense marked. A message with a picture
shows only "Sent an image" in the notification, so the bot sends the box
first, then adds the picture by editing the message, which sends no second
notification, once Clash Lens has saved the battle into the player's day,
usually seconds later. After 5 minutes it stops waiting, and the alert stays
without a picture. `/stats` shows the same picture of today's Legend day. The
picture uses the website's fonts and colours, light unless the Clasher turns
on "Dark picture" on the Discord alerts page: one switch for the account, a
row in `discord_alert_dark_picture` (migration 0136), which `/stats` follows
for the person who ran it, light for anyone without an account. It is drawn
in memory and sent to Discord with the message, and kept nowhere else. The
bot draws one picture at a time: each takes about 50 MB while it is drawn,
and the bot may use 384 MiB. The fonts the website does not use, for names in
other scripts and emoji, are Noto's, in the container image.

Messages about one of the
Clasher's own linked accounts (attack alerts, the alerts-on message, that
account's card in a Legend day summary, and `/stats` for it) use that
account's colour, set by the order the accounts were linked, so an account
keeps its colour everywhere. Everything else uses Clash Lens yellow, except
the grey card in a Legend day summary naming the players whose day is not
finished yet. `/stats`
ends with when the player's profile was last read ("Updated N min ago"); no
other message has that line. `/stats` shows today's trophies gained and lost
with how many attacks and defenses, then the picture of today's Legend day,
then a "View day" link that opens the player page at that day, then the
Season; with today not saved yet it says so and has no picture, and with a
battle that may be missing it points to the day on Clash Lens. A `/stats`
reply that
shows a status word
instead of numbers also shows the next Reset as a Discord timestamp, which
each viewer sees in their own local time.

## Legend day summary

Once a minute the bot checks whether the Daily board for the latest Reset is
published. Once it is, each Clash Lens account with Discord connected and a
player's summary turned on gets one DM summing up the Legend day that Reset
ended. Its first line, which a phone's notification shows, is the Season day
and date, such as "📅 Day 22 (26-10-2026)". Then each verified player whose
summary is on gets a box in their colour, headed by their name and the day's
label when it isn't Verified (Calculated, Uncertain): their place on that
Daily board and end-of-day trophies in large text; the places moved since
the Daily board of the Reset before (not on a Season's Day 1) and the trophy
change; then the day's attack and defense trophies, with the count when it
isn't 8, and a link to the day on the player page. The numbers come from the
player page's own read, so they match it. The end of day is the one the worker
saved with that version of the day, as the Season page shows it, before any
weekly or Season reset at that Reset, which is shown beside the change; it
shows as a dash when the worker saved none. Every place in one round of
summaries comes from the same version of that Daily board. A player whose day
is not saved yet, or whom that board went without, is named as not finished; a
player with no Legend day that day is left out, and an account with no
finished day gets no DM. A summary too long for one message goes as several,
in order, each sent as soon as the one before is delivered.

- Each player's summary is off until turned on on the website's Discord
  alerts page, separately from its attack alerts. Each player turned on is
  one row in `discord_day_summary_on` (migration 0112), deleted when the
  player is unverified or moved to another account, so it is off again if it
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
   It prints `registered 4 commands: alerts, help, link, stats`. Commands appear only
   where the app is installed, so nobody else sees them yet.
6. **Test in a DM.** Open the install link from the Installation page, choose
   "Add to My Apps", then DM the bot:
   - `/help` before connecting: says the account isn't connected yet.
   - `/link` and `/alerts`: offer "Sign up with Discord". Connect Discord on
     the website, run them again: they list your linked accounts and their
     alert switches, with a button to change them on the website.
   - With two or more players, press Make default on one of them on the
     website's Settings page, then run `/stats` with no player.
   - `/stats` with a tag and with a name: a normal message you can forward.
7. **Test in a test server.** Install the app to a private test server ("Add to
   Server"). Run `/stats`: everyone in the channel sees it. Have a second,
   unconnected account run `/link`: only they see the sign-up message.
8. **Test attack alerts.** Before announcing alerts, turn attack alerts on for
   a test player on the website's Discord alerts page. A DM saying "Attack
   alerts are on" should arrive; then, after that player's next real
   defense, the attack alert DM should arrive too.
9. **Test the Legend day summary.** With a test player verified, Discord
   connected and the player's summary turned on on the website's Discord
   alerts page, a DM titled "Legend day summary" should arrive
   shortly after the next Reset's Daily board is published.
10. **Launch.** Turn "Public Bot" on when ready, and share the install link.

To turn the bot off, set `CLASHLENS_DISCORD_BOT=off` (or remove the line) and
run `./ops up`; the token file can stay. To stop it at once without `up`:
`systemctl --user stop clashlens-discord-bot.service`.

## Changing the commands

Command names and descriptions are in `replies.COMMANDS`, what each command
answers someone not connected is in `commands.py`, and which replies stay
private is `Reply.private` in `replies.py`. Re-run step 5 after changing names or options.
