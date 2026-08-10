# Clash Lens
Clash Lens makes competitive Clash of Clans ranked data accessible for all. It brings official observations together into trustworthy tracking and analysis so players can make evidence-led decisions.

## Glossary
We need to be on the same page with terminology. When communicating use this language:
- **you** means the agent reading this file and changing Clash Lens.
- **me**, **I**, and **maintainers** mean Zubair and Surbhi building Clash Lens. These are who you are talking to now.
- **user**, **clasher** means the person that will use Clash Lens to view data and analyse.
- **Legend day** means a complete Legend day that starts at 05:00 UTC and ends at 05:00 UTC the next day.
- **season** means a full legend season that lasts exactly 28 days.
- **tournament** means a ranked competition period. Tournaments are weekly in all Ranked Leagues except Legend I, where one tournament runs for 4 weeks and consists of 28 Legend days.
- **reset** means the time when a Legend day ends, i.e. 05:00 UTC.
- **EOD**, **end of day** means the trophy count at the end of the Legend day.

## Working rules
- Keep it simple, stupid.
- Really channel the "measure twice, cut once" and "yagni" aggressively.
- Preserve maintainer changes and make the smallest complete change.
- Add or update only the tests needed for changed behavior. Smallest proof that the change works.
- Fight scope creep, try to honor the maintainer's intent in the most simple and realistic way.
- Use the authenticated `gh` CLI for this private repository.
- Do not commit, push, rebase, or open a pull request unless the maintainer asks.
- Report what changed, what was verified, what was not verified, and what remains open.
- The rest of this file is to help you navigate the project, but these are not "hard rules", think of them as "good defaults". The maintainers should be able to override anything written here.  

## Roles
- The maintainer-facing agent uses `gpt-5.6-sol` and owns coordination, decisions, final review, and the final response. It babysits the subagents.
- Delegate heavy implementation to `gpt-5.6-luna` or `deepseek-v4-flash` through subagents/threads.
- Direct maintainer instructions override this split.

## Sources
- Codebase is the source of truth, but if still uncertain read only the sources relevant to the task, the docs are previously accepted decisions and useful context but can be stale.
- [`docs/product.md`](docs/product.md) for product scope and behavior.
- [`docs/domain.md`](docs/domain.md) for domain rules.
- [`docs/architecture.md`](docs/architecture.md) for runtime boundaries and open technology choices.
- [`docs/deployment.md`](docs/deployment.md), [`docs/collector-prototype.md`](docs/collector-prototype.md), or [`python/README.md`](python/README.md) for their respective implementation areas.
- If an important uncertainty remains after inspecting the code and relevant documentation, ask the maintainer.
