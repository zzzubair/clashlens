from __future__ import annotations

import hashlib
import json
import warnings
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import battle_day_repair, boundary, boundary_publication, reset_settlement
from .army_decoder import (
    DECODER_VERSION,
    DecodedArmy,
    DecodeFailure,
    decode_army_share_code,
)
from .army_season_summaries import (
    acquire_army_season_lock,
    materialize_army_season,
    store_army_day_totals,
)
from .catalog import CATALOG_HASH, CATALOG_VERSION
from .db import Claim, Database, _text_value
from .domain import SEASON_ANCHOR_RULE_VERSION, DomainRuleError, anchored_ranked_day

ARMY_FACT_PLAYER_BATCH = 500


def _decode_is_current(
    active: Any, evidence_id: int, raw_code: Any, decoded: DecodedArmy | DecodeFailure
) -> bool:
    """Whether the active decode row (id, evidence_id, raw_code, identity_hash,
    status, failure_category) already records this evidence's result."""
    if active is None or int(active[1]) != int(evidence_id):
        return False
    if (active[2] is None) != (raw_code is None) or (
        raw_code is not None and _text_value(active[2]) != _text_value(raw_code)
    ):
        return False
    if isinstance(decoded, DecodedArmy):
        return (
            active[3] is None
            if decoded.identity_hash is None
            else _text_value(active[3]) == decoded.identity_hash
        ) and _text_value(active[4]) == decoded.status
    return _text_value(active[5]) == decoded.category


