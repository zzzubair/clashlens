package collector

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

type expiredLeaseFixture struct {
	job       *collectionJob
	attemptID int64
}

func TestExpiredLeaseRejectsProtectedMutations(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)

	prepareEndpoint := func(t *testing.T, fixture expiredLeaseFixture) {
		t.Helper()
		if _, _, err := store.prepareAttempt(ctx, fixture.job, time.Now().UTC()); err != nil {
			t.Fatalf("prepareAttempt returned an error: %v", err)
		}
	}
	beginEndpoint := func(t *testing.T, fixture expiredLeaseFixture) {
		t.Helper()
		prepareEndpoint(t, fixture)
		if _, err := store.beginEndpointRequest(ctx, fixture.job, fixture.attemptID, profileEndpoint, time.Now().UTC()); err != nil {
			t.Fatalf("beginEndpointRequest returned an error: %v", err)
		}
	}

	tests := []struct {
		name   string
		setup  func(*testing.T, expiredLeaseFixture)
		invoke func(expiredLeaseFixture) error
	}{
		{
			name: "renewal",
			invoke: func(fixture expiredLeaseFixture) error {
				return store.renewLease(ctx, fixture.job, time.Now().UTC().Add(time.Minute))
			},
		},
		{
			name: "attempt preparation",
			invoke: func(fixture expiredLeaseFixture) error {
				_, _, err := store.prepareAttempt(ctx, fixture.job, time.Now().UTC())
				return err
			},
		},
		{
			name:  "endpoint request start",
			setup: prepareEndpoint,
			invoke: func(fixture expiredLeaseFixture) error {
				_, err := store.beginEndpointRequest(ctx, fixture.job, fixture.attemptID, profileEndpoint, time.Now().UTC())
				return err
			},
		},
		{
			name:  "observation commit",
			setup: beginEndpoint,
			invoke: func(fixture expiredLeaseFixture) error {
				now := time.Now().UTC()
				return store.commitObservation(
					ctx,
					fixture.job,
					fixture.attemptID,
					profileEndpoint,
					1,
					officialResponse{
						requestStartedAt:    now.Add(-time.Second),
						responseCompletedAt: now,
						statusCode:          200,
						headers:             map[string]string{"Content-Type": "application/json"},
					},
					"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
					"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
					"test",
					"normal",
					"observed",
					nil,
				)
			},
		},
		{
			name:  "storage failure",
			setup: beginEndpoint,
			invoke: func(fixture expiredLeaseFixture) error {
				now := time.Now().UTC()
				return store.recordStorageFailure(ctx, fixture.job, fixture.attemptID, profileEndpoint, officialResponse{
					requestStartedAt:    now.Add(-time.Second),
					responseCompletedAt: now,
					statusCode:          503,
				}, "archive_unavailable", "normal")
			},
		},
		{
			name:  "transport failure",
			setup: beginEndpoint,
			invoke: func(fixture expiredLeaseFixture) error {
				now := time.Now().UTC()
				return store.recordTransportFailure(
					ctx,
					fixture.job,
					fixture.attemptID,
					profileEndpoint,
					now.Add(-time.Second),
					now,
					now.Add(time.Minute),
					"transport_error",
					"normal",
				)
			},
		},
		{
			name:  "attempt resolution",
			setup: prepareEndpoint,
			invoke: func(fixture expiredLeaseFixture) error {
				return store.resolveAttempt(ctx, fixture.job, fixture.attemptID, time.Now().UTC(), 3)
			},
		},
		{
			name: "attempt completion",
			setup: func(t *testing.T, fixture expiredLeaseFixture) {
				t.Helper()
				prepareEndpoint(t, fixture)
				if _, err := store.pool.Exec(ctx, `
					UPDATE collector_endpoint_results SET outcome = 'observed' WHERE attempt_id = $1
				`, fixture.attemptID); err != nil {
					t.Fatalf("mark endpoint results observed: %v", err)
				}
			},
			invoke: func(fixture expiredLeaseFixture) error {
				return store.finishAttempt(ctx, fixture.job, fixture.attemptID, time.Now().UTC())
			},
		},
	}

	for index, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			fixture := insertExpiredLeaseFixture(t, ctx, store, index)
			if test.setup != nil {
				test.setup(t, fixture)
			}
			if _, err := store.pool.Exec(ctx, `
				UPDATE collector_jobs SET lease_expires_at = $2 WHERE id = $1
			`, fixture.job.id, time.Now().UTC().Add(-time.Second)); err != nil {
				t.Fatalf("expire lease: %v", err)
			}
			if err := test.invoke(fixture); !errors.Is(err, errLeaseLost) {
				t.Fatalf("operation error = %v, want errLeaseLost", err)
			}
		})
	}
}

func insertExpiredLeaseFixture(t *testing.T, ctx context.Context, store *store, sequence int) expiredLeaseFixture {
	t.Helper()
	now := time.Now().UTC()
	tag := fmt.Sprintf("#LEASE%d", sequence)
	var playerID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ($1, true, $2)
		RETURNING id
	`, tag, now).Scan(&playerID); err != nil {
		t.Fatalf("insert player: %v", err)
	}
	var jobID int64
	leaseToken := fmt.Sprintf("lease-token-%d", sequence)
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority, due_at,
			coalescing_key, status, lease_owner, lease_token, lease_expires_at
		)
		VALUES ('initial_collection', $1, $2, 'normal', 100, $3, $4, 'leased', 'test-owner', $5, $6)
		RETURNING id
	`, playerID, tag, now, fmt.Sprintf("expired-lease:%d", sequence), leaseToken, now.Add(time.Minute)).Scan(&jobID); err != nil {
		t.Fatalf("insert collector job: %v", err)
	}
	var attemptID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at)
		VALUES ($1, 'running', $2)
		RETURNING id
	`, jobID, now).Scan(&attemptID); err != nil {
		t.Fatalf("insert collector attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET result_attempt_id = $2 WHERE id = $1
	`, jobID, attemptID); err != nil {
		t.Fatalf("link collector attempt: %v", err)
	}
	return expiredLeaseFixture{
		job: &collectionJob{
			id:            jobID,
			workType:      "initial_collection",
			playerID:      pgtype.Int8{Int64: playerID, Valid: true},
			normalizedTag: tag,
			pool:          normalPool,
			leaseToken:    leaseToken,
		},
		attemptID: attemptID,
	}
}
