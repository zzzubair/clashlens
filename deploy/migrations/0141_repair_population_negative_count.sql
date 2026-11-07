-- Clash Lens deployment migration 0141.
-- A Legend II or III profile can show a negative trophy count: on
-- 4 November 2026 one Legend II profile reported -179. The worker now saves
-- such a profile and lists the player for the Monday re-check without a
-- count, since the promotion list keeps no negative count. The population
-- repair copied the saved count and so failed on that list's check for every
-- run; it now copies a negative count as empty too. Only that function
-- changes; it stores nothing new.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_repair_population(instant timestamptz)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
    unanswered bigint[];
    marked integer;
    listed integer;
BEGIN
    SELECT COALESCE(array_agg(player.id ORDER BY player.id), '{}') INTO unanswered
    FROM players AS player
    WHERE NOT player.active AND player.eligibility_state IN ('unknown', 'uncertain')
      AND player.eligibility_due_at IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM collector_response_state AS state
          WHERE state.scope = 'player' AND state.identity_key = player.normalized_tag
            AND state.endpoint = 'profile' AND state.last_not_found_at IS NOT NULL
      );
    marked := clashlens_mark_eligibility_due(unanswered, instant);
    INSERT INTO promotion_candidates (normalized_tag, league_tier_id, trophies, checked_at)
    SELECT player.normalized_tag, latest.league_tier_id,
           CASE WHEN latest.trophies >= 0 THEN latest.trophies END, latest.observed_at
    FROM players AS player
    CROSS JOIN LATERAL clashlens_latest_recognized_tier(player.id) AS latest
    WHERE NOT player.active AND latest.league_tier_id IN (105000034, 105000035)
    ON CONFLICT (normalized_tag) DO NOTHING;
    GET DIAGNOSTICS listed = ROW_COUNT;
    RETURN jsonb_build_object(
        'unanswered_found', cardinality(unanswered),
        'marked_due', marked,
        'promotion_rows_added', listed
    );
END $$;

-- Replacing a function clears its search_path; ownership and grants stay.
DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_repair_population(timestamptz) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;

INSERT INTO clash_lens_schema_migrations(version) VALUES (141)
ON CONFLICT (version) DO NOTHING;
COMMIT;