def _upsert_army_decodes(
    database: Database,
    connection: Any,
    battle_ids: list[int],
    *,
    reset_baseline: tuple[int, int, datetime] | None = None,
    observation_id: int | None = None,
    reset_lock_wait: str | None = None,
    publication_boundaries: Collection[datetime] = (),
) -> None:
    """Save the battles' army decodes. ``publication_boundaries`` are Resets
    whose publication locks the caller needs after this, taken in the same
    oldest-first pass even when no decode changes."""
    if not battle_ids and not publication_boundaries:
        return
    exists = connection.execute("SELECT to_regclass('battle_army_decodes')").fetchone()
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
    decoded_rows = []
    for battle_id, evidence_id, perspective, raw_code, source_json in rows:
        source_code = (
            source_json.get("armyShareCode") if isinstance(source_json, dict) else None
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
        decoded_rows.append(
            (battle_id, evidence_id, _text_value(perspective), raw_code, decoded)
        )
    current = {
        (
            int(row[0]),
            _text_value(row[1]),
            _text_value(row[2]),
            _text_value(row[3]),
        ): row[4:]
        for row in connection.execute(
            """
            SELECT battle_id, perspective, decoder_version, catalog_version,
                   id, evidence_id, raw_code, identity_hash, status, failure_category
            FROM battle_army_decodes
            WHERE battle_id = ANY(%s::bigint[]) AND is_active
            """,
            (battle_ids,),
        ).fetchall()
    }
    decoded_rows = [
        row
        for row in decoded_rows
        if not _decode_is_current(
            current.get(
                (row[0], row[2], row[4].decoder_version, row[4].catalog_version)
            ),
            row[1],
            row[3],
            row[4],
        )
    ]
    # Most battle logs repeat battles whose decodes are already saved. Those
    # change nothing a Reset publishes, so they skip the Reset locks below,
    # which every battle log for the same Legend day would otherwise queue on.
    if not decoded_rows and not publication_boundaries:
        return
    # Lock order everywhere: battle locks, then a Reset baseline's work lock,
    # then Reset publication locks, then Reset settlement locks, then army
    # rows. A battle lock keeps two jobs saving one battle's armies from
    # interleaving, so neither replaces the other's newer evidence or races
    # the one-active-per-perspective unique index. Different battles save
    # concurrently.
    for battle_id in sorted({row[0] for row in decoded_rows}):
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"army-redecode-battle:{battle_id}",),
        )
    players_by_day: dict[datetime, set[int]] = {}
    for day_start, attacker_id, defender_id in connection.execute(
        """
        SELECT ranked_day_start, attacker_player_id, defender_player_id
        FROM legend_battles WHERE id = ANY(%s::bigint[])
        """,
        (sorted({row[0] for row in decoded_rows}),),
    ).fetchall():
        players_by_day.setdefault(day_start.astimezone(UTC), set()).update(
            (int(attacker_id), int(defender_id))
        )
    # A Reset battle log records its baseline and re-judges its Resets after
    # these army writes, so it takes all of them first; otherwise it could
    # hold an army or generation row another job needs while that job holds a
    # lock. A Reset with no sweep yet is only shared, so battle logs for the
    # current Legend day do not queue behind each other, and stays shared for
    # this transaction: two jobs upgrading their shared locks would deadlock.
    boundaries = {day_start + timedelta(days=1) for day_start in players_by_day} | {
        boundary_at.astimezone(UTC) for boundary_at in publication_boundaries
    }
    resets: list[tuple[int, datetime]] = []
    if reset_lock_wait is not None:
        # Give up on a busy Reset after this wait; the whole transaction rolls
        # back and the job retries later rather than holding its rows.
        previous_wait = connection.execute(
            "SELECT current_setting('lock_timeout')"
        ).fetchone()[0]
        connection.execute(
            "SELECT set_config('lock_timeout', %s, true)", (reset_lock_wait,)
        )
    if reset_baseline is not None:
        work_id, player_id, boundary_at = reset_baseline
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"reset-baseline:{work_id}",),
        )
        boundaries.add(boundary_at.astimezone(UTC))
        resets.append((player_id, boundary_at))
    unswept: set[datetime] = set()
    for boundary_at in sorted(boundaries):
        if not boundary.lock_boundary_publication_once_swept(connection, boundary_at):
            unswept.add(boundary_at)
    reset_settlement.lock_resets(database, connection, observation_id, resets)
    if reset_lock_wait is not None:
        connection.execute(
            "SELECT set_config('lock_timeout', %s, true)", (previous_wait,)
        )
    current_evidence = {
        (int(battle_id), _text_value(perspective)): int(evidence_id)
        for battle_id, perspective, evidence_id in connection.execute(
            """
            SELECT battle_id, perspective, evidence_id
            FROM battle_perspectives
            WHERE battle_id = ANY(%s::bigint[])
            """,
            (sorted({row[0] for row in decoded_rows}),),
        ).fetchall()
    }
    decoded_rows = [
        row for row in decoded_rows if current_evidence.get((row[0], row[2])) == row[1]
    ]
    if not decoded_rows:
        return
    # Write shared exact_armies rows in one fixed order so two battle logs that
    # share armies cannot each hold one and wait for the other (deadlock).
    decoded_rows.sort(key=lambda row: getattr(row[4], "identity_hash", None) or "")
    for battle_id, evidence_id, perspective, raw_code, decoded in decoded_rows:
        # Recheck under the locks: the job that held them may have saved it.
        active = connection.execute(
            "SELECT id, evidence_id, raw_code, identity_hash, status, failure_category FROM battle_army_decodes WHERE battle_id = %s AND perspective = %s AND decoder_version = %s AND catalog_version = %s AND is_active = true",
            (
                battle_id,
                perspective,
                decoded.decoder_version,
                decoded.catalog_version,
            ),
        ).fetchone()
        if _decode_is_current(active, evidence_id, raw_code, decoded):
            continue
        supersedes = None
        if active is not None:
            connection.execute(
                "UPDATE battle_army_decodes SET is_active = false WHERE id = %s",
                (active[0],),
            )
            supersedes = int(active[0])
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
                    Jsonb([(f.typed_id, f.quantity, f.origin) for f in decoded.spells]),
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
                    Jsonb([(f.typed_id, f.quantity, f.origin) for f in decoded.siege]),
                    Jsonb(
                        [(f.typed_id, f.quantity, f.origin) for f in decoded.cc_troops]
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
    for day_start, player_ids in sorted(players_by_day.items()):
        boundary_publication._enqueue_army_analytics(
            database,
            connection,
            ranked_day_start=day_start,
            player_ids=sorted(player_ids),
            swept=day_start + timedelta(days=1) not in unswept,
        )


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
                boundary_at = datetime.fromisoformat(str(boundary_text)).astimezone(UTC)
                generation_row = connection.execute(
                    """
                    SELECT id, generation, sweep_id, snapshot_state, army_state,
                           army_manifest_id, army_rule_version, target_at, target_rule
                    FROM boundary_publication_generations
                    WHERE boundary_at = %s AND generation = %s
                    """,
                    (boundary_at, int(generation_input)),
                ).fetchone()
                if generation_row is None:
                    raise ValueError("boundary publication generation does not exist")
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
                    raise ValueError("boundary army generation is stale or superseded")
                if _text_value(generation_row[3]) == "superseded":
                    raise ValueError("boundary publication generation is superseded")
                manifest_id = (
                    int(generation_row[5]) if generation_row[5] is not None else None
                )
                if (
                    manifest_id is None
                    or int(claim.input_json.get("manifest_id", 0)) != manifest_id
                ):
                    raise ValueError("boundary army manifest identity does not match")
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
                    raise ValueError("army publication target dependency is not ready")
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
                    season_id, _season_day = _season_metadata_for_ranked_day(
                        database, connection, boundary_at - timedelta(days=1)
                    )
                else:
                    season_id = _text_value(season_id_row[0])
            else:
                ranked_day_str = claim.input_json.get("ranked_day_start")
                season_id = claim.input_json.get("official_season_id")
            if ranked_day_str is None or season_id is None:
                raise ValueError("army analytics requires ranked-day and season inputs")
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
            # The generation row is read without a lock. The snapshot and
            # analytics builds lock that row, so holding it for the whole
            # fact build made them wait; the publish update below locks it
            # only at the end and fences on the state read here.
            manifest_versions = None
            if generation_row is not None:
                manifest_versions = [
                    int(row[0])
                    for row in connection.execute(
                        """
                        SELECT ranked_day_version_id
                        FROM boundary_publication_manifest_entries(%s)
                        WHERE ranked_day_version_id IS NOT NULL
                        ORDER BY ordinal
                        """,
                        (manifest_id,),
                    ).fetchall()
                ]
            ranked_day_start = datetime.fromisoformat(str(ranked_day_str)).astimezone(
                UTC
            )
            # No two army day builds interleave, for any days: fact
            # versions are computed as latest+1 and the day sweep marks
            # is_current, and a battle moved across a Reset replaces
            # another day's current fact and saved totals while that
            # day's build may be counting and saving them. One shared
            # lock cannot deadlock; a day build takes about 80 seconds.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("army-facts",),
            )
            # A build of this generation that held the lock may have
            # published it meanwhile.
            if generation_row is not None and _text_value(
                connection.execute(
                    "SELECT army_state FROM boundary_publication_generations"
                    " WHERE id = %s",
                    (generation_row[0],),
                ).fetchone()[0]
            ) in {"superseded", "published"}:
                database._finish_claim(
                    connection, claim, job, state="complete", outcome="stale_superseded"
                )
                return
            season_id = _ensure_army_day_dependency(
                database,
                connection,
                ranked_day_start,
                ranked_version_ids=manifest_versions,
                allow_empty=generation_row is not None,
                official_season_id=str(season_id),
            )
            _build_army_facts(
                database,
                connection,
                str(ranked_day_str),
                manifest_id=manifest_id if generation_row is not None else None,
                ranked_version_ids=manifest_versions,
            )
            if generation_row is not None:
                boundary.lock_boundary_publication(connection, boundary_at)
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
                    WHERE id = %s AND army_state IN ('ready', 'building')
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
                boundary_publication._maybe_emit_boundary_signal(
                    database, connection, int(generation_row[0])
                )
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
        raise ValueError("dependency_not_ready: army analytics day is not completed")
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
    manifest_id: int | None = None,
    member_ids: list[int] | None = None,
    ranked_version_ids: list[int] | None = None,
    decode_ids: list[int] | None = None,
    evidence_ids: list[int] | None = None,
    daily_log_ids: list[int] | None = None,
    battle_ids: list[int] | None = None,
) -> None:
    ranked_day_start = datetime.fromisoformat(ranked_day_str).astimezone(UTC)
    active_keys: set[tuple[int, str]] = set()
    if manifest_id is not None:
        _build_manifest_army_facts(
            database, connection, ranked_day_start, manifest_id, active_keys
        )
    else:
        _build_listed_army_facts(
            database,
            connection,
            ranked_day_start,
            active_keys,
            member_ids=member_ids,
            ranked_version_ids=ranked_version_ids,
            decode_ids=decode_ids,
            evidence_ids=evidence_ids,
            daily_log_ids=daily_log_ids,
            battle_ids=battle_ids,
        )
    _finish_army_fact_day(
        database, connection, ranked_day_start, ranked_version_ids, active_keys
    )


