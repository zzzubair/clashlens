# Clash Lens

Clash Lens tracks Clash of Clans Legend League data so players can see how they and others are really doing.

## About me

I'm Zubair. I own this product. I can follow Python and TypeScript, I can't read Go, and I don't know most engineering jargon. Every line here was written by agents like you. I decide, you build, and you explain it so I can judge it.

## How to talk to me

- Plain English. Define every technical term or abbreviation the first time you use it, in the same sentence—even in findings and proposed requirements.
- Before sending any reply, scan for unexplained jargon, including in status updates and review findings. Replace internal labels with plain descriptions of the behavior or result, such as "checks on this exact version of the code" for "exact-head checks".
- Answer first, detail after. If you need a decision from me, put that in the first line.
- Options: max three, tell me which one you'd pick and why, and what I lose with each. Don't hand me a list and make me choose blind.
- Numbers, not adjectives. "Deletes 4,200 lines", not "simplifies a lot".
- If something breaks, tell me what broke, what you tried, and what you need from me. If the same fix fails twice, don't try it a third time. Change approach or stop and ask. If I've told you to run unattended, don't stop, but write down each attempt and why it failed so I can read the trail when I'm back.
- Don't tell me it works because a command exited 0. Tell me what you ran, what you saw, and what you didn't check.

## Who drives

I do.

- Agreeing with a plan in chat is not me telling you to build it. Wait for "do it" or something like it.
- Once I say go, finish that piece and its checks. Don't stop halfway to ask if you should continue.
- Stay inside what I asked for. If you spot something else that should change, put it in your report. Don't fix it.
- Ask me before you: commit, push, open or merge a PR, deploy, delete data, delete files you didn't create this session, add a dependency, add a new script or tool, or touch anything under `deploy/`.
- Don't add rules to this file. Suggest them to me.

## Product words

Use these exactly.

- **Clasher** or **user**: the player using Clash Lens.
- **Legend day**: 05:00 UTC to 05:00 UTC the next day.
- **Reset**: the Legend day boundary at 05:00 UTC.
- **EOD**: trophy count at the end of a Legend day.
- **Season**: exactly 28 Legend days.
- **Tournament**: a ranked competition period. Weekly in other Ranked Leagues, 28 Legend days in Legend I.

## Technical words

These are what the words in this repo mean. Use the plain version when you talk to me.

- **Collector**: the program that calls the official Clash of Clans API on a loop and saves what comes back.
- **Raw response**: the exact bytes the API returned, saved as proof. Everything else is calculated from these.
- **Spool**: the folder on disk where the collector saves each raw response before anything else happens to it.
- **Archive**: long-term storage for raw responses. Currently Scaleway.
- **Worker**: the Python program that turns raw responses into player, battle and leaderboard records.
- **Job**: one unit of work for the worker. Usually "process this one response".
- **Lease**: a time limit a worker gets on a job. Run over it and another worker can take the job.
- **Migration**: a numbered SQL file under `deploy/migrations` that changes the database shape.
- **Index**: an extra lookup list in the database that helps it find matching records without reading every row.
- **Fixture**: a fake version of an outside service (Clash API, Google login, storage) that runs locally so nothing needs real credentials.
- **Trial**: a timed run of the whole system against fake players to see if it keeps up.
- **Reconciliation**: working out a player's real daily result when their battles and profile snapshots don't line up.
- **Webhook**: a secret Discord URL the alert check posts messages to.
- **Quadlet**: Podman's text-file way of running a container as a system service.
- **Provider-outage pause**: a shared wait for all API keys after the Clash API keeps failing, with occasional test requests until it answers again.
- **Season rule**: a Season's Day 1 start of 5,000, taken from the game's rule that every Legend I player starts a Season there, used when the Reset reading can't give the start itself.
- **Boundary settlement**: whether a Reset's trophy count is proven to include the previous day's automatic defense loss; until then it is provisional.
- **Promotion list**: Legend II and III players saved so the Monday re-check can find the ones promoted into Legend I.

If you introduce a new term, add it here in the same PR. One plain sentence.

## How to build

- Smallest change that does the job. Reuse what's there. No abstractions for things that don't exist yet.
- Delete before you add. Any PR adding more than 300 lines says what it deletes, or why nothing can go.
- No new scripts, harnesses or checkers without asking me. There are already too many.
- A file over 1,500 lines, tests included, is something you report, not somewhere you add more.
- Check line counts before editing source or test files. If a target is over 1,500 lines, report its path and count and propose a way to make the change without growing it.
- Use `gh` for issues, PRs and checks.

## Reporting

End every piece of work with:

1. What changed, one paragraph, plain words.
2. What you ran and what you saw.
3. What you didn't check.
4. Anything out of scope I should know about.
5. What you need me to decide, if anything.

For failed, skipped, blocked or unrun checks, name the behavior left unverified and why. Keep unresolved failures visible even when narrower checks pass; distinguish reading code from running it.

Short. I'll ask if I want more.

Default to 2–4 short sentences or at most 3 short bullets, under 80 words. Cover the five reporting points together and link detailed evidence when needed. Expand when I ask for detail or need it to decide.

## The server

One Fedora box. 8 cores, 16 threads, 16 GiB RAM, 8 GiB swap, roughly 1 TiB NVMe. Swap is not a budget. Before you add anything that grows forever, tell me how fast and how big. If a requirement costs too much to keep, say so instead of keeping it quietly. Don't weaken data integrity or delete kept data without agreeing it with me first.

## What counts as true

I define what the product should do. Code, tests and docs show what got built, not what has to stay. Open GitHub issues record what we agreed. When code, docs and issues disagree, tell me. Don't pick one silently.
