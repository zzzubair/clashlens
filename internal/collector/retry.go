package collector

import (
	"math/rand/v2"
	"net/http"
	"strconv"
	"time"
)

type retryPolicy struct {
	baseDelay    time.Duration
	maximumDelay time.Duration
	jitter       func(time.Duration) time.Duration
}

func newRetryPolicy(baseDelay, maximumDelay time.Duration, jitterFraction float64) retryPolicy {
	return retryPolicy{
		baseDelay:    baseDelay,
		maximumDelay: maximumDelay,
		jitter: func(delay time.Duration) time.Duration {
			if jitterFraction <= 0 {
				return 0
			}
			span := float64(delay) * jitterFraction
			return time.Duration((rand.Float64()*2 - 1) * span)
		},
	}
}

func (p retryPolicy) nextRetryAt(now time.Time, retryNumber int, retryAfter string) time.Time {
	if retryNumber < 1 {
		retryNumber = 1
	}
	delay := p.baseDelay
	for range retryNumber - 1 {
		if delay >= p.maximumDelay/2 {
			delay = p.maximumDelay
			break
		}
		delay *= 2
	}
	if delay > p.maximumDelay {
		delay = p.maximumDelay
	}
	if p.jitter != nil {
		delay += p.jitter(delay)
	}
	if delay < 0 {
		delay = 0
	}

	if retryAfterDelay, ok := parseRetryAfter(now, retryAfter); ok && retryAfterDelay > delay {
		delay = retryAfterDelay
	}
	return now.Add(delay)
}

func parseRetryAfter(now time.Time, value string) (time.Duration, bool) {
	if seconds, err := strconv.Atoi(value); err == nil && seconds >= 0 {
		return time.Duration(seconds) * time.Second, true
	}
	if retryAt, err := http.ParseTime(value); err == nil {
		delay := retryAt.Sub(now)
		if delay < 0 {
			delay = 0
		}
		return delay, true
	}
	return 0, false
}
