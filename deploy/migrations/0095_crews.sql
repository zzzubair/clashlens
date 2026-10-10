-- Clash Lens deployment migration 0095.
-- Crews: invite-only groups of Legend League accounts with shared boards.
-- Rows grow with people and crews, never with time: a crew holds at most
-- 100 places, an account at most 5 crews, and expired invite links are
-- deleted whenever that crew makes a new one.
BEGIN;

CREATE TABLE IF NOT EXISTS crews (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL UNIQUE,
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
    size smallint NOT NULL CHECK (size BETWEEN 2 AND 100),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- One row per Clash Lens account in a crew; holds its role.
CREATE TABLE IF NOT EXISTS crew_accounts (
    crew_id bigint NOT NULL REFERENCES crews (id) ON DELETE CASCADE,
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    joined_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (crew_id, account_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS crew_accounts_one_owner
    ON crew_accounts (crew_id) WHERE role = 'owner';
CREATE INDEX IF NOT EXISTS crew_accounts_by_account ON crew_accounts (account_id);

-- A place must belong to the account that owns the game account's link.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'verified_player_links_player_account'
          AND conrelid = 'verified_player_links'::regclass
    ) THEN
        ALTER TABLE verified_player_links
            ADD CONSTRAINT verified_player_links_player_account
            UNIQUE (player_id, account_id);
    END IF;
END
$$;

-- One row per Clash of Clans account in a crew: one place.
CREATE TABLE IF NOT EXISTS crew_players (
    crew_id bigint NOT NULL,
    account_id bigint NOT NULL,
    player_id bigint NOT NULL,
    joined_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (crew_id, player_id),
    FOREIGN KEY (crew_id, account_id)
        REFERENCES crew_accounts (crew_id, account_id) ON DELETE CASCADE,
    FOREIGN KEY (player_id, account_id)
        REFERENCES verified_player_links (player_id, account_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS crew_players_by_player ON crew_players (player_id);
CREATE INDEX IF NOT EXISTS crew_players_by_member ON crew_players (crew_id, account_id);

CREATE TABLE IF NOT EXISTS crew_invites (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL UNIQUE,
    crew_id bigint NOT NULL REFERENCES crews (id) ON DELETE CASCADE,
    created_by_account_id bigint NOT NULL
        REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    code text NOT NULL UNIQUE CHECK (code ~ '^[A-Za-z0-9_-]{22}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    CHECK (expires_at = created_at + interval '48 hours'),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);
CREATE INDEX IF NOT EXISTS crew_invites_by_crew_creator
    ON crew_invites (crew_id, created_by_account_id, created_at DESC);

-- A non-owner left with no places is out of the crew, however the last
-- place went: a kick, removing it, or the game account's link moving.
-- Everything that removes a place holds the crew row first, so two places
-- going at once check in turn and the second sees the first gone.
CREATE OR REPLACE FUNCTION clashlens_crew_member_without_places()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM crew_accounts AS member
    WHERE member.crew_id = OLD.crew_id
      AND member.account_id = OLD.account_id
      AND member.role <> 'owner'
      AND NOT EXISTS (
          SELECT 1 FROM crew_players AS place
          WHERE place.crew_id = OLD.crew_id AND place.account_id = OLD.account_id
      );
    RETURN NULL;
END
$$;
DROP TRIGGER IF EXISTS crew_players_member_without_places ON crew_players;
CREATE TRIGGER crew_players_member_without_places
    AFTER DELETE ON crew_players
    FOR EACH ROW EXECUTE FUNCTION clashlens_crew_member_without_places();

-- When the operator's support transfer moves a game account to another
-- Clash Lens account, it leaves every crew first. BEFORE, so the place
-- is gone before the link check above sees the new owner. It locks those
-- crews first, as every crew write does. A join waiting on this link while
-- holding one of those crews is a deadlock: PostgreSQL aborts one side with
-- nothing changed, the website tries again and the operator reruns.
CREATE OR REPLACE FUNCTION clashlens_crew_link_moved()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM crews
    WHERE id IN (SELECT crew_id FROM crew_players WHERE player_id = OLD.player_id)
    ORDER BY id
    FOR UPDATE;
    DELETE FROM crew_players WHERE player_id = OLD.player_id;
    RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS verified_player_links_crew_link_moved ON verified_player_links;
CREATE TRIGGER verified_player_links_crew_link_moved
    BEFORE UPDATE OF account_id ON verified_player_links
    FOR EACH ROW WHEN (OLD.account_id IS DISTINCT FROM NEW.account_id)
    EXECUTE FUNCTION clashlens_crew_link_moved();

REVOKE ALL ON crews, crew_accounts, crew_players, crew_invites FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON crews, crew_accounts, crew_players, crew_invites TO clashlens_python_api;
GRANT USAGE ON SEQUENCE crews_id_seq, crew_invites_id_seq TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations (version) VALUES (95)
ON CONFLICT (version) DO NOTHING;
COMMIT;
