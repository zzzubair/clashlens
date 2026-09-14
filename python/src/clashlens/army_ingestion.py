from __future__ import annotations

import hashlib
import json
import warnings
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import boundary, boundary_publication
from .army_decoder import (
    DECODER_VERSION,
    DecodedArmy,
    DecodeFailure,
    decode_army_share_code,
)
from .army_season_summaries import acquire_army_season_lock, materialize_army_season
from .catalog import CATALOG_HASH, CATALOG_VERSION
from .db import Claim, Database, _text_value
from .domain import SEASON_ANCHOR_RULE_VERSION, DomainRuleError


def _upsert_army_decodes(database: Database, connection: Any, battle_ids: list[int]) -> None:
    if not battle_ids:
        return
    exists = connection.execute(
        "SELECT to_regclass('battle_army_decodes')"
    ).fetchone()
    if exists is None or exists[0] is None:
        return
    catalog = connection.execute(
        "SELECT content_hash FROM unit_catalog_versions WHERE version = %s",
        (CATALOG_VERSION,),
    ).fetchone()
    catalog_ready = catalog is not None and _text_value(catalog[0]) == CATALOG_HASH
    rows = connection.execute(
        """
        SELECT b.id, p.evidence_id, p.perspective, e.army_share_code,
               source_row.source_json
        FROM legend_battles AS b
        JOIN battle_perspectives AS p ON p.battle_id = b.id
        JOIN battle_evidence AS e ON e.id = p.evidence_id
        JOIN battle_source_rows AS source_row ON source_row.id = e.source_row_id
        WHERE b.id = ANY(%s::bigint[])
        """,
        (battle_ids,),
    ).fetchall()
    for battle_id, evidence_id, perspective, raw_code, source_json in rows:
        perspective = _text_value(perspective)
        source_code = (
            source_json.get("armyShareCode")
            if isinstance(source_json, dict)
            else None
        )
        if source_code is not None and not isinstance(source_code, str):
            decoded: DecodedArmy | DecodeFailure = DecodeFailure(
                None,
                "malformed",
                "armyShareCode must be text",
                DECODER_VERSION,
                CATALOG_VERSION,
                CATALOG_HASH,
            )
        elif catalog_ready:
            decoded = decode_army_share_code(raw_code)
        else:
            decoded = DecodeFailure(
                raw_code,
                "catalog_version_unavailable",
                "pinned unit catalog is unavailable or has the wrong hash",
                DECODER_VERSION,
                CATALOG_VERSION,
                CATALOG_HASH,
            )
        is_decoded = isinstance(decoded, DecodedArmy)
        if is_decoded:
            exact_army_id = None
            existing = None
            if decoded.identity_hash is not None:
                existing = connection.execute(
                    "SELECT id FROM exact_armies WHERE identity_hash = %s",
                    (decoded.identity_hash,),
                ).fetchone()
            if decoded.identity_hash is not None and existing is None:
                troop_quantities: dict[str, int] = {}
                for fact in decoded.home_troops:
                    troop_quantities[fact.typed_id] = (
                        troop_quantities.get(fact.typed_id, 0) + fact.quantity
                    )
                spell_quantities: dict[str, int] = {}
                for fact in decoded.spells:
                    spell_quantities[fact.typed_id] = (
                        spell_quantities.get(fact.typed_id, 0) + fact.quantity
                    )
                inserted = connection.execute(
                    """
                    INSERT INTO exact_armies (identity_hash, decoder_version, catalog_version, catalog_hash, home_troops, spells, heroes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (identity_hash) DO UPDATE SET identity_hash = EXCLUDED.identity_hash
                    RETURNING id
                    """,
                    (
                        decoded.identity_hash,
                        decoded.decoder_version,
                        decoded.catalog_version,
                        decoded.catalog_hash,
                        Jsonb(sorted(troop_quantities.items())),
                        Jsonb(sorted(spell_quantities.items())),
                        Jsonb(
                            [
                                {
                                    "hero": h.hero_typed_id,
                                    "pet": h.pet_typed_id,
                                    "equipment": list(h.equipment_typed_ids),
                                }
                                for h in decoded.heroes
                            ]
                        ),
                    ),
                ).fetchone()
                assert inserted is not None
                exact_army_id = int(inserted[0])
            elif existing is not None:
                exact_army_id = int(existing[0])
            active = connection.execute(
                "SELECT id, evidence_id, raw_code, identity_hash, status FROM battle_army_decodes WHERE battle_id = %s AND perspective = %s AND decoder_version = %s AND catalog_version = %s AND is_active = true",
                (
                    battle_id,
                    perspective,
                    decoded.decoder_version,
                    decoded.catalog_version,
                ),
            ).fetchone()
            raw_cmp_equal = False
            if active is not None:
                active_raw = active[2]
                if (active_raw is None and raw_code is None) or (
                    active_raw is not None
                    and raw_code is not None
                    and _text_value(active_raw) == _text_value(raw_code)
                ):
                    raw_cmp_equal = True
                if (
                    int(active[1]) == int(evidence_id)
                    and raw_cmp_equal
                    and (
                        active[3] is None
                        if decoded.identity_hash is None
                        else _text_value(active[3]) == decoded.identity_hash
                    )
                    and _text_value(active[4]) == decoded.status
                ):
                    continue
                connection.execute(
                    "UPDATE battle_army_decodes SET is_active = false WHERE id = %s",
                    (active[0],),
                )
                supersedes = int(active[0])
            else:
                supersedes = None
            connection.execute(
                """
                INSERT INTO battle_army_decodes (battle_id, evidence_id, perspective, raw_code, decoder_version, catalog_version, catalog_hash, status, exact_army_id, identity_hash, home_troops, spells, home_spells, cc_spells, siege, cc_troops, heroes, raw_m, unresolved_components, is_active, supersedes_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s)
                """,
                (
                    battle_id,
                    evidence_id,
                    perspective,
                    raw_code,
                    decoded.decoder_version,
                    decoded.catalog_version,
                    decoded.catalog_hash,
                    decoded.status,
                    exact_army_id,
                    decoded.identity_hash,
                    Jsonb(
                        [
                            (f.typed_id, f.quantity, f.origin)
                            for f in decoded.home_troops
                        ]
                    ),
                    Jsonb(
                        [(f.typed_id, f.quantity, f.origin) for f in decoded.spells]
                    ),
                    Jsonb(
                        [
                            (f.typed_id, f.quantity, f.origin)
                            for f in decoded.home_spells_raw
                        ]
                    ),
                    Jsonb(
                        [
                            (f.typed_id, f.quantity, f.origin)
                            for f in decoded.cc_spells_raw
                        ]
                    ),
                    Jsonb(
                        [(f.typed_id, f.quantity, f.origin) for f in decoded.siege]
                    ),
                    Jsonb(
                        [
                            (f.typed_id, f.quantity, f.origin)
                            for f in decoded.cc_troops
                        ]
                    ),
                    Jsonb(
                        [
                            {
                                "hero": h.hero_typed_id,
                                "pet": h.pet_typed_id,
                                "equipment": list(h.equipment_typed_ids),
                                "raw_m": h.raw_m,
                            }
                            for h in decoded.heroes
                        ]
                    ),
                    Jsonb([h.raw_m for h in decoded.heroes if h.raw_m]),
                    Jsonb(
                        [
                            {
                                "numeric_id": fact.numeric_id,
                                "quantity": fact.quantity,
                                "section": fact.section,
                                "origin": fact.origin,
                            }
                            for fact in decoded.unknown
                        ]
                    ),
                    supersedes,
                ),
            )
        else:
            failure: DecodeFailure = decoded  # type: ignore[assignment]
            active = connection.execute(
                "SELECT id, evidence_id, raw_code, failure_category FROM battle_army_decodes WHERE battle_id = %s AND perspective = %s AND decoder_version = %s AND catalog_version = %s AND is_active = true",
                (
                    battle_id,
                    perspective,
                    failure.decoder_version,
                    failure.catalog_version,
                ),
            ).fetchone()
            raw_cmp_equal = False
            if active is not None:
                active_raw = active[2]
                if (active_raw is None and raw_code is None) or (
                    active_raw is not None
                    and raw_code is not None
                    and _text_value(active_raw) == _text_value(raw_code)
                ):
                    raw_cmp_equal = True
            if (
                active is not None
                and int(active[1]) == int(evidence_id)
                and raw_cmp_equal
                and _text_value(active[3]) == failure.category
            ):
                continue
            if active is not None:
                connection.execute(
                    "UPDATE battle_army_decodes SET is_active = false WHERE id = %s",
                    (active[0],),
                )
                supersedes = int(active[0])
            else:
                supersedes = None
            connection.execute(
                """
                INSERT INTO battle_army_decodes (battle_id, evidence_id, perspective, raw_code, decoder_version, catalog_version, catalog_hash, status, failure_category, failure_detail, is_active, supersedes_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'failed', %s, %s, true, %s)
                """,
                (
                    battle_id,
                    evidence_id,
                    perspective,
                    raw_code,
                    failure.decoder_version,
                    failure.catalog_version,
                    failure.catalog_hash,
                    failure.category,
                    failure.detail,
                    supersedes,
                ),
            )
    day_rows = connection.execute(
        "SELECT DISTINCT ranked_day_start FROM legend_battles WHERE id = ANY(%s::bigint[])",
        (battle_ids,),
    ).fetchall()
    for (day_start,) in day_rows:
        boundary_publication._enqueue_army_analytics(database, connection, ranked_day_start=day_start)


