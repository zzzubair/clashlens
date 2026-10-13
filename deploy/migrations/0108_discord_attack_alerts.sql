-- Clash Lens deployment migration 0108.
-- Discord attack alerts: a Clasher turns them on per verified player on the
-- website, and the Discord bot then sends a short confirmation DM, and a DM
-- for each defense of that player saved after the switch went on.
-- Reconnecting Discord asks for the confirmation again without moving when
-- the switch went on, so no saved defense is skipped.
--
-- discord_attack_alerts holds one row per player whose alerts are on, so it
-- is never larger than the verified player list. Turning alerts off deletes
-- the row. Unverifying the player or moving it to another account deletes it
-- in the same transaction, so moving it away and back does not restore it.
--
-- discord_attack_alert_deliveries is the bot's log of every DM it tried: one
-- row per battle and account, and one per confirmation asked for. The bot
-- writes the row before it first sends, and every try sends the same Discord
-- nonce, which Discord checks for repeats for a few minutes. A DM Discord did
-- not take, whatever the cause (a 5xx or 429 answer, a network error, a
-- timeout, an answer cut off), is tried again only within 2 minutes of its
-- first try, when this row was written, inside Discord's nonce check. That
-- limit is checked with a fresh clock before each try; past it the DM is
-- logged as not delivered. A refusal is never tried again, and a crash leaves
-- the row unfinished, never tried again, so no DM Discord took is sent twice.
-- It is the only table the bot writes; the bot deletes rows older than 7
-- days, so at most 7 days of alerts are kept: about 8 rows a day per player
-- with alerts on.
BEGIN;

CREATE TABLE IF NOT EXISTS discord_attack_alerts (
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    enabled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- When the bot was last asked to confirm the switch: when it went on, or
    -- when Discord was last connected again since.
    confirm_requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (account_id, player_id)
);

-- battle_id is a legend_battles row, without a reference so the log never
-- holds up the worker changing or removing battles. A confirmation has no
-- battle; `confirms` is the confirm_requested_at it answers.
CREATE TABLE IF NOT EXISTS discord_attack_alert_deliveries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    player_id bigint NOT NULL,
    battle_id bigint,
    confirms timestamptz,
    -- sending: claimed and not finished; the bot tries it again while Discord
    -- cannot be reached, and a crash leaves it here, never tried again.
    -- refused: Discord would not deliver it, for `reason`; failed: Discord did
    -- not take it within 2 minutes of its first try.
    state text NOT NULL CHECK (state IN ('sending', 'sent', 'refused', 'failed')),
    reason text CHECK (reason IN ('no_shared_server', 'dms_closed', 'unknown_user', 'other')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    CHECK ((battle_id IS NULL) <> (confirms IS NULL)),
    UNIQUE (account_id, battle_id),
    UNIQUE (account_id, player_id, confirms)
);

REVOKE ALL PRIVILEGES ON TABLE discord_attack_alerts, discord_attack_alert_deliveries
    FROM PUBLIC;
-- The website saves the switches through the private API, and the bot runs
-- with the API's database login. The delivery log is the one table the bot
-- writes; the website only reads it, to show alerts Discord refused.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE discord_attack_alerts TO clashlens_python_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE discord_attack_alert_deliveries
    TO clashlens_python_api;

CREATE OR REPLACE FUNCTION clashlens_forget_attack_alerts()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.account_id IS DISTINCT FROM NEW.account_id THEN
        DELETE FROM discord_attack_alerts
        WHERE account_id = OLD.account_id AND player_id = OLD.player_id;
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS verified_player_links_forget_attack_alerts ON verified_player_links;
CREATE TRIGGER verified_player_links_forget_attack_alerts
AFTER DELETE OR UPDATE OF account_id ON verified_player_links
FOR EACH ROW EXECUTE FUNCTION clashlens_forget_attack_alerts();

-- The alerts due now: each defense saved since `since` against a player
-- whose alerts are on, saved after they went on, for the owning account's
-- Discord user, and not yet in the delivery log. The battle's numbers come
-- from the defender's report when saved, else the attacker's. An attacker
-- with no saved profile is named as the defender's report names them. The
-- bot cannot read battles itself; this shows it only these. Battle ids grow
-- as battles are saved, so only battles above the newest one saved 5 minutes
-- before `since` are read: a short walk back from the newest battle, then the
-- few hundred battles saved since, without a new index.
CREATE OR REPLACE FUNCTION clashlens_discord_attack_alerts_due(since timestamptz)
RETURNS TABLE (
    account_id bigint,
    discord_user_id text,
    battle_id bigint,
    defender_player_id bigint,
    defender_tag text,
    defender_name text,
    attacker_tag text,
    attacker_name text,
    stars integer,
    destruction_percentage integer,
    trophies_lost integer,
    saved_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT alert.account_id, identity.provider_subject, battle.id, battle.defender_player_id,
           defender.normalized_tag, defender_profile.name,
           attacker.normalized_tag, COALESCE(attacker_profile.name, evidence.opponent_name),
           evidence.stars, evidence.destruction_percentage, evidence.defender_loss,
           battle.created_at
    FROM discord_attack_alerts AS alert
    JOIN verified_player_links AS link
        ON link.player_id = alert.player_id AND link.account_id = alert.account_id
    JOIN account_provider_identities AS identity
        ON identity.account_id = alert.account_id AND identity.provider = 'discord'
    JOIN legend_battles AS battle
        ON battle.defender_player_id = alert.player_id
       AND battle.id > COALESCE((
               SELECT older.id FROM legend_battles AS older
               WHERE older.created_at < since - interval '5 minutes'
               ORDER BY older.id DESC
               LIMIT 1
           ), 0)
       AND battle.created_at >= since
       AND battle.created_at > alert.enabled_at
    JOIN LATERAL (
        SELECT report.stars, report.destruction_percentage, report.defender_loss,
               CASE WHEN side.perspective = 'defender' THEN
                   CASE report.parser_version
                       WHEN 'supercell-source-parser-v1'
                           THEN source_row.source_json -> 'opponent' ->> 'name'
                       ELSE source_row.source_json ->> 'opponentName'
                   END
               END AS opponent_name
        FROM battle_perspectives AS side
        JOIN battle_evidence AS report ON report.id = side.evidence_id
        LEFT JOIN battle_source_rows AS source_row ON source_row.id = report.source_row_id
        WHERE side.battle_id = battle.id
        ORDER BY side.perspective = 'defender' DESC
        LIMIT 1
    ) AS evidence ON true
    JOIN players AS defender ON defender.id = battle.defender_player_id
    JOIN players AS attacker ON attacker.id = battle.attacker_player_id
    LEFT JOIN player_profile_versions AS defender_profile
        ON defender_profile.id = defender.current_profile_version_id
    LEFT JOIN player_profile_versions AS attacker_profile
        ON attacker_profile.id = attacker.current_profile_version_id
    WHERE NOT EXISTS (
        SELECT 1 FROM discord_attack_alert_deliveries AS sent
        WHERE sent.account_id = alert.account_id AND sent.battle_id = battle.id
    )
    ORDER BY battle.created_at, battle.id;
$$;

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_forget_attack_alerts() SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_discord_attack_alerts_due(timestamptz) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_forget_attack_alerts(),
    clashlens_discord_attack_alerts_due(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clashlens_discord_attack_alerts_due(timestamptz)
    TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (108)
ON CONFLICT (version) DO NOTHING;
COMMIT;
