-- Clash Lens deployment migration 0094.
-- Linking a player queues an immediate profile check (api_verification.py), so
-- its name and league show and its own profile decides whether it is tracked.
-- A support transfer, which moves a verified link to its new owner, saved the
-- new owner without one. The transfer function is re-created unchanged except
-- that it queues the same check, skipping the 30-second wait after a recent
-- check, when it moves the link. A repeated call for an already finished
-- transfer queues nothing. No row changes.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_support_transfer(
    requested_verification_request_id uuid,
    requested_player_tag text,
    requested_from_account_public_id uuid,
    requested_to_account_public_id uuid,
    requested_operator_identity text,
    requested_reason text
)
RETURNS TABLE (status text, tag text)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    candidate_row record;
    current_link bigint;
BEGIN
    IF session_user <> 'clashlens_support_transfer' THEN
        RAISE EXCEPTION 'support role required' USING ERRCODE = '42501';
    END IF;
    IF requested_player_tag !~ '^#[0289PYLQGRJCUV]{3,15}$'
       OR requested_from_account_public_id IS NULL
       OR requested_to_account_public_id IS NULL
       OR requested_from_account_public_id = requested_to_account_public_id
       OR char_length(requested_operator_identity) NOT BETWEEN 1 AND 255
       OR char_length(requested_reason) NOT BETWEEN 8 AND 500
       OR requested_operator_identity ~ '[[:cntrl:]]'
       OR requested_reason ~ '[[:cntrl:]]'
    THEN
        RAISE EXCEPTION 'invalid support transfer request' USING ERRCODE = '22023';
    END IF;

    SELECT candidate.player_id,
           candidate.from_account_id,
           candidate.to_account_id,
           candidate.verified_at,
           candidate.expires_at,
           candidate.state,
           player.normalized_tag,
           from_account.public_id AS from_public_id,
           to_account.public_id AS to_public_id
    INTO candidate_row
    FROM support_player_link_transfer_candidates AS candidate
    JOIN players AS player ON player.id = candidate.player_id
    JOIN clash_lens_accounts AS from_account
      ON from_account.id = candidate.from_account_id
    JOIN clash_lens_accounts AS to_account
      ON to_account.id = candidate.to_account_id
    WHERE candidate.verification_request_id = requested_verification_request_id
    FOR UPDATE OF candidate;

    IF NOT FOUND THEN
        RETURN QUERY SELECT 'transfer_not_found'::text, NULL::text;
        RETURN;
    END IF;
    IF candidate_row.normalized_tag <> requested_player_tag
       OR candidate_row.from_public_id <> requested_from_account_public_id
       OR candidate_row.to_public_id <> requested_to_account_public_id
    THEN
        RETURN QUERY SELECT 'transfer_conflict'::text, NULL::text;
        RETURN;
    END IF;

    IF candidate_row.state IN ('consumed', 'completed') THEN
        IF EXISTS (
            SELECT 1
            FROM support_player_link_transfer_audits AS audit
            WHERE audit.verification_request_id = requested_verification_request_id
              AND audit.operator_identity = requested_operator_identity
              AND audit.reason = requested_reason
        ) THEN
            RETURN QUERY SELECT 'transferred'::text, candidate_row.normalized_tag;
        ELSE
            RETURN QUERY SELECT 'transfer_conflict'::text, NULL::text;
        END IF;
        RETURN;
    END IF;
    IF candidate_row.state = 'expired' THEN
        RETURN QUERY SELECT 'fresh_verification_required'::text, NULL::text;
        RETURN;
    END IF;
    IF candidate_row.state <> 'pending' THEN
        RETURN QUERY SELECT 'transfer_not_pending'::text, NULL::text;
        RETURN;
    END IF;
    IF clock_timestamp() >= candidate_row.expires_at THEN
        UPDATE support_player_link_transfer_candidates
        SET state = 'expired'
        WHERE verification_request_id = requested_verification_request_id;
        RETURN QUERY SELECT 'fresh_verification_required'::text, NULL::text;
        RETURN;
    END IF;

    SELECT account_id INTO current_link
    FROM verified_player_links
    WHERE player_id = candidate_row.player_id
    FOR UPDATE;
    IF NOT FOUND OR current_link <> candidate_row.from_account_id THEN
        RETURN QUERY SELECT 'link_owner_changed'::text, NULL::text;
        RETURN;
    END IF;

    UPDATE verified_player_links
    SET account_id = candidate_row.to_account_id,
        verification_request_id = requested_verification_request_id,
        verified_at = candidate_row.verified_at,
        updated_at = clock_timestamp()
    WHERE player_id = candidate_row.player_id;
    PERFORM clashlens_enqueue_interactive(
        'initial_collection', candidate_row.normalized_tag, 30, true
    );
    UPDATE support_player_link_transfer_candidates
    SET state = 'consumed', consumed_at = clock_timestamp(), completed_at = clock_timestamp()
    WHERE verification_request_id = requested_verification_request_id;
    INSERT INTO support_player_link_transfer_audits (
        verification_request_id, player_id, from_account_id, to_account_id,
        operator_identity, reason
    ) VALUES (
        requested_verification_request_id, candidate_row.player_id,
        candidate_row.from_account_id, candidate_row.to_account_id,
        requested_operator_identity, requested_reason
    );
    RETURN QUERY SELECT 'transferred'::text, candidate_row.normalized_tag;
END
$$;

DO $$
DECLARE
    support_schema_name text := current_schema();
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_support_transfer(uuid, text, uuid, uuid, text, text) SET search_path TO pg_catalog, %I',
        support_schema_name,
        support_schema_name
    );
END
$$;

INSERT INTO clash_lens_schema_migrations(version) VALUES (94)
ON CONFLICT (version) DO NOTHING;
COMMIT;
