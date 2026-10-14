-- Clash Lens deployment migration 0115.
-- The weekly league record, stage 1: which league each player was in each
-- week, Monday Reset to Monday Reset, decided by the worker from every piece
-- of evidence about the week (python/src/clashlens/league_week.py,
-- docs/domain.md, Weekly league record). In this stage nothing reads it.
--
-- player_league_weeks: one row per player per week with any league evidence:
-- Legend I, below Legend I or unknown, how a Legend I week began, the proof
-- and the strongest evidence against it. A row changes only when its
-- decision does; decision_number counts the changes. Measured on the copy of
-- 14 October 2026: 97,649 rows for the weeks of 7 September to 12 October,
-- 22 MB with both keys, 234 bytes a row; so about 25,000 rows and 5.9 MB a
-- week, about 305 MB a year if never deleted. The (player, week, league)
-- key is what a saved Legend day will reference in stage 2, so a public day
-- can exist only in a Legend I week.
--
-- league_week_dirty: a mark per (player, week) whose evidence changed since
-- it was last decided. Triggers on every evidence table write the marks in
-- the evidence's own transaction, whichever code or migration saves it; the
-- worker deletes them as it decides. It only ever holds a backlog: on 13
-- October 2026 about 92,000 battles and 180,000 profile answers were saved,
-- so at most about 450,000 marks a day pass through it.
--
-- The seed marks every (player, week) with evidence since Monday 28
-- September 2026, the previous Season's last week, so the worker decides
-- them all. The current Season's first week needs that week before it:
-- without it no answer read in the hour after the 5 October Reset counts
-- and nothing carries forward, so #V02JGRJC, read in Legend II at 05:14 and
-- 05:21 after a Legend I week, stayed unknown. A marked week decides its
-- whole Season, so the previous Season's earlier weeks are decided too,
-- from battles and league history only, as raw responses start on 1
-- October 2026. On that copy: 75,434 marks for 27,351 players, written in
-- 27 seconds while the evidence tables' triggers were being added.
--
-- Two indexes let the worker read only the evidence a week needs: lower
-- league profiles (600 kB, built in 2 to 10 seconds on that copy) and
-- battles by defender and day (15 MB for 1.4 million battles, 11 bytes a
-- battle, so about 1 MB a day and 380 MB a year at 92,000 battles a day;
-- built in 0.5 seconds). Saving profiles and battles waits for the whole
-- migration, about 30 to 40 seconds on that copy.
BEGIN;

-- The Monday 05:00 UTC Reset starting the week holding `at`. Monday
-- 12 October 2026 05:00 UTC was one.
CREATE OR REPLACE FUNCTION clashlens_week_start(at timestamptz) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN date_bin('7 days', at, timestamptz '2026-10-12 05:00:00+00');

