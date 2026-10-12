# Player dashboard data needs

What each card in the [dashboard design](dashboard.md) needs, checked on **2026-10-12**. The Discord bot commands will reuse this list.

- **Exists:** Clash Lens already stores it and the website can read it.
- **New read:** the data is stored, but needs a new query, calculation or API field.
- **New data:** not stored today; needs new saved rows (small, per user).
- **New collection:** needs something not collected from the Clash API today. No settled card needs this.

## By card

| Card | Needs | Status | Notes |
|---|---|---|---|
| Legend day | Live trophies and rank, net today, rank at the last Reset, today's attacks and defenses (stars, trophies) | Exists | |
| | Next Reset rank range | Exists | `/v1/players/{tag}/today` returns it. Range only: a lab test was right 99.99% of the time and took about 0.1 s for 13,300 players. Trophies move ±40 per unused attack or defense. A narrow single-number estimate was right at most 51% of the time, so it is not shown. |
| | Automatic defense loss preview | Exists | `/v1/players/{tag}/today` returns it. The same formula the worker uses at Reset ([domain.md](domain.md)). Shows nothing if either day's defenses are unknown. |
| Legend clock | Today's battles with times | Exists | |
| Bases you attacked | Each opponent's trophies at Reset and their defenses today (held or tripled, trophies lost, which one was yours) | Exists | `/v1/players/{tag}/today` returns up to 8 opponents with their trophies at Reset and each defense today (stars, trophies lost, and which one was yours). |
| | Legends held % | Exists | `/v1/players/{tag}/today` returns it as `legends_held`, worked out live from today's defenses across the live leaderboard (re-read at most once a minute). About 52% on a typical day (see below); not fixed. |
| | Legends trophies lost a defense | None | Fixed at −32 a defense (see below). |
| | Add to group | Exists | Groups hold up to 20 players. |
| Goal | Trophies at rank N on the last Reset board and 7 days earlier; your 7-day pace | Exists | Saved daily boards. The shield row needs nothing new. |
| Armies on defense | Your defenses grouped by the attacker's army type over the last 1–28 days | Exists | `/v1/players/{tag}/armies?lens=defense` returns it. Army types exist since #458. Both rows can only use days still kept: a finished Season's daily logs and army battle records are deleted 7 days after it ends, so early in a new Season a long Custom window covers fewer days. |
| | Legends held % and trophies lost per army type | Exists | The same read adds them up from the saved daily counts per army, which keep the trophies won and lost since migration 0104. |
| Rivals watch | Live board around you; each shown player's today and Season summary | Exists | `/v1/leaderboards/live?focus_tag=`, then `/v1/players/{tag}` for the 7 players: today's battles (hit rate, offense, defense), their rank at the last Reset (for ▲▼) and this Season's finished days (for Season averages). 8 reads per refresh. Bundle them later only if it proves slow. |
| | Season: the board around you at the last Reset | Exists | `/v1/leaderboards/frozen` for that day has no focus option, so read the 1–2 pages around your rank at the last Reset, plus the same 7 player reads. |
| Season heat map | Each finished day's net, offense and defense totals, attack and defense counts, trophies at the end | Exists | Same daily results as Season graph. |
| Cutoffs | Trophies at each Top line, live and on the last Reset board | Exists | |
| Season graph | Trophies and rank at each Reset this Season; best rank; last Season's official finish | Exists | Use the official finish, never a tracked-only rank. |
| Recent form | Each finished day's net, offense and defense, triples and held defenses | Exists | |
| Offense consistency | Your attacks (stars, trophies) over the last 3 or 7 days | Exists | |
| Group watch | Your groups in the order you made them | Exists | `/v1/account/groups` returns each group's `created_at`; the card shows the oldest first. |
| | Group comparison over the last 7 days | Exists | `/v1/account/groups/{id}/comparison?days=7`. |
| | Each member's whole-Season numbers | Exists | The comparison only offers 3, 7 or 14 days, so Whole Season reads `/v1/players/{tag}` (this Season's finished days) for each member and you: up to 21 reads. |
| Armies on offense | Your daily attack counts per army type; Legends hit rate per type | Exists | `/v1/players/{tag}/armies?lens=offense` returns your attacks per type (counts saved since #458) and every tracked player's per type, from the saved daily counts per army (`army_package_day_counts`) the Armies page also adds up. |

## Shared

| Need | Status | Notes |
|---|---|---|
| Per-user settings: default or own layout and card order, first-visit choice, Goal rank, Cutoffs lines, Group watch group and window, Bases mode | Exists | Saved under `dashboard` in the account preferences box (capped at 4,096 bytes); under 1 KB per account. The default layout itself is a list in the website code; users on the default follow it at each release. |
| "updated N min ago" on live cards | Exists | Taken from the saved time of each reading the card shows. |

## Open owner decision

- **Custom defense window across Seasons:** a full 28-day window early in a new Season would need saved per-player defense totals or longer retention of daily logs and army battle records. Until then it covers only the days still kept.

## Real numbers behind the design

Measured read-only on production on 2026-10-12, Legend I only.

- **Defenses, 29 Sep–11 Oct:** 1,161,037 defenses; 51.92% held (not tripled), 2.383 stars, −32.13 trophies a defense. Daily values ranged 50.95–52.62% held and −31.94 to −32.31 a defense, which is why the dashboard fixes −32 a defense. The held rate is worked out live from today's defenses, so 52% is only a typical value.
- **Star split, 10 Oct (88,932 defenses):** 3★ 48.2% cost 40; 2★ 43.0% cost 27.6 on average; 1★ 8.5% cost 13.5; 0★ 0.3% cost 0. A full 8-defense day has a median loss of 262.
- **Army types, 5–10 Oct:** 66% of players used one army type, 22% two, 11% three or more; a player's main type is 92% of their attacks on average. Among named types with 10,000+ attacks, Legends hit rates ran from 45.8% (Super Yeti) to 54.4% (Dragon Rider); Thrower, the most used, was 50.3%.
- **Repeat attackers, 5–11 Oct:** of 617,255 attacker-defender pairs, 99.7% happened once.
