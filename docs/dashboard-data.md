# Player dashboard data needs

What each card in the [dashboard design](dashboard.md) needs, checked on **2026-10-12**.

- **Exists:** Clash Lens already stores it and the website can read it.
- **New read:** the data is stored, but needs a new query, calculation or API field.
- **New data:** not stored today; needs new saved rows (small, per user).
- **New collection:** needs something not collected from the Clash API today. No settled card needs this.

## By card

| Card | Needs | Status | Notes |
|---|---|---|---|
| Legend day | Live trophies and rank, net today, rank at the last Reset, today's attacks and defenses (stars, trophies) | Exists | |
| | Next Reset rank range | Exists | `/v1/players/{tag}/today` returns it. A lab test was right 99.99% of the time and took about 0.1 s for 13,300 players. Trophies move ±40 per unused attack or defense. The projected place inside it (`projected_rank`) is the middle of 200 imagined finishes of the day, copied from the last 7 days' saved examples (`dashboard_remaining_changes`, see `projected_place.py`) and ranked by the Daily board's order for equal trophies: the latest battle that moved a player's trophies ended at the attacker's report time, from the attacker's own log even once they have left the board, else 155 seconds after the defender's. The worker saves a day's examples once its Daily board publishes, every due day in one run, so its first run after migration 0107 saves all of the last 7 days within minutes; the card hides the projected place only while no day's examples are saved at all, and for the first seconds of a Legend day, after the data service restarts or after a player first appears on the board, when it shows the range and asks again up to 3 times, each request waiting up to 4 s for the projected place being worked out. It is worked out after each once-a-minute board read, in a separate process so no request waits for it (about 1 s of database reads and 3.3 s of calculation for 13,300 players, 6 s after a restart), so it can be a minute or so older than the range; one 10 minutes old is not shown. A battle counts in the examples from a minute after it was first saved; a report corrected later counts with its corrected trophies from that first save, a small known gap. On the held-back Sunday 11 October it typically missed by about 570 places at 06:00 UTC, 290 at 17:00, 100 at 23:00 and 50 at 01:00; about 25-35 for players who had finished their battles and 250-300 for those with one battle left at 23:00. |
| | Automatic defense loss preview | Exists | `/v1/players/{tag}/today` returns it. The same formula the worker uses at Reset ([domain.md](domain.md)). Shows nothing if either day's defenses are unknown. |
| Legend clock | Today's battles with times | Exists | |
| Bases you attacked | Each opponent's trophies at Reset and their defenses today (held or tripled, trophies lost, which one was yours) | Exists | `/v1/players/{tag}/today` returns up to 8 opponents with their trophies at Reset and each defense today (stars, trophies lost, and which one was yours). |
| | Legends held % | Exists | `/v1/players/{tag}/today` returns it on each opponent as `legends_held` (`held`, `defenses`, and `lost`, the trophies those defenses lost), worked out live from today's defenses on the live leaderboard (re-read at most once a minute) by players whose day started within 20 trophies of the opponent's Reset trophies, widened evenly until that holds 200 defenses but never past 300 trophies either side; with fewer than 200 there, those if at least 50. Null with fewer than 50, when the opponent's Reset trophies are unknown, or while that board is not read yet today or is over 10 minutes old. The top-level `legends_held` is every player's, kept only for websites released before the per-opponent one; it is never used for a base. About 52% across all Legends on a typical day (see below); lower at higher trophies; not fixed. |
| | Earlier Legend days | Exists | `/v1/players/{tag}/bases?day=N` returns the same opponents for Day N of this Season, any day before today. Each `legends_held` comes from every player's latest saved log of that day by Reset trophies, as a finished day has no Live Leaderboard: about 2.8 to 4.1 s for 11,700 players on 13 and 14 October 2026, as it reads the whole table, so the API keeps each day's counts in memory for an hour, at most 28 days, and reads one day at a time, so these reads hold one of its 8 database connections between them. A request that waits over 8 s for its turn gets 503 `busy`, unless its day was read meanwhile. The website reads a day only once clicking stops for 0.4 s, and waits up to 15 s for it. `opponents` is null when the player has no log that day. |
| | Legends trophies lost a defense | Exists | `lost` in each opponent's `legends_held`, over the same players and defenses as the held %. About −32 across all Legends (see below), higher at higher trophies. |
| | Add to watchlist | Exists | Watchlists hold up to 20 players. |
| Goal | Trophies at rank N on the last Reset board and 7 days earlier; your 7-day pace | Exists | Saved daily boards. The shield row needs nothing new. |
| Armies on defense | Your defenses grouped by the attacker's army type over the last 1–28 days | Exists | `/v1/players/{tag}/armies?lens=defense` returns it. Army types exist since #458. Both rows can only use days still kept: a finished Season's daily logs and army battle records are deleted 7 days after it ends, so early in a new Season a long Custom window covers fewer days. |
| | Legends held % and trophies lost per army type | Exists | The same read returns every tracked player's defenses per type and in all, from the worker's saved daily defense counts per defender and army (`army_package_defense_day_counts`, the defense counterpart of the attack counts: about 88,000 rows and 10.6 MB a day, 300 MB a Season, kept across Seasons), made from defenders' own battle reports. A defense whose attacking army could not be read counts only in the overall average. The worker counts the current Season and the last 28 days at most one day a minute, so after a deploy the card has full numbers within about 30 minutes. |
| Rivals watch | Live board around you; each shown player's today and Season summary | Exists | `/v1/leaderboards/live?focus_tag=`, then `/v1/players/{tag}` for the 7 players: today's battles (hit rate, offense, defense), their rank at the last Reset (for ▲▼) and this Season's finished days (for Season averages). 8 reads per refresh. Bundle them later only if it proves slow. |
| | Season: the board around you at the last Reset | Exists | `/v1/leaderboards/frozen` for that day has no focus option, so read the 1–2 pages around your rank at the last Reset, plus the same 7 player reads. |
| Season heat map | Each finished day's net, offense and defense totals, attack and defense counts, trophies at the end | Exists | Same daily results as Season graph. |
| Cutoffs | Trophies at each Top line, live and on the last Reset board | Exists | |
| Season graph | Trophies and rank at each Reset this Season; best rank; last Season's official finish | Exists | Use the official finish, never a tracked-only rank. |
| Recent form | Each finished day's net, offense and defense, triples and held defenses | Exists | |
| Offense consistency | Your attacks (stars, trophies) over the last 3 or 7 days or this Season's finished days | Exists | |
| Watchlist | Your watchlists in the order you made them | Exists | `/v1/account/groups` returns each one's `created_at`; the card shows the oldest first. |
| | Watchlist comparison over the last 7 days | Exists | `/v1/account/groups/{id}/comparison?days=7`. |
| | Each member's whole-Season numbers | Exists | The comparison only offers 3, 7 or 14 days, so Whole Season reads `/v1/players/{tag}` (this Season's finished days) for each member and you: up to 21 reads. |
| Armies on offense | Your daily attack counts per army type; Legends hit rate per type | Exists | `/v1/players/{tag}/armies?lens=offense` returns your attacks per type over `days=3`, `days=7` or `days=season` (this Season's finished days) (counts saved since #458) and every tracked player's per type, from the saved daily counts per army (`army_package_day_counts`) the Armies page also adds up. |
| Notebook | The account's base today, its run so far (first day, held of defenses) and a base picked from tomorrow; also the Armies on defense base line | Exists | `/v1/account/notebook`, read once per dashboard load for both cards and only while the Notebook is on. |

## Shared

| Need | Status | Notes |
|---|---|---|
| Per-user settings: default or own layout and card order, first-visit choice, Goal rank, Cutoffs lines, the Watchlist card's watchlist and window, Bases mode | Exists | Saved under `dashboard` in the account preferences box (capped at 4,096 bytes); under 1 KB per account. The default layout itself is a list in the website code; users on the default follow it at each release. |
| "updated N min ago" on live cards | Exists | Taken from the saved time of each reading the card shows. |

## Open owner decision

- **Custom defense window across Seasons:** a full 28-day window early in a new Season would need saved per-player defense totals or longer retention of daily logs and army battle records. Until then it covers only the days still kept.

## Real numbers behind the design

Measured read-only on production on 2026-10-12, Legend I only.

- **Defenses, 29 Sep–11 Oct:** 1,161,037 defenses; 51.92% held (not tripled), 2.383 stars, −32.13 trophies a defense. Daily values ranged 50.95–52.62% held and −31.94 to −32.31 a defense, which is why the dashboard first fixed −32 a defense; it now compares with Legends at similar trophies, as losses rise with trophies (13 Oct: about −31 near 4,900, −36 at 5,300+). The held rate is worked out live from today's defenses, so 52% is only a typical value.
- **Star split, 10 Oct (88,932 defenses):** 3★ 48.2% cost 40; 2★ 43.0% cost 27.6 on average; 1★ 8.5% cost 13.5; 0★ 0.3% cost 0. A full 8-defense day has a median loss of 262.
- **Army types, 5–10 Oct:** 66% of players used one army type, 22% two, 11% three or more; a player's main type is 92% of their attacks on average. Among named types with 10,000+ attacks, Legends hit rates ran from 45.8% (Super Yeti) to 54.4% (Dragon Rider); Thrower, the most used, was 50.3%.
- **Repeat attackers, 5–11 Oct:** of 617,255 attacker-defender pairs, 99.7% happened once.
