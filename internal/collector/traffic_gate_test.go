package collector

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

func TestParseBearerTokenBytesPreservesExactASCIIAndOneLineEnding(t *testing.T) {
	for name, input := range map[string][]byte{
		"no line ending": []byte("abc-DEF_123.456"),
		"LF":             []byte("abc-DEF_123.456\n"),
		"CRLF":           []byte("abc-DEF_123.456\r\n"),
	} {
		t.Run(name, func(t *testing.T) {
			got, err := parseBearerTokenBytes(input)
			if err != nil {
				t.Fatalf("parse exact bearer token: %v", err)
			}
			if got != "abc-DEF_123.456" {
				t.Fatalf("parsed bearer token = %q", got)
			}
		})
	}

	for name, input := range map[string][]byte{
		"leading space":    []byte(" token"),
		"trailing space":   []byte("token "),
		"tab":              []byte("to\tken"),
		"two line endings": []byte("token\n\n"),
		"bare carriage":    []byte("token\r"),
		"non ASCII":        []byte("tok\xc3\xa9n"),
		"empty":            nil,
		"only line ending": []byte("\n"),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := parseBearerTokenBytes(input); err == nil {
				t.Fatal("invalid bearer-token bytes were accepted")
			}
		})
	}
}

func TestSharedTrafficGateEnforcesCallerAndCombinedRollingBudgets(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	fingerprint := bearerTokenFingerprint("shared-interactive-secret")
	if err := store.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "collector:test"); err != nil {
		t.Fatalf("register shared credential: %v", err)
	}

	type result struct {
		caller  string
		granted bool
		err     error
	}
	results := make(chan result, 32)
	var wait sync.WaitGroup
	for range 30 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			permit, err := store.acquireSharedPermit(ctx, fingerprint, "go")
			results <- result{caller: "go", granted: permit.granted, err: err}
		}()
	}
	for range 2 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			permit, err := store.acquireSharedPermit(ctx, fingerprint, "python")
			results <- result{caller: "python", granted: permit.granted, err: err}
		}()
	}
	wait.Wait()
	close(results)

	granted := map[string]int{}
	for result := range results {
		if result.err != nil {
			t.Fatalf("acquire %s permit: %v", result.caller, result.err)
		}
		if result.granted {
			granted[result.caller]++
		}
	}
	if granted["go"] != 29 || granted["python"] != 1 {
		t.Fatalf("shared permits = %v, want 29 Go and 1 Python", granted)
	}

	if err := store.registerSharedCredential(ctx, fingerprint, 28, 1, 29, "collector:conflict"); !errors.Is(err, errSharedCredentialConflict) {
		t.Fatalf("conflicting shared credential registration error = %v", err)
	}
}

