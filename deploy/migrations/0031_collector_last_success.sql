-- Keep the last successful provider response across collector restarts and
-- later provider failures without creating a row for every unchanged poll.
BEGIN;

ALTER TABLE collector_response_state
    ADD COLUMN last_success_at timestamptz;

UPDATE collector_response_state AS state
SET last_success_at = success.observed_at
FROM (
    SELECT scope,
           CASE WHEN scope = 'global' THEN 'global' ELSE normalized_tag END
               AS identity_key,
           endpoint, max(response_completed_at) AS observed_at
    FROM collector_observations
    WHERE http_status BETWEEN 200 AND 299
    GROUP BY scope,
             CASE WHEN scope = 'global' THEN 'global' ELSE normalized_tag END,
             endpoint
) AS success
WHERE success.scope = state.scope
  AND success.identity_key = state.identity_key
  AND success.endpoint = state.endpoint;

UPDATE collector_response_state AS state
SET last_success_at = GREATEST(state.last_success_at, state.last_seen_at)
FROM collector_observations AS observation
WHERE observation.id = state.last_observation_id
  AND observation.http_status BETWEEN 200 AND 299;

INSERT INTO clash_lens_schema_migrations(version) VALUES (31)
ON CONFLICT (version) DO NOTHING;
COMMIT;
