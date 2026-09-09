package collector

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// Durable run-scoped outbound request budgets for issue #92 (B2). When
// enabled, every official dispatch path reserves one unit at
// beginEndpointRequest, before officialAPIClient.fetch, through a single
// atomic UPDATE that succeeds only below the configured cap and before the
// run deadline. Reservations persist in collector_endpoint_budgets across
// process and container restarts, survive concurrency, and are never
// refunded: ambiguous attempts stay consumed. A nil *endpointBudgetConfig
// disables budgeting entirely and leaves ordinary behavior unchanged.

var (
	errEndpointBudgetExhausted    = errors.New("collector endpoint request budget is exhausted")
	errEndpointBudgetUnconfigured = errors.New("collector endpoint request budget is not configured")
	errEndpointBudgetConflict     = errors.New("collector endpoint request budget conflicts with durable state")
)

var endpointBudgetRunIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

type endpointBudgetConfig struct {
	enabled  bool
	runID    string
	caps     map[endpointName]int
	deadline time.Time
}

func loadEndpointBudgetConfig(getenv func(string) string) (endpointBudgetConfig, error) {
	enabled, err := optionalBool(getenv, "CLASHLENS_ENDPOINT_BUDGET_ENABLED", false)
	if err != nil {
		return endpointBudgetConfig{}, err
	}
	if !enabled {
		return endpointBudgetConfig{}, nil
	}
	runID := strings.TrimSpace(getenv("CLASHLENS_ENDPOINT_BUDGET_RUN_ID"))
	if !endpointBudgetRunIDPattern.MatchString(runID) {
		return endpointBudgetConfig{}, errors.New("CLASHLENS_ENDPOINT_BUDGET_RUN_ID must be 1-128 [A-Za-z0-9._-] characters")
	}
	deadlineRaw := strings.TrimSpace(getenv("CLASHLENS_ENDPOINT_BUDGET_DEADLINE_AT"))
	deadline, err := time.Parse(time.RFC3339, deadlineRaw)
	if err != nil {
		return endpointBudgetConfig{}, errors.New("CLASHLENS_ENDPOINT_BUDGET_DEADLINE_AT must be RFC 3339")
	}
	caps := make(map[endpointName]int, 3)
	for endpoint, setting := range map[endpointName]string{
		profileEndpoint:              "CLASHLENS_ENDPOINT_BUDGET_PROFILE",
		globalPlayerRankingsEndpoint: "CLASHLENS_ENDPOINT_BUDGET_GLOBAL_RANKINGS",
		battleLogEndpoint:            "CLASHLENS_ENDPOINT_BUDGET_BATTLE_LOG",
	} {
		cap, err := endpointBudgetCap(getenv, setting)
		if err != nil {
			return endpointBudgetConfig{}, err
		}
		caps[endpoint] = cap
	}
	return endpointBudgetConfig{
		enabled:  true,
		runID:    runID,
		caps:     caps,
		// PostgreSQL timestamptz keeps microsecond precision; truncate
		// here so the restart agreement check compares exact values.
		deadline: deadline.UTC().Truncate(time.Microsecond),
	}, nil
}

func endpointBudgetCap(getenv func(string) string, name string) (int, error) {
	raw := strings.TrimSpace(getenv(name))
	if raw == "" {
		return 0, fmt.Errorf("%s must be a non-negative integer", name)
	}
	parsed := 0
	for _, digit := range []byte(raw) {
		if digit < '0' || digit > '9' {
			return 0, fmt.Errorf("%s must be a non-negative integer", name)
		}
		parsed = parsed*10 + int(digit-'0')
		if parsed > 1000000 {
			return 0, fmt.Errorf("%s must be a non-negative integer", name)
		}
	}
	return parsed, nil
}

func (s *store) setEndpointBudget(config endpointBudgetConfig) {
	if !config.enabled {
		s.endpointBudget = nil
		return
	}
	s.endpointBudget = &config
}

// reserveEndpointBudget consumes one durable budget unit for endpoint. The
// seed INSERT is a no-op across restarts; the stored cap and deadline must
// agree with configuration or the reservation fails closed; the consuming
// UPDATE is atomic so concurrent dispatchers can never exceed the cap.
func (s *store) reserveEndpointBudget(ctx context.Context, endpoint endpointName) error {
	budget := s.endpointBudget
	if budget == nil {
		return nil
	}
	cap, ok := budget.caps[endpoint]
	if !ok {
		return errEndpointBudgetUnconfigured
	}
	if _, err := s.pool.Exec(ctx, `
		INSERT INTO collector_endpoint_budgets (run_id, endpoint, cap, consumed, deadline_at)
		VALUES ($1, $2, $3, 0, $4)
		ON CONFLICT (run_id, endpoint) DO NOTHING
	`, budget.runID, string(endpoint), cap, budget.deadline); err != nil {
		return fmt.Errorf("seed endpoint budget: %w", err)
	}
	var storedCap int
	var storedDeadline time.Time
	if err := s.pool.QueryRow(ctx, `
		SELECT cap, deadline_at
		FROM collector_endpoint_budgets
		WHERE run_id = $1 AND endpoint = $2
	`, budget.runID, string(endpoint)).Scan(&storedCap, &storedDeadline); err != nil {
		return fmt.Errorf("read endpoint budget: %w", err)
	}
	if storedCap != cap || !storedDeadline.UTC().Equal(budget.deadline) {
		return errEndpointBudgetConflict
	}
	var consumed int
	if err := s.pool.QueryRow(ctx, `
		UPDATE collector_endpoint_budgets
		SET consumed = consumed + 1,
			updated_at = clock_timestamp()
		WHERE run_id = $1
			AND endpoint = $2
			AND consumed < cap
			AND clock_timestamp() < deadline_at
		RETURNING consumed
	`, budget.runID, string(endpoint)).Scan(&consumed); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return errEndpointBudgetExhausted
		}
		return fmt.Errorf("consume endpoint budget: %w", err)
	}
	return nil
}
