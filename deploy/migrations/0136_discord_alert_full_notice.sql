-- Clash Lens deployment migration 0136.
-- Attack alerts with every detail in the notification: one switch per Clash
-- Lens account, off until the Clasher turns it on on the website's Discord
-- alerts page. A phone's notification shows only a DM's text, never its box,
-- so with the switch on the bot also writes the attacker, stars, destruction
-- and the attacker's stats into that text.
--
-- discord_alert_full_notice holds one row per account with the switch on, so
-- it is never larger than the account list. Turning it off deletes the row,
-- and deleting the account deletes it.
BEGIN;

CREATE TABLE IF NOT EXISTS discord_alert_full_notice (
    account_id bigint PRIMARY KEY REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    enabled_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL PRIVILEGES ON TABLE discord_alert_full_notice FROM PUBLIC;
-- The website saves the switch through the private API, and the bot runs
-- with the API's database login.
GRANT SELECT, INSERT, DELETE ON TABLE discord_alert_full_notice TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (136)
ON CONFLICT (version) DO NOTHING;
COMMIT;
