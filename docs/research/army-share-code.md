# `armyShareCode` Representation and Army Classification Research

- **Research date:** 2026-07-30
- **Status:** Research recommendation, not a confirmed product or domain rule
- **Scope:** Clash of Clans home-village army payloads observed through the official battle-log field named `armyShareCode`; no player tags or player data were accessed

## Executive summary

Supercell officially confirms that Clash of Clans can share saved or previously used army compositions through non-expiring links, but its public articles do not specify the wire format. The official developer Swagger page obtains its schema URL and bearer token from authenticated cookies, so this research could not independently retrieve an official schema or payload example for `armyShareCode`.

Current public decoding implementations agree on a compact, sectioned payload:

```text
h<hero-loadouts>i<clan-castle-troops>d<clan-castle-spells>u<home-troops>s<home-spells>
```

Troop and spell entries use `quantity x relative-id`; hero entries can carry one pet and up to two equipment IDs. Siege machines do not have a dedicated section: they occupy the troop namespace and must be identified from versioned unit metadata. Hero levels, pet levels, equipment levels, and troop/spell levels are not encoded by the inspected parsers. A token interpreted by tests as a hero skin is accepted but discarded.

The format is not sufficiently documented by an owning official source to treat every detail as confirmed. Clash Lens should preserve the untouched raw value, parse conservatively with a versioned decoder, retain unknown tokens and IDs, and expose malformed or incomplete parses rather than silently dropping them.

For primary-army classification, use only the decoded home-army troop section after separating siege machines and a versioned support-troop set. Treat Clan Castle troops, all spells, siege machines, heroes, pets, equipment, and skin/cosmetic metadata as secondary facets. Assign an archetype only through an explicit, ordered, versioned rule registry; otherwise return `Unclassified` with reason codes. Do not let a secondary facet change the primary archetype.

## Evidence labels used here

- **Confirmed fact:** stated by Supercell or directly visible in Supercell-owned material.
- **Source-code observation:** behavior in inspected public decoder/schema code; useful evidence, but not an official specification.
- **Hypothesis:** a plausible interpretation not established by an owning source.
- **Recommendation:** proposed Clash Lens behavior; not an existing product/domain rule.
- **Open question:** evidence is currently insufficient.

## Sources and verification limits

### Official Supercell sources

