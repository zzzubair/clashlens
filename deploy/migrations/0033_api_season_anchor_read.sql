-- Clash Lens deployment migration 0033.
-- Current-season army analytics resolves its season from the confirmed anchor.
BEGIN;

GRANT SELECT ON TABLE legend_season_anchors TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (33)
ON CONFLICT (version) DO NOTHING;
COMMIT;
