package collector

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var errIncompatibleContract = errors.New("incompatible shared contract version")

type store struct {
	pool               *pgxpool.Pool
	contractVersion    int
	maxContractVersion int
	commitTx           func(context.Context, pgx.Tx) error
}

func openStore(ctx context.Context, databaseURL string, expectedContractVersion int) (*store, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("configure PostgreSQL pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("PostgreSQL readiness: %w", err)
	}

	var actualVersion int
	if err := pool.QueryRow(ctx, `SELECT version FROM clash_lens_contract WHERE singleton`).Scan(&actualVersion); err != nil {
		pool.Close()
		return nil, fmt.Errorf("read shared contract version: %w", err)
	}
	if !supportsContractVersion(actualVersion, expectedContractVersion) {
		pool.Close()
		return nil, fmt.Errorf("%w: got %d, support through %d", errIncompatibleContract, actualVersion, expectedContractVersion)
	}
	return &store{
		pool:               pool,
		contractVersion:    actualVersion,
		maxContractVersion: expectedContractVersion,
	}, nil
}

func supportsContractVersion(actualVersion, maxContractVersion int) bool {
	if maxContractVersion == 2 {
		return actualVersion == 1 || actualVersion == 2
	}
	return actualVersion == maxContractVersion
}

func (s *store) close() {
	s.pool.Close()
}

func (s *store) scheduleDueRegular(ctx context.Context, now time.Time, cycle time.Duration, batchSize int) (int, error) {
	if cycle <= 0 {
		return 0, errors.New("poll cycle must be positive")
	}
	if batchSize < 1 {
		return 0, errors.New("scheduler batch size must be positive")
	}

	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return 0, fmt.Errorf("begin regular scheduling transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	rows, err := transaction.Query(ctx, `
		SELECT id, normalized_tag, next_due_at
		FROM players
		WHERE active AND next_due_at <= $1
		ORDER BY next_due_at, id
		FOR UPDATE SKIP LOCKED
		LIMIT $2
	`, now, batchSize)
	if err != nil {
		return 0, fmt.Errorf("select due players: %w", err)
	}
	type duePlayer struct {
		id      int64
		tag     string
		nextDue time.Time
	}
	players := make([]duePlayer, 0, batchSize)
	for rows.Next() {
		var player duePlayer
		if err := rows.Scan(&player.id, &player.tag, &player.nextDue); err != nil {
			rows.Close()
			return 0, fmt.Errorf("scan due player: %w", err)
		}
		players = append(players, player)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, fmt.Errorf("read due players: %w", err)
	}
	rows.Close()

	cycleStart := now.Truncate(cycle)
	nextCycleStart := cycleStart.Add(cycle)
	created := 0
	for _, player := range players {
		priority := 100
		if now.Sub(player.nextDue) >= cycle {
			priority = 200
		}
		coalescingKey := "regular:" + strconv.FormatInt(player.id, 10) + ":" + strconv.FormatInt(cycleStart.Unix(), 10)
		command, err := transaction.Exec(ctx, `
			INSERT INTO collector_jobs (
				work_type,
				player_id,
				normalized_tag,
				capacity_pool,
				priority,
				due_at,
				coalescing_key,
				status
			)
			VALUES ('regular_poll', $1, $2, 'normal', $3, $4, $5, 'pending')
			ON CONFLICT DO NOTHING
		`, player.id, player.tag, priority, now, coalescingKey)
		if err != nil {
			return 0, fmt.Errorf("insert regular poll for player %d: %w", player.id, err)
		}
		created += int(command.RowsAffected())

		offset := deterministicStagger(player.id, cycle)
		if _, err := transaction.Exec(ctx, `
			UPDATE players
			SET next_due_at = $2
			WHERE id = $1
		`, player.id, nextCycleStart.Add(offset)); err != nil {
			return 0, fmt.Errorf("advance due time for player %d: %w", player.id, err)
		}
	}

	if err := transaction.Commit(ctx); err != nil {
		return 0, fmt.Errorf("commit regular scheduling transaction: %w", err)
	}
	return created, nil
}

func deterministicStagger(playerID int64, cycle time.Duration) time.Duration {
	cycleSeconds := int64(cycle / time.Second)
	if cycleSeconds < 1 {
		return 0
	}
	return time.Duration((playerID-1)%cycleSeconds) * time.Second
}
