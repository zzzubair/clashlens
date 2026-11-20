-- Clash Lens deployment migration 0146.
-- The Discord bot gets its own database role instead of the private API's.
-- It reads only the tables its code reads (the API's read code it shares for
-- /stats, /link and /alerts, and its own alert and summary queries), with the
-- same columns the API may read there, runs the one owner-run function that
-- lists due attack alerts, and writes only its two delivery logs. It cannot
-- change accounts, sign-in connections, verified players, defaults, Watchlists,
-- Crews, overlay links or alert switches.
-- ./ops gives the role a fresh password and login on every production `up`,
-- as it does for finished-job cleanup (migration 0053); here it cannot log in.
-- Nothing is stored or rewritten. ./ops applies it once; run again by hand,
-- it changes nothing but the login, which the next `up` gives back.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clashlens_discord_bot') THEN
        CREATE ROLE clashlens_discord_bot NOLOGIN;
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO clashlens_discord_bot', current_schema());
END
$$;
ALTER ROLE clashlens_discord_bot
    NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Who is asking: the Discord user's account, its verified players, default
-- player and colours, its switches, and whether it is the site owner's.
GRANT SELECT ON TABLE
    clash_lens_accounts, account_provider_identities, verified_player_links,
    account_default_players, site_owner_accounts, discord_attack_alerts,
    discord_day_summary_on, discord_alert_dark_picture
    TO clashlens_discord_bot;
-- What it shows: player cards and pages, live and Daily board places,
-- Season totals, search by name and whether a player is still being checked.
GRANT SELECT ON TABLE
    players, player_profile_versions, player_profile_effects,
    api_player_daily_logs, player_league_weeks, player_league_history_entries,
    player_season_summaries, live_unplayed_profiles, live_attack_tallies,
    leaderboard_snapshots, leaderboard_snapshot_entries,
    boundary_publication_generations, boundary_publication_generation_members,
    collector_work, python_processing_jobs
    TO clashlens_discord_bot;
GRANT SELECT (scope, identity_key, endpoint, last_observation_id, last_success_at,
              last_not_found_at)
    ON TABLE collector_response_state TO clashlens_discord_bot;
GRANT SELECT (player_id, failed_at) ON TABLE league_week_dirty TO clashlens_discord_bot;
GRANT SELECT (id, player_id, ranked_day_start, ranked_day_end, official_season_id,
              season_day_number, version, start_trophies, final_trophies_before_reset,
              shield_state, input_evidence)
    ON TABLE ranked_day_versions TO clashlens_discord_bot;

-- Its only writes: the log of each attack alert and Legend day summary DM.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    discord_attack_alert_deliveries, discord_day_summary_deliveries
    TO clashlens_discord_bot;
GRANT EXECUTE ON FUNCTION clashlens_discord_attack_alerts_due(timestamptz)
    TO clashlens_discord_bot;

INSERT INTO clash_lens_schema_migrations(version) VALUES (146)
ON CONFLICT (version) DO NOTHING;
COMMIT;
