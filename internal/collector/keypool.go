package collector

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"sync"
	"time"
)

type capacityPool string

const (
	normalPool      capacityPool = "normal"
	interactivePool capacityPool = "interactive"
	// recoveryPool is a claim lane, not an API-key pool. Recovery requests
	// use the one shared interactive key through their own database permit
	// budget and therefore never occupy interactive worker slots.
	recoveryPool capacityPool = "recovery"
)

var (
	errNoHealthyKey = errors.New("no healthy API key")
	errRateLimited  = errors.New("API key pool is rate limited")
)

type APIKey struct {
	Label  string
	Secret string
	Pool   capacityPool
}

type keyState struct {
	APIKey
	quarantined bool
	requests    []time.Time
}

type keyPool struct {
	mu                sync.Mutex
	keys              []*keyState
	requestsPerSecond int
	next              int
}

type APIKeyStatus struct {
	Label                string
	Pool                 capacityPool
	Quarantined          bool
	RequestsInLastSecond int
	Cooldown             time.Duration
}

func newKeyPool(keys []APIKey, requestsPerSecond int, unsafeNormalFallback bool) (*keyPool, error) {
	if unsafeNormalFallback {
		return nil, errors.New("interactive API keys cannot be used for normal work")
	}
	if requestsPerSecond < 1 || requestsPerSecond > 30 {
		return nil, errors.New("requests per second per key must be between 1 and 30")
	}
	seen := make(map[string]struct{}, len(keys))
	seenSecrets := make(map[[sha256.Size]byte]struct{}, len(keys))
	states := make([]*keyState, 0, len(keys))
	for _, key := range keys {
		if key.Label == "" || key.Secret == "" {
			return nil, errors.New("API key label and secret are required")
		}
		if key.Pool != normalPool && key.Pool != interactivePool {
			return nil, fmt.Errorf("unknown API key pool %q", key.Pool)
		}
		if _, ok := seen[key.Label]; ok {
			return nil, fmt.Errorf("duplicate API key label %q", key.Label)
		}
		secretHash := sha256.Sum256([]byte(key.Secret))
		if _, ok := seenSecrets[secretHash]; ok {
			return nil, errors.New("duplicate API key secret")
		}
		seen[key.Label] = struct{}{}
		seenSecrets[secretHash] = struct{}{}
		states = append(states, &keyState{APIKey: key})
	}
	return &keyPool{
		keys:              states,
		requestsPerSecond: requestsPerSecond,
	}, nil
}

func (p *keyPool) tryAcquire(now time.Time, requestedPool capacityPool) (APIKey, time.Duration, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	if len(p.keys) == 0 {
		return APIKey{}, 0, errNoHealthyKey
	}

	hasHealthyCandidate := false
	minimumWait := time.Duration(1<<63 - 1)
	for offset := range len(p.keys) {
		index := (p.next + offset) % len(p.keys)
		state := p.keys[index]
		if state.quarantined || !p.matchesPool(state.Pool, requestedPool) {
			continue
		}
		hasHealthyCandidate = true
		state.requests = trimRequestWindow(state.requests, now)
		if len(state.requests) < p.requestsPerSecond {
			state.requests = append(state.requests, now)
			p.next = (index + 1) % len(p.keys)
			return state.APIKey, 0, nil
		}
		wait := time.Second - now.Sub(state.requests[0])
		if wait < minimumWait {
			minimumWait = wait
		}
	}

	if !hasHealthyCandidate {
		return APIKey{}, 0, errNoHealthyKey
	}
	return APIKey{}, minimumWait, errRateLimited
}

// sharedInteractiveKey selects the configured shared key without applying the
// local limiter. PostgreSQL is the only rate authority for this key.
func (p *keyPool) sharedInteractiveKey() (APIKey, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	for _, state := range p.keys {
		if state.Pool == interactivePool && !state.quarantined {
			return state.APIKey, nil
		}
	}
	return APIKey{}, fmt.Errorf("%w for %s pool", errNoHealthyKey, interactivePool)
}

func (p *keyPool) quarantine(label string) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	for _, state := range p.keys {
		if state.Label == label {
			state.quarantined = true
			return nil
		}
	}
	return fmt.Errorf("unknown API key label %q", label)
}

func (p *keyPool) statuses(now time.Time) []APIKeyStatus {
	p.mu.Lock()
	defer p.mu.Unlock()

	statuses := make([]APIKeyStatus, 0, len(p.keys))
	for _, state := range p.keys {
		state.requests = trimRequestWindow(state.requests, now)
		cooldown := time.Duration(0)
		if len(state.requests) >= p.requestsPerSecond {
			cooldown = time.Second - now.Sub(state.requests[0])
		}
		statuses = append(statuses, APIKeyStatus{
			Label:                state.Label,
			Pool:                 state.Pool,
			Quarantined:          state.quarantined,
			RequestsInLastSecond: len(state.requests),
			Cooldown:             cooldown,
		})
	}
	return statuses
}

func (p *keyPool) ready() error {
	if err := p.readyForPool(normalPool); err != nil {
		return err
	}
	if err := p.readyForPool(interactivePool); err != nil {
		return err
	}
	return nil
}

func (p *keyPool) readyForPool(requestedPool capacityPool) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	for _, state := range p.keys {
		if !state.quarantined && p.matchesPool(state.Pool, requestedPool) {
			return nil
		}
	}
	return fmt.Errorf("%w for %s pool", errNoHealthyKey, requestedPool)
}

func (p *keyPool) matchesPool(keyPool, requestedPool capacityPool) bool {
	return keyPool == requestedPool
}

func trimRequestWindow(requests []time.Time, now time.Time) []time.Time {
	firstCurrent := 0
	for firstCurrent < len(requests) && now.Sub(requests[firstCurrent]) >= time.Second {
		firstCurrent++
	}
	return requests[firstCurrent:]
}
