-- Clash Lens deployment migration 0066.
-- Find a battle log's saved battle reports by the response they came from.
-- After each battle log is processed, the worker looks up that response's
-- reports to choose which Resets to re-judge (reset_settlement.py). With no
-- index starting with observation_id that covers every report, Postgres read
-- all 1.63 million reports each time: on production on 2026-10-04 that one
-- lookup averaged 258 ms and took 57% of all database time and 68% of the
-- pages read. With this index it reads about 100 pages, under 3 ms.
--
-- Repeated observation ids share one index entry: built over 1.63 million
-- reports it is 12 MB, about 7.6 bytes a report, so at about 330,000 reports
-- saved a day it grows by about 2.5 MB a day and shrinks as reports are
-- deleted. ./ops up stops every service before migrations, so a plain build
-- inside the transaction blocks nothing that is running.
BEGIN;

CREATE INDEX IF NOT EXISTS battle_evidence_observation
    ON battle_evidence (observation_id);

INSERT INTO clash_lens_schema_migrations(version) VALUES (66)
ON CONFLICT (version) DO NOTHING;
COMMIT;
