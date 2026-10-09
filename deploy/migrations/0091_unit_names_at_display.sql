-- Clash Lens deployment migration 0091.
-- Saved armies hold the game's ids; the unit list names them only when a page
-- shows them.
--
-- Migration 0089 queued jobs to re-decode the current Season's battles under
-- unit catalog v3, because pages read only armies saved under the catalog the
-- running code pinned. Pages now read each battle side's newest saved army
-- whatever catalog saved it, and count an id the catalog did not name (the
-- Portal Pendant before v3) like any other unit, so those re-decodes would
-- change nothing a page shows. This marks 0089's unfinished jobs complete as
-- superseded: complete, not cancelled, so the Season-close check counts them
-- as accounted for. No battle or army row is touched. On 2026-10-09, 3,609 of
-- them were pending, held until 2026-10-10 08:00 UTC, and 278 had completed.
BEGIN;

UPDATE python_processing_jobs
SET status = 'complete', outcome = 'stale_superseded',
    failure_category = NULL, failure_detail = NULL,
    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
    completed_at = clock_timestamp(), updated_at = clock_timestamp()
WHERE work_type = 'redecode_army'
  AND starts_with(
      deduplication_key, 'redecode_army:army-decoder-v2:unit-catalog-v3:'
  )
  AND status <> 'complete';

INSERT INTO clash_lens_schema_migrations(version) VALUES (91)
ON CONFLICT (version) DO NOTHING;
COMMIT;
