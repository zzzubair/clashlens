-- Clash Lens deployment migration 0093.
-- A day's recheck after new evidence gets its own background lane, priority
-- 26, and a day usually waits in one recheck at most: two saves for the same
-- day committing at the same moment can each queue one, and both run.
--
-- From 16:41 on 9 October 2026 every saved battle log, profile and changed
-- battle report queued a recheck of each ended day it could change, at
-- backfill priority 25: 21,088 in 39 minutes, about 535 a minute, for only
-- 3,782 player days. Two background jobs at a time finished about 350 a
-- minute, so 7,941 waited by 17:19 and the oldest had waited 16 minutes.
--
-- The worker now runs up to 4 day rechecks and, separately, up to 2 other
-- background jobs at once (background_pacing.py). New evidence for a day
-- whose recheck has not started adds nothing (queue_refresh.py); the index
-- below finds that waiting recheck. It holds only waiting rechecks, a few
-- thousand rows at most.
--
-- Live claims probe priorities 100 and 300 and a catch-all for any other
-- priority; the catch-all's indexes now leave out 26 as they leave out 25,
-- so a live claim never reads waiting rechecks. Rechecks already waiting
-- move to priority 26, as many as waited, not one a day: at 18:40 UTC on
-- 9 October 2026 that was 29,785 for about 6,570 player days, the oldest 62
-- minutes old, and it grew by about 180 a minute. Expect them to drain once,
-- at 4 at a time in roughly 45 minutes, while new evidence for those days
-- merges into them.
--
-- The indexes are built without blocking writes, so this file runs outside a
-- transaction: ./ops sends it to psql one statement at a time. A build that
-- fails leaves an unusable index and records no version, so the next run
-- drops that index and builds it again. Each build reads the jobs table, about
-- 1 GB and 1.6 million rows on 9 October 2026, and the priority move reads it
-- once more. On a 1,042 MB copy of that shape, cached and on an idle
-- machine, the three builds took 0.3, 0.9 and 0.4 seconds and the move 4.6.
-- ./ops runs this with the worker and collector stopped, so expect about 10
-- seconds more downtime, longer if the table is not cached.
DROP INDEX CONCURRENTLY IF EXISTS python_processing_jobs_pending_day_recheck;

CREATE INDEX CONCURRENTLY python_processing_jobs_pending_day_recheck
    ON python_processing_jobs ((input_json ->> 'player_id'), (input_json ->> 'ranked_day_start'))
    WHERE status = 'pending' AND priority = 26 AND work_type = 'reconcile_ranked_day';

DROP INDEX CONCURRENTLY IF EXISTS python_processing_jobs_unknown_priority_v2;

CREATE INDEX CONCURRENTLY python_processing_jobs_unknown_priority_v2
    ON python_processing_jobs (due_at, created_at, id, priority)
    WHERE status IN ('pending','waiting_retry')
      AND claim_compatibility_version IN (1,2,3,4,5,6,7)
      AND attempt_count < max_attempts
      AND priority NOT IN (25,26,100);

DROP INDEX CONCURRENTLY IF EXISTS python_processing_jobs_waiting_dependency_unknown_priority_v3;

CREATE INDEX CONCURRENTLY python_processing_jobs_waiting_dependency_unknown_priority_v3
    ON python_processing_jobs (due_at, created_at, id, priority)
    WHERE status = 'waiting_dependency'
      AND claim_compatibility_version IN (1,2,3,4,5,6,7)
      AND priority NOT IN (25,26,100);

UPDATE python_processing_jobs SET priority = 26
WHERE status IN ('pending', 'waiting_retry') AND priority = 25
  AND work_type = 'reconcile_ranked_day'
  AND input_json ->> 'trigger' IN ('evidence', 'battle_log_check');

INSERT INTO clash_lens_schema_migrations(version) VALUES (93)
ON CONFLICT (version) DO NOTHING;
