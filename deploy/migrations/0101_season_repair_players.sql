-- Clash Lens deployment migration 0101.
-- The players a scoped Season repair (python/src/clashlens/domain_repair.py
-- season_repair with --players) chose when its first queue ran, kept on its
-- receipt so every later step counts, queues and stores summaries for the
-- same players however their saved days change meanwhile. A full repair
-- keeps none (NULL). About 1,300 ids, a few kilobytes, written once per
-- scoped repair by an operator command; never grown by collection.
BEGIN;

ALTER TABLE season_repairs ADD COLUMN IF NOT EXISTS players bigint[];

INSERT INTO clash_lens_schema_migrations(version) VALUES (101)
ON CONFLICT (version) DO NOTHING;
COMMIT;
