# Clash Lens

Clash Lens makes competitive Clash of Clans ranked data accessible through trustworthy tracking and analysis so clashers can make evidence-led decisions.

## Domain language

- **Clasher** or **user** means the player using Clash Lens.
- **Legend day** runs from 05:00 UTC to 05:00 UTC the next day.
- **Reset** is the Legend day boundary at 05:00 UTC.
- **EOD** means the trophy count at the end of a Legend day.
- **Season** means a full Legend season of exactly 28 days.
- **Tournament** means a ranked competition period: weekly in other Ranked Leagues, and 28 Legend days in Legend I.

## Working agreement

- Make the smallest complete change that meets the maintainer's goal. Reuse suitable existing code and avoid unrelated cleanup or speculative abstractions.
- Preserve maintainer changes. Use subagents only when useful and consistent with the maintainer's instructions; delegation is not required.
- Discussion and agreement on a proposal do not authorize implementation. Once implementation is authorized, finish the agreed work and relevant checks without asking for routine confirmation.
- Ask before publication, deployment, destructive changes, or material scope changes unless explicitly authorized. Do not commit, push, rebase, or open a pull request unless asked.
- Test behavior rather than incidental implementation details. Keep validation proportional to risk and reuse suitable existing test files. Replace or remove tests when their requirements are deliberately superseded, not merely because they fail.
- Use the authenticated GitHub CLI for repository issues, pull requests, and checks.
- Report concisely what changed, what was verified, what was not verified, and what remains open.

## Hosting constraints

Clash Lens must run comfortably on the Fedora host. Treat its resources and hosting cost as design constraints:

- Fedora Linux, x86_64; 8 CPU cores and 16 threads.
- 16 GiB RAM and 8 GiB swap. Swap is not a normal operating budget.
- Nominal 1 TiB NVMe SSD; measure actual usable space and leave operating headroom.

Estimate storage growth and resource use before introducing permanently growing data. Question requirements that threaten affordability rather than preserving them blindly. Do not weaken data integrity or delete retained data without an agreed policy.

## Intent and evidence

Maintainers define desired behavior and authorize changes. Code, migrations, fixtures, and tests show implemented behavior; they do not make that behavior an obligation to preserve.

Live GitHub issues record agreed scope and status. Documentation provides durable guidance but may be stale. Inspect sources relevant to the task, flag conflicts, and resolve consequential uncertainty with the maintainer rather than silently treating existing code or prose as the desired outcome.
