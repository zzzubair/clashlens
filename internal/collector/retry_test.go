package collector

import (
	"net/http"
	"testing"
	"time"
)

func TestRetryPolicyUsesBoundedExponentialBackoff(t *testing.T) {
	t.Parallel()

	policy := retryPolicy{
		baseDelay:    time.Second,
		maximumDelay: 10 * time.Second,
		jitter:       func(time.Duration) time.Duration { return 0 },
	}
	now := time.Date(2026, time.August, 2, 12, 0, 0, 0, time.UTC)

	for _, test := range []struct {
		retry int
		want  time.Duration
	}{
		{retry: 1, want: time.Second},
		{retry: 2, want: 2 * time.Second},
		{retry: 3, want: 4 * time.Second},
		{retry: 8, want: 10 * time.Second},
	} {
		if got := policy.nextRetryAt(now, test.retry, "").Sub(now); got != test.want {
			t.Fatalf("retry %d delay = %v, want %v", test.retry, got, test.want)
		}
	}
}

func TestRetryPolicyHonorsRetryAfter(t *testing.T) {
	t.Parallel()

	policy := retryPolicy{
		baseDelay:    time.Second,
		maximumDelay: 30 * time.Second,
		jitter:       func(time.Duration) time.Duration { return 0 },
	}
	now := time.Date(2026, time.August, 2, 12, 0, 0, 0, time.UTC)

	if got := policy.nextRetryAt(now, 2, "7"); !got.Equal(now.Add(7 * time.Second)) {
		t.Fatalf("delta Retry-After produced %s, want %s", got, now.Add(7*time.Second))
	}
	httpDate := now.Add(9 * time.Second).Format(http.TimeFormat)
	if got := policy.nextRetryAt(now, 2, httpDate); !got.Equal(now.Add(9 * time.Second)) {
		t.Fatalf("date Retry-After produced %s, want %s", got, now.Add(9*time.Second))
	}
}
