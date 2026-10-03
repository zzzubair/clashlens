-- A battle reported in the first 5 minutes after a Reset finished an attack
-- of the previous Legend day: no new-day attack can start that early. Battles
-- are now saved under the day of their timestamp less 5 minutes
-- (domain.BATTLE_DAY_GRACE). This moves battles saved under the old rule.
-- On production on 2026-10-03 that was 90 battles, which gave 59 stored
-- player-days a ninth attack or defense. `up` runs it while services are
-- stopped, so no new report can arrive mid-move.
BEGIN;

-- The late-battle check's index, rebuilt with battle days binned from 05:05.
-- Its query repeats this condition. About 25 more rows a day than before.
DROP INDEX IF EXISTS battle_evidence_late_report;
CREATE INDEX battle_evidence_late_report
    ON battle_evidence (battle_timestamp)
    WHERE date_bin('24 hours', created_at, TIMESTAMPTZ '2000-01-01 04:55+00')
        > date_bin('24 hours', battle_timestamp, TIMESTAMPTZ '2000-01-01 05:05+00');

-- One row per player's report moved below; the republish command rebuilds
-- each listed player's published days from the earlier of the two. Written
-- only here: 92 rows on production on 2026-10-03.
CREATE TABLE IF NOT EXISTS battle_day_repairs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_battle_id bigint NOT NULL,
    to_battle_id bigint NOT NULL,
    perspective text NOT NULL CHECK (perspective IN ('attacker', 'defender')),
    evidence_id bigint NOT NULL,
    attacker_player_id bigint NOT NULL,
    defender_player_id bigint NOT NULL,
    from_day timestamptz NOT NULL,
    to_day timestamptz NOT NULL,
    repaired_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
GRANT SELECT ON battle_day_repairs TO clashlens_python_worker;

-- A battle is one row per Legend day, attacker and defender, holding each
-- side's report. A battle whose reports all move, and whose new day has no
-- row for that pair, just changes day. Otherwise each moving side's reports,
-- army decodes and army facts join the new day's row, which is created if
-- missing, and the old row is deleted once nothing refers to it. Usually the
-- defender's report, stamped before the Reset, is already there. A side the
-- new day's row already has is left in place and counted as skipped: no
-- battle is dropped or saved twice. Running this again changes nothing.
-- Army decodes (1.5 GB on production) have no battle lookup except for active
-- ones, so they are moved and checked in one pass after the loop.
CREATE TEMPORARY TABLE battle_day_merges (
    from_battle_id bigint PRIMARY KEY,
    to_battle_id bigint NOT NULL,
    perspectives text[] NOT NULL
) ON COMMIT DROP;

DO $$
DECLARE
    battle record;
    target_id bigint;
    lenses text[];
    moved integer := 0;
    merged integer := 0;
    skipped integer := 0;
    kept bigint[];
