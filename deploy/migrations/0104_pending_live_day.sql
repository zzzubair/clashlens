-- Clash Lens deployment migration 0104.
-- A player's Legend day usually waits in one live day calculation at most:
-- new battles for a day whose live calculation has not started add nothing,
-- as that calculation reads the newest battles when it runs; one already
-- running may have read too early, so they queue one more. Two saves for the
-- same day committing at the same moment can each queue one, and both run.
--
-- Each changed set of a day's battles queued one more live calculation
-- (reconciliation_db.py), whether or not one was already waiting. At 09:42 on
-- 12 October 2026 62,519 waited for 10,696 player days, 5.8 each, while the
-- worker caught up after the Reset: up to 51,823 calculations repeated work a
-- waiting one would have done. The index below finds the waiting one, as
-- migration 0093's does for day rechecks. It holds only live calculations
-- that wait and have not started, and an entry leaves it when its job starts,
-- its space reused after the next vacuum: 2,654 rows at 12:23 on 12 October
-- 2026 and 62,519 at the worst, which measure 1 MB, 0.5 MB once merged to one
-- for each of the 10,696 player days. It does not grow with history.
--
-- The index is built without blocking writes, so this file runs outside a
-- transaction: ./ops sends it to psql one statement at a time. A build that
-- fails leaves an unusable index and records no version, so the next run
-- drops that index and builds it again. The build reads the jobs table once.
DROP INDEX CONCURRENTLY IF EXISTS python_processing_jobs_pending_live_day;

CREATE INDEX CONCURRENTLY python_processing_jobs_pending_live_day
    ON python_processing_jobs ((input_json ->> 'player_id'), (input_json ->> 'ranked_day_start'))
    WHERE status = 'pending' AND priority = 100 AND work_type = 'reconcile_ranked_day';

INSERT INTO clash_lens_schema_migrations(version) VALUES (104)
ON CONFLICT (version) DO NOTHING;
