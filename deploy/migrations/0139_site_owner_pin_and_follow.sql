-- Clash Lens deployment migration 0139.
-- The site owner (migration 0138) may pin one player, so a plain load of
-- their dashboard opens that player's read-only View as, and may follow any
-- player's Discord attack alerts and Legend day summary on their own Discord,
-- the same DMs the player's linked account gets.
--
-- site_owner_pins holds at most one row per owner account, and its reference
-- to site_owner_accounts means only an owner can hold one: removing the owner
-- removes the pin. The data service writes it only through api_view_as.py.
--
-- A followed player's switches are the existing discord_attack_alerts and
-- discord_day_summary_on rows, keyed by the owner's own account, so they
-- never touch the linked account's rows. The bot now sends an account a
-- player's DMs while that player is linked to the account, as before, or
-- while the account is a site owner. The link-change triggers (migrations
-- 0108 and 0112) delete only the rows of the account the link leaves, so a
-- player moving between two other accounts never changes the owner's rows.
-- Removing an owner deletes, in the same transaction, the owner's switches
-- for players not linked to that account, and the bot checks the owner record
-- again before each DM, so none is sent once the owner record is gone.
-- Growth: one pin row per owner, one switch row per player the owner follows.
-- This migration deletes nothing.
BEGIN;

CREATE TABLE IF NOT EXISTS site_owner_pins (
    account_id bigint PRIMARY KEY
        REFERENCES site_owner_accounts (account_id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    pinned_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL ON site_owner_pins FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON site_owner_pins TO clashlens_python_api;

CREATE OR REPLACE FUNCTION clashlens_forget_owner_follows()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    DELETE FROM discord_attack_alerts AS alert
    WHERE alert.account_id = OLD.account_id
      AND NOT EXISTS (
          SELECT 1 FROM verified_player_links AS link
          WHERE link.account_id = alert.account_id AND link.player_id = alert.player_id
      );
    DELETE FROM discord_day_summary_on AS summary_on
    WHERE summary_on.account_id = OLD.account_id
      AND NOT EXISTS (
          SELECT 1 FROM verified_player_links AS link
          WHERE link.account_id = summary_on.account_id
            AND link.player_id = summary_on.player_id
      );
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS site_owner_accounts_forget_follows ON site_owner_accounts;
CREATE TRIGGER site_owner_accounts_forget_follows
AFTER DELETE ON site_owner_accounts
FOR EACH ROW EXECUTE FUNCTION clashlens_forget_owner_follows();

-- As migration 0108's, but a battle is due for an account whose alerts for
-- the defender are on while the defender is linked to that account or the
-- account is a site owner.
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
    saved_at timestamptz,
    ranked_day_start timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT alert.account_id, identity.provider_subject, battle.id, battle.defender_player_id,
           defender.normalized_tag, defender_profile.name,
           attacker.normalized_tag, COALESCE(attacker_profile.name, evidence.opponent_name),
           evidence.stars, evidence.destruction_percentage, evidence.defender_loss,
           battle.created_at, battle.ranked_day_start
    FROM discord_attack_alerts AS alert
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
    WHERE (
            EXISTS (
                SELECT 1 FROM verified_player_links AS link
                WHERE link.player_id = alert.player_id AND link.account_id = alert.account_id
            )
            OR EXISTS (
                SELECT 1 FROM site_owner_accounts AS owner
                WHERE owner.account_id = alert.account_id
            )
        )
      AND NOT EXISTS (
        SELECT 1 FROM discord_attack_alert_deliveries AS sent
        WHERE sent.account_id = alert.account_id AND sent.battle_id = battle.id
    )
    ORDER BY battle.created_at, battle.id;
$$;

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_forget_owner_follows() SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_discord_attack_alerts_due(timestamptz) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_forget_owner_follows(),
    clashlens_discord_attack_alerts_due(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clashlens_discord_attack_alerts_due(timestamptz)
    TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (139)
ON CONFLICT (version) DO NOTHING;
COMMIT;
