# Official Top-200 Source and Provenance Contract

**Research date:** 2026-08-04
**Status:** One live response was safely characterized through the approved fixed-egress path. The authenticated OpenAPI document was not available, and no raw response was retained.

## Decision

Use the official Clash of Clans API as the only source for an official global player rank. The official developer site states that the API provides global and local leaderboards.[2] The official Swagger UI is the source of record for the API operation and schema.[1]

Do not use a competitor API, a cached leaderboard, a web scrape, or a player profile as a substitute. A profile can support player data. It cannot create official rank evidence.

The one-time characterized operation is:

```text
GET https://api.clashofclans.com/v1/locations/{locationId}/rankings/players
```

The one-time characterized global selector is:

```text
locationId=global
```

On 2026-08-04, a maintainer-approved request used an existing protected collector key through the approved fixed-egress path. The key, proxy address, player tags, and player names were not printed or saved. `limit=200` returned `200 OK`, exactly 200 entries, and official ranks 1 through 200 with no gaps.[5]

## What the official sources prove

- Supercell's official developer site advertises global and local leaderboards.[2]
- Official game support says Legend I tournaments last four weeks and that the top players appear on local and global leaderboards based on Trophy count.[6][7] This defines the game context. It does not define the API response schema.
- The official documentation is a Swagger UI. Its page obtains the schema URL from `game-api-url` and injects a bearer token from `game-api-token`.[1] An anonymous fetch therefore does not expose the operation definitions.
- The live API requires authorization. A protected live request through the approved fixed-egress path characterized the operation, global selector, 200-entry request, and current response shape once.[5]
- The Fan Content Policy requires an unofficiality notice and prohibits an impression that Supercell endorses the fan content.[3]
- Supercell's Terms prohibit disruption or overloading of its service.[4] Collection must remain within the current API terms and key limits.

## Verification matrix

| Contract item | Current result | Implementation rule |
|---|---|---|
| Authority | Official Clash of Clans API and its authenticated Swagger/OpenAPI document. | Store the official documentation URL and the exact schema or operation revision used by the collector. |
| Operation | One live request observed `GET /locations/{locationId}/rankings/players`. | Use only this official operation. Do not silently switch to a third-party route. |
| Global selector | One live request observed `locationId=global`. | Keep the selector explicit. Do not use an empty selector, a country code, or a guessed numeric ID. |
| Top-200 request | One live request observed `limit=200`. The exact documented maximum above 200 and the default remain unknown. | Always send `limit=200`. The contract does not need or assume a larger value. |
| Top-200 retrieval | One response returned exactly 200 entries with ranks 1 through 200 once each. | Treat a short response, rank gap, duplicate rank, or unexpected cursor as partial and require adapter review. Do not pad the result. |
| Season selector and fields | The characterized request sent no season selector. The response contained no season field. | Store `NULL` plus `season_provenance=not_supplied`. A Clash Lens date association is derived context, not official season provenance. |
| Response fields | The characterized response contained top-level `items` and `paging`. Observed entry fields include `tag`, `rank`, `previousRank`, `trophies`, `league`, and `leagueTier`. | Keep the untouched JSON. Require the official player tag and rank. Parse only versioned accepted fields and preserve unknown fields in the raw evidence. |
| Pagination | The 200-entry response contained `paging.cursors` with no cursor fields or values. | Use one request. Do not invent pagination. Treat a future cursor or short result as a contract change until validated. |
| Ordering and ties | Not proved that response order is stable or that ties have a documented rule. | Use the supplied official rank. Do not recompute a rank from trophies, list position, or a Clash Lens tie-breaker. |
| Snapshot atomicity | No official guarantee was found that all entries in one response represent the same server instant. | Treat the result as a collection window, not an atomic game-state snapshot. Record start and end times and disclose this limit. |

## Source and provenance contract

### 1. Request identity

Each leaderboard collection attempt MUST record:

- `source_system = supercell_clash_of_clans_api`;
- the exact HTTPS method and path;
- the normalized query, including the validated global selector and `limit`;
- the returned paging-envelope shape and whether an unexpected cursor was present;
- request start and response completion times in UTC;
- HTTP status and safe response headers;
- a non-secret API-key label, never the key value;
- collector version and official schema/adapter version.

Do not record bearer tokens in PostgreSQL, logs, metrics, traces, or archive metadata.

### 2. Raw evidence

For every successful HTTP response, the Go collector MUST:

1. receive the response bytes;
2. compute the response hash;
3. write or verify the immutable archive object;
4. append the observation metadata;
5. only then enqueue Python processing.

The archive body MUST be byte-for-byte evidence of the response. Do not archive a re-serialized JSON object as the source body. Preserve non-success responses and transport failures as collection evidence, but do not parse them as leaderboard entries.

The existing migration already stores response hash, archive reference, HTTP status, request and response times, collector version, headers, and append-only observations in `collector_observations`. It currently allows only `profile` and `battle_log` in the endpoint checks. A later migration must add a leaderboard endpoint and a durable collection work type. This report does not change that migration.

