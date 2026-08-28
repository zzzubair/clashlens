package collector

import (
	"context"
	"fmt"
	"regexp"
	"strings"
	"testing"
	"time"
)

func TestCollectorMetricsProcessIdentityIsStableAndOpaque(t *testing.T) {
	first := newCollectorMetrics()
	second := newCollectorMetrics()
	if first.processIdentity == second.processIdentity {
		t.Fatalf("process identities match: %q", first.processIdentity)
	}
	identityPattern := regexp.MustCompile(`^[0-9a-f]{32}$`)
	for _, identity := range []string{first.processIdentity, second.processIdentity} {
		if !identityPattern.MatchString(identity) {
			t.Fatalf("process identity %q is not a fixed-format lowercase hex value", identity)
		}
	}
	if first.processStartedAt.IsZero() || second.processStartedAt.IsZero() {
		t.Fatal("process start time must be initialized")
	}
}

func TestCollectorMetricsRenderProcessIdentityAndStartTime(t *testing.T) {
	metrics := newCollectorMetrics()
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

	output, err := metrics.render(ctx, store, keys, time.Now().UTC())
	if err != nil {
		t.Fatalf("render metrics: %v", err)
	}
	for _, metric := range []string{
		fmt.Sprintf("clashlens_collector_process_start_time_seconds %d", metrics.processStartedAt.Unix()),
		fmt.Sprintf("clashlens_collector_process_identity_info{process_id=%q} 1", metrics.processIdentity),
	} {
		if !strings.Contains(output, metric) {
			t.Errorf("metrics output does not contain %q", metric)
		}
	}
}

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
		{Label: "player-#SECRET", Secret: "normal-secret", Pool: normalPool},
		{Label: "interactive", Secret: "interactive-secret", Pool: interactivePool},
	}, 30, false)
	if err != nil {
		t.Fatalf("newKeyPool returned an error: %v", err)
	}

	metrics := newCollectorMetrics()
	metrics.recordStageDuration("claim", 750*time.Microsecond)
	metrics.recordStageDuration("claim", 3*time.Millisecond)
	output, err := metrics.render(ctx, store, keys, time.Now().UTC())
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
		"clashlens_spool_abandoned_temporary_bytes",
		"clashlens_spool_abandoned_temporary_objects",
		"clashlens_spool_free_bytes",
		"clashlens_collector_key_cooldown_seconds{pool=\"normal\"}",
		"clashlens_collector_stage_duration_seconds_bucket{stage=\"claim\",le=\"0.001\"} 1",
		"clashlens_collector_stage_duration_seconds_count{stage=\"claim\"} 2",
		"clashlens_collector_stage_duration_seconds_sum{stage=\"claim\"} 0.003750",
	} {
		if !strings.Contains(output, metric) {
			t.Errorf("metrics output does not contain %q", metric)
		}
	}
	if strings.Contains(output, "player-#SECRET") || strings.Contains(output, "key_label") {
		t.Fatal("collector metrics exposed a configured API key label")
	}
}
