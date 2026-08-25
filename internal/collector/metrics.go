package collector

import (
	"context"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type collectorMetrics struct {
	mu               sync.Mutex
	jobs             map[string]uint64
	apiRequests      map[string]uint64
	apiOutcomes      map[string]uint64
	apiDurationCount map[string]uint64
	apiDurationSum   map[string]float64
	storageErrors    map[string]uint64
	retries          map[string]uint64
	quarantines      map[string]uint64
	stageDurations   map[string]durationHistogram
}

var collectorDurationBuckets = [...]float64{
	0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025,
	0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10,
}

type durationHistogram struct {
	count   uint64
	sum     float64
	buckets [len(collectorDurationBuckets) + 1]uint64
}

func newCollectorMetrics() *collectorMetrics {
	return &collectorMetrics{
		jobs:             map[string]uint64{},
		apiRequests:      map[string]uint64{},
		apiOutcomes:      map[string]uint64{},
		apiDurationCount: map[string]uint64{},
		apiDurationSum:   map[string]float64{},
		storageErrors:    map[string]uint64{},
		retries:          map[string]uint64{},
		quarantines:      map[string]uint64{},
		stageDurations:   map[string]durationHistogram{},
	}
}

func (m *collectorMetrics) increment(target map[string]uint64, key string) {
	if m == nil {
		return
	}
	m.mu.Lock()
	target[key]++
	m.mu.Unlock()
}

func (m *collectorMetrics) recordJob(workType, pool, outcome string) {
	if m == nil {
		return
	}
	m.increment(m.jobs, workType+"\x00"+pool+"\x00"+outcome)
}

func (m *collectorMetrics) recordAPIRequest(endpoint, pool string) {
	if m == nil {
		return
	}
	m.increment(m.apiRequests, endpoint+"\x00"+pool)
}

func (m *collectorMetrics) recordAPIOutcome(endpoint, outcome string) {
	if m == nil {
		return
	}
	m.increment(m.apiOutcomes, endpoint+"\x00"+outcome)
}

func (m *collectorMetrics) recordAPIDuration(endpoint, pool string, duration time.Duration) {
	if m == nil {
		return
	}
	key := endpoint + "\x00" + pool
	m.mu.Lock()
	m.apiDurationCount[key]++
	m.apiDurationSum[key] += duration.Seconds()
	m.mu.Unlock()
}

func (m *collectorMetrics) recordStorageError(category string) {
	if m == nil {
		return
	}
	m.increment(m.storageErrors, category)
}

func (m *collectorMetrics) recordRetry(endpoint string) {
	if m == nil {
		return
	}
	m.increment(m.retries, endpoint)
}

func (m *collectorMetrics) recordQuarantine(label, pool string) {
	if m == nil {
		return
	}
	m.increment(m.quarantines, label+"\x00"+pool)
}

func (m *collectorMetrics) recordStageDuration(stage string, duration time.Duration) {
	if m == nil {
		return
	}
	seconds := duration.Seconds()
	m.mu.Lock()
	histogram := m.stageDurations[stage]
	histogram.count++
	histogram.sum += seconds
	for index, upperBound := range collectorDurationBuckets {
		if seconds <= upperBound {
			histogram.buckets[index]++
		}
	}
	histogram.buckets[len(collectorDurationBuckets)]++
	m.stageDurations[stage] = histogram
	m.mu.Unlock()
}

func (m *collectorMetrics) render(ctx context.Context, store *store, keys *keyPool, now time.Time, spools ...*evidenceSpool) (string, error) {
	statistics, err := store.queueStatistics(ctx)
	if err != nil {
		return "", err
	}

	m.mu.Lock()
	jobs := cloneCounterMap(m.jobs)
	apiRequests := cloneCounterMap(m.apiRequests)
	apiOutcomes := cloneCounterMap(m.apiOutcomes)
	apiDurationCount := cloneCounterMap(m.apiDurationCount)
	apiDurationSum := cloneFloatMap(m.apiDurationSum)
	storageErrors := cloneCounterMap(m.storageErrors)
	retries := cloneCounterMap(m.retries)
	quarantines := cloneCounterMap(m.quarantines)
	stageDurations := cloneHistogramMap(m.stageDurations)
	m.mu.Unlock()

	var output strings.Builder
	fmt.Fprintf(&output, "clashlens_collector_queue_depth %d\n", statistics.depth)
	fmt.Fprintf(&output, "clashlens_collector_active_leases %d\n", statistics.activeLeases)
	fmt.Fprintf(&output, "clashlens_collector_expired_leases %d\n", statistics.expiredLeases)
	fmt.Fprintf(&output, "clashlens_collector_failed_jobs %d\n", statistics.failedJobs)
	fmt.Fprintf(&output, "clashlens_collector_waiting_retries %d\n", statistics.waitingRetries)
	fmt.Fprintf(&output, "clashlens_collector_waiting_dependencies %d\n", statistics.waitingDependencies)
	fmt.Fprintf(&output, "clashlens_collector_pending_remote_verifications %d\n", statistics.pendingRemoteVerifications)
	spool := spoolMetrics{}
	var orphanCount, orphanBytes int64
	if len(spools) > 0 && spools[0] != nil {
		var spoolErr error
		spool, spoolErr = spools[0].metrics()
		if spoolErr != nil {
			return "", fmt.Errorf("read spool metrics: %w", spoolErr)
		}
		orphanCount, orphanBytes, spoolErr = spools[0].orphanMetrics(ctx, store.catalogueContains, store.pendingContains)
		if spoolErr != nil {
			return "", fmt.Errorf("read orphan metrics: %w", spoolErr)
		}
	}
	fmt.Fprintf(&output, "clashlens_spool_final_bytes %d\n", spool.finalBytes)
	fmt.Fprintf(&output, "clashlens_spool_temporary_bytes %d\n", spool.temporaryBytes)
	fmt.Fprintf(&output, "clashlens_spool_high_water_bytes %d\n", spool.highWaterBytes)
	fmt.Fprintf(&output, "clashlens_spool_final_objects %d\n", spool.finalObjects)
	fmt.Fprintf(&output, "clashlens_spool_temporary_objects %d\n", spool.temporaryObjects)
	fmt.Fprintf(&output, "clashlens_spool_reserved_bytes %d\n", spool.reservedBytes)
	fmt.Fprintf(&output, "clashlens_spool_live_reservations %d\n", spool.reservedObjects)
	fmt.Fprintf(&output, "clashlens_spool_allocated_bytes %d\n", spool.allocatedBytes)
	fmt.Fprintf(&output, "clashlens_spool_free_inodes %d\n", spool.freeInodes)
	fmt.Fprintf(&output, "clashlens_spool_orphan_count %d\n", orphanCount)
	fmt.Fprintf(&output, "clashlens_spool_orphan_bytes %d\n", orphanBytes)
	fmt.Fprintf(&output, "clashlens_collector_incomplete_attempts %d\n", statistics.incompleteAttempts)
	oldestAge := 0.0
	if statistics.oldestDueAt.Valid && statistics.oldestDueAt.Time.Before(now) {
		oldestAge = now.Sub(statistics.oldestDueAt.Time).Seconds()
	}
	fmt.Fprintf(&output, "clashlens_collector_oldest_due_age_seconds %s\n", strconv.FormatFloat(oldestAge, 'f', 3, 64))
	profileFreshness := metricAgeSeconds(now, statistics.latestProfileAt.Time, statistics.latestProfileAt.Valid)
	battleLogFreshness := metricAgeSeconds(now, statistics.latestBattleLogAt.Time, statistics.latestBattleLogAt.Valid)
	fmt.Fprintf(&output, "clashlens_collector_observation_freshness_seconds{endpoint=%q} %s\n", "profile", strconv.FormatFloat(profileFreshness, 'f', 3, 64))
	fmt.Fprintf(&output, "clashlens_collector_observation_freshness_seconds{endpoint=%q} %s\n", "battle_log", strconv.FormatFloat(battleLogFreshness, 'f', 3, 64))
	resetMissing := statistics.resetMembers - statistics.resetObserved
	if resetMissing < 0 {
		resetMissing = 0
	}
	resetElapsed := metricAgeSeconds(now, statistics.resetCreatedAt.Time, statistics.resetCreatedAt.Valid)
	if resetElapsed < 0 {
		resetElapsed = 0
	}
	fmt.Fprintf(&output, "clashlens_collector_reset_sweep_members_total %d\n", statistics.resetMembers)
	fmt.Fprintf(&output, "clashlens_collector_reset_sweep_observed %d\n", statistics.resetObserved)
	fmt.Fprintf(&output, "clashlens_collector_reset_sweep_missing %d\n", resetMissing)
	fmt.Fprintf(&output, "clashlens_collector_reset_sweep_elapsed_seconds %s\n", strconv.FormatFloat(resetElapsed, 'f', 3, 64))
	liveRefreshLatency := -1.0
	if statistics.latestLiveRefreshLatencySecond.Valid {
		liveRefreshLatency = statistics.latestLiveRefreshLatencySecond.Float64
	}
	fmt.Fprintf(&output, "clashlens_collector_live_refresh_latest_latency_seconds %s\n", strconv.FormatFloat(liveRefreshLatency, 'f', 3, 64))
	fmt.Fprintf(&output, "clashlens_collector_live_refresh_coalesced_total %d\n", statistics.liveRefreshCoalesced)
	fmt.Fprintf(&output, "clashlens_collector_live_refresh_cooldown_hits_total %d\n", statistics.liveRefreshCooldownHits)

	writeCounterMap(&output, "clashlens_collector_jobs_total", []string{"work_type", "pool", "outcome"}, jobs)
	writeCounterMap(&output, "clashlens_collector_api_requests_total", []string{"endpoint", "pool"}, apiRequests)
	writeCounterMap(&output, "clashlens_collector_api_outcomes_total", []string{"endpoint", "outcome"}, apiOutcomes)
	writeCounterMap(&output, "clashlens_collector_api_duration_seconds_count", []string{"endpoint", "pool"}, apiDurationCount)
	writeFloatMap(&output, "clashlens_collector_api_duration_seconds_sum", []string{"endpoint", "pool"}, apiDurationSum)
	writeCounterMap(&output, "clashlens_collector_storage_errors_total", []string{"category"}, storageErrors)
	writeCounterMap(&output, "clashlens_collector_retries_total", []string{"endpoint"}, retries)
	writeCounterMap(&output, "clashlens_collector_key_quarantines_total", []string{"key_label", "pool"}, quarantines)
	writeDurationHistograms(&output, stageDurations)
	poolStats := store.pool.Stat()
	fmt.Fprintf(&output, "clashlens_collector_database_pool_max_connections %d\n", poolStats.MaxConns())
	fmt.Fprintf(&output, "clashlens_collector_database_pool_acquired_connections %d\n", poolStats.AcquiredConns())
	fmt.Fprintf(&output, "clashlens_collector_database_pool_idle_connections %d\n", poolStats.IdleConns())
	fmt.Fprintf(&output, "clashlens_collector_database_pool_empty_acquires_total %d\n", poolStats.EmptyAcquireCount())
	fmt.Fprintf(&output, "clashlens_collector_database_pool_cancelled_acquires_total %d\n", poolStats.CanceledAcquireCount())
	fmt.Fprintf(&output, "clashlens_collector_database_pool_acquire_duration_seconds_total %s\n", strconv.FormatFloat(poolStats.AcquireDuration().Seconds(), 'f', 6, 64))

	for _, status := range keys.statuses(now) {
		healthy := 1
		if status.Quarantined {
			healthy = 0
		}
		fmt.Fprintf(
			&output,
			"clashlens_collector_key_healthy{key_label=%q,pool=%q} %d\n",
			status.Label,
			status.Pool,
			healthy,
		)
		fmt.Fprintf(
			&output,
			"clashlens_collector_key_requests_last_second{key_label=%q,pool=%q} %d\n",
			status.Label,
			status.Pool,
			status.RequestsInLastSecond,
		)
		fmt.Fprintf(
			&output,
			"clashlens_collector_key_cooldown_seconds{key_label=%q,pool=%q} %s\n",
			status.Label,
			status.Pool,
			strconv.FormatFloat(status.Cooldown.Seconds(), 'f', 3, 64),
		)
	}
	return output.String(), nil
}

func metricAgeSeconds(now, recordedAt time.Time, valid bool) float64 {
	if !valid {
		return -1
	}
	age := now.Sub(recordedAt).Seconds()
	if age < 0 {
		return 0
	}
	return age
}

func cloneCounterMap(source map[string]uint64) map[string]uint64 {
	clone := make(map[string]uint64, len(source))
	for key, value := range source {
		clone[key] = value
	}
	return clone
}

func cloneFloatMap(source map[string]float64) map[string]float64 {
	clone := make(map[string]float64, len(source))
	for key, value := range source {
		clone[key] = value
	}
	return clone
}

func cloneHistogramMap(source map[string]durationHistogram) map[string]durationHistogram {
	clone := make(map[string]durationHistogram, len(source))
	for key, value := range source {
		clone[key] = value
	}
	return clone
}

func writeDurationHistograms(output *strings.Builder, histograms map[string]durationHistogram) {
	stages := make([]string, 0, len(histograms))
	for stage := range histograms {
		stages = append(stages, stage)
	}
	sort.Strings(stages)
	for _, stage := range stages {
		histogram := histograms[stage]
		for index, upperBound := range collectorDurationBuckets {
			fmt.Fprintf(
				output,
				"clashlens_collector_stage_duration_seconds_bucket{stage=%q,le=%q} %d\n",
				stage,
				strconv.FormatFloat(upperBound, 'f', -1, 64),
				histogram.buckets[index],
			)
		}
		fmt.Fprintf(
			output,
			"clashlens_collector_stage_duration_seconds_bucket{stage=%q,le=%q} %d\n",
			stage,
			"+Inf",
			histogram.buckets[len(collectorDurationBuckets)],
		)
		fmt.Fprintf(output, "clashlens_collector_stage_duration_seconds_count{stage=%q} %d\n", stage, histogram.count)
		fmt.Fprintf(output, "clashlens_collector_stage_duration_seconds_sum{stage=%q} %s\n", stage, strconv.FormatFloat(histogram.sum, 'f', 6, 64))
	}
}

func writeCounterMap(output *strings.Builder, name string, labels []string, values map[string]uint64) {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		parts := strings.Split(key, "\x00")
		output.WriteString(name)
		output.WriteByte('{')
		for index, label := range labels {
			if index > 0 {
				output.WriteByte(',')
			}
			value := ""
			if index < len(parts) {
				value = parts[index]
			}
			fmt.Fprintf(output, "%s=%q", label, value)
		}
		fmt.Fprintf(output, "} %d\n", values[key])
	}
}

func writeFloatMap(output *strings.Builder, name string, labels []string, values map[string]float64) {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		parts := strings.Split(key, "\x00")
		output.WriteString(name)
		output.WriteByte('{')
		for index, label := range labels {
			if index > 0 {
				output.WriteByte(',')
			}
			value := ""
			if index < len(parts) {
				value = parts[index]
			}
			fmt.Fprintf(output, "%s=%q", label, value)
		}
		fmt.Fprintf(output, "} %s\n", strconv.FormatFloat(values[key], 'f', 6, 64))
	}
}
