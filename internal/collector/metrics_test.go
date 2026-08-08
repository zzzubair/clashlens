package collector

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestMetricsIncludeRequiredDurableOperationalGauges(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)
	keys, err := newKeyPool([]APIKey{
		{Label: "normal", Secret: "normal-secret", Pool: normalPool},
		{Label: "interactive", Secret: "interactive-secret", Pool: interactivePool},
	}, 30, false)
	if err != nil {
		t.Fatalf("newKeyPool returned an error: %v", err)
	}

	output, err := newCollectorMetrics().render(ctx, store, keys, time.Now().UTC())
	if err != nil {
		t.Fatalf("render metrics: %v", err)
	}
	for _, metric := range []string{
		"clashlens_collector_expired_leases",
		"clashlens_collector_incomplete_attempts",
		"clashlens_collector_observation_freshness_seconds{endpoint=\"profile\"}",
		"clashlens_collector_observation_freshness_seconds{endpoint=\"battle_log\"}",
		"clashlens_collector_reset_sweep_members_total",
		"clashlens_collector_reset_sweep_observed",
		"clashlens_collector_reset_sweep_missing",
		"clashlens_collector_reset_sweep_elapsed_seconds",
		"clashlens_collector_live_refresh_latest_latency_seconds",
		"clashlens_collector_live_refresh_coalesced_total",
		"clashlens_collector_live_refresh_cooldown_hits_total",
		"clashlens_collector_key_cooldown_seconds",
	} {
		if !strings.Contains(output, metric) {
			t.Errorf("metrics output does not contain %q", metric)
		}
	}
}
