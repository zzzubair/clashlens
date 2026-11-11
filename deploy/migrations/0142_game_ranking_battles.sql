-- Clash Lens deployment migration 0142.
-- The game-rankings score (game_rankings.py) compares each country list's
-- trophies with the Daily board's. A player who battled between the Reset
-- and the list's request differs by exactly what those battles moved: on 10
-- November 2026, 22 of 23 differences. The scorer's collector login cannot
-- read battles, so this read-only function gives it just that sum for the
-- few players who differ. It stores nothing.
BEGIN;

-- For each (tag, time), what the player's battles add to the game's
-- trophies at that time over the Daily board's at the Reset at
-- list_reset_at. The board counts a battle when its attacker's report,
-- stamped when the attack ends, is before the Reset, or, with no attacker's
-- report, when it belongs to the day before (a report up to 5 minutes after
-- the Reset finishes that day's attack, so both days are read). The game
-- counts it once it has ended. With no attacker's report the attack ended
-- within 4 minutes after the defender's report: unsure_battles counts those
-- that may or may not have ended by the time. Each side's change comes from
-- its own report when there is one. A tag with no player or no battle on
-- those two days returns no row.
CREATE FUNCTION clashlens_game_ranking_battles(
    list_tags text[], list_reset_at timestamptz, list_times timestamptz[]
)
RETURNS TABLE (normalized_tag text, trophy_change integer, unsure_battles integer)
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT wanted.tag,
           COALESCE(sum(
               CASE WHEN mine.attacking
                    THEN COALESCE(attacker.attacker_gain, defender.attacker_gain)
                    ELSE -COALESCE(defender.defender_loss, attacker.defender_loss) END
               * CASE WHEN attacker.id IS NOT NULL
                      THEN (attacker.battle_timestamp >= list_reset_at
                            AND attacker.battle_timestamp <= wanted.at)::integer
                      WHEN defender.battle_timestamp + interval '4 minutes' <= wanted.at
                      THEN (mine.ranked_day_start = list_reset_at)::integer
                      WHEN defender.battle_timestamp > wanted.at
                      THEN -(mine.ranked_day_start < list_reset_at)::integer
                      ELSE 0 END
           ), 0)::integer,
           count(*) FILTER (WHERE attacker.id IS NULL
                              AND defender.battle_timestamp <= wanted.at
                              AND wanted.at < defender.battle_timestamp + interval '4 minutes')::integer
    FROM unnest(list_tags, list_times) AS wanted(tag, at)
    JOIN players AS player ON player.normalized_tag = wanted.tag
    CROSS JOIN LATERAL (
        SELECT battle.id, battle.ranked_day_start, true AS attacking
        FROM legend_battles AS battle
        WHERE battle.ranked_day_start IN (list_reset_at - interval '1 day', list_reset_at)
          AND battle.attacker_player_id = player.id
        UNION ALL
        SELECT battle.id, battle.ranked_day_start, false
        FROM legend_battles AS battle
        WHERE battle.defender_player_id = player.id
          AND battle.ranked_day_start IN (list_reset_at - interval '1 day', list_reset_at)
    ) AS mine
    LEFT JOIN battle_perspectives AS attacker_side
      ON attacker_side.battle_id = mine.id AND attacker_side.perspective = 'attacker'
    LEFT JOIN battle_evidence AS attacker ON attacker.id = attacker_side.evidence_id
    LEFT JOIN battle_perspectives AS defender_side
      ON defender_side.battle_id = mine.id AND defender_side.perspective = 'defender'
    LEFT JOIN battle_evidence AS defender ON defender.id = defender_side.evidence_id
    GROUP BY wanted.tag, wanted.at;
$$;

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_game_ranking_battles(text[], timestamptz, timestamptz[]) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_game_ranking_battles(text[], timestamptz, timestamptz[])
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_game_ranking_battles(text[], timestamptz, timestamptz[])
    TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (142)
ON CONFLICT (version) DO NOTHING;
COMMIT;
