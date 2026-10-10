-- Clash Lens deployment migration 0096.
-- Saved players are gone: a Clasher saves a player by adding it to one of
-- their groups, and an account holds at most 10 groups.
--
-- Each account's saved players move into a new group named "Saved players"
-- when they fit: the account has fewer than 10 groups, none already named
-- "Saved players", and at most 20 saved players (the most a group holds).
-- The saved-player table is dropped only when every row moved. Rows that did
-- not fit stay where they are, and a warning says how many.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_account_group_limit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- One account's new groups take turns, so two at once cannot both see
    -- room for the tenth.
    PERFORM 1 FROM clash_lens_accounts WHERE id = NEW.account_id FOR NO KEY UPDATE;
    IF (SELECT count(*) FROM account_groups WHERE account_id = NEW.account_id) >= 10 THEN
        RAISE EXCEPTION 'an account holds at most 10 groups'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'account_groups_limit';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS account_groups_limit ON account_groups;
CREATE TRIGGER account_groups_limit
BEFORE INSERT ON account_groups
FOR EACH ROW
EXECUTE FUNCTION clashlens_account_group_limit();

CREATE TEMPORARY TABLE saved_players_moving ON COMMIT DROP AS
SELECT saved.account_id
FROM account_saved_players AS saved
GROUP BY saved.account_id
HAVING count(*) <= 20
   AND (SELECT count(*) FROM account_groups AS group_row
        WHERE group_row.account_id = saved.account_id) < 10
   AND NOT EXISTS (SELECT 1 FROM account_groups AS group_row
                   WHERE group_row.account_id = saved.account_id
                     AND group_row.normalized_name = 'Saved players');

INSERT INTO account_groups (public_id, account_id, name, normalized_name)
SELECT gen_random_uuid(), account_id, 'Saved players', 'Saved players'
FROM saved_players_moving;

INSERT INTO account_group_players (group_id, player_id, created_at)
SELECT group_row.id, saved.player_id, saved.created_at
FROM account_saved_players AS saved
JOIN saved_players_moving AS moving ON moving.account_id = saved.account_id
JOIN account_groups AS group_row
  ON group_row.account_id = saved.account_id
 AND group_row.normalized_name = 'Saved players';

DELETE FROM account_saved_players AS saved
USING saved_players_moving AS moving
WHERE saved.account_id = moving.account_id;

DO $$
DECLARE
    left_behind bigint;
BEGIN
    SELECT count(*) INTO left_behind FROM account_saved_players;
    IF left_behind = 0 THEN
        DROP TABLE account_saved_players;
    ELSE
        RAISE WARNING '% saved players did not fit a group and stay in account_saved_players',
            left_behind;
    END IF;
END
$$;

INSERT INTO clash_lens_schema_migrations(version) VALUES (96)
ON CONFLICT (version) DO NOTHING;
COMMIT;