func TestSharedTrafficGatePersistsCooldownAndQuarantine(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	fingerprint := bearerTokenFingerprint("persistent-shared-secret")
	if err := store.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "collector:test"); err != nil {
		t.Fatalf("register shared credential: %v", err)
	}
	if err := store.cooldownSharedCredential(ctx, fingerprint, 30*time.Second, "collector:test", "official_api_429"); err != nil {
		t.Fatalf("cool down shared credential: %v", err)
	}
	permit, err := store.acquireSharedPermit(ctx, fingerprint, "go")
	if err != nil {
		t.Fatalf("read cooldown decision: %v", err)
	}
	if permit.granted || permit.state != "cooldown" || permit.nextEligibleAt == nil {
		t.Fatalf("cooldown permit = %+v", permit)
	}

	if err := store.quarantineSharedCredential(ctx, fingerprint, "collector:test", "official_api_authentication_failure"); err != nil {
		t.Fatalf("quarantine shared credential: %v", err)
	}
	permit, err = store.acquireSharedPermit(ctx, fingerprint, "python")
	if err != nil {
		t.Fatalf("read quarantine decision: %v", err)
	}
	if permit.granted || permit.state != "quarantined" || permit.nextEligibleAt != nil {
		t.Fatalf("quarantine permit = %+v", permit)
	}

	var events int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM shared_api_credential_events
		WHERE credential_fingerprint = $1
	`, fingerprint).Scan(&events); err != nil {
		t.Fatalf("count credential events: %v", err)
	}
	if events != 3 {
		t.Fatalf("credential events = %d, want registration, cooldown, quarantine", events)
	}
}

// TestSharedTrafficGateLateCooldownDoesNotOverwriteQuarantine proves
// quarantine has monotonic precedence over cooldown: a successful-call
// cooldown that lands after a quarantine committed must not rewrite the
// durable state, clear the quarantine reason, bump the row timestamp, or
// insert a misleading cooldown event. The losing update is a valid no-op,
// not an error. The same guard must also leave terminal operator states
// such as retired untouched.
func TestSharedTrafficGateLateCooldownDoesNotOverwriteQuarantine(t *testing.T) {
	ctx := context.Background()

	t.Run("quarantined wins over a late cooldown", func(t *testing.T) {
		store := startVersionTwoStore(t, ctx)
		fingerprint := bearerTokenFingerprint("late-cooldown-shared-secret")
		if err := store.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "collector:test"); err != nil {
			t.Fatalf("register shared credential: %v", err)
		}
		if err := store.quarantineSharedCredential(ctx, fingerprint, "collector:test", "official_api_authentication_failure"); err != nil {
			t.Fatalf("quarantine shared credential: %v", err)
		}
		var updatedAt time.Time
		if err := store.pool.QueryRow(ctx, `
			SELECT updated_at FROM shared_api_credentials
			WHERE credential_fingerprint = $1
		`, fingerprint).Scan(&updatedAt); err != nil {
			t.Fatalf("read quarantine timestamp: %v", err)
		}

		// The successful call's cooldown lands after the quarantine won.
		if err := store.cooldownSharedCredential(ctx, fingerprint, 30*time.Second, "collector:test", "official_api_429"); err != nil {
			t.Fatalf("late cooldown must be a valid no-op, got error: %v", err)
		}

		var state string
		var quarantineReason *string
		var cooldownUntil *time.Time
		if err := store.pool.QueryRow(ctx, `
			SELECT state, quarantine_reason, cooldown_until
			FROM shared_api_credentials
			WHERE credential_fingerprint = $1
		`, fingerprint).Scan(&state, &quarantineReason, &cooldownUntil); err != nil {
			t.Fatalf("read shared credential state: %v", err)
		}
		if state != "quarantined" {
			t.Fatalf("late cooldown changed state to %q, want quarantined", state)
		}
		if quarantineReason == nil || *quarantineReason != "official_api_authentication_failure" {
			t.Fatalf("late cooldown changed quarantine reason to %v", quarantineReason)
		}
		if cooldownUntil != nil {
			t.Fatalf("late cooldown set cooldown_until = %v on a quarantined credential", *cooldownUntil)
		}
		var updatedAfter time.Time
		if err := store.pool.QueryRow(ctx, `
			SELECT updated_at FROM shared_api_credentials
			WHERE credential_fingerprint = $1
		`, fingerprint).Scan(&updatedAfter); err != nil {
			t.Fatalf("read updated timestamp: %v", err)
		}
		if !updatedAfter.Equal(updatedAt) {
			t.Fatalf("late cooldown bumped updated_at from %v to %v", updatedAt, updatedAfter)
		}

		permit, err := store.acquireSharedPermit(ctx, fingerprint, "go")
		if err != nil {
			t.Fatalf("read post-cooldown gate decision: %v", err)
		}
		if permit.granted || permit.state != "quarantined" || permit.nextEligibleAt != nil {
			t.Fatalf("post-cooldown permit = %+v, want denied quarantined", permit)
		}

		var cooldownEvents int
		if err := store.pool.QueryRow(ctx, `
			SELECT count(*) FROM shared_api_credential_events
			WHERE credential_fingerprint = $1 AND event_type = 'cooldown'
		`, fingerprint).Scan(&cooldownEvents); err != nil {
			t.Fatalf("count cooldown events: %v", err)
		}
		if cooldownEvents != 0 {
			t.Fatalf("late cooldown inserted %d cooldown events, want 0", cooldownEvents)
		}
		var totalEvents int
		if err := store.pool.QueryRow(ctx, `
			SELECT count(*) FROM shared_api_credential_events
			WHERE credential_fingerprint = $1
		`, fingerprint).Scan(&totalEvents); err != nil {
			t.Fatalf("count credential events: %v", err)
		}
		if totalEvents != 2 {
			t.Fatalf("credential events = %d, want registration and quarantine only", totalEvents)
		}
	})

	t.Run("retired terminal state wins over a late cooldown", func(t *testing.T) {
		store := startVersionTwoStore(t, ctx)
		fingerprint := bearerTokenFingerprint("retired-late-cooldown-secret")
		if err := store.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "collector:test"); err != nil {
			t.Fatalf("register shared credential: %v", err)
		}
		if _, err := store.pool.Exec(ctx, `
			UPDATE shared_api_credentials
			SET state = 'retired', updated_at = clock_timestamp()
			WHERE credential_fingerprint = $1
		`, fingerprint); err != nil {
			t.Fatalf("retire shared credential: %v", err)
		}

		if err := store.cooldownSharedCredential(ctx, fingerprint, 30*time.Second, "collector:test", "official_api_429"); err != nil {
			t.Fatalf("late cooldown must be a valid no-op on retired, got error: %v", err)
		}

		var state string
		if err := store.pool.QueryRow(ctx, `
			SELECT state FROM shared_api_credentials
			WHERE credential_fingerprint = $1
		`, fingerprint).Scan(&state); err != nil {
			t.Fatalf("read retired credential state: %v", err)
		}
		if state != "retired" {
			t.Fatalf("late cooldown changed retired state to %q", state)
		}
		var cooldownEvents int
		if err := store.pool.QueryRow(ctx, `
			SELECT count(*) FROM shared_api_credential_events
			WHERE credential_fingerprint = $1 AND event_type = 'cooldown'
		`, fingerprint).Scan(&cooldownEvents); err != nil {
			t.Fatalf("count cooldown events: %v", err)
		}
		if cooldownEvents != 0 {
			t.Fatalf("late cooldown inserted %d cooldown events on retired, want 0", cooldownEvents)
		}
	})
}

func TestSharedTrafficGateCleanupIsBounded(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	fingerprint := bearerTokenFingerprint("cleanup-shared-secret")
	if err := store.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "collector:test"); err != nil {
		t.Fatalf("register cleanup credential: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO shared_api_permits (credential_fingerprint, caller, permitted_at)
		VALUES ($1, 'go', clock_timestamp() - interval '11 minutes'),
		       ($1, 'go', clock_timestamp() - interval '11 minutes')
	`, fingerprint); err != nil {
		t.Fatalf("insert expired permits: %v", err)
	}
	deleted, err := store.cleanupSharedPermits(ctx, 1)
	if err != nil {
		t.Fatalf("clean up expired permits: %v", err)
	}
	if deleted != 1 {
		t.Fatalf("bounded permit cleanup deleted %d rows, want 1", deleted)
	}
	var remaining int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM shared_api_permits WHERE credential_fingerprint = $1
	`, fingerprint).Scan(&remaining); err != nil {
		t.Fatalf("count retained permits: %v", err)
	}
	if remaining != 1 {
		t.Fatalf("bounded permit cleanup retained %d rows, want 1", remaining)
	}
}

func TestSharedTrafficGateAcquisitionCleansUpToOneHundredExpiredPermits(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	fingerprint := bearerTokenFingerprint("cleanup-acquisition-secret")
	if err := store.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "collector:test"); err != nil {
		t.Fatalf("register cleanup credential: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO shared_api_permits (credential_fingerprint, caller, permitted_at)
		SELECT $1, 'go', clock_timestamp() - interval '11 minutes'
		FROM generate_series(1, 150)
	`, fingerprint); err != nil {
		t.Fatalf("insert expired permits: %v", err)
	}

	countExpired := func() int {
		t.Helper()
		var expired int
		if err := store.pool.QueryRow(ctx, `
			SELECT count(*) FROM shared_api_permits
			WHERE credential_fingerprint = $1
			  AND permitted_at < clock_timestamp() - interval '10 minutes'
		`, fingerprint).Scan(&expired); err != nil {
			t.Fatalf("count expired permits: %v", err)
		}
		return expired
	}

	permit, err := store.acquireSharedPermit(ctx, fingerprint, "go")
	if err != nil {
		t.Fatalf("acquire permit with expired rows: %v", err)
	}
	if !permit.granted {
		t.Fatalf("permit with expired rows not granted: %+v", permit)
	}
	if remaining := countExpired(); remaining != 50 {
		t.Fatalf("first acquisition removed %d expired permits, want 100", 150-remaining)
	}

	permit, err = store.acquireSharedPermit(ctx, fingerprint, "go")
	if err != nil {
		t.Fatalf("acquire second permit: %v", err)
	}
	if !permit.granted {
		t.Fatalf("second permit not granted: %+v", permit)
	}
	if remaining := countExpired(); remaining != 0 {
		t.Fatalf("second acquisition left %d expired permits", remaining)
	}

	var fresh int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM shared_api_permits
		WHERE credential_fingerprint = $1
		  AND permitted_at >= clock_timestamp() - interval '10 minutes'
	`, fingerprint).Scan(&fresh); err != nil {
		t.Fatalf("count fresh permits: %v", err)
	}
	if fresh != 2 {
		t.Fatalf("fresh permits = %d, want the two acquisitions", fresh)
	}
}
