## Intent

<!--
1 to 3 sentences: what this PR changes and why, stated as fact.
Plain words throughout the body, no em dashes, no filler.
Write as the owner, about the change. Never repeat, quote or mention the prompt
or the owner's request: no "the owner asked", "as requested", "per @zzzubair".
End this section with the line below, filled in with the real model and the
tool you run in, never a placeholder. Example:
Written by Claude Opus 5.5 in Claude Code on behalf of @zzzubair
-->

Written by <MODEL_NAME> in <HARNESS_NAME> on behalf of @zzzubair

## What changed

<!--
Group by file, or by folder for many small files, one bullet each:
- `path/to/file.py`: the behaviour change in this file
Describe behaviour, not the diff line by line.
Evidence is mandatory and goes here: before/after screenshots of the real
rendered page for any UI change (plus phone width when layout changed), a
before/after table of measured values for any speed, size or accuracy change,
and evidence files, JSON or log excerpts for backend changes. Never invent
numbers. Sanitize first: no secrets, tokens or personal data.
-->

## Risk Assessment

<!--
Pick one level, bold, then a short paragraph on why: blast radius,
data/migration/deploy impact, and how easy it is to roll back.
Low = isolated, easy revert. Medium = shared code paths, config, or
user-visible behaviour. High = migrations, auth, money, data loss, prod
infra, or hard to roll back. Example:
**Low**: one template file, nothing runs it, a revert undoes it.
-->

## Testing

<!--
List only what actually ran, with real results:
- `exact command`: result, e.g. 42 passed
- a manual check and what it showed
If nothing ran, write "None run" and why. Never invent output.
-->
