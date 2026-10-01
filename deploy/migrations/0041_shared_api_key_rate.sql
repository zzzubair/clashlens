BEGIN;

ALTER TABLE shared_api_credentials
    DROP CONSTRAINT shared_api_credentials_total_budget_check,
    DROP CONSTRAINT shared_api_credentials_collector_budget_v4_check,
    DROP CONSTRAINT shared_api_credentials_budget_split_v4_check,
    ALTER COLUMN total_budget SET DEFAULT 25,
    ALTER COLUMN collector_budget SET DEFAULT 24;

UPDATE shared_api_credentials
SET total_budget = 25, collector_budget = 24,
    updated_at = clock_timestamp();

ALTER TABLE shared_api_credentials
    ADD CONSTRAINT shared_api_credentials_total_budget_check
        CHECK (total_budget BETWEEN 1 AND 29),
    ADD CONSTRAINT shared_api_credentials_collector_budget_check
        CHECK (collector_budget = GREATEST(total_budget - python_budget, 1));

INSERT INTO clash_lens_schema_migrations(version) VALUES (41)
ON CONFLICT (version) DO NOTHING;
COMMIT;
