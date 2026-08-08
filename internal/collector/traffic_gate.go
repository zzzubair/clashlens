package collector

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
)

var errSharedCredentialConflict = errors.New("shared API credential registration conflicts with the durable gate")

type trafficGateMode string

const (
	bridgeTrafficGateMode   trafficGateMode = "bridge"
	requiredTrafficGateMode trafficGateMode = "required"
)

type sharedPermit struct {
	granted        bool
	databaseTime   time.Time
	nextEligibleAt *time.Time
	state          string
}

func parseBearerTokenBytes(contents []byte) (string, error) {
	if len(contents) >= 2 && contents[len(contents)-2] == '\r' && contents[len(contents)-1] == '\n' {
		contents = contents[:len(contents)-2]
	} else if len(contents) >= 1 && contents[len(contents)-1] == '\n' {
		contents = contents[:len(contents)-1]
	}
	if len(contents) == 0 {
		return "", errors.New("bearer token is empty")
	}
	for _, value := range contents {
		if value < 0x21 || value > 0x7e {
			return "", errors.New("bearer token must contain only non-whitespace ASCII bytes")
		}
	}
	return string(contents), nil
}

func bearerTokenFingerprint(secret string) string {
	digest := sha256.Sum256([]byte(secret))
	return hex.EncodeToString(digest[:])
}

func (s *store) validateTrafficGateMode(ctx context.Context, mode trafficGateMode) error {
	var expectedVersion int
	switch mode {
	case bridgeTrafficGateMode:
		expectedVersion = 1
	case requiredTrafficGateMode:
		expectedVersion = 2
	default:
		return fmt.Errorf("unknown shared traffic-gate mode %q", mode)
	}
	version, err := s.currentContractVersion(ctx)
	if err != nil {
		return fmt.Errorf("read contract for shared traffic gate: %w", err)
	}
	if version != expectedVersion {
		return fmt.Errorf("%s shared traffic gate needs contract version %d, found %d", mode, expectedVersion, version)
	}
	return nil
}

func (s *store) registerSharedCredential(
	ctx context.Context,
	fingerprint string,
	goBudget int,
	pythonBudget int,
	totalBudget int,
	actor string,
) error {
	if goBudget != 29 || pythonBudget != 1 || totalBudget != 30 {
		return errSharedCredentialConflict
	}
	transaction, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin shared credential registration: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()
	command, err := transaction.Exec(ctx, `
		INSERT INTO shared_api_credentials (
			credential_fingerprint, go_budget, python_budget, total_budget
		) VALUES ($1, $2, $3, $4)
		ON CONFLICT (credential_fingerprint) DO NOTHING
	`, fingerprint, goBudget, pythonBudget, totalBudget)
	if err != nil {
		return fmt.Errorf("register shared credential: %w", err)
	}
	var registeredGo, registeredPython, registeredTotal int
	var kind string
	if err := transaction.QueryRow(ctx, `
		SELECT credential_kind, go_budget, python_budget, total_budget
		FROM shared_api_credentials
		WHERE credential_fingerprint = $1
		FOR UPDATE
	`, fingerprint).Scan(&kind, &registeredGo, &registeredPython, &registeredTotal); err != nil {
		return fmt.Errorf("read shared credential registration: %w", err)
	}
	if kind != "supercell_interactive" || registeredGo != goBudget ||
		registeredPython != pythonBudget || registeredTotal != totalBudget {
		return errSharedCredentialConflict
	}
	if command.RowsAffected() == 1 {
		if _, err := transaction.Exec(ctx, `
			INSERT INTO shared_api_credential_events (
				credential_fingerprint, event_type, actor
			) VALUES ($1, 'registered', $2)
		`, fingerprint, actor); err != nil {
			return fmt.Errorf("audit shared credential registration: %w", err)
		}
	}
	if err := transaction.Commit(ctx); err != nil {
		return fmt.Errorf("commit shared credential registration: %w", err)
	}
	return nil
}

func (s *store) acquireSharedPermit(ctx context.Context, fingerprint, caller string) (sharedPermit, error) {
	var permit sharedPermit
	if err := s.pool.QueryRow(ctx, `
		SELECT granted, database_time, next_eligible_at, credential_state
		FROM clashlens_acquire_shared_api_permit($1, $2)
	`, fingerprint, caller).Scan(
		&permit.granted,
		&permit.databaseTime,
		&permit.nextEligibleAt,
		&permit.state,
	); err != nil {
		return sharedPermit{}, fmt.Errorf("acquire shared API permit: %w", err)
	}
	return permit, nil
}

func (s *store) cooldownSharedCredential(
	ctx context.Context,
	fingerprint string,
	duration time.Duration,
	actor string,
	reason string,
) error {
	if duration <= 0 {
		duration = time.Second
	}
	if duration > 5*time.Minute {
		duration = 5 * time.Minute
	}
	transaction, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin shared credential cooldown: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()
	var cooldownUntil time.Time
	if err := transaction.QueryRow(ctx, `
		UPDATE shared_api_credentials
		SET state = 'cooldown',
			cooldown_until = clock_timestamp() + ($2 * interval '1 microsecond'),
			quarantine_reason = NULL,
			updated_at = clock_timestamp()
		WHERE credential_fingerprint = $1
		  AND state IN ('active', 'cooldown')
		RETURNING cooldown_until
	`, fingerprint, duration.Microseconds()).Scan(&cooldownUntil); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			// A quarantine or terminal operator state won the race.
			// Quarantine precedence is monotonic, so the losing cooldown
			// is a valid no-op: leave the durable state untouched and do
			// not record a misleading cooldown event.
			return nil
		}
		return fmt.Errorf("cool down shared credential: %w", err)
	}
	if _, err := transaction.Exec(ctx, `
		INSERT INTO shared_api_credential_events (
			credential_fingerprint, event_type, actor, reason, cooldown_until
		) VALUES ($1, 'cooldown', $2, $3, $4)
	`, fingerprint, actor, reason, cooldownUntil); err != nil {
		return fmt.Errorf("audit shared credential cooldown: %w", err)
	}
	if err := transaction.Commit(ctx); err != nil {
		return fmt.Errorf("commit shared credential cooldown: %w", err)
	}
	return nil
}

func (s *store) quarantineSharedCredential(ctx context.Context, fingerprint, actor, reason string) error {
	transaction, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin shared credential quarantine: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()
	command, err := transaction.Exec(ctx, `
		UPDATE shared_api_credentials
		SET state = 'quarantined',
			cooldown_until = NULL,
			quarantine_reason = $2,
			updated_at = clock_timestamp()
		WHERE credential_fingerprint = $1
	`, fingerprint, reason)
	if err != nil {
		return fmt.Errorf("quarantine shared credential: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errors.New("shared credential is not registered")
	}
	if _, err := transaction.Exec(ctx, `
		INSERT INTO shared_api_credential_events (
			credential_fingerprint, event_type, actor, reason
		) VALUES ($1, 'quarantined', $2, $3)
	`, fingerprint, actor, reason); err != nil {
		return fmt.Errorf("audit shared credential quarantine: %w", err)
	}
	if err := transaction.Commit(ctx); err != nil {
		return fmt.Errorf("commit shared credential quarantine: %w", err)
	}
	return nil
}

func (s *store) cleanupSharedPermits(ctx context.Context, batchSize int) (int, error) {
	var deleted int
	if err := s.pool.QueryRow(ctx, `
		SELECT clashlens_cleanup_shared_api_permits($1)
	`, batchSize).Scan(&deleted); err != nil {
		return 0, fmt.Errorf("clean up shared API permits: %w", err)
	}
	return deleted, nil
}