def _build_manifest_army_facts(
    database: Database,
    connection: Any,
    ranked_day_start: datetime,
    manifest_id: int,
    active_keys: set[tuple[int, str]],
) -> None:
    """Build facts from a frozen manifest, one page of manifest rows at a time.

    Each page reads only its own players' frozen daily log, ranked-day
    version, battle, decode and evidence IDs. Passing the whole day's ID
    lists to every page made each lookup sift the full day again.
    """
    version_filter = (
        "AND d.ranked_day_version_id = m.ranked_day_version_id"
        if getattr(database, "_supports_coordinator_contract", False)
        else ""
    )
    # Pages are ordinal ranges: rebuilding a reused proof's rows in order
    # to take the next 500 would rebuild every remaining row for each page.
    last_ordinal = connection.execute(
        "SELECT max(ordinal) FROM boundary_publication_manifest_entries(%s)",
        (manifest_id,),
    ).fetchone()[0]
    for after_ordinal in range(0, last_ordinal or 0, ARMY_FACT_PLAYER_BATCH):
        rows = connection.execute(
            f"""
            SELECT m.ordinal, rv.id, d.player_id, d.battles,
                   d.official_season_id, d.season_day_number, rv.start_trophies,
                   m.input_identity -> 'battle_ids',
                   m.input_identity -> 'decode_ids',
                   m.input_identity -> 'evidence_ids'
            FROM boundary_publication_manifest_entries(%s) AS m
            LEFT JOIN api_player_daily_logs AS d
              ON d.id = (m.input_identity ->> 'daily_log_id')::bigint
             AND d.player_id = m.player_id
             AND d.ranked_day_start = %s
             {version_filter}
            LEFT JOIN ranked_day_versions AS rv
              ON rv.id = m.ranked_day_version_id
             AND rv.player_id = d.player_id
             AND rv.ranked_day_start = d.ranked_day_start
            WHERE m.ordinal > %s AND m.ordinal <= %s
            ORDER BY m.ordinal
            """,
            (
                manifest_id,
                ranked_day_start,
                after_ordinal,
                after_ordinal + ARMY_FACT_PLAYER_BATCH,
            ),
        ).fetchall()
        if not rows:
            continue
        battle_ids, decode_ids, evidence_ids = (
            sorted(
                {
                    int(value)
                    for row in rows
                    if isinstance(row[column], list)
                    for value in row[column]
                }
            )
            for column in (7, 8, 9)
        )
        _build_army_fact_batch(
            connection,
            ranked_day_start,
            [row[1:7] for row in rows if row[1] is not None],
            battle_ids=battle_ids,
            decode_ids=decode_ids,
            evidence_ids=evidence_ids,
            active_keys=active_keys,
        )


