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
| | Next Reset rank range | New read | A new calculation. Range only: a lab test was right 99.99% of the time and took about 0.1 s for 13,300 players. Trophies move ±40 per unused attack or defense. A narrow single-number estimate was right at most 51% of the time, so it is not shown. |
| | Automatic defense loss preview | New read | The same formula the worker uses at Reset ([domain.md](domain.md)). Shows nothing if either day's defenses are unknown. |
| Legend clock | Today's battles with times | Exists | |
| Bases you attacked | Each opponent's trophies at Reset and their defenses today (held or tripled, trophies lost, which one was yours) | New read | Combines up to 8 opponents' daily logs. |
| | Legends average | None | Fixed at 52% held and −32 a defense (see below). |
| | Add to group | Exists | Groups hold up to 20 players. |
| Goal | Trophies at rank N on the last Reset board and 7 days earlier; your 7-day pace | Exists | Saved daily boards. The shield row needs nothing new. |
| Armies on defense | Your defenses grouped by the attacker's army type over the last 1–28 days | New read | Army types exist since #458. |
| | Legends held % and trophies lost per army type | New read | Daily totals per army type across Legends (defenses, held, trophies lost): about 40 types, so a few rows a day. |
| Rivals watch | Live board around you; each shown player's today and Season summary | Exists | `/v1/leaderboards/live?focus_tag=`, then `/v1/players/{tag}/today` and `/seasons` for 7 players: 7 reads per refresh. Bundle them later only if it proves slow. |
| Season heat map | Each finished day's net, offense and defense totals, attack and defense counts, trophies at the end | Exists | Same daily results as Season graph. |
| Cutoffs | Trophies at each Top line, live and on the last Reset board | Exists | |
| Season graph | Trophies and rank at each Reset this Season; best rank; last Season's official finish | Exists | Use the official finish, never a tracked-only rank. |
| Recent form | Each finished day's net, offense and defense, triples and held defenses | Exists | |
| Offense consistency | Your attacks (stars, trophies) over the last 3 or 7 days | Exists | |
| Group watch | Group comparison over 7 days; each member's Season summary | Exists | `/v1/account/groups/{id}/comparison?days=7`; Whole Season reuses `/v1/players/{tag}/seasons`. |
| Armies on offense | Your daily attack counts per army type; Legends hit rate per type | Mostly exists | Counts saved since #458; the Legends hit rate is what the Armies page already adds up. Average gain per attack needs a read of your battles. |

## Shared

| Need | Status | Notes |
|---|---|---|
| Per-user settings: default or own layout and card order, first-visit choice, Goal rank, Cutoffs lines, Group watch group and window, Bases mode | New data | Fits the account preferences box (capped at 4,096 bytes); under 1 KB per account. |
| The shared default layout list | New data | One list for everyone; users on the default follow it. |
| "updated N min ago" on live cards | New read | Mostly display. |

## Real numbers behind the design

Measured read-only on production on 2026-10-12, Legend I only.

- **Defenses, 29 Sep–11 Oct:** 1,161,037 defenses; 51.92% held (not tripled), 2.383 stars, −32.13 trophies a defense. Daily values ranged 50.95–52.62% held and −31.94 to −32.31 a defense, which is why the dashboard fixes 52% and −32.
- **Star split, 10 Oct (88,932 defenses):** 3★ 48.2% cost 40; 2★ 43.0% cost 27.6 on average; 1★ 8.5% cost 13.5; 0★ 0.3% cost 0. A full 8-defense day has a median loss of 262.
- **Army types, 5–10 Oct:** 66% of players used one army type, 22% two, 11% three or more; a player's main type is 92% of their attacks on average. Among named types with 10,000+ attacks, Legends hit rates ran from 45.8% (Super Yeti) to 54.4% (Dragon Rider); Thrower, the most used, was 50.3%.
- **Repeat attackers, 5–11 Oct:** of 617,255 attacker-defender pairs, 99.7% happened once.
