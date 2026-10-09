-- Clash Lens deployment migration 0092.
-- Legend shields stack (a 2-day and a 1-day shield give 3 whole days), so a
-- run of shielded days can last longer than 2 days, and the worker saves such
-- days as 'inferred_shielded' with their full run length. The trophy formula
-- check still allowed only 1 or 2, so saving a 3rd shielded day failed: the
-- late-battle sweep failed for 2 players on 7 October 2026, and a Season
-- repair would have failed about 51 days. The check is re-created unchanged
-- except that a shielded run may be any length of at least 1 day.
--
-- Every saved row passed the old, stricter check, so NOT VALID skips reading
-- them again and the table is locked only for the swap. No row changes.
BEGIN;

ALTER TABLE ranked_day_versions
    DROP CONSTRAINT IF EXISTS ranked_day_versions_formula_v2_check;
ALTER TABLE ranked_day_versions
    ADD CONSTRAINT ranked_day_versions_formula_v2_check
        CHECK (
            (net_trophy_change IS NULL OR (
                start_trophies IS NOT NULL
                AND final_trophies_before_reset IS NOT NULL
                AND net_trophy_change = final_trophies_before_reset - start_trophies
            ))
            AND (expected_next_start_trophies IS NULL OR (
                final_trophies_before_reset IS NOT NULL
                AND expected_next_start_trophies =
                    final_trophies_before_reset + boundary_adjustment
            ))
            AND (observed_boundary_adjustment IS NULL OR (
                next_start_trophies IS NOT NULL
                AND final_trophies_before_reset IS NOT NULL
                AND observed_boundary_adjustment =
                    next_start_trophies - final_trophies_before_reset
            ))
            AND (unexplained_residual IS NULL OR (
                next_start_trophies IS NOT NULL
                AND expected_next_start_trophies IS NOT NULL
                AND unexplained_residual =
                    next_start_trophies - expected_next_start_trophies
            ))
            AND (
                automatic_defense_evidence_state = 'unknown'
                OR (
                    automatic_defense_evidence_state = 'not_applicable'
                    AND automatic_defense_loss IS NULL
                )
                OR (
                    automatic_defense_evidence_state IN ('calculated', 'confirmed')
                    AND automatic_defense_loss IS NOT NULL
                )
            )
            AND (
                boundary_adjustment_type IS NULL
                OR boundary_adjustment_type IN ('weekly_reset', 'season_reset')
            )
            AND (
                shield_duration_days IS NULL
                OR shield_duration_days > 0
            )
            AND (
                shield_state <> 'inferred_shielded'
                OR shield_duration_days >= 1
            )
        ) NOT VALID;

INSERT INTO clash_lens_schema_migrations(version) VALUES (92)
ON CONFLICT (version) DO NOTHING;
COMMIT;
