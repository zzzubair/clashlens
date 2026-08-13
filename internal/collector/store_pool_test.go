package collector

import (
	"context"
	"strings"
	"testing"
)

// The collector PostgreSQL pool must be explicitly bounded. The production
// 48-slot collector stage needs exactly 48 connections; the pgxpool implicit
// default (max(4, NumCPU)) would silently cap at 16 on the 16-core host.
func TestOpenStoreWithPoolSizeBindsExactMaximumConnections(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx := context.Background()

	opened, err := openStoreWithPoolSize(ctx, databaseURL, 1, 48)
	if err != nil {
		t.Fatalf("openStoreWithPoolSize returned an error: %v", err)
	}
	defer opened.close()

	if maximum := opened.pool.Config().MaxConns; maximum != 48 {
		t.Fatalf("pool Config().MaxConns = %d, want exactly 48", maximum)
	}
	if maximum := opened.pool.Stat().MaxConns(); maximum != 48 {
		t.Fatalf("pool Stat().MaxConns() = %d, want exactly 48", maximum)
	}
}

// The compatibility wrapper keeps the safe default of 16 so every existing
// test seam and the maintenance command behave exactly as before.
func TestOpenStoreWrapperKeepsSafeDefaultPoolBound(t *testing.T) {
	databaseURL := startContractDatabase(t)

	opened, err := openStore(context.Background(), databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	defer opened.close()

	if maximum := opened.pool.Config().MaxConns; maximum != defaultCollectorDatabasePoolSize {
		t.Fatalf("pool Config().MaxConns = %d, want the safe default %d",
			maximum, defaultCollectorDatabasePoolSize)
	}
}

func TestOpenStoreWithPoolSizeRejectsNonPositiveMaximum(t *testing.T) {
	databaseURL := startContractDatabase(t)

	for _, maximum := range []int{0, -1} {
		opened, err := openStoreWithPoolSize(context.Background(), databaseURL, 1, maximum)
		if opened != nil {
			opened.close()
			t.Fatalf("openStoreWithPoolSize with %d returned a store, want an error", maximum)
		}
		if err == nil || !strings.Contains(err.Error(), "pool size") {
			t.Fatalf("openStoreWithPoolSize(%d) error = %v, want pool size rejection", maximum, err)
		}
	}
}
