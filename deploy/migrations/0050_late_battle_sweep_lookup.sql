-- Let the once-per-Reset late-battle check read only what it needs. On
-- production on 2026-10-02 its two selections took 11.4 s and 28.9 s: the
-- first joined every kept battle report to find the few saved late, and the
-- second unpacked every saved day's stored input, about 6.4 KB each, to read
-- one number.
BEGIN;

-- Reports saved within 5 minutes of their Legend day's end or later: about
-- 300 a day, plus 197,898 from the first days' backfill. A battle's Legend
-- day is the 05:00 UTC day of its timestamp, so the report is late when the
-- 04:55 UTC day of its saving is later than that day.
CREATE INDEX IF NOT EXISTS battle_evidence_late_report
    ON battle_evidence (battle_timestamp)
    WHERE date_bin('24 hours', created_at, TIMESTAMPTZ '2000-01-01 04:55+00')
        > date_bin('24 hours', battle_timestamp, TIMESTAMPTZ '2000-01-01 05:00+00');

-- Each saved day version's number joined to the previous day's version it
-- was built from, so the check finds a result built from the current previous
-- day without unpacking its stored input. About 75 bytes a saved day version:
-- about 11 MB a day at 150,000 versions a day.
CREATE INDEX IF NOT EXISTS ranked_day_versions_previous_day
    ON ranked_day_versions (
        player_id, ranked_day_start, reconciliation_rule_version,
        (version::text || ':'
            || (input_evidence -> 'previous_day' ->> 'version_id'))
    );

INSERT INTO clash_lens_schema_migrations(version) VALUES (50)
ON CONFLICT (version) DO NOTHING;
COMMIT;
