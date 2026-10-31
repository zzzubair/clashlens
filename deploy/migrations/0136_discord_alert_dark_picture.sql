-- Clash Lens deployment migration 0136.
-- The picture at the bottom of each attack alert is drawn light, or dark for
-- a Clash Lens account that turned on the dark picture on the website's
-- Discord alerts page; one switch per account, off until turned on.
--
-- discord_alert_dark_picture holds one row per account with the switch on, so
-- it is never larger than the account list. Turning it off deletes the row,
-- and deleting the account deletes it.
BEGIN;

CREATE TABLE IF NOT EXISTS discord_alert_dark_picture (
    account_id bigint PRIMARY KEY REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    enabled_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL PRIVILEGES ON TABLE discord_alert_dark_picture FROM PUBLIC;
-- The website saves the switch through the private API, and the bot runs
-- with the API's database login.
GRANT SELECT, INSERT, DELETE ON TABLE discord_alert_dark_picture TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (136)
ON CONFLICT (version) DO NOTHING;
COMMIT;
