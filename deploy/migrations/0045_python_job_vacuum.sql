-- Every Python job is rewritten as it is queued, leased and finished, and the
-- old row versions stay in the queue indexes until vacuum removes them.
-- Production keeps about a million finished jobs, so the default 20% trigger
-- waited for about 210,000 dead rows and slowed every queue query meanwhile.
-- Locally, vacuuming this table took 0.04 s at 16,000 dead rows and 0.25 s at
-- 107,000; clean it at 1%.
BEGIN;

ALTER TABLE python_processing_jobs SET (autovacuum_vacuum_scale_factor = 0.01);

INSERT INTO clash_lens_schema_migrations(version) VALUES (45)
ON CONFLICT (version) DO NOTHING;
COMMIT;
