# ClashKing Reference and Reuse Assessment

- **Research date:** 2026-07-30
- **Status:** Research recommendation; it does not change a Clash Lens product, domain, or architecture rule.
- **Scope:** Public source and metadata from ClashKingInc, official Supercell policy and API documentation pages, and the confirmed Clash Lens documents. No ClashKing service, collected data, player tag, or raw player response was used.

## Executive summary

Clash Lens should **not use ClashKing as a data source**. `docs/product.md` permits discovery only from official Clash of Clans API sources and user-submitted tags; it also prohibits scraping competitor services. That excludes ClashKing's public API, hosted proxy, its existing tracked populations, and any fixtures or assets whose provenance is not independently established.

The core `ClashKingTracking` and `ClashKingAPI` repositories are GPL-3.0. They are useful evidence of implementation choices and failure modes, but must remain **reference-only** unless a maintainer deliberately elects to license the relevant combined work under GPL-3.0 and meets its distribution conditions. That is not proposed here. In particular, do not copy, translate, or adapt their routines into the current codebase.

There are three narrowly useful, permissively licensed candidates:

1. MIT-licensed [`clashy.go`](https://github.com/ClashKingInc/clashy.go/tree/9ea0166b5e043feb1c52ce791a7faa2557373aea) can be evaluated as a pinned Go API-client dependency or as a source for small, attributed transport/model adaptations. Its in-memory cache, concurrency limiter, and typed model path do **not** implement Clash Lens's per-key rate ceiling, append-only evidence archive, durable queues, or observation provenance.
2. MIT-licensed [`clashy.py`](https://github.com/ClashKingInc/clashy.py/tree/0703aee64a24c48aef296856bd688704d434181f) can be evaluated in the Python domain runtime for its battle model and army-link parser. It must not become the collector and must be wrapped: its normal request path mutates decoded responses with client metadata and can log complete API data at debug level.
3. Apache-2.0 [`ClashTestingAPI`](https://github.com/ClashKingInc/ClashTestingAPI/tree/8f4008cf451d9ea0513cad18df773f0f535c0e2f) is a possible local-test-harness starting point. Reuse code only, not its bundled response fixtures. Its battle-log schema is intentionally incomplete for Clash Lens: it omits `battleTimestamp` and `battleTime`, so it cannot certify event attribution or day completeness.

MIT-licensed `ClashKingProxy` contains useful, isolated ideas—key rotation, header forwarding, aggregate-only metrics—but its application is not a Phase 1 collector. It has neither a per-key requests-per-second limiter nor durable scheduling/evidence storage, and adopting it as another runtime would prematurely choose a service boundary. A self-hosted, reviewed subset could be reused later; the ClashKing-operated proxy must not be used.

No inspected repository provides a Phase 1-ready implementation of canonical two-sided battle linking, reconciliation/completeness, inferred shields or automatic defenses, versioned army classification, Google Sheets export, or an OBS overlay. Those remain Clash Lens-owned Python-domain or open-integration work as specified in `docs/architecture.md`.

## Evidence labels and reuse vocabulary

- **Confirmed source fact:** directly present in the linked repository revision or official Supercell page.
- **Inference:** a conclusion drawn by comparing a source fact with the confirmed Clash Lens documents.
- **Recommendation:** a proposed next action, not a product rule.
- **Direct-code candidate:** the repository has a permissive code license, but incorporation still requires preserving notices, dependency/security review, and a Phase 1 design fit. It never authorizes use of the repository's data or hosted service.
- **Reference-only:** inspect behavior and independently implement an idea; do not copy source. For GPL repositories this avoids imposing GPL obligations on the combined product. This is an engineering licensing assessment, not legal advice.

## Repository and licensing inventory

The following is the public-repository inventory returned by GitHub's organization endpoint on the research date: [ClashKingInc repository metadata](https://api.github.com/orgs/ClashKingInc/repos?per_page=100&type=public). The detailed assessment prioritizes repositories with a plausible Phase 1 connection rather than treating unrelated public code as a dependency candidate.

| Repository | License evidence | Phase 1 disposition |
| --- | --- | --- |
| [`ClashKingTracking`](https://github.com/ClashKingInc/ClashKingTracking/tree/ff704a7dd8abe343ec85d9921a0d60a679ff9e50) | [GPL-3.0](https://github.com/ClashKingInc/ClashKingTracking/blob/ff704a7dd8abe343ec85d9921a0d60a679ff9e50/LICENSE) | Reference-only. It is the most relevant tracking example, but is a GPL application and has incompatible data/architecture semantics. |
| [`ClashKingAPI`](https://github.com/ClashKingInc/ClashKingAPI/tree/50473a00b87c9efa96f2822f77a676ffb0376041) | [GPL-3.0](https://github.com/ClashKingInc/ClashKingAPI/blob/50473a00b87c9efa96f2822f77a676ffb0376041/LICENSE) | Reference-only code; its hosted/derived data is not an allowed Clash Lens source. |
| [`ClashKingApp`](https://github.com/ClashKingInc/ClashKingApp) | GitHub metadata reports GPL-3.0 | Do not reuse. A mobile application is outside the accepted Phase 1 surface/technology shape. |
| [`ClashKingAssets`](https://github.com/ClashKingInc/ClashKingAssets) | GitHub metadata reports GPL-3.0 | Do not reuse code or bundled game-derived assets. Supercell asset permission is separate from an upstream code license. |
| [`clashy.go`](https://github.com/ClashKingInc/clashy.go/tree/9ea0166b5e043feb1c52ce791a7faa2557373aea) | [MIT](https://github.com/ClashKingInc/clashy.go/blob/9ea0166b5e043feb1c52ce791a7faa2557373aea/LICENSE) | Direct-code candidate, subject to the limits below. This is the strongest Go-collector-adjacent option. |
| [`clashy.py`](https://github.com/ClashKingInc/clashy.py/tree/0703aee64a24c48aef296856bd688704d434181f) | [MIT](https://github.com/ClashKingInc/clashy.py/blob/0703aee64a24c48aef296856bd688704d434181f/LICENSE) | Direct-code candidate only in the Python domain/API side, not in the Go collector. |
| [`ClashKingProxy`](https://github.com/ClashKingInc/ClashKingProxy/tree/649f8d1db9e060676f1f6dff13c531f85e6c97aa) | [MIT](https://github.com/ClashKingInc/ClashKingProxy/blob/649f8d1db9e060676f1f6dff13c531f85e6c97aa/LICENSE) | Direct-code candidate for isolated, self-hosted transport/metrics ideas; defer adopting the service wholesale. |
| [`ClashTestingAPI`](https://github.com/ClashKingInc/ClashTestingAPI/tree/8f4008cf451d9ea0513cad18df773f0f535c0e2f) | [Apache-2.0](https://github.com/ClashKingInc/ClashTestingAPI/blob/8f4008cf451d9ea0513cad18df773f0f535c0e2f/LICENSE) | Direct-code candidate for local tests only; do not carry its fixtures forward or regard it as an official specification. |
| [`ClashKingBot`](https://github.com/ClashKingInc/ClashKingBot/tree/906e4c19f04a2192c97ee988093608854aec2f07) | [MIT](https://github.com/ClashKingInc/ClashKingBot/blob/906e4c19f04a2192c97ee988093608854aec2f07/LICENSE) | License permits code reuse, but the application is Discord clan management, roles, and war tracking. Defer; do not import its product scope. |
| [`ClashKingDashboard`](https://github.com/ClashKingInc/ClashKingDashboard/tree/d3a1fe05036e27af0b40cd0a87b7bddde7ab1d8c) | [MIT](https://github.com/ClashKingInc/ClashKingDashboard/blob/d3a1fe05036e27af0b40cd0a87b7bddde7ab1d8c/LICENSE) | License permits code reuse, but it configures the ClashKing bot and would prematurely choose Next.js/shadcn/Discord-specific assumptions. Defer. |
| [`ClashKingDocs`](https://github.com/ClashKingInc/ClashKingDocs), [`.github`](https://github.com/ClashKingInc/.github), and [`DevKit`](https://github.com/ClashKingInc/DevKit/tree/65efc1b98a194a964d815e8191cab60dd655a143) | No top-level license was present in the inspected public revisions/metadata. | No code, text, templates, or configuration may be copied. Public visibility is not a grant of reuse rights. |
| [`Gists`](https://github.com/ClashKingInc/Gists) | GitHub metadata reports MIT | Not analyzed further; no direct Phase 1 value established. Do not pull snippets without first pinning and checking each file's provenance. |
| [`num2words`](https://github.com/ClashKingInc/num2words) | GitHub metadata reports LGPL-2.1 | No Phase 1 relevance identified; do not introduce it for this project. |

For permissive candidates, include the applicable license and copyright notices with any copied substantial portion. Apache-2.0 also requires retaining its license/NOTICE requirements. `clashy.go` and `clashy.py` bundle game static data; their MIT repository licenses do not independently settle Supercell intellectual-property or update/provenance questions for those data files.

## Reusable versus reference-only matrix

| Need | What the source actually provides | Can Clash Lens use it directly? | Required boundary or rejection |
| --- | --- | --- | --- |
| Official API request models and endpoint paths | `clashy.go` has typed player and battle-log methods, including [`GetPlayer` and `GetBattleLog`](https://github.com/ClashKingInc/clashy.go/blob/9ea0166b5e043feb1c52ce791a7faa2557373aea/client.go#L789-L802). | **Candidate, MIT.** | Collector still has to archive untouched bytes and observation metadata before typed/domain processing. Disable/avoid response caching for evidence collection. |
| Go token rotation and HTTP mechanics | `clashy.go` accepts direct tokens and rotates them; its default config uses the official API base URL and a 30 **concurrent-request** limit ([client/config](https://github.com/ClashKingInc/clashy.go/blob/9ea0166b5e043feb1c52ce791a7faa2557373aea/client.go#L25-L62), [default config](https://github.com/ClashKingInc/clashy.go/blob/9ea0166b5e043feb1c52ce791a7faa2557373aea/config.go#L41-L61)). | **Candidate, MIT.** | Concurrency is not the required configurable 30 requests/second **per key**. Own per-key token buckets, health/quarantine, retry persistence, and scheduling remain necessary. |
| Raw battle-log fields | `clashy.go` models `battleType`, `attack`, `armyShareCode`, opponent tag, stars, and destruction; [`clashy.py` additionally maps `battleTimestamp` and `battleTime`](https://github.com/ClashKingInc/clashy.py/blob/0703aee64a24c48aef296856bd688704d434181f/coc/battlelogs.py#L44-L123). | **Models are candidates, MIT; schema facts are not official confirmation.** | Preserve response body unchanged before either library maps it. Do not infer trophy delta from a library type. |
| Army-share decoding | Both MIT libraries parse `h`, `i`, `d`, `u`, and `s` sections into heroes, Clan Castle contents, home troops, and spells ([Go parser](https://github.com/ClashKingInc/clashy.go/blob/9ea0166b5e043feb1c52ce791a7faa2557373aea/game.go#L245-L400), [Python parser](https://github.com/ClashKingInc/clashy.py/blob/0703aee64a24c48aef296856bd688704d434181f/coc/game_data.py#L545-L684)). | **Candidate only in Python, MIT.** | Wrap it in a versioned decoder that retains raw tokens/unknown IDs/errors. Neither parser is a versioned archetype classifier or a proof that a composition was deployed. |
| Python API client/rate limiter | `clashy.py` has a per-second `BatchThrottler`, but computes one aggregate `key_count × throttle_limit` budget ([throttlers](https://github.com/ClashKingInc/clashy.py/blob/0703aee64a24c48aef296856bd688704d434181f/coc/http.py#L44-L118), [HTTP client](https://github.com/ClashKingInc/clashy.py/blob/0703aee64a24c48aef296856bd688704d434181f/coc/http.py#L176-L344)). | **Not for collection.** | The accepted architecture assigns official collection/key handling to Go. Python can use a parser/model after evidence is durable. |
| Key-rotating proxy and aggregate metrics | `ClashKingProxy` rotates configured keys, forwards request bodies, copies selected cache headers, and records aggregate endpoint/status/latency counters ([README](https://github.com/ClashKingInc/ClashKingProxy/blob/649f8d1db9e060676f1f6dff13c531f85e6c97aa/README.md#L1-L53), [implementation](https://github.com/ClashKingInc/ClashKingProxy/blob/649f8d1db9e060676f1f6dff13c531f85e6c97aa/proxy.go#L18-L219)). | **Candidate snippets/self-hosted only, MIT.** | No durable queue, per-key RPS ceiling, independent key quarantine, archive, or domain boundary. Do not add a proxy runtime without maintainer approval; never send user requests through `proxy.clashk.ing`. |
| Local mock API | `ClashTestingAPI` routes player and battle-log requests from fixture files ([routes](https://github.com/ClashKingInc/ClashTestingAPI/blob/8f4008cf451d9ea0513cad18df773f0f535c0e2f/app/routes/players.py#L17-L63), [fixture loader](https://github.com/ClashKingInc/ClashTestingAPI/blob/8f4008cf451d9ea0513cad18df773f0f535c0e2f/app/routes/common.py#L1-L141)). | **Candidate test code, Apache-2.0.** | Use only synthetic Clash Lens fixtures. Its `BattleLogEntry` omits timestamps/duration ([model](https://github.com/ClashKingInc/ClashTestingAPI/blob/8f4008cf451d9ea0513cad18df773f0f535c0e2f/app/models/players.py#L59-L71)); therefore it cannot test exact events or day reconciliation. |
| Legend collection and derived daily stats | GPL `ClashKingTracking` polls profiles, compares aggregate trophy/win deltas, and stores estimated attacks/defenses using poll time ([`LegendTracking`](https://github.com/ClashKingInc/ClashKingTracking/blob/ff704a7dd8abe343ec85d9921a0d60a679ff9e50/scripts/legends.py#L12-L211)). | **No—reference-only.** | Its data/reconstruction strategy is insufficient for the Clash Lens exact-event, raw-evidence, two-sided-deduplication, and completeness rules. |
| Existing ClashKing player/history API | The GPL API advertises cached public historical data and asks consumers not to represent it as self-collected ([README](https://github.com/ClashKingInc/ClashKingAPI/blob/50473a00b87c9efa96f2822f77a676ffb0376041/README.md#L1-L27)). | **No.** | Calling it, seeding from it, or using its returned data conflicts with official-source-only discovery/collection. |
| Discord, Sheets, and OBS | The MIT bot is a clan-management/war-tracking Discord application ([README](https://github.com/ClashKingInc/ClashKingBot/blob/906e4c19f04a2192c97ee988093608854aec2f07/README.md#L28-L101)). Inspected target repositories showed no Google Sheets or OBS browser-overlay implementation. | **No current adoption.** | Minimal Discord workflow and Sheets/OBS designs remain open; retain one Python API contract rather than adapting bot-specific outputs. |

## Detailed technical discoveries

### 1. `ClashKingTracking` demonstrates why profile-delta reconstruction is not sufficient

**Confirmed source fact:** `LegendTracking._find_changes` fetches player profiles after an initial clan-member screen, retains only a short field list, and compares the previous profile with the current profile. On a negative trophy delta it manufactures defense entries; on a positive delta it manufactures attack entries. The stored timestamp is the polling time, not an official battle timestamp. It does not fetch the player's battle-log endpoint in this workflow. See [`scripts/legends.py`](https://github.com/ClashKingInc/ClashKingTracking/blob/ff704a7dd8abe343ec85d9921a0d60a679ff9e50/scripts/legends.py#L47-L211).

**Inference:** this cannot satisfy Clash Lens's required exact events, preservation of `armyShareCode`, battle-time attribution, two-sided battle linking, zero-trophy event handling, or proof of daily completeness. It is useful as an anti-pattern test case: a trophy change and counter delta must never be represented as a timestamped battle without official battle-log evidence.

**Confirmed source fact:** its generic tracker uses broad asyncio concurrency and an in-process throttler, while its project dependencies select MongoDB, Redis, and Kafka ([generic tracker](https://github.com/ClashKingInc/ClashKingTracking/blob/ff704a7dd8abe343ec85d9921a0d60a679ff9e50/scripts/tracking.py#L15-L129), [project dependencies](https://github.com/ClashKingInc/ClashKingTracking/blob/ff704a7dd8abe343ec85d9921a0d60a679ff9e50/pyproject.toml#L1-L30)). Its date helper uses a 05:00 UTC boundary ([`gen_legend_date`](https://github.com/ClashKingInc/ClashKingTracking/blob/ff704a7dd8abe343ec85d9921a0d60a679ff9e50/utility/time.py#L81-L85)).

**Recommendation:** retain the 05:00 UTC result only as corroborating source-code observation; Clash Lens already owns that rule in `docs/domain.md`. Keep the accepted PostgreSQL durable-queue/Go-collector/Python-domain split. Do not inherit MongoDB, Redis, Kafka, proxy routing, or an arbitrary high-concurrency setting from this GPL implementation.

### 2. The permissive clients are useful adapters, not an evidence pipeline

`clashy.go` exposes raw bytes at its lower HTTP layer, but the normal client flow unmarshals into typed structs and can serve/refresh an in-memory GET cache ([HTTP `Do`](https://github.com/ClashKingInc/clashy.go/blob/9ea0166b5e043feb1c52ce791a7faa2557373aea/http.go#L82-L175)). Its rate limiter is a buffered concurrency semaphore, not a time-window RPS limiter ([rate limiter](https://github.com/ClashKingInc/clashy.go/blob/9ea0166b5e043feb1c52ce791a7faa2557373aea/ratelimit.go#L7-L53)).

`clashy.py` is more directly unsuitable for collection. Its request function parses a response, adds `status_code`, `timestamp`, and cache metadata to that decoded object, and has debug calls that include request/response data ([HTTP path](https://github.com/ClashKingInc/clashy.py/blob/0703aee64a24c48aef296856bd688704d434181f/coc/http.py#L276-L367)). `raw_attribute` then exposes this altered object to model users. This conflicts with an untouched raw-response archive and the rule against logging unnecessary player data.

**Recommendation:** if either client is selected later, pin an exact version/revision, document the license notice, set collection caching off, and place an owned transport wrapper in Go that:

- assigns a particular key before a request and enforces a 30 RPS ceiling for that key;
- records request/response timestamps, status, key-health outcome, and a content hash;
- writes the original response bytes into the immutable archive before decoding;
- emits only safe aggregate observability data; and
- passes the stored observation reference to Python for linking/reconciliation/classification.

The wrapper—not the third-party client—must be the product's evidence boundary.

### 3. Army-share parsing is a starting point, not classification

Both MIT parsers recognize the same compact section grammar and separate home troops/spells from Clan Castle contents and hero loadouts. They resolve numeric values through bundled static data. This reinforces the existing local research note, [`army-share-code.md`](army-share-code.md), but remains source-code behavior rather than an official wire-format specification.

There are material gaps:

- The Go parser silently skips malformed sections and lets failed integer conversion fall through to zero-valued IDs/quantities; the Python parser raises on malformed item tokens and falls back to a base static record for an unknown ID. Neither preserves unknown tokens, duplicate sections, or a structured parse-error record.
- Neither parser attaches a decoder-version, metadata-snapshot version, token provenance, classification confidence, or `Unclassified` reason to the result.
- Neither implements a reusable, versioned army-archetype classifier. In particular, neither resolves the required Phase 1 separation between exact composition, siege/support/Clan-Castle facets, and an explicitly ordered archetype rule set.

**Recommendation:** do not invoke either parser directly on evidence and discard the result. Store the untouched `armyShareCode`, then run an owned, versioned Python decoder that may borrow a tested MIT parser implementation but adds preservation/error behavior. Classify only in Python, in accordance with `docs/architecture.md`; keep unclassified and malformed results visible in aggregates.

### 4. Proxy and mock code have bounded, non-production value

`ClashKingProxy` is unusually aligned with the desired handling of secrets: its documentation says not to log configured keys, bearer headers, bodies, or client IPs, and to expose aggregate-only metrics ([privacy notes](https://github.com/ClashKingInc/ClashKingProxy/blob/649f8d1db9e060676f1f6dff13c531f85e6c97aa/docs/privacy_compliance.md#L1-L28)). Its code also normalizes endpoint labels before metrics aggregation. That is an appropriate observability principle.

It does, however, round-robin keys globally and forwards requests immediately. It neither meters each key per second nor makes polling intent/retries durable. It forwards inbound bearer credentials on `/dev/`, which must not be made reachable from a browser under the Clash Lens architecture. It is therefore not a substitute for the Go collector.

`ClashTestingAPI` is a fixture-backed FastAPI service whose README labels it a mock API ([README](https://github.com/ClashKingInc/ClashTestingAPI/blob/8f4008cf451d9ea0513cad18df773f0f535c0e2f/README.md#L1-L39)). Its models/fixtures are not official documents and do not include the timestamp fields the Python client expects. Use it only to test transport errors, pagination shape, and normal API responses after replacing the fixtures with synthetic values. Add owned fixtures covering two-sided duplicates, delayed appearance, zero-trophy events, mixed battle types, malformed `armyShareCode`, paired-endpoint failure, reset adjustment, inferred shield, and stale snapshot entries.

### 5. No repository closes the product-surface gaps

`ClashKingBot` explicitly centers clan management, roles, leaderboards, and war tracking; those features are out of scope for Phase 1. `ClashKingDashboard` is a bot-settings dashboard whose README identifies a Next.js/React/shadcn stack and Discord OAuth configuration, not a public Legend I analytics site ([dashboard README](https://github.com/ClashKingInc/ClashKingDashboard/blob/d3a1fe05036e27af0b40cd0a87b7bddde7ab1d8c/README.md#L1-L146)).

**Recommendation:** do not use these MIT applications to decide Discord workflow, website framework, account model, or product terminology. No inspected source provided a Google Sheets exporter or OBS browser overlay; build those later as thin consumers of the Clash Lens Python public API and shared, versioned analytics summaries.

## Official API and policy validation

### Performed, sanitized validation

- I opened Supercell's official [Clash of Clans API Swagger UI](https://developer.clashofclans.com/api-docs/index.html). It did not expose a public schema through this environment; its authenticated schema/token flow was not bypassed.
- I opened the current official [Fan Content Policy](https://supercell.com/en/fan-content-policy/) and [Terms of Service](https://supercell.com/en/terms-of-service/). The Fan Content Policy requires a legible unofficiality notice when Supercell assets are used, prohibits implying Supercell endorsement, and says the applicable developer policies/agreements must be followed. It also permits only the stated non-commercial fan-content uses except the listed monetization exceptions. These constraints reinforce—not replace—the existing Clash Lens rules.
- No authenticated `api.clashofclans.com` request was made. An attempt to use the key path named in the task solely in an `Authorization` header was declined by the execution safety layer as untrusted temporary-file credential use. The key was not read, printed, copied, logged, or added to the repository; no workaround was attempted. Consequently, no player response, tag, or raw API body was collected or retained.

### Consequences and remaining validation

The `battleTimestamp`, `battleTime`, and field observations above are implementation-model observations from MIT source, **not** independent official API-schema confirmation. Before implementation, validate the official profile, battle-log, and global/player-ranking endpoint schemas with an authorized, approved collection test that:

1. uses a maintainer-approved credential source and one transient official leaderboard entry only;
2. prints only endpoint status, root/item field names, types, and counts—never tags, names, tokens, or raw bodies;
3. exercises one profile and one battle-log request through the proposed Go collector wrapper; and
4. archives no test response unless it is deliberately accepted into the normal immutable evidence pipeline with its provenance.

This is also the correct time to verify the official current rate-limit contract and developer API terms rather than treating a third-party library's default `30` as authoritative.

## Clash Lens fit, gaps, and rejected ideas

| Area | Fit/gap | Decision |
| --- | --- | --- |
| Official collection | `clashy.go` can reduce basic transport/model work, but none of the sources implement the accepted evidence-first collector. | Consider a short, pinned dependency spike only after approval; keep Go collector ownership. |
| Discovery | ClashKing has populations and endpoints, but their data is not official-source discovery. | Reject data/tag seeding, hosted API use, and hosted proxy use. Use only official leaderboards, clans, battle-log opponents, and submitted tags. |
| Linking and reconciliation | No source implements Clash Lens's canonical link, exact/complete distinction, or documented adjustment/shield rules. | Build in Python from retained observations; do not port profile-delta heuristics. |
| Army analysis | Parsers are helpful; classifier and trust behavior are absent. | Reuse only a parser under MIT terms, wrapped by owned versioned decoder/classifier code. |
| Queues/storage | Tracking uses MongoDB/Redis/Kafka; Clash Lens has accepted PostgreSQL queues and raw archive. | Reject technology transfer. Take only generic ideas such as bounded work and aggregate metrics. |
| API/web/Discord | GPL APIs and MIT bot/dashboard encode a different, clan-management product. | Reference presentation/ergonomic ideas only; do not reuse product endpoints, terms, or data contracts. |
| Sheets/OBS | No relevant source found. | Explicitly deferred to their open Phase 1 designs. |

## Prioritized recommendations

1. **Do not consume ClashKing data or services.** Add this to implementation review checklists: no `api.clashk.ing`, `proxy.clashk.ing`, ClashKing fixture corpus, or copied player tags as an ingestion source.
2. **Define the owned Go evidence-client interface before selecting a library.** Require byte-for-byte archival and append-only observation records before decode; per-key RPS state and failure quarantine must be outside the library.
3. **Run an approved official-schema characterization test.** Treat it as a small collector acceptance test, not an ad hoc script, and record only sanitized schema results.
4. **Prototype the Python army decoder/classifier separately.** It may consume a pinned MIT parser, but must persist raw payload/token/errors, metadata and decoder versions, then produce an explicit `Unclassified` outcome. Reuse the recommendations and caveats in [`army-share-code.md`](army-share-code.md).
5. **Create owned synthetic test fixtures.** `ClashTestingAPI` can accelerate a local mock, but acceptance tests must encode the Clash Lens domain invariants—not third-party mock omissions.
6. **Defer framework/service decisions.** Do not use the MIT dashboard, bot, or proxy as implicit approval for Next.js, disnake, a proxy runtime, MongoDB/Redis/Kafka, Google Sheets format, or OBS delivery. Those choices remain open in `docs/architecture.md`.

## Explicitly rejected or deferred

- Reusing or adapting GPL `ClashKingTracking`/`ClashKingAPI` code.
- Calling a ClashKing API/proxy, importing its historical data, seeding player tags from it, or using a competitor fixture as evidence.
- Copying code/text/configuration from an unlicensed public repository.
- Reusing ClashKingAssets or bundled game static data merely because the surrounding repository has a permissive source-code license.
- Treating profile trophy changes as exact battles, using collection time as battle time, or inferring full days from a delta alone.
- Moving canonical battle linking, day reconciliation, shield/automatic-defense inference, army classification, or analytics into Go or TypeScript.
- Adding war, clan-management, ranked tournament, base-management, or prescriptive recommendation behavior because it appears in a sibling application.
- Selecting a web framework, Discord library, raw archive product, Google Sheets integration, or OBS architecture from this research.

## Verification and limitations

- Source-code citations for the repositories inspected in depth are commit-pinned to the cloned revision used for inspection. The organization-inventory endpoint and the shallow inventory links for unrelated repositories are intentionally dynamic. All 53 external links in this report returned HTTP 200 when checked after the report was written.
- The repository worktree was not otherwise changed; this report is the only created file. No commit, push, or pull request was made.
- The public organization listing can change after the research date. License conclusions apply to the cited revisions/metadata and should be rechecked before copying any material.
- Official API live schema/rate validation remains outstanding because no credential was used. The report intentionally distinguishes the public Swagger/policy sources from third-party implementation models.
