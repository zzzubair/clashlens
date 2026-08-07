package collector

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func TestKeyPoolKeepsNormalAndInteractiveCapacitySeparate(t *testing.T) {
	t.Parallel()

	pool, err := newKeyPool([]APIKey{
		{Label: "normal-a", Secret: "secret-a", Pool: normalPool},
		{Label: "normal-b", Secret: "secret-b", Pool: normalPool},
		{Label: "interactive", Secret: "secret-i", Pool: interactivePool},
	}, 30, false)
	if err != nil {
		t.Fatalf("newKeyPool returned an error: %v", err)
	}

	now := time.Unix(1_700_000_000, 0)
	normal, _, err := pool.tryAcquire(now, normalPool)
	if err != nil {
		t.Fatalf("normal acquire returned an error: %v", err)
	}
	if normal.Pool != normalPool {
		t.Fatalf("normal acquire used %q pool", normal.Pool)
	}

	interactive, _, err := pool.tryAcquire(now, interactivePool)
	if err != nil {
		t.Fatalf("interactive acquire returned an error: %v", err)
	}
	if interactive.Pool != interactivePool {
		t.Fatalf("interactive acquire used %q pool", interactive.Pool)
	}
}

func TestKeyPoolUsesAggregateNormalCapacityWithoutExceedingPerKeyLimit(t *testing.T) {
	t.Parallel()

	keys := []APIKey{
		{Label: "normal-a", Secret: "a", Pool: normalPool},
		{Label: "normal-b", Secret: "b", Pool: normalPool},
		{Label: "normal-c", Secret: "c", Pool: normalPool},
		{Label: "normal-d", Secret: "d", Pool: normalPool},
	}
	pool, err := newKeyPool(keys, 30, false)
	if err != nil {
		t.Fatalf("newKeyPool returned an error: %v", err)
	}

	now := time.Unix(1_700_000_000, 0)
	counts := make(map[string]int)
	for range 120 {
		key, wait, err := pool.tryAcquire(now, normalPool)
		if err != nil {
			t.Fatalf("acquire returned an error before aggregate capacity was used: %v", err)
		}
		if wait != 0 {
			t.Fatalf("acquire wait = %v before aggregate capacity was used", wait)
		}
		counts[key.Label]++
	}
	for _, key := range keys {
		if counts[key.Label] != 30 {
			t.Fatalf("key %q handled %d requests, want 30", key.Label, counts[key.Label])
		}
	}

	key, wait, err := pool.tryAcquire(now, normalPool)
	if !errors.Is(err, errRateLimited) {
		t.Fatalf("121st acquire error = %v, want errRateLimited", err)
	}
	if key.Label != "" {
		t.Fatalf("121st acquire returned key %q", key.Label)
	}
	if wait != time.Second {
		t.Fatalf("121st acquire wait = %v, want 1s", wait)
	}
}

func TestKeyPoolQuarantinesOnlyTheAffectedKey(t *testing.T) {
	t.Parallel()

	pool, err := newKeyPool([]APIKey{
		{Label: "normal-a", Secret: "a", Pool: normalPool},
		{Label: "normal-b", Secret: "b", Pool: normalPool},
	}, 30, false)
	if err != nil {
		t.Fatalf("newKeyPool returned an error: %v", err)
	}

	now := time.Unix(1_700_000_000, 0)
	first, _, err := pool.tryAcquire(now, normalPool)
	if err != nil {
		t.Fatalf("first acquire returned an error: %v", err)
	}
	if err := pool.quarantine(first.Label); err != nil {
		t.Fatalf("quarantine returned an error: %v", err)
	}
	second, _, err := pool.tryAcquire(now, normalPool)
	if err != nil {
		t.Fatalf("second acquire returned an error: %v", err)
	}
	if second.Label == first.Label {
		t.Fatalf("quarantined key %q was reused", first.Label)
	}
}

func TestKeyPoolRejectsNormalFallbackToInteractiveByDefault(t *testing.T) {
	t.Parallel()

	pool, err := newKeyPool([]APIKey{
		{Label: "interactive", Secret: "i", Pool: interactivePool},
	}, 30, false)
	if err != nil {
		t.Fatalf("newKeyPool returned an error: %v", err)
	}

	key, _, err := pool.tryAcquire(time.Unix(1_700_000_000, 0), normalPool)
	if !errors.Is(err, errNoHealthyKey) {
		t.Fatalf("normal acquire error = %v, want errNoHealthyKey", err)
	}
	if key.Label != "" {
		t.Fatalf("normal acquire returned interactive key %q", key.Label)
	}
}

func TestKeyPoolRejectsUnsafeNormalFallback(t *testing.T) {
	t.Parallel()

	_, err := newKeyPool([]APIKey{
		{Label: "normal", Secret: "n", Pool: normalPool},
		{Label: "interactive", Secret: "i", Pool: interactivePool},
	}, 30, true)
	if err == nil || !strings.Contains(err.Error(), "interactive") {
		t.Fatalf("newKeyPool error = %v, want unsafe fallback rejection", err)
	}
}

func TestKeyPoolStatusReportsRateLimitCooldown(t *testing.T) {
	t.Parallel()
	pool, err := newKeyPool([]APIKey{
		{Label: "normal", Secret: "n", Pool: normalPool},
	}, 1, false)
	if err != nil {
		t.Fatalf("newKeyPool returned an error: %v", err)
	}
	now := time.Unix(1_700_000_000, 0).UTC()
	if _, _, err := pool.tryAcquire(now, normalPool); err != nil {
		t.Fatalf("tryAcquire returned an error: %v", err)
	}
	statuses := pool.statuses(now.Add(250 * time.Millisecond))
	if len(statuses) != 1 || statuses[0].Cooldown != 750*time.Millisecond {
		t.Fatalf("key statuses = %#v, want 750ms cooldown", statuses)
	}
}

func TestKeyPoolRejectsDuplicateSecretAcrossLabels(t *testing.T) {
	t.Parallel()
	_, err := newKeyPool([]APIKey{
		{Label: "normal", Secret: "same-secret", Pool: normalPool},
		{Label: "interactive", Secret: "same-secret", Pool: interactivePool},
	}, 30, false)
	if err == nil || !strings.Contains(err.Error(), "duplicate API key secret") {
		t.Fatalf("newKeyPool error = %v, want duplicate-secret error", err)
	}
	if strings.Contains(err.Error(), "same-secret") {
		t.Fatal("duplicate-secret error exposes the API key value")
	}
}

func TestKeyPoolRejectsPerKeyRateAboveOfficialLimit(t *testing.T) {
	t.Parallel()
	_, err := newKeyPool([]APIKey{
		{Label: "normal", Secret: "secret", Pool: normalPool},
	}, 31, false)
	if err == nil || !strings.Contains(err.Error(), "between 1 and 30") {
		t.Fatalf("newKeyPool error = %v, want per-key rate limit error", err)
	}
}
