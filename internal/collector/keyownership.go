package collector

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"sort"
	"sync"
	"time"
)

type apiKeyOwnershipLock struct {
	id    int64
	label string
}

func (s *store) acquireAPIKeyOwnership(ctx context.Context, keys []APIKey) (func(), error) {
	locksByID := make(map[int64]string, len(keys))
	for _, key := range keys {
		digest := sha256.Sum256([]byte(key.Secret))
		lockID := int64(binary.BigEndian.Uint64(digest[:8]))
		if _, exists := locksByID[lockID]; !exists {
			locksByID[lockID] = key.Label
		}
	}
	locks := make([]apiKeyOwnershipLock, 0, len(locksByID))
	for id, label := range locksByID {
		locks = append(locks, apiKeyOwnershipLock{id: id, label: label})
	}
	sort.Slice(locks, func(left, right int) bool { return locks[left].id < locks[right].id })

	connection, err := s.pool.Acquire(ctx)
	if err != nil {
		return nil, fmt.Errorf("reserve PostgreSQL connection for API key ownership: %w", err)
	}
	var releaseOnce sync.Once
	release := func() {
		releaseOnce.Do(func() {
			releaseContext, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			if _, err := connection.Exec(releaseContext, `SELECT pg_advisory_unlock_all()`); err != nil {
				underlying := connection.Hijack()
				_ = underlying.Close(context.Background())
				return
			}
			connection.Release()
		})
	}
	for _, lock := range locks {
		var owned bool
		if err := connection.QueryRow(ctx, `SELECT pg_try_advisory_lock($1)`, lock.id).Scan(&owned); err != nil {
			release()
			return nil, fmt.Errorf("acquire API key ownership for label %q: %w", lock.label, err)
		}
		if !owned {
			release()
			return nil, fmt.Errorf("API key label %q is already owned by another collector process", lock.label)
		}
	}
	return release, nil
}
