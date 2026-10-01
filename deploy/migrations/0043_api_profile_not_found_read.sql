-- Clash Lens deployment migration 0043.
BEGIN;

ALTER TABLE collector_response_state
    ADD COLUMN IF NOT EXISTS last_not_found_at timestamptz;

UPDATE collector_response_state AS state
SET last_not_found_at = GREATEST(state.last_not_found_at, not_found.observed_at)
FROM (
    SELECT normalized_tag, max(response_completed_at) AS observed_at
    FROM collector_observations
    WHERE scope = 'player' AND endpoint = 'profile' AND http_status = 404
    GROUP BY normalized_tag
) AS not_found
WHERE state.scope = 'player'
  AND state.identity_key = not_found.normalized_tag
  AND state.endpoint = 'profile';

UPDATE collector_response_state AS state
SET last_not_found_at = GREATEST(state.last_not_found_at, state.last_seen_at)
FROM collector_observations AS observation
WHERE observation.id = state.last_observation_id
  AND state.scope = 'player' AND state.endpoint = 'profile'
  AND observation.http_status = 404;

GRANT SELECT (last_not_found_at) ON TABLE collector_response_state
    TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (43)
ON CONFLICT (version) DO NOTHING;
COMMIT;
