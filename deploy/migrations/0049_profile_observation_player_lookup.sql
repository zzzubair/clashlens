-- Freezing a Reset's player list looks up, for each player without an
-- accepted profile, their latest profile response before the Reset. With no
-- player index, each lookup scanned every saved response: on 2026-10-02 that
-- was 1.08 million rows (1.1 GB), about 0.2 s per player, and about 1,500
-- such players per Reset, so one repair job ran for minutes and lost its lease.
-- Production had 721,000 profile responses (an estimated 30 MB of index) and
-- adds about 180,000 a day (about 7 MB a day). `up` applies this while services are
-- stopped, so the build blocks no live writes.
BEGIN;

CREATE INDEX IF NOT EXISTS collector_observations_profile_player_time
    ON collector_observations (player_id, response_completed_at DESC, id DESC)
    WHERE endpoint = 'profile';

INSERT INTO clash_lens_schema_migrations(version) VALUES (49)
ON CONFLICT (version) DO NOTHING;
COMMIT;