def _build_listed_army_facts(
    database: Database,
    connection: Any,
    ranked_day_start: datetime,
    active_keys: set[tuple[int, str]],
    *,
    member_ids: list[int] | None,
    ranked_version_ids: list[int] | None,
    decode_ids: list[int] | None,
    evidence_ids: list[int] | None,
    daily_log_ids: list[int] | None,
    battle_ids: list[int] | None,
) -> None:
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
        version_filter = (
            "AND (%s::bigint[] IS NULL OR d.ranked_day_version_id = ANY(%s::bigint[]))"
        )
        version_params = (ranked_version_ids, ranked_version_ids)
    # A Legend day has about 183,000 facts for 13,000 players. Building them
    # in player batches keeps the worker's memory and each insert bounded;
    # the whole day still commits as one transaction.
    after_player_id = 0
    while True:
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
              AND d.player_id > %s
              {daily_state_filter}
              AND (%s::bigint[] IS NULL OR d.player_id = ANY(%s::bigint[]))
              {version_filter}
              {daily_log_filter}
              AND (%s::bigint[] IS NULL OR rv.id = ANY(%s::bigint[]))
            ORDER BY d.player_id, d.version DESC, rv.version DESC
            LIMIT %s
            """,
            (
                ranked_day_start,
                after_player_id,
                member_ids,
                member_ids,
                *version_params,
                *daily_log_params,
                ranked_version_ids,
                ranked_version_ids,
                ARMY_FACT_PLAYER_BATCH,
            ),
        ).fetchall()
        if versions:
            after_player_id = int(versions[-1][1])
            _build_army_fact_batch(
                connection,
                ranked_day_start,
                versions,
                battle_ids=battle_ids,
                decode_ids=decode_ids,
                evidence_ids=evidence_ids,
                active_keys=active_keys,
            )
        if len(versions) < ARMY_FACT_PLAYER_BATCH:
            break


# A completed day's marker hash: SHA-256 of the day's current
# [battle_id,"lens","input_hash"] list as compact JSON, built in PostgreSQL
# without sending every fact back.
_DAY_FACT_INPUT_HASH = """
    SELECT encode(sha256(convert_to(
               '[' || COALESCE(string_agg(
                   '[' || battle_id || ',"' || lens || '","'
                       || input_hash || '"]',
                   ',' ORDER BY battle_id, lens
               ), '') || ']',
               'UTF8'
           )), 'hex')
    FROM army_analytics_battle_facts
    WHERE ranked_day_start = %s AND is_current