def complete_army_analytics(database: Database, claim: Claim) -> None:
    with database.pool.connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            generation_input = claim.input_json.get("generation")
            generation_row = None
            season_id_row = None
            if generation_input is not None:
                boundary_text = claim.input_json.get("boundary_at")
                if boundary_text is None:
                    raise ValueError("army boundary is required")
                boundary_at = datetime.fromisoformat(str(boundary_text)).astimezone(
                    UTC
                )
                generation_row = connection.execute(
                    """
                    SELECT id, generation, sweep_id, snapshot_state, army_state,
                           army_manifest_id, army_rule_version, target_at, target_rule
                    FROM boundary_publication_generations
                    WHERE boundary_at = %s AND generation = %s
                    FOR UPDATE
                    """,
                    (boundary_at, int(generation_input)),
                ).fetchone()
                if generation_row is None:
                    raise ValueError(
                        "boundary publication generation does not exist"
                    )
                if _text_value(generation_row[4]) in {"superseded", "published"}:
                    database._finish_claim(
                        connection,
                        claim,
                        job,
                        state="complete",
                        outcome="stale_superseded",
                    )
                    return
                if _text_value(generation_row[4]) not in {"ready", "building"}:
                    raise ValueError(
                        "boundary army generation is stale or superseded"
                    )
                if _text_value(generation_row[3]) == "superseded":
                    raise ValueError(
                        "boundary publication generation is superseded"
                    )
                manifest_id = (
                    int(generation_row[5])
                    if generation_row[5] is not None
                    else None
                )
                if (
                    manifest_id is None
                    or int(claim.input_json.get("manifest_id", 0)) != manifest_id
                ):
                    raise ValueError(
                        "boundary army manifest identity does not match"
                    )
                manifest_digest = connection.execute(
                    "SELECT digest FROM boundary_publication_manifests WHERE id = %s",
                    (manifest_id,),
                ).fetchone()
                if manifest_digest is None or _text_value(
                    manifest_digest[0]
                ) != claim.input_json.get("manifest_digest"):
                    raise ValueError("boundary army manifest digest does not match")
                if _text_value(generation_row[6]) != claim.analytics_rule_version:
                    raise ValueError("boundary army rule version does not match")
                if _text_value(generation_row[8]) != "boundary-delay-v1":
                    raise ValueError("unsupported boundary target rule")
                now_row = connection.execute("SELECT clock_timestamp()").fetchone()
                assert now_row is not None
                if now_row[0] < generation_row[7]:
                    raise ValueError(
                        "army publication target dependency is not ready"
                    )
                manifest_digest_value = _text_value(manifest_digest[0])
                pending_members = connection.execute(
                    """
                    SELECT 1
                    FROM boundary_publication_generation_members
                    WHERE generation_id = %s AND army_status = 'pending'
                    LIMIT 1
                    """,
                    (generation_row[0],),
                ).fetchone()
                if pending_members is not None:
                    raise ValueError(
                        "boundary army publication dependency is not terminal"
                    )
                ranked_day_str = (boundary_at - timedelta(days=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                season_id_row = connection.execute(
                    """
                    SELECT ranked.official_season_id
                    FROM boundary_publication_generation_members AS member
                    JOIN ranked_day_versions AS ranked
                      ON ranked.id = member.ranked_day_version_id
                    WHERE member.generation_id = %s
                    ORDER BY ranked.id DESC
                    LIMIT 1
                    """,
                    (generation_row[0],),
                ).fetchone()
                if season_id_row is None:
                    season_id, _season_day = _season_metadata_for_ranked_day(database, 
                        connection, boundary_at - timedelta(days=1)
                    )
                else:
                    season_id = _text_value(season_id_row[0])
            else:
                ranked_day_str = claim.input_json.get("ranked_day_start")
                season_id = claim.input_json.get("official_season_id")
            if ranked_day_str is None or season_id is None:
                raise ValueError(
                    "army analytics requires ranked-day and season inputs"
                )
            from .season_retirement import (
                SEASON_DETAIL_RETIRED,
                acquire_season_lock_shared,
                is_season_detail_retired,
            )

            acquire_season_lock_shared(connection, str(season_id))
            if is_season_detail_retired(connection, str(season_id)):
                raise DomainRuleError(
                    SEASON_DETAIL_RETIRED,
                    f"season {season_id} detail is retired",
                )
            if generation_row is not None:
                connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET army_state = 'building', updated_at = clock_timestamp()
                    WHERE id = %s AND army_state = 'ready'
                    """,
                    (generation_row[0],),
                )
            manifest_members = None
            manifest_versions = None
            manifest_decodes = None
            manifest_daily_logs = None
            manifest_battles = None
            if generation_row is not None:
                manifest_rows = connection.execute(
                    "SELECT player_id, ranked_day_version_id, input_identity->'decode_ids', input_identity FROM boundary_publication_manifest_rows WHERE manifest_id = %s ORDER BY ordinal",
                    (manifest_id,),
                ).fetchall()
                manifest_members = [int(row[0]) for row in manifest_rows]
                manifest_versions = [
                    int(row[1]) for row in manifest_rows if row[1] is not None
                ]
                manifest_decodes = [
                    int(decode_id)
                    for row in manifest_rows
                    for decode_id in (row[2] or [])
                ]
                manifest_daily_logs = [
                    int(row[3]["daily_log_id"])
                    for row in manifest_rows
                    if isinstance(row[3], dict)
                    and row[3].get("daily_log_id") is not None
                ]
                manifest_battles = [
                    int(battle_id)
                    for row in manifest_rows
                    for battle_id in (
                        row[3].get("battle_ids", [])
                        if isinstance(row[3], dict)
                        else []
                    )
                ]
                manifest_evidence = [
                    int(evidence_id)
                    for row in manifest_rows
                    for evidence_id in (
                        row[3].get("evidence_ids", [])
                        if isinstance(row[3], dict)
                        else []
                    )
                ]
            else:
                manifest_evidence = None
            ranked_day_start = datetime.fromisoformat(
                str(ranked_day_str)
            ).astimezone(UTC)
            # Two builds for one ranked day must not interleave: fact
            # versions are computed as latest+1 and the day sweep marks
            # is_current, so a collision would surface as a unique
            # violation instead of a clean retry. Different days build
            # concurrently.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"army-facts:{ranked_day_start.isoformat()}",),
            )
            season_id = _ensure_army_day_dependency(database, 
                connection,
                ranked_day_start,
                ranked_version_ids=manifest_versions,
                allow_empty=generation_row is not None,
                official_season_id=str(season_id),
            )
            _build_army_facts(database, 
                connection,
                str(ranked_day_str),
                member_ids=manifest_members,
                ranked_version_ids=manifest_versions,
                decode_ids=manifest_decodes,
                evidence_ids=manifest_evidence,
                daily_log_ids=manifest_daily_logs,
                battle_ids=manifest_battles,
            )
            if generation_row is not None:
                marker = connection.execute(
                    """
                    SELECT fact_input_hash
                    FROM army_analytics_completed_days
                    WHERE ranked_day_start = %s
                    """,
                    (ranked_day_start,),
                ).fetchone()
                army_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "manifest_digest": manifest_digest_value,
                            "rule_versions": {
                                "army_rule_version": _text_value(generation_row[6]),
                                "decoder_version": DECODER_VERSION,
                                "catalog_version": CATALOG_VERSION,
                            },
                            "output": {
                                "day_fact_input_hash": (
                                    _text_value(marker[0])
                                    if marker is not None
                                    else None
                                ),
                            },
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                army_identity = boundary._create_boundary_artifact_identity(
                    connection,
                    generation_id=int(generation_row[0]),
                    artifact_kind="army",
                    manifest_id=int(manifest_id),
                    input_hash=army_hash,
                    source_identity={
                        "ranked_day_start": str(ranked_day_str),
                        "decoder_version": DECODER_VERSION,
                        "catalog_version": CATALOG_VERSION,
                    },
                )
                published_army = connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET army_state = 'published', army_input_hash = %s,
                        army_publication_id = %s,
                        army_coverage = jsonb_build_object(
                            'expected', (SELECT count(*) FROM boundary_publication_generation_members WHERE generation_id = %s),
                            'included', (SELECT count(*) FROM boundary_publication_generation_members WHERE generation_id = %s AND army_status IN ('complete','partial')),
                            'excluded', (SELECT count(*) FROM boundary_publication_generation_members WHERE generation_id = %s AND army_status NOT IN ('complete','partial')),
                            'terminal', (SELECT count(*) FROM boundary_publication_generation_members WHERE generation_id = %s AND army_status <> 'pending'),
                            'classifications', (SELECT COALESCE(jsonb_object_agg(army_status, count), '{}'::jsonb) FROM (SELECT army_status, count(*) FROM boundary_publication_generation_members WHERE generation_id = %s GROUP BY army_status) AS counts)
                        ),
                        updated_at = clock_timestamp()
                    WHERE id = %s AND army_state = 'building'
                      AND army_manifest_id = %s
                    RETURNING id
                    """,
                    (
                        army_hash,
                        army_identity,
                        generation_row[0],
                        generation_row[0],
                        generation_row[0],
                        generation_row[0],
                        generation_row[0],
                        generation_row[0],
                        manifest_id,
                    ),
                ).fetchone()
                if published_army is None:
                    raise ValueError("boundary army publication fence was lost")
                boundary_publication._maybe_emit_boundary_signal(database, connection, int(generation_row[0]))
            database._finish_claim(
                connection, claim, job, state="complete", outcome="processed"
            )


def _ensure_army_day_dependency(
    database: Database,
    connection: Any,
    ranked_day_start: datetime,
    *,
    ranked_version_ids: list[int] | None,
    allow_empty: bool,
    official_season_id: str,
) -> str:
    """Keep the completed-day gate without rebuilding legacy rollups."""
    ranked_day_start = ranked_day_start.astimezone(UTC)
    now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
    if ranked_day_start >= now or ranked_day_start + timedelta(days=1) > now:
        raise ValueError(
            "dependency_not_ready: army analytics day is not completed"
        )
    completed_filter = "AND state = 'Complete' AND coverage_complete"
    completed_params: tuple[Any, ...] = (ranked_day_start,)
    if ranked_version_ids is not None:
        completed_filter = "AND id = ANY(%s::bigint[])"
        completed_params = (ranked_day_start, ranked_version_ids)
    completed = connection.execute(
        f"""
        SELECT official_season_id
        FROM ranked_day_versions
        WHERE ranked_day_start = %s
          {completed_filter}
        ORDER BY version DESC
        LIMIT 1
        """,
        completed_params,
    ).fetchone()
    if completed is None:
        if not allow_empty:
            raise ValueError("dependency_not_ready: ranked day is not complete")
        return official_season_id
    return _text_value(completed[0])


def _build_army_facts(
    database: Database,
    connection: Any,
    ranked_day_str: str,
    *,
    member_ids: list[int] | None = None,
    ranked_version_ids: list[int] | None = None,
    decode_ids: list[int] | None = None,
    evidence_ids: list[int] | None = None,
    daily_log_ids: list[int] | None = None,
    battle_ids: list[int] | None = None,
) -> None:
    ranked_day_start = datetime.fromisoformat(ranked_day_str).astimezone(UTC)
    # Published daily logs own the canonical per-lens battle events;
    # ranked_day_versions contributes the battle-time starting trophies.
    version_filter = ""
    version_params: tuple[Any, ...] = ()
    daily_log_filter = ""
    daily_log_params: tuple[Any, ...] = ()
    ranked_state_filter = "AND rv.state = 'Complete' AND rv.coverage_complete"
    daily_state_filter = "AND d.state = 'Complete' AND d.coverage = 'complete'"
    if daily_log_ids is not None:
        daily_log_filter = "AND d.id = ANY(%s::bigint[])"
        daily_log_params = (daily_log_ids,)
        ranked_state_filter = ""
        daily_state_filter = ""
    if getattr(database, "_supports_coordinator_contract", False):
        version_filter = "AND (%s::bigint[] IS NULL OR d.ranked_day_version_id = ANY(%s::bigint[]))"
        version_params = (ranked_version_ids, ranked_version_ids)
    versions = connection.execute(
        f"""
        SELECT DISTINCT ON (d.player_id)
               rv.id, d.player_id, d.battles, d.official_season_id,
               d.season_day_number, rv.start_trophies
        FROM api_player_daily_logs AS d
        JOIN ranked_day_versions AS rv
          ON rv.player_id = d.player_id
         AND rv.ranked_day_start = d.ranked_day_start
         {ranked_state_filter}
        WHERE d.ranked_day_start = %s
          {daily_state_filter}
          AND (%s::bigint[] IS NULL OR d.player_id = ANY(%s::bigint[]))
          {version_filter}
          {daily_log_filter}
          AND (%s::bigint[] IS NULL OR rv.id = ANY(%s::bigint[]))
        ORDER BY d.player_id, d.version DESC, rv.version DESC
        """,
        (
            ranked_day_start,
            member_ids,
            member_ids,
            *version_params,
            *daily_log_params,
            ranked_version_ids,
            ranked_version_ids,
        ),
    ).fetchall()
    # Load every input relation once. The Python pass below only preserves
    # the existing event ordering and input-hash semantics; all writes are
    # bulk statements outside the per-event loop.
    streams: list[tuple[Any, ...]] = []
    pinned_battle_ids = set(battle_ids) if battle_ids is not None else None
    selected_battle_ids: set[int] = set()
    perspectives: set[str] = set()
    for version_id, player_id, battles, season_id, day_number, start_trophies in versions:
        if not isinstance(battles, list):
            continue
        events = [
            event
            for event in battles
            if isinstance(event, dict)
            and event.get("included") is not False
            and event.get("lens") in {"offense", "defense"}
            and str(event.get("battle_id", "")).isdigit()
            and (
                pinned_battle_ids is None
                or int(event["battle_id"]) in pinned_battle_ids
            )
        ]
        events.sort(
            key=lambda event: (
                str(event.get("battle_timestamp", "")),
                int(event["battle_id"]),
            )
        )
        streams.append(
            (version_id, player_id, season_id, day_number, start_trophies, events)
        )
        for event in events:
            selected_battle_ids.add(int(event["battle_id"]))
            perspectives.add(
                "attacker" if event["lens"] == "offense" else "defender"
            )
    battle_id_values = sorted(selected_battle_ids)
    perspective_values = sorted(perspectives)
    lens_values = [
        "offense" if perspective == "attacker" else "defense"
        for perspective in perspective_values
    ]
    decode_filter = "AND is_active"
    decode_params: tuple[Any, ...] = ()
    if decode_ids is not None:
        decode_filter = "AND id = ANY(%s::bigint[])"
        decode_params = (decode_ids,)
    decode_rows = connection.execute(
        f"""
        SELECT battle_id, perspective, id, evidence_id, status, failure_category,
               home_troops, spells, siege, cc_troops, heroes, unresolved_components
        FROM battle_army_decodes
        WHERE battle_id = ANY(%s::bigint[])
          AND perspective = ANY(%s::text[])
          AND decoder_version = %s AND catalog_version = %s
          {decode_filter}
        """,
        (
            battle_id_values,
            perspective_values,
            DECODER_VERSION,
            CATALOG_VERSION,
            *decode_params,
        ),
    ).fetchall()
    decodes = {
        (
            int(row[0]),
            "offense" if _text_value(row[1]) == "attacker" else "defense",
        ): row
        for row in decode_rows
    }
    evidence_rows = connection.execute(
        """
        SELECT p.battle_id, p.perspective, p.evidence_id,
               b.disagreement_state
        FROM legend_battles AS b
        JOIN battle_perspectives AS p ON p.battle_id = b.id
        WHERE b.id = ANY(%s::bigint[])
          AND p.perspective = ANY(%s::text[])
          AND (%s::bigint[] IS NULL OR p.evidence_id = ANY(%s::bigint[]))
        """,
        (battle_id_values, perspective_values, evidence_ids, evidence_ids),
    ).fetchall()
    evidence = {
        (
            int(row[0]),
            "offense" if _text_value(row[1]) == "attacker" else "defense",
        ): row
        for row in evidence_rows
    }
    fact_rows_by_key = connection.execute(
        """
        SELECT fact.battle_id, fact.lens, max(fact.version),
               current_fact.id, current_fact.input_hash
        FROM army_analytics_battle_facts AS fact
        LEFT JOIN army_analytics_battle_facts AS current_fact
          ON current_fact.battle_id = fact.battle_id
         AND current_fact.lens = fact.lens
         AND current_fact.is_current
        WHERE fact.battle_id = ANY(%s::bigint[])
          AND fact.lens = ANY(%s::text[])
        GROUP BY fact.battle_id, fact.lens, current_fact.id,
                 current_fact.input_hash
        """,
        (battle_id_values, lens_values),
    ).fetchall()
    fact_history = {
        (int(row[0]), _text_value(row[1])): {
            "latest_version": int(row[2]),
            "current": (
                {
                    "id": int(row[3]),
                    "input_hash": _text_value(row[4]),
                }
                if row[3] is not None
                else None
            ),
        }
        for row in fact_rows_by_key
    }
    active_keys: set[tuple[int, str]] = set()
    superseded_ids: list[int] = []
    fact_rows: list[dict[str, Any]] = []
    for version_id, player_id, season_id, day_number, start_trophies, events in streams:
        trophies = int(start_trophies) if start_trophies is not None else None
        for event in events:
            battle_id = int(event["battle_id"])
            lens = str(event["lens"])
            key = (battle_id, lens)
            evidence_row = evidence.get(key)
            if evidence_row is None:
                continue
            decode = decodes.get(key)
            state = (
                _text_value(decode[5])
                if decode and _text_value(decode[4]) == "failed" and decode[5]
                else _text_value(decode[4]) if decode else "decode_missing"
            )
            disagreement = _text_value(evidence_row[3]) == "disagreement"
            payload = {
                "source_ranked_day_version_id": int(version_id),
                "battle_id": battle_id,
                "lens": lens,
                "battle_time_trophies": trophies,
                "event": event,
                "decode_id": int(decode[2]) if decode else None,
                "perspective_disagreement": disagreement,
            }
            input_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            active_keys.add(key)
            history = fact_history.get(key)
            existing = history["current"] if history is not None else None
            if existing is not None and existing["input_hash"] == input_hash:
                change = event.get("trophy_change")
                if isinstance(change, bool) or not isinstance(change, int):
                    trophies = None
                elif trophies is not None:
                    trophies += change
                continue
            supersedes = existing["id"] if existing is not None else None
            if supersedes is not None:
                superseded_ids.append(supersedes)
            fact_rows.append(
                {
                    "battle_id": battle_id,
                    "evidence_id": int(evidence_row[2]),
                    "decode_id": int(decode[2]) if decode else None,
                    "source_ranked_day_version_id": int(version_id),
                    "official_season_id": _text_value(season_id),
                    "season_day_number": int(day_number),
                    "lens": lens,
                    "population_player_id": int(player_id),
                    "battle_time_trophies": trophies,
                    "stars": int(event.get("stars") or 0),
                    "destruction_percentage": int(
                        event.get("destruction_percentage") or 0
                    ),
                    "army_state": state,
                    "failure_reason": (
                        _text_value(decode[5]) if decode and decode[5] else None
                    ),
                    "home_troops": (decode[6] or []) if decode else [],
                    "spells": (decode[7] or []) if decode else [],
                    "siege": (decode[8] or []) if decode else [],
                    "cc_troops": (decode[9] or []) if decode else [],
                    "heroes": (decode[10] or []) if decode else [],
                    "unresolved_components": (
                        (decode[11] or []) if decode else []
                    ),
                    "perspective_disagreement": disagreement,
                    "input_hash": input_hash,
                    "version": (
                        history["latest_version"] + 1
                        if history is not None
                        else 1
                    ),
                    "supersedes_id": supersedes,
                }
            )
            change = event.get("trophy_change")
            if isinstance(change, bool) or not isinstance(change, int):
                trophies = None
            elif trophies is not None:
                trophies += change
    if superseded_ids:
        connection.execute(
            "UPDATE army_analytics_battle_facts SET is_current=false WHERE id = ANY(%s::bigint[])",
            (superseded_ids,),
        )
    if fact_rows:
        connection.execute(
            """
            INSERT INTO army_analytics_battle_facts (
                battle_id, evidence_id, decode_id, source_ranked_day_version_id,
                ranked_day_start, official_season_id, season_day_number, lens,
                population_player_id, battle_time_trophies, stars,
                destruction_percentage, army_state, failure_reason, home_troops,
                spells, siege, cc_troops, heroes, unresolved_components,
                perspective_disagreement, input_hash, version, supersedes_id
            )
            SELECT row.battle_id, row.evidence_id, row.decode_id,
                   row.source_ranked_day_version_id, %s, row.official_season_id,
                   row.season_day_number, row.lens, row.population_player_id,
                   row.battle_time_trophies, row.stars, row.destruction_percentage,
                   row.army_state, row.failure_reason, row.home_troops,
                   row.spells, row.siege, row.cc_troops, row.heroes,
                   row.unresolved_components, row.perspective_disagreement,
                   row.input_hash, row.version, row.supersedes_id
            FROM jsonb_to_recordset(%s::jsonb) AS row(
                battle_id bigint, evidence_id bigint, decode_id bigint,
                source_ranked_day_version_id bigint, official_season_id text,
                season_day_number integer, lens text, population_player_id bigint,
                battle_time_trophies integer, stars integer,
                destruction_percentage integer, army_state text,
                failure_reason text, home_troops jsonb, spells jsonb,
                siege jsonb, cc_troops jsonb, heroes jsonb,
                unresolved_components jsonb, perspective_disagreement boolean,
                input_hash text, version integer, supersedes_id bigint
            )
            """,
            (ranked_day_start, Jsonb(fact_rows)),
        )
    connection.execute(
        """
        UPDATE army_analytics_battle_facts AS fact
        SET is_current = false
        WHERE fact.ranked_day_start = %s AND fact.is_current
          AND NOT EXISTS (
              SELECT 1
              FROM unnest(%s::bigint[], %s::text[]) AS active(battle_id, lens)
              WHERE active.battle_id = fact.battle_id
                AND active.lens = fact.lens
          )
        """,
        (
            ranked_day_start,
            [battle_id for battle_id, _lens in active_keys],
            [lens for _battle_id, lens in active_keys],
        ),
    )
    # Durable per-day completion marker, atomic with the facts above.
    marker_filter = "AND state = 'Complete' AND coverage = 'complete'"
    marker_params: tuple[Any, ...] = (ranked_day_start,)
    if ranked_version_ids is not None:
        marker_filter = "AND ranked_day_version_id = ANY(%s::bigint[])"
        marker_params = (ranked_day_start, ranked_version_ids)
    marker_day = connection.execute(
        f"""
        SELECT DISTINCT official_season_id, season_day_number
        FROM api_player_daily_logs
        WHERE ranked_day_start = %s
          {marker_filter}
        LIMIT 1
        """,
        marker_params,
    ).fetchone()
    if marker_day is not None:
        marker_rows = connection.execute(
            """
            SELECT battle_id, lens, input_hash
            FROM army_analytics_battle_facts
            WHERE ranked_day_start = %s AND is_current
            ORDER BY battle_id, lens
            """,
            (ranked_day_start,),
        ).fetchall()
        marker_hash = hashlib.sha256(
            json.dumps(
                [
                    [int(r[0]), _text_value(r[1]), _text_value(r[2])]
                    for r in marker_rows
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO army_analytics_completed_days (
                ranked_day_start, official_season_id, season_day_number,
                fact_input_hash
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (ranked_day_start) DO UPDATE SET
                official_season_id = EXCLUDED.official_season_id,
                season_day_number = EXCLUDED.season_day_number,
                fact_input_hash = EXCLUDED.fact_input_hash,
                completed_at = clock_timestamp()
            """,
            (
                ranked_day_start,
                _text_value(marker_day[0]),
                int(marker_day[1]),
                marker_hash,
            ),
        )
        # A late correction refreshes an already-summarized season.
        # Seasons without summaries (live seasons stay on explicit
        # preview-first backfill) cost one season lock plus one existence
        # lookup; summarized seasons pay one projection per lens per day build (fact scan
        # plus per-category upserts, writes skipped when digests match).
        # Each lens refreshes in a savepoint so a projection failure
        # warns without rolling back the day facts/marker above.
        _refresh_army_season_summaries(database, 
            connection, _text_value(marker_day[0])
        )


def _refresh_army_season_summaries(
    database, connection: Any, season_id: str
) -> None:
    """Refresh whole-season army summaries without risking the caller.

    The caller is the enclosing day build: its facts and completion
    marker are already written in the same transaction. Both lens
    writer locks are held before the existence check so a first
    backfill and a concurrent correction still serialize: either the
    backfill projects the committed correction, or the correction
    waits and then refreshes the newly published summary. Each lens
    refreshes in a savepoint, so a projection failure rolls back only
    that lens refresh, warns observably, and leaves the day build
    green; the other lens still refreshes. Seasons with no summaries
    yet cost three locks plus one existence lookup.
    """
    if not getattr(database, "_supports_army_season_summaries", False):
        return
    from .season_retirement import (
        acquire_season_lock_shared,
        is_season_detail_retired,
    )

    # Retired detail stays retired: a late day build must not replace
    # verified summaries from a reduced post-retirement sample.
    acquire_season_lock_shared(connection, season_id)
    if is_season_detail_retired(connection, season_id):
        return
    # The shared season lock does not hold back a racing first
    # materialization, so the existence probe below runs under both
    # lens writer locks: an in-flight backfill either commits first
    # (and the probe sees its summaries) or waits until this refresh
    # decides, and the later materialization then sees the committed
    # facts either way.
    for summary_lens in ("offense", "defense"):
        acquire_army_season_lock(connection, season_id, summary_lens)
    summarized = connection.execute(
        """
        SELECT 1 FROM army_season_summaries
        WHERE official_season_id = %s LIMIT 1
        """,
        (season_id,),
    ).fetchone()
    if summarized is None:
        return
    for summary_lens in ("offense", "defense"):
        try:
            with connection.transaction():
                materialize_army_season(
                    connection,
                    season_id,
                    summary_lens,
                )
        except Exception:  # noqa: BLE001 - day facts/marker stay durable; warn, keep the day build green
            warnings.warn(
                "army_season_summary_refresh_failed:"
                f"{season_id}:{summary_lens}",
                RuntimeWarning,
                stacklevel=2,
            )


def _season_metadata_for_ranked_day(
    database, connection: Any, ranked_day_start: datetime
) -> tuple[str, int]:
    ranked_day_start = ranked_day_start.astimezone(UTC)
    anchor = connection.execute(
        """
        SELECT current_league_season_id, previous_league_season_id,
               current_start, previous_start
        FROM legend_season_anchors
        WHERE state = 'confirmed' AND anchor_rule_version = %s
        """,
        (SEASON_ANCHOR_RULE_VERSION,),
    ).fetchone()
    if anchor is None or ranked_day_start < anchor[3]:
        raise ValueError(
            "dependency_not_ready: confirmed season anchor is unavailable"
        )
    if ranked_day_start >= anchor[2]:
        season_id, season_start = _text_value(anchor[0]), anchor[2]
    else:
        season_id, season_start = _text_value(anchor[1]), anchor[3]
    season_day = (ranked_day_start - season_start).days + 1
    if not 1 <= season_day <= 28:
        raise ValueError(
            "dependency_not_ready: ranked day is outside confirmed season"
        )
    return season_id, season_day


def complete_army_redecode(database: Database, claim: Claim) -> None:
    with database.pool.connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"army-redecode:{claim.job_id}",),
            )
            battle_ids: list[int] = []
            bid = claim.input_json.get("battle_id")
            if bid is not None:
                battle_ids.append(int(bid))
            bids = claim.input_json.get("battle_ids")
            if isinstance(bids, list) and bids:
                for x in bids[:100]:
                    try:
                        battle_ids.append(int(x))
                    except (TypeError, ValueError) as e:
                        raise ValueError(f"battle_ids must be integers: {e}") from e
            if not battle_ids:
                raise ValueError("redecode requires battle_id or battle_ids")
            if len(battle_ids) > 100:
                raise ValueError("redecode batch limited to 100")
            from .season_retirement import (
                SEASON_DETAIL_RETIRED,
                acquire_season_lock_shared,
                is_season_detail_retired,
            )

            seasons = [
                _text_value(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT ranked.official_season_id
                    FROM legend_battles AS battle
                    JOIN ranked_day_versions AS ranked
                      ON ranked.ranked_day_start = battle.ranked_day_start
                    WHERE battle.id = ANY(%s::bigint[])
                    """,
                    (sorted(set(battle_ids)),),
                ).fetchall()
            ]
            if not seasons:
                raise ValueError("redecode season metadata is unavailable")
            for season_id in sorted(set(seasons)):
                acquire_season_lock_shared(connection, season_id)
                if is_season_detail_retired(connection, season_id):
                    raise DomainRuleError(
                        SEASON_DETAIL_RETIRED,
                        f"season {season_id} detail is retired",
                    )
            # Two redecodes for one battle must not interleave: the
            # deactivate-then-insert sequence would otherwise race the
            # one-active-per-perspective unique index into a transaction
            # abort. Different battles redecode concurrently.
            for battle_id in sorted(set(battle_ids)):
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"army-redecode-battle:{battle_id}",),
                )
            _upsert_army_decodes(database, connection, battle_ids)
            database._finish_claim(
                connection, claim, job, state="complete", outcome="processed"
            )