1. [Quality of Life Improvements & More! (2021-06-14)](https://supercell.com/en/games/clashofclans/blog/game-updates/quality-of-life-improvements-june21/) confirms Army Composition Sharing, including saved armies, previously used armies, external links, copying into Quick Train, and that army links never expire.
2. [Welcome to Clash Anytime Update! (2025-03-24)](https://supercell.com/en/games/clashofclans/blog/release-notes/welcome-to-clash-anytime-update/) confirms the Army Recipes rework and that armies can be created, edited, and shared. It does not document the encoding or category grammar.
3. [Welcome to Let’s Get Crafty Update! (2025-06-16)](https://supercell.com/en/games/clashofclans/blog/release-notes/welcome-to-lets-get-crafty-update/) says Starter Armies can include regular troops, spells, and common equipment. This confirms that equipment is conceptually part of modern recipes, but not how it is serialized.
4. [Meet the Hero Pets! (2021-04-10)](https://supercell.com/en/games/clashofclans/blog/game-updates/meet-the-hero-pets-1-2/) confirms that one pet may be assigned to a hero and that assignments can vary by army strategy. It does not say whether sharing preserves the assignment.
5. [Introducing Hero Equipment! (2023-12-12)](https://supercell.com/en/games/clashofclans/blog/news/introducing-hero-equipment/) confirms that each hero equips two pieces of equipment. It does not document army-link serialization.
6. [Official Clash of Clans API Swagger UI](https://developer.clashofclans.com/api-docs/index.html) is Supercell-owned. Its page initializes Swagger from a `game-api-url` cookie and adds a bearer token from a `game-api-token` cookie. Without an authenticated developer session, the schema itself was not available for verification.

### Inspected public implementation sources

The user permitted inspection of public ClashKing source only for technical decoding/classification logic. No competitor player tags, payload collections, or player data were accessed.

- [`clashy.go` battle-log model at revision `9ea0166`](https://github.com/ClashKingInc/clashy.go/blob/9ea0166b5e043feb1c52ce791a7faa2557373aea/battle_logs.go#L11-L32) models `armyShareCode` as an optional string in a battle-log entry. This corroborates the field shape but is not official schema evidence.
- [`clashy.go` army parser at revision `9ea0166`](https://github.com/ClashKingInc/clashy.go/blob/9ea0166b5e043feb1c52ce791a7faa2557373aea/game.go#L235-L389) implements the section grammar and normalized result model.
- [`clashy.go` parser tests at revision `9ea0166`](https://github.com/ClashKingInc/clashy.go/blob/9ea0166b5e043feb1c52ce791a7faa2557373aea/tests/game_test.go#L9-L169) contain synthetic/example army links covering heroes, pets, equipment, Clan Castle contents, troops, and spells.
- [`clashy.py` army parser at revision `0703aee`](https://github.com/ClashKingInc/clashy.py/blob/0703aee64a24c48aef296856bd688704d434181f/coc/game_data.py#L545-L684) independently implements the same grammar and exposes the resolved hero/pet/equipment groupings.

These two implementations are maintained by the same organization and should not be treated as independent confirmation of undocumented Supercell behavior.

## What is confirmed by Supercell

1. **Confirmed fact:** an army link may represent a saved army or a previously used army and can be copied into a Quick Train slot.
2. **Confirmed fact:** army links do not expire.
3. **Confirmed fact:** current Army Recipes can be created, edited, and shared.
4. **Confirmed fact:** a hero can have one assigned pet.
5. **Confirmed fact:** a hero can equip two pieces of Hero Equipment.
6. **Confirmed fact:** Supercell's public news and release-note pages do not define the compact `army=` grammar, numeric ID namespaces, malformed-input behavior, or the relationship between a battle-log `armyShareCode` and an external `CopyArmy` URL.

No official public source inspected here confirms whether `armyShareCode` describes the trained army, the army taken into battle, the units actually deployed, the post-battle surviving army, or a normalized recipe. The field should therefore be described as the battle's supplied army-share payload, not as proof of deployment order or actual use of every represented item.

## Source-code-observed representation

### Container and section ordering

**Source-code observation:** both inspected parsers accept either a full URL with an `army` query parameter or a raw payload. They recognize these section markers:

| Marker | Observed interpretation | Primary/secondary recommendation |
|---|---|---|
| `h` | Hero loadouts | Secondary |
| `i` | Requested/Clan Castle troops | Secondary |
| `d` | Requested/Clan Castle spells | Secondary |
| `u` | Home-army troops and troop-namespace items | Primary input after filtering |
| `s` | Home-army spells | Secondary |

**Open question:** the inspected implementations allow absent sections and do not enforce one canonical order. It is not officially verified whether Supercell always emits the order `h`, `i`, `d`, `u`, `s`, or whether repeated sections are possible.

### Troops

Observed entry grammar:

```text
u<quantity>x<relative-troop-id>-<quantity>x<relative-troop-id>...
```

Example structural reading only:

```text
u10x11-2x1
```

means two troop-namespace entries: quantity 10 of relative ID 11, and quantity 2 of relative ID 1.

**Source-code observation:** the current static-data namespace stores troop IDs at `4,000,000 + relative-id`. The relative ID is not a stable display order and may be sparse.

**Omissions/uncertainties:**

- No troop level appears in this grammar.
- No deployment order appears.
- The code does not prove whether all encoded troops were deployed.
- Seasonal, temporary, transformed, spawned, or future units require current versioned metadata; unknown IDs must not be guessed.

### Spells

Observed entry grammar:

```text
s<quantity>x<relative-spell-id>-<quantity>x<relative-spell-id>...
```

**Source-code observation:** home spells use `s`; Clan Castle spells use `d` with the same `quantity x relative-id` entry form. Current static data stores spell IDs at `26,000,000 + relative-id`.

**Omissions/uncertainties:**

- No spell level appears.
- The payload does not establish that every represented spell was cast.
- Seasonal and newly introduced spell IDs can be sparse and require versioned metadata.

### Siege machines

**Source-code observation:** siege machines have no dedicated section marker. They are records in the troop ID namespace. In the inspected metadata, they are distinguished from ordinary troops by a production-building/category value corresponding to the Workshop. Therefore a siege may appear in `u` or in Clan Castle troops `i`, depending on where the recipe places it.

**Recommendation:** classify siege machines by a versioned stable-ID/category mapping, remove them from core troop composition, and retain them as secondary facets such as `home_siege` and `clan_castle_siege`.

**Open question:** official documentation does not confirm whether every battle-log payload serializes a selected siege, whether an unselected set of available siege choices can appear, or how future siege-like units will be categorized.

### Clan Castle troops and spells

Observed entry grammars:

```text
i<quantity>x<relative-troop-id>-...
d<quantity>x<relative-spell-id>-...
```

**Source-code observation:** the parsers place `i` entries in Clan Castle troops and `d` entries in Clan Castle spells. Because `i` uses the troop namespace, it can also contain a siege-machine ID.

**Recommendation:** keep all `i` and `d` contents secondary. They may distinguish variants of the same primary army but should not select the primary archetype.

### Heroes, pets, equipment, and the `m` token

Observed hero-entry grammar:

```text
<hero-id>[m<number>][p<pet-id>][e<equipment-1-id>[_<equipment-2-id>]]
```

Multiple hero entries are separated by `-` after `h`:

```text
h<hero-entry>-<hero-entry>-...
```

| Component | Source-code observation | Omission/uncertainty |
|---|---|---|
| Leading number | Relative hero ID | No hero level or mode is decoded |
| `p<number>` | Pet assigned to that hero | No pet level is decoded |
| `e<number>` | First equipment item | No equipment level is decoded |
| `_<number>` | Second equipment item | No equipment level is decoded |
| `m<number>` | Accepted between hero and pet/equipment; ignored by parsers | A test calls this a hero skin, but no owning official source was found; treat that meaning as a hypothesis |

**Source-code observation:** current bundled metadata uses absolute namespaces `28,000,000 + hero-id`, `73,000,000 + pet-id`, and `90,000,000 + equipment-id`.

**Important implementation discrepancy:** at revision `9ea0166`, [`clashy.go/constants.go`](https://github.com/ClashKingInc/clashy.go/blob/9ea0166b5e043feb1c52ce791a7faa2557373aea/constants.go#L7-L17) declares hero, pet, and equipment bases of 2,000,000, 60,000,000, and 30,000,000, while that revision's bundled static records and the Python parser use 28,000,000, 73,000,000, and 90,000,000. The Go tests check parsed element counts/presence but not resolved identities, so they do not catch this mismatch. This is evidence not to copy fixed offsets blindly.

**Recommendation:** resolve relative IDs against a versioned metadata snapshot and validate that the computed absolute ID exists in the expected category. Store the raw relative ID regardless of resolution. Do not silently substitute an empty/default record for an unknown ID.

### Data not represented by the inspected grammar

The inspected parsers do not decode:

- troop levels;
- spell levels;
- siege-machine levels;
- hero levels;
- pet levels;
- equipment levels;
- deployment order or timing;
- whether an encoded unit/spell/hero ability was actually used;
- donated unit levels;
- explicit housing-space totals or capacity;
- Town Hall level;
- an explicit format version.

Some of these values may be inferable from separate observations, but they are not established by this payload and must not be injected into the decoded composition as if encoded.

## Recommended decoder contract

Use a decoder version independent from the classifier version, for example `army-share-decoder/v1`.

### Preserve

For every observation retain:

- untouched `armyShareCode`;
- source observation and fetch timestamp;
- decoder version;
- metadata snapshot/version;
- ordered raw sections and tokens;
- parsed relative IDs and quantities;
- resolved stable IDs/names where known;
- unknown sections, tokens, IDs, duplicate entries, and parse errors;
- a normalized composition produced only after raw preservation.

### Validate conservatively

A parse should record, rather than hide:

- missing or empty payload;
- invalid URL/query decoding;
- unknown section marker;
- repeated section;
- malformed `quantity x id` token;
- zero or negative quantity;
- duplicate item IDs within a section;
- unknown or wrong-category ID;
- more than one pet on a hero or more than two equipment IDs if a future grammar exposes them;
- hero token suffixes not understood by the decoder;
- capacity inconsistencies, when capacity is available from trusted contextual metadata.

Normalization may combine duplicate IDs for analysis, but the raw duplicate tokens must remain available.

## Recommended deterministic primary-army classifier

Use an independently versioned classifier, for example `primary-army/v1`, with immutable configuration containing:

- decoder version(s) it accepts;
- unit metadata snapshot/version;
- siege-machine stable-ID/category set;
- support-troop stable-ID set;
- housing-space values;
- ordered archetype rules and stable archetype IDs;
- thresholds;
- tie-breaking rule;
- confidence rules and reason codes.

The exact support-troop set, named archetype registry, and thresholds require maintainer approval before becoming product/domain rules.

### 1. Build the primary composition

From the normalized `u` section only:

1. Reject unresolved, malformed, or non-home-village item IDs from the eligible primary pool.
2. Separate siege machines into secondary facets.
3. Separate items in `supportTroopIds` into secondary support facets.
4. Aggregate remaining troops by stable troop ID.
5. Compute housing contribution as `quantity × versioned housing space`.
6. Compute each remaining troop's share of core housing.
7. Sort by share descending, then by stable numeric ID ascending for deterministic ties.

Do not include `i`, `d`, `s`, `h`, pets, equipment, or the `m` token in these shares.

### 2. Apply explicit ordered archetype rules

Each rule should be data, not ad hoc code, and should specify:

- stable archetype ID and display label;
- required core troop IDs;
- optional core troop IDs;
- forbidden core troop IDs, if necessary;
- minimum/maximum housing shares;
- minimum known-core housing;
- rule priority;
- rule revision notes and evidence.

Evaluate rules in fixed priority order. A rule matches only when all of its predicates are satisfied. If multiple rules at the same priority match, use the rule's stable ID as the deterministic tie-break and lower confidence; preferably make overlapping rules invalid during classifier validation.

Named archetypes should be added only with an approved rule and representative validation examples. A newly seen composition must not be forced into the nearest familiar label.

### 3. Keep secondary facets separate

Attach, but do not use to choose the primary archetype:

- support troop composition;
- home siege selection;
- Clan Castle troops, siege, and spells;
- home spell composition;
- heroes present;
- hero-to-pet assignments;
- hero equipment loadouts;
- understood cosmetic/skin metadata;
- unknown secondary tokens.

This permits analysis such as one primary archetype with different spell, siege, hero, pet, equipment, or donation variants without fragmenting primary usage counts.

### 4. Confidence

Recommended per-observation confidence states:

- **High:** payload parses fully under a known decoder; every `u` ID and housing value resolves; no unknown section/token can affect troop composition; exactly one non-overlapping archetype rule matches with comfortable threshold margin.
- **Medium:** primary troop composition is fully known and one rule matches, but the match is near a threshold, support/core role treatment is materially relevant, or unknown data exists only in verified secondary sections.
- **Low:** a tentative rule matches but unresolved/ambiguous data could change the primary composition or multiple rules overlap. Low-confidence results should normally be surfaced as `Unclassified` in aggregates unless a future approved rule explicitly permits tentative labels.
- **Unclassified:** no trustworthy unique primary label can be assigned.

Confidence is about classification, not battle-event accuracy or ranked-day completeness. Those remain separate states under `docs/domain.md`.

### 5. `Unclassified` is a first-class result

Use stable reason codes, permitting more than one:

- `missing_share_code`
- `empty_share_code`
- `malformed_share_code`
- `unsupported_format`
- `unknown_section`
- `unknown_primary_unit_id`
- `wrong_category_unit_id`
- `missing_housing_metadata`
- `insufficient_known_core`
- `no_archetype_rule_match`
- `multiple_archetype_rule_matches`
- `threshold_ambiguity`
- `metadata_version_unavailable`
- `decoder_version_unsupported`

Every aggregate should count `Unclassified` and expose its reason distribution. Reclassification under a new version must not overwrite prior published results.

## Suggested classifier validation before adoption

1. Build hand-reviewed, synthetic payload fixtures for every supported section and malformed condition; do not use competitor player data.
2. Add identity assertions, not only element-count assertions, for troops, spells, siege machines, heroes, pets, and equipment.
3. Test sparse and newly introduced IDs against a pinned metadata snapshot.
4. Test every archetype rule at boundary values and one unit/housing step on either side.
5. Test rule overlap and fail classifier publication when equal-priority rules overlap unexpectedly.
6. Test that changing spells, siege, Clan Castle contents, heroes, pets, or equipment leaves the primary archetype unchanged.
7. Test that changing a core troop across a rule boundary changes the result deterministically.
8. Publish fixture coverage, unknown-ID rates, `Unclassified` rate, and hand-review precision before claiming reliable classification.

## Open questions requiring owning evidence or maintainer decisions

1. Is the official battle-log `armyShareCode` byte-for-byte the raw `army` query payload used by `CopyArmy`, or only compatible with it?
2. Does it represent the army selected before battle, the army actually taken, or only items actually deployed/used?
3. Under what battle types or privacy/error conditions is the field absent?
4. Is section order canonical, and can sections repeat?
5. What is the official meaning of `m<number>` in a hero entry?
6. Are hero IDs selections, deployment slots, transformations, or another concept in modes where more heroes exist than can be taken?
7. Can a payload include more than two equipment IDs per hero in future formats, and how will format evolution be signaled?
8. Are equipment, pet, and hero loadouts consistently present in battle-log share codes created before and after their respective feature releases?
9. Can `u` contain a siege and can `i` contain a donated siege in every current recipe variant, or are there other siege encodings?
10. What exact support-troop set and archetype rule registry should Clash Lens adopt for its first classifier version?
11. What measured validation threshold is required before an archetype can receive High confidence?
12. Can Supercell provide or expose an authenticated official schema and representative redacted payload fixtures that confirm the field and grammar?

## Conclusion

The section grammar is strong implementation evidence and is suitable for a conservative experimental decoder, but it is not an official public specification. The ID-base discrepancy found in a current Go implementation demonstrates why Clash Lens should pin metadata, verify category identity, preserve unknowns, and version decoding independently from classification.

A primary classifier should derive only from core home-troop composition. Everything else in the payload is valuable context but should remain a secondary facet. Explicit `Unclassified` outcomes and classification-confidence reasons are necessary for trustworthy aggregates and align with the repository's existing trust requirements.