CREATE TABLE IF NOT EXISTS player_league_weeks (
    player_id bigint NOT NULL REFERENCES players (id),
    week_start timestamptz NOT NULL CHECK (week_start = clashlens_week_start(week_start)),
    league text NOT NULL CHECK (league IN ('legend_i', 'below_legend_i', 'unknown')),
    -- How a Legend I week began at its Monday Reset; decides that Monday's start.
    entry text CHECK (entry IN ('season_start', 'stayed', 'promoted', 'first_seen', 'unknown')),
    -- 4 a Legend battle or league history, 3 an accepted answer, 2 an answer
    -- naming Season 0, 1 the week before's league carried forward, 0 nothing.
    strength smallint NOT NULL CHECK (strength BETWEEN 0 AND 4),
    unknown_reason text CHECK (unknown_reason IN ('no_evidence', 'conflicting_evidence')),
    -- The earliest strongest evidence for the league, or for Legend I in a
    -- conflict: an answer's response, a battle, a league history row, or the
    -- week before's record, at its Monday Reset, when the week's own
    -- evidence claims nothing.
    proof_kind text CHECK (proof_kind IN ('battle', 'league_history', 'accepted_answer',
                                          'unconfirmed_answer', 'unconfirmed_pair',
                                          'previous_week')),
    proof_at timestamptz,
    proof_source_id bigint,
    proof_second_id bigint,
    -- The strongest evidence for the other league, or for below in a conflict.
    against_kind text CHECK (against_kind IN ('battle', 'league_history', 'accepted_answer',
                                              'unconfirmed_answer', 'unconfirmed_pair')),
    against_at timestamptz,
    against_source_id bigint,
    rule_version smallint NOT NULL,
    decision_number integer NOT NULL DEFAULT 1 CHECK (decision_number > 0),
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (player_id, week_start),
    UNIQUE (player_id, week_start, league),
    CHECK ((league = 'legend_i') = (entry IS NOT NULL)),
    CHECK ((league = 'unknown') = (unknown_reason IS NOT NULL)),
    CHECK ((strength = 0) = (unknown_reason IS NOT DISTINCT FROM 'no_evidence')),
    CHECK ((proof_kind IS NULL) = (strength = 0)
           AND (proof_kind IS NULL) = (proof_at IS NULL)
           AND (proof_kind IS NULL OR proof_kind = 'previous_week') = (proof_source_id IS NULL)),
    CHECK ((proof_kind IS NOT DISTINCT FROM 'previous_week') = (strength = 1)
           AND (proof_kind IS DISTINCT FROM 'previous_week'
                OR (proof_at = week_start - interval '7 days' AND league <> 'unknown'))),
    CHECK ((proof_kind IS NOT DISTINCT FROM 'unconfirmed_pair') = (proof_second_id IS NOT NULL)),
    CHECK ((against_kind IS NULL) = (against_at IS NULL)
           AND (against_kind IS NULL) = (against_source_id IS NULL)),
    CHECK (unknown_reason IS DISTINCT FROM 'conflicting_evidence' OR against_kind IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS league_week_dirty (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id bigint NOT NULL,
    week_start timestamptz NOT NULL,
    source text NOT NULL CHECK (source IN ('profile', 'battle', 'league_history', 'decision', 'seed')),
    marked_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- The worker deletes a player's Season of marks at once.
CREATE INDEX IF NOT EXISTS league_week_dirty_player_week
    ON league_week_dirty (player_id, week_start);

-- The worker reads a player's lower league profiles first: a week with a
-- Legend battle needs its other profiles only when one exists.
CREATE INDEX IF NOT EXISTS player_profile_versions_lower_league
    ON player_profile_versions (player_id) WHERE eligibility_state = 'ineligible';
-- And each week's first battle, attack or defense: the battles' own index
-- finds a player's attacks, but its defenses only by reading every battle
-- of each day.
CREATE INDEX IF NOT EXISTS legend_battles_defender_day
    ON legend_battles (defender_player_id, ranked_day_start);

REVOKE ALL PRIVILEGES ON TABLE player_league_weeks, league_week_dirty FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON TABLE player_league_weeks TO clashlens_python_worker;
GRANT SELECT, INSERT, DELETE ON TABLE league_week_dirty TO clashlens_python_worker;

-- A profile answer marks its player's week.
CREATE OR REPLACE FUNCTION clashlens_mark_league_week_profiles()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF TG_TABLE_NAME = 'player_profile_versions' THEN
        INSERT INTO league_week_dirty (player_id, week_start, source)
        SELECT DISTINCT player_id, clashlens_week_start(observed_at), 'profile'
        FROM new_rows;
    ELSE
        INSERT INTO league_week_dirty (player_id, week_start, source)
        SELECT DISTINCT version.player_id, clashlens_week_start(seen.observed_at), 'profile'
        FROM new_rows AS seen
        JOIN player_profile_versions AS version ON version.id = seen.profile_version_id;
    END IF;
    RETURN NULL;
END $$;

-- A Legend battle saved, moved to another day or player, or deleted marks
-- the week of each side, before and after. An update that leaves the day
-- and both players alone, as each second report of a battle does, marks
-- nothing.
CREATE OR REPLACE FUNCTION clashlens_mark_league_week_battles()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO league_week_dirty (player_id, week_start, source)
        SELECT DISTINCT side.player_id, clashlens_week_start(battle.ranked_day_start), 'battle'
        FROM new_rows AS battle
        CROSS JOIN LATERAL (VALUES (battle.attacker_player_id),
                                   (battle.defender_player_id)) AS side (player_id);
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO league_week_dirty (player_id, week_start, source)
        SELECT DISTINCT side.player_id, clashlens_week_start(battle.ranked_day_start), 'battle'
        FROM old_rows AS battle
        CROSS JOIN LATERAL (VALUES (battle.attacker_player_id),
                                   (battle.defender_player_id)) AS side (player_id);
    ELSE
        INSERT INTO league_week_dirty (player_id, week_start, source)
        SELECT DISTINCT side.player_id, clashlens_week_start(side.day), 'battle'
        FROM old_rows AS earlier
        JOIN new_rows AS later ON later.id = earlier.id
        CROSS JOIN LATERAL (VALUES
            (earlier.attacker_player_id, earlier.ranked_day_start),
            (earlier.defender_player_id, earlier.ranked_day_start),
            (later.attacker_player_id, later.ranked_day_start),
            (later.defender_player_id, later.ranked_day_start)) AS side (player_id, day)
        WHERE (earlier.ranked_day_start, earlier.attacker_player_id, earlier.defender_player_id)
              IS DISTINCT FROM
              (later.ranked_day_start, later.attacker_player_id, later.defender_player_id);
    END IF;
    RETURN NULL;
END $$;

-- A league history row is labelled with the Monday Reset ending its
-- tournament, so it marks the week before that Reset: a week in Legend II
-- or lower, a Season's last week in Legend I.
CREATE OR REPLACE FUNCTION clashlens_mark_league_week_history()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO league_week_dirty (player_id, week_start, source)
    SELECT DISTINCT player_id,
           clashlens_week_start(to_timestamp(league_season_id::bigint) - interval '7 days'),
           'league_history'
    FROM new_rows;
    RETURN NULL;
END $$;

DO $$
DECLARE
    function_name text;
BEGIN
    FOREACH function_name IN ARRAY ARRAY[
        'clashlens_mark_league_week_profiles', 'clashlens_mark_league_week_battles',
        'clashlens_mark_league_week_history'
    ] LOOP
        EXECUTE format('ALTER FUNCTION %I.%I() SET search_path TO pg_catalog, %I, pg_temp',
                       current_schema(), function_name, current_schema());
        EXECUTE format('REVOKE ALL ON FUNCTION %I.%I() FROM PUBLIC',
                       current_schema(), function_name);
    END LOOP;
END $$;

-- PostgreSQL gives a trigger with transition tables one event only.
CREATE OR REPLACE TRIGGER player_profile_versions_league_week
AFTER INSERT ON player_profile_versions REFERENCING NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION clashlens_mark_league_week_profiles();
CREATE OR REPLACE TRIGGER player_profile_effects_league_week
AFTER INSERT ON player_profile_effects REFERENCING NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION clashlens_mark_league_week_profiles();
CREATE OR REPLACE TRIGGER legend_battles_league_week_insert
AFTER INSERT ON legend_battles REFERENCING NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION clashlens_mark_league_week_battles();
CREATE OR REPLACE TRIGGER legend_battles_league_week_update
AFTER UPDATE ON legend_battles REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION clashlens_mark_league_week_battles();
CREATE OR REPLACE TRIGGER legend_battles_league_week_delete
AFTER DELETE ON legend_battles REFERENCING OLD TABLE AS old_rows
FOR EACH STATEMENT EXECUTE FUNCTION clashlens_mark_league_week_battles();
CREATE OR REPLACE TRIGGER player_league_history_entries_league_week_insert
AFTER INSERT ON player_league_history_entries REFERENCING NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION clashlens_mark_league_week_history();
CREATE OR REPLACE TRIGGER player_league_history_entries_league_week_update
AFTER UPDATE ON player_league_history_entries REFERENCING NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION clashlens_mark_league_week_history();

INSERT INTO league_week_dirty (player_id, week_start, source)
SELECT player_id, week_start, 'seed'
FROM (
    SELECT version.player_id, clashlens_week_start(seen.observed_at) AS week_start
    FROM player_profile_effects AS seen
    JOIN player_profile_versions AS version ON version.id = seen.profile_version_id
    WHERE seen.observed_at >= timestamptz '2026-09-28 05:00:00+00'
    UNION
    SELECT player_id, clashlens_week_start(observed_at)
    FROM player_profile_versions
    WHERE observed_at >= timestamptz '2026-09-28 05:00:00+00'
    UNION
    SELECT side.player_id, clashlens_week_start(battle.ranked_day_start)
    FROM legend_battles AS battle
    CROSS JOIN LATERAL (VALUES (battle.attacker_player_id),
                               (battle.defender_player_id)) AS side (player_id)
    WHERE battle.ranked_day_start >= timestamptz '2026-09-28 05:00:00+00'
    UNION
    SELECT player_id,
           clashlens_week_start(to_timestamp(league_season_id::bigint) - interval '7 days')
    FROM player_league_history_entries
    WHERE to_timestamp(league_season_id::bigint) - interval '7 days'
          >= timestamptz '2026-09-28 05:00:00+00'
) AS evidence
ORDER BY player_id, week_start;

INSERT INTO clash_lens_schema_migrations(version) VALUES (115)
ON CONFLICT (version) DO NOTHING;
COMMIT;
