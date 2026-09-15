-- Prioritize a player's first successful battle-log collection without
-- scanning response state on every regular-poll claim.
BEGIN;

ALTER TABLE players
    ADD COLUMN IF NOT EXISTS first_battle_pending boolean NOT NULL DEFAULT true;

UPDATE players AS player
SET first_battle_pending = false
WHERE player.first_battle_pending
  AND EXISTS (
      SELECT 1
      FROM collector_response_state AS state
      WHERE state.scope = 'player'
        AND state.identity_key = player.normalized_tag
        AND state.endpoint = 'battle_log'
        AND state.last_success_at IS NOT NULL
  );

CREATE INDEX IF NOT EXISTS players_due_regular_poll_v3
    ON players (first_battle_pending DESC, next_due_at, id)
    INCLUDE (normalized_tag)
    WHERE active AND next_due_at IS NOT NULL;

GRANT UPDATE (first_battle_pending) ON TABLE players TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (34)
ON CONFLICT (version) DO NOTHING;
COMMIT;
