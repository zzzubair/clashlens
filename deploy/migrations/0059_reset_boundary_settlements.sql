-- Whether each player's Reset trophies are known to have settled. The game
-- can apply the previous day's automatic defense loss minutes after the
-- 05:00 UTC Reset, so the profile read at the Reset is only provisional.
-- This first step records every Reset as provisional; nothing reads the
-- table yet and no day result changes. One row per player and Reset, about
-- 230 bytes with its keys: about 13,300 rows and 3 MB a day at October 2026
-- membership, 85 MB a Season. Nothing deletes these rows.
BEGIN;

CREATE TABLE IF NOT EXISTS reset_boundary_settlements (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id bigint NOT NULL REFERENCES players (id),
    boundary_at timestamptz NOT NULL
        CHECK ((boundary_at AT TIME ZONE 'UTC')::time = TIME '05:00'),
    sweep_id bigint REFERENCES collector_reset_sweeps (id),
    -- The Reset pair evidence this row was last recorded from. Not a foreign
    -- key, so the proof outlives that evidence's own retention.
    early_baseline_id bigint,
    delayed_work_id bigint,
    state text NOT NULL DEFAULT 'provisional'
        CHECK (state IN ('provisional', 'settled', 'unresolved')),
    selected_trophies integer,
    proof_kind text
        CHECK (proof_kind IN ('observed_adjustment', 'calculated_target')),
    proof_rule_version text,
    change_number integer NOT NULL DEFAULT 1 CHECK (change_number >= 1),
    proof_fingerprint text,
    proof_json jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(proof_json) = 'object'),
    reasons jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(reasons) = 'array'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (player_id, boundary_at),
    -- Only a settled Reset carries accepted trophies, and always with the
    -- proof that settled it.
    CHECK (
        CASE WHEN state = 'settled' THEN
            selected_trophies IS NOT NULL
            AND proof_kind IS NOT NULL
            AND proof_rule_version IS NOT NULL
            AND proof_fingerprint IS NOT NULL
            AND proof_json <> '{}'::jsonb
        ELSE
            selected_trophies IS NULL AND proof_kind IS NULL
        END
    )
);

GRANT SELECT, INSERT, UPDATE ON TABLE reset_boundary_settlements
    TO clashlens_python_worker;
GRANT USAGE, SELECT ON SEQUENCE reset_boundary_settlements_id_seq
    TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (59)
ON CONFLICT (version) DO NOTHING;
COMMIT;