"""


def _finish_army_fact_day(
    database: Database,
    connection: Any,
    ranked_day_start: datetime,
    ranked_version_ids: list[int] | None,
    active_keys: set[tuple[int, str]],
) -> None:
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
    if marker_day is None:
        # Totals from an earlier build of this day no longer describe it.
        connection.execute(
            "DELETE FROM army_analytics_day_totals WHERE ranked_day_start = %s",
            (ranked_day_start,),
        )
        connection.execute(
            "DELETE FROM army_analytics_rank_band_totals WHERE ranked_day_start = %s",
            (ranked_day_start,),
        )
    else:
        marker = connection.execute(
            f"""
            INSERT INTO army_analytics_completed_days (
                ranked_day_start, official_season_id, season_day_number,
                fact_input_hash
            )
            VALUES (%s, %s, %s, ({_DAY_FACT_INPUT_HASH}))
            ON CONFLICT (ranked_day_start) DO UPDATE SET
                official_season_id = EXCLUDED.official_season_id,
                season_day_number = EXCLUDED.season_day_number,
                fact_input_hash = EXCLUDED.fact_input_hash,
                completed_at = clock_timestamp()
            RETURNING fact_input_hash
            """,
            (
                ranked_day_start,
                _text_value(marker_day[0]),
                int(marker_day[1]),
                ranked_day_start,
            ),
        ).fetchone()
        store_army_day_totals(
            connection,
            ranked_day_start,
            _text_value(marker_day[0]),
            int(marker_day[1]),
            _text_value(marker[0]),
        )
        # A late correction refreshes an already-summarized season.
        # Seasons without summaries (live seasons stay on explicit
        # preview-first backfill) cost one season lock plus one existence
        # lookup; summarized seasons pay one projection per lens per day build
        # (adding up stored day totals plus per-category upserts, writes
        # skipped when digests match).
        # Each lens refreshes in a savepoint so a projection failure
        # warns without rolling back the day facts/marker above.
        _refresh_army_season_summaries(database, connection, _text_value(marker_day[0]))


def _build_army_fact_batch(
    connection: Any,
    ranked_day_start: datetime,
    versions: list[Any],
    *,
    battle_ids: list[int] | None,
    decode_ids: list[int] | None,
    evidence_ids: list[int] | None,
    active_keys: set[tuple[int, str]],
) -> None:
    # Load every input relation of the batch once. The Python pass below only
    # preserves the existing event ordering and input-hash semantics; all
    # writes are bulk statements outside the per-event loop.
    streams: list[tuple[Any, ...]] = []
    pinned_battle_ids = set(battle_ids) if battle_ids is not None else None
    selected_battle_ids: set[int] = set()
    perspectives: set[str] = set()
    for (
        version_id,
        player_id,
        battles,
        season_id,
        day_number,
        start_trophies,
    ) in versions:
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
            perspectives.add("attacker" if event["lens"] == "offense" else "defender")
    # A frozen report 0057 moved is read, decoded and counted where it is now,
    # as is the moved report of a side the frozen inputs list none for.
    moved, moved_reports = (
        battle_day_repair.merged_battles(
            connection, sorted(selected_battle_ids), evidence_ids
        )
        if evidence_ids is not None
        else ({}, [])
    )
    selected_battle_ids.update(moved.values())
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
        SELECT battle_id, perspective, id, evidence_id, status, failure_category
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
    if evidence_ids is None:
        evidence_rows = connection.execute(
            """
            SELECT p.battle_id, p.perspective, p.evidence_id,
                   b.disagreement_state
            FROM legend_battles AS b
            JOIN battle_perspectives AS p ON p.battle_id = b.id
            WHERE b.id = ANY(%s::bigint[])
              AND p.perspective = ANY(%s::text[])
            """,
            (battle_id_values, perspective_values),
        ).fetchall()
    else:
        # A frozen manifest names the evidence it promised to use. Read those
        # saved records, not the current pointer a later report may have moved.
        # The battle and perspective conditions keep the battle/perspective
        # index usable; the IDs alone read every evidence row from disk.
        evidence_rows = connection.execute(
            """
            SELECT battle_id, perspective, id, NULL
            FROM battle_evidence
            WHERE battle_id = ANY(%s::bigint[])
              AND perspective = ANY(%s::text[])
              AND id = ANY(%s::bigint[])
            """,
            (
                battle_id_values,
                perspective_values,
                [*evidence_ids, *moved_reports],
            ),
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
    superseded_ids: list[int] = []
    fact_rows: list[dict[str, Any]] = []
    for version_id, player_id, season_id, day_number, start_trophies, events in streams:
        trophies = int(start_trophies) if start_trophies is not None else None
        for event in events:
            lens = str(event["lens"])
            battle_id = moved.get(
                (int(event["battle_id"]), lens), int(event["battle_id"])
            )
            key = (battle_id, lens)
            evidence_row = evidence.get(key)
            if evidence_row is None:
                if evidence_ids is not None:
                    raise ValueError(
                        f"frozen army evidence missing for battle {battle_id} {lens}"
                    )
                continue
            decode = decodes.get(key)
            state = (
                _text_value(decode[5])
                if decode and _text_value(decode[4]) == "failed" and decode[5]
                else _text_value(decode[4])
                if decode
                else "decode_missing"
            )
            disagreement = (
                event.get("disagreement") is True
                if evidence_ids is not None
                else _text_value(evidence_row[3]) == "disagreement"
            )
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
                    "perspective_disagreement": disagreement,
                    "input_hash": input_hash,
                    "version": (
                        history["latest_version"] + 1 if history is not None else 1
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
        other_days = connection.execute(
            """
            WITH superseded AS (
                UPDATE army_analytics_battle_facts SET is_current = false
                WHERE id = ANY(%s::bigint[])
                RETURNING ranked_day_start
            ), other_days AS (
                SELECT DISTINCT ranked_day_start FROM superseded
                WHERE ranked_day_start <> %s
            ), day_totals AS (
                DELETE FROM army_analytics_day_totals
                WHERE ranked_day_start IN (SELECT ranked_day_start FROM other_days)
            ), rank_band_totals AS (
                DELETE FROM army_analytics_rank_band_totals
                WHERE ranked_day_start IN (SELECT ranked_day_start FROM other_days)
            )
            SELECT ranked_day_start FROM other_days
            """,
            (superseded_ids, ranked_day_start),
        ).fetchall()
        # A battle that left another day changes that day's facts, so its
        # completion marker is recomputed with them.
        for (other_day,) in other_days:
            connection.execute(
                f"""
                UPDATE army_analytics_completed_days
                SET fact_input_hash = ({_DAY_FACT_INPUT_HASH})
                WHERE ranked_day_start = %s
                """,
                (other_day, other_day),
            )
    if fact_rows:
        connection.execute(
            """
            INSERT INTO army_analytics_battle_facts (
                battle_id, evidence_id, decode_id, source_ranked_day_version_id,
                ranked_day_start, official_season_id, season_day_number, lens,
                population_player_id, battle_time_trophies, stars,
                destruction_percentage, army_state, failure_reason,
                perspective_disagreement, input_hash, version, supersedes_id
            )
            SELECT row.battle_id, row.evidence_id, row.decode_id,
                   row.source_ranked_day_version_id, %s, row.official_season_id,
                   row.season_day_number, row.lens, row.population_player_id,
                   row.battle_time_trophies, row.stars, row.destruction_percentage,
                   row.army_state, row.failure_reason,
                   row.perspective_disagreement, row.input_hash, row.version,
                   row.supersedes_id
            FROM jsonb_to_recordset(%s::jsonb) AS row(
                battle_id bigint, evidence_id bigint, decode_id bigint,
                source_ranked_day_version_id bigint, official_season_id text,
                season_day_number integer, lens text, population_player_id bigint,
                battle_time_trophies integer, stars integer,
                destruction_percentage integer, army_state text,
                failure_reason text, perspective_disagreement boolean,
                input_hash text, version integer, supersedes_id bigint
            )
            """,
            (ranked_day_start, Jsonb(fact_rows)),
        )


def _refresh_army_season_summaries(database, connection: Any, season_id: str) -> None:
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
                f"army_season_summary_refresh_failed:{season_id}:{summary_lens}",
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
        raise ValueError("dependency_not_ready: confirmed season anchor is unavailable")
    try:
        day = anchored_ranked_day(
            ranked_day_start, _text_value(anchor[0]), _text_value(anchor[1])
        )
    except DomainRuleError as error:
        raise ValueError("dependency_not_ready: confirmed season anchor is off phase") from error
    return day.official_season_id, day.day_number


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
            _upsert_army_decodes(database, connection, battle_ids)
            database._finish_claim(
                connection, claim, job, state="complete", outcome="processed"
            )