BEGIN
    FOR battle IN
        SELECT p.battle_id, b.ranked_day_start AS from_day, new_day.to_day,
               b.attacker_player_id, b.defender_player_id,
               array_agg(p.perspective ORDER BY p.perspective) AS perspectives,
               array_agg(p.evidence_id ORDER BY p.perspective) AS evidence_ids
        FROM battle_perspectives AS p
        JOIN battle_evidence AS e ON e.id = p.evidence_id
        JOIN legend_battles AS b ON b.id = p.battle_id
        CROSS JOIN LATERAL (
            SELECT date_bin('24 hours', e.battle_timestamp - interval '5 minutes',
                            TIMESTAMPTZ '2000-01-01 05:00+00') AS to_day
        ) AS new_day
        WHERE b.ranked_day_start <> new_day.to_day
        GROUP BY p.battle_id, b.ranked_day_start, new_day.to_day,
                 b.attacker_player_id, b.defender_player_id
        ORDER BY b.ranked_day_start, p.battle_id
    LOOP
        lenses := ARRAY(
            SELECT CASE side WHEN 'attacker' THEN 'offense' ELSE 'defense' END
            FROM unnest(battle.perspectives) AS side
        );
        SELECT id INTO target_id
        FROM legend_battles
        WHERE ranked_day_start = battle.to_day
          AND attacker_player_id = battle.attacker_player_id
          AND defender_player_id = battle.defender_player_id;
        IF target_id IS NULL AND NOT EXISTS (
            SELECT 1 FROM battle_perspectives
            WHERE battle_id = battle.battle_id
              AND perspective <> ALL (battle.perspectives)
        ) THEN
            UPDATE legend_battles
            SET ranked_day_start = battle.to_day, updated_at = clock_timestamp()
            WHERE id = battle.battle_id;
            target_id := battle.battle_id;
            moved := moved + 1;
        ELSE
            IF target_id IS NULL THEN
                INSERT INTO legend_battles (
                    ranked_day_start, attacker_player_id, defender_player_id
                ) VALUES (
                    battle.to_day, battle.attacker_player_id,
                    battle.defender_player_id
                )
                RETURNING id INTO target_id;
            ELSIF EXISTS (
                SELECT 1 FROM battle_evidence
                WHERE battle_id = target_id
                  AND perspective = ANY (battle.perspectives)
            ) OR EXISTS (
                SELECT 1 FROM battle_army_decodes
                WHERE battle_id = target_id AND is_active
                  AND perspective = ANY (battle.perspectives)
            ) OR EXISTS (
                SELECT 1 FROM army_analytics_battle_facts
                WHERE battle_id = target_id AND lens = ANY (lenses)
            ) THEN
                RAISE NOTICE 'battle % not moved: day % already has that report',
                    battle.battle_id, battle.to_day;
                skipped := skipped + 1;
                CONTINUE;
            END IF;
            UPDATE battle_evidence SET battle_id = target_id
            WHERE battle_id = battle.battle_id
              AND perspective = ANY (battle.perspectives);
            UPDATE battle_perspectives
            SET battle_id = target_id, updated_at = clock_timestamp()
            WHERE battle_id = battle.battle_id
              AND perspective = ANY (battle.perspectives);
            UPDATE army_analytics_battle_facts SET battle_id = target_id
            WHERE battle_id = battle.battle_id AND lens = ANY (lenses);
            INSERT INTO battle_day_merges VALUES (
                battle.battle_id, target_id, battle.perspectives
            );
            merged := merged + 1;
        END IF;
        INSERT INTO battle_day_repairs (
            from_battle_id, to_battle_id, perspective, evidence_id,
            attacker_player_id, defender_player_id, from_day, to_day
        )
        SELECT battle.battle_id, target_id, side.perspective, side.evidence_id,
               battle.attacker_player_id, battle.defender_player_id,
               battle.from_day, battle.to_day
        FROM unnest(battle.perspectives, battle.evidence_ids)
            AS side(perspective, evidence_id);
    END LOOP;

    UPDATE battle_army_decodes AS decode
    SET battle_id = merge.to_battle_id
    FROM battle_day_merges AS merge
    WHERE decode.battle_id = merge.from_battle_id
      AND decode.perspective = ANY (merge.perspectives);
    kept := ARRAY(
        SELECT DISTINCT battle_id FROM battle_army_decodes
        WHERE battle_id = ANY (ARRAY(SELECT from_battle_id FROM battle_day_merges))
    );
    DELETE FROM legend_battles AS old
    USING battle_day_merges AS merge
    WHERE old.id = merge.from_battle_id
      AND old.id <> ALL (kept)
      AND NOT EXISTS (SELECT 1 FROM battle_perspectives WHERE battle_id = old.id)
      AND NOT EXISTS (SELECT 1 FROM battle_evidence WHERE battle_id = old.id)
      AND NOT EXISTS (
          SELECT 1 FROM army_analytics_battle_facts WHERE battle_id = old.id
      );
    RAISE NOTICE 'battle days: % battles moved, % merged, % skipped',
        moved, merged, skipped;
END
$$;

INSERT INTO clash_lens_schema_migrations(version) VALUES (57)
ON CONFLICT (version) DO NOTHING;
COMMIT;
