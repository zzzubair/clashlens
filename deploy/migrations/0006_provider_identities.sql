-- Permit the two Phase 1 login providers and add the provider audit.
-- Google rows are preserved unchanged; uniqueness stays on both
-- (provider, provider_subject) and (account_id, provider).

BEGIN;

LOCK TABLE clash_lens_accounts IN ACCESS EXCLUSIVE MODE;

ALTER TABLE account_provider_identities
    DROP CONSTRAINT IF EXISTS account_provider_identities_provider_check,
    DROP CONSTRAINT IF EXISTS account_provider_identities_provider_v2_check;
ALTER TABLE account_provider_identities
    ADD CONSTRAINT account_provider_identities_provider_v2_check
    CHECK (provider IN ('google', 'discord'));

-- Minimum durable audit for provider link, unlink, and support recovery
-- events. Provider subjects are not duplicated here: the identity row is the
-- single carrier, so an audit never widens personal-data storage.
-- Self-service refusals (collision, final provider) stay in
-- private_api_requests only; refused_collision and failed belong to support
-- recovery after target-account resolution.
CREATE TABLE IF NOT EXISTS provider_identity_audits (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    provider text NOT NULL CHECK (provider IN ('google', 'discord')),
    action text NOT NULL CHECK (action IN ('link', 'unlink', 'support_recovery')),
    result text NOT NULL CHECK (
        result IN (
            'succeeded',
            'refused_collision',
            'failed'
        )
    ),
    operator_identity text CHECK (
        operator_identity IS NULL
        OR char_length(operator_identity) BETWEEN 1 AND 255
    ),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS provider_identity_audits_account_time_v1
    ON provider_identity_audits (account_id, created_at DESC);
REVOKE ALL PRIVILEGES ON TABLE provider_identity_audits FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE provider_identity_audits_id_seq FROM PUBLIC;

-- Unlink removes one owned identity row, so the Python API role needs
-- exactly this DELETE grant; no other runtime role gains any DELETE.
GRANT DELETE ON TABLE account_provider_identities TO clashlens_python_api;

-- The Python API owns link, unlink, and support-recovery audit writes; no
-- other runtime role receives any privilege on the audit relation.
GRANT SELECT, INSERT ON TABLE provider_identity_audits TO clashlens_python_api;
GRANT USAGE ON SEQUENCE provider_identity_audits_id_seq TO clashlens_python_api;
REVOKE ALL ON TABLE provider_identity_audits
    FROM clashlens_collector, clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations (version)
VALUES (6)
ON CONFLICT (version) DO NOTHING;

COMMIT;
