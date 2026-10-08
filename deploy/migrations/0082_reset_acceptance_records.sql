-- One record per Reset of how it went against the 05:30 board target, kept
-- apart from the board itself: how many members were captured; when their
-- Reset readings were collected and processed; when the first frozen board's
-- inputs froze, when it was saved as published and when the website's public
-- Daily leaderboard page first showed it; that board's input states; and how
-- many of the Reset's boundaries were settled when the website first showed it.
-- The alert check fills it once a minute through the worker's role
-- (reset_acceptance.py). Under 1 KB a row, one row a day: about 0.4 MB a year.
-- Nothing deletes these rows.
BEGIN;

CREATE TABLE IF NOT EXISTS reset_acceptance_records (
    boundary_at timestamptz PRIMARY KEY
        CHECK ((boundary_at AT TIME ZONE 'UTC')::time = TIME '05:00'),
    -- Not a foreign key, so the record outlives the sweep's own retention.
    sweep_id bigint NOT NULL,
    captured_count integer NOT NULL CHECK (captured_count >= 0),
    membership_captured_at timestamptz,
    collected_count integer NOT NULL DEFAULT 0 CHECK (collected_count >= 0),
    not_collected_count integer NOT NULL DEFAULT 0 CHECK (not_collected_count >= 0),
    collection_finished_at timestamptz,
    proof_processed_at timestamptz,
    inputs_frozen_at timestamptz,
    published_at timestamptz,
    readable_at timestamptz,
    board_inputs jsonb CHECK (jsonb_typeof(board_inputs) = 'object'),
    settlement jsonb CHECK (jsonb_typeof(settlement) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

GRANT SELECT, INSERT, UPDATE ON TABLE reset_acceptance_records
    TO clashlens_python_worker;

-- A Reset item that saves a newer response of an endpoint keeps the one it
-- replaced here, so the record still waits for that response's processing.
-- Only replaced Reset readings are kept: a few ids on a retried item.
ALTER TABLE collector_work
    ADD COLUMN IF NOT EXISTS replaced_observation_ids bigint[] NOT NULL DEFAULT '{}';

INSERT INTO clash_lens_schema_migrations(version) VALUES (82)
ON CONFLICT (version) DO NOTHING;
COMMIT;