### 3. Entry identity and rank meaning

An official-rank entry MUST contain the player tag and the rank field required by the validated official schema. Store the rank exactly as returned by Supercell. Store the player tag in normalized form only as a derived lookup value; retain the raw response as evidence.

The target set is official ranks `1` through `200`, inclusive. A player is in the **official Top 200** only when a successful official response supplies a rank in that range. A player outside those ranks, or a player added from another official endpoint, has no official Top-200 rank.

`official_rank` and `clash_lens_rank` are different fields:

- `official_rank` is copied from a validated leaderboard response.
- `clash_lens_rank` is the position in the actively tracked Clash Lens population.

Never fill a missing official rank with the Clash Lens rank. Never describe the Clash Lens position as an official position.

### 4. Completeness and pagination

A collection attempt is `complete` only when all of the following are true:

- the one `limit=200` request returned one successful response;
- that response passed the validated schema check;
- exactly 200 valid unique normalized player tags are present;
- ranks 1 through 200 are present exactly once;
- no normalized player tag appears at more than one rank;
- the response did not require or advertise an unexpected next page;
- the raw response and metadata are durable.

A short response, an unexpected cursor, a duplicate normalized tag, a duplicate rank, a schema failure, or a gap in ranks makes the attempt `partial` or `invalid`, as appropriate. Preserve the received response. Do not fetch or combine extra pages under adapter version 1. Treat new paging behavior as an official contract change that requires adapter review.

The request start and response completion times define an observation window. They do not prove that every entry was generated by the server at one instant. The public surface MUST say “official rank observed at [time/window]” rather than “the exact global rank at reset” unless a future official source proves that stronger claim.

### 5. Season handling

A season value is authoritative only when the validated official request or response supplies it. Store the exact value and its source location (`request`, `response`, or both). If no season value exists, store `NULL` and state that the rank is a current observation with no official season identifier.

Do not derive a season from the 05:00 UTC Legend I day boundary, the local 28-day rule, the current profile, or a timestamp. Do not label a live global ranking as a historical or season-end ranking.

### 6. Confidence and public wording

Use these states:

- `official_observed`: the official response is durable, schema-valid, and supplies the rank;
- `official_partial`: at least one expected page or rank is missing, failed, or conflicted;
- `official_contract_changed`: the response no longer satisfies the accepted adapter contract;
- `not_official_rank`: no qualifying official leaderboard rank was observed.

Only `official_observed` entries may display an official rank. Every entry must expose the observation time, collection attempt, source name, and freshness. A frozen Clash Lens snapshot may retain the official rank provenance, but it must not hide a partial official source attempt.

## Required adapter test before implementation

The 2026-08-04 characterization check completed items 2 through 5 below for the live response. The collector adapter test MUST preserve this safe shape and MUST:

1. use the approved fixed-egress path and a protected key without printing either value;
2. send the characterized method, path, global selector, and `limit=200`;
3. verify the `items` and `paging` envelope, the required `tag` and `rank` fields, the optional accepted fields, the absence or presence of season data, and the cursor shape;
4. require exactly 200 entries, exactly 200 unique valid normalized player tags, and ranks 1 through 200 once each for a complete result;
5. print only sanitized field names, types, counts, status codes, and cursor presence; never print tags, names, tokens, proxy details, or raw bodies;
6. keep the transient response out of the repository unless it enters the normal immutable evidence pipeline.

The authenticated OpenAPI document can still strengthen parameter and schema checks later. The one-time characterization is sufficient for a maintainer-approved provisional adapter contract, but implementation must add sanitized synthetic fixtures and an opt-in protected contract check. Add the leaderboard endpoint to the Go collector contract and migration stream. Python may process the durable observation, but it must not fabricate rank evidence.

## Clash Lens fit

This contract matches the confirmed rules in [domain.md](../domain.md), [architecture.md](../architecture.md), and issue [#29](https://github.com/zzzubair/ClashLens/issues/29): preserve official ordering in the separate official view, keep official rank provenance separate from Tracked Players position, keep frozen and live views separate, and show measured coverage. The current collector migration ([0001_collector.sql](../../deploy/migrations/0001_collector.sql)) does not yet support leaderboard observations.

Clash Lens must show the Supercell unofficiality notice on applicable public surfaces. It must not imply Supercell endorsement.[3]

## Sources

[1] https://developer.clashofclans.com/api-docs/index.html — Official Clash of Clans API Swagger UI
[2] https://developer.clashofclans.com — Official Clash of Clans API developer site
[3] https://supercell.com/en/fan-content-policy — Supercell Fan Content Policy
[4] https://supercell.com/en/terms-of-service — Supercell Terms of Service
[5] https://api.clashofclans.com/v1/locations/global/rankings/players?limit=200 — Official Clash of Clans global player-ranking request, safely characterized on 2026-08-04
[6] https://support.supercell.com/clash-of-clans/en/articles/legend-league-4.html — Official Legend League support
[7] https://support.supercell.com/clash-of-clans/en/articles/legend-league-matchmaking-3.html — Official Legend I tournament and leaderboard support
