package collector

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/zzzubair/clashlens/internal/testsupport"
)

// capacityProbeMarker is the single machine-readable evidence line for the
// Slice D normal-capacity harness. Every value is a bounded aggregate measured
// from the integrated run below: real scheduler claims, key-pool admissions,
// loopback official fetches, archive commits, and disposable-DB rows. It never
// retains identities, secrets, bodies, or per-attempt timings.
const capacityProbeMarker = "CLASHLENS_CAPACITY_PROBE="

const (
	capacityPlayersDefault = 12833
	capacityKeys           = 4
	capacityPerKeyRPS      = 25
	capacityAggregateRPS   = 100
	capacityWorkersDefault = 32
	// Every 32nd player fails its first battle-log fetch with a deterministic
	// 503, exercising the production retry/backoff and parent/result lineage.
	capacityRetryEvery = 32
)

// capacityTagAlphabet mirrors the Python runner _tag() helper so every tag
// is valid for normalize_player_tag (^#[0289PYLQGRJCUV]+$) and reversible.
const capacityTagAlphabet = "0289PYLQGRJCUV"

func capacityTag(index int) string {
	if index < 0 {
		panic("capacity tag index must be non-negative")
	}
	encoded := ""
	for n := index; n > 0; n /= len(capacityTagAlphabet) {
		encoded = string(capacityTagAlphabet[n%len(capacityTagAlphabet)]) + encoded
	}
	for len(encoded) < 5 {
		encoded = "0" + encoded
	}
	return "#P" + encoded
}

func capacityTagIndex(tag string) int {
	if len(tag) < 3 || tag[0] != '#' || tag[1] != 'P' {
		return -1
	}
	index := 0
	for _, r := range tag[2:] {
		value := strings.IndexRune(capacityTagAlphabet, r)
		if value < 0 {
			return -1
		}
		index = index*len(capacityTagAlphabet) + value
	}
	return index
}

// capacityArrival records one loopback official request attributed to the pool
// key whose secret authorized it.
type capacityArrival struct {
	at       time.Time
	key      int
	endpoint string
}

type capacityOfficial struct {
	mu             sync.Mutex
	secrets        map[string]int
	profileTmpl    []byte
	battle         []byte
	ranking        []byte
	arrivals       []capacityArrival
	hits           int
	unknownSecrets int
	bodyBytes      int64
	injected503    int
	failedOnce     map[string]bool
}

func (s *capacityOfficial) keyOf(secret string) (int, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	// Unknown secrets are rejected, never served: unattributed traffic
	// cannot escape the per-key maxima below.
	key, ok := s.secrets[secret]
	if !ok {
		s.unknownSecrets++
		return -1, false
	}
	return key, true
}

func (s *capacityOfficial) playerIndex(tag string) int {
	// Dry tags never fetch; workload tags invert to 1-based ordinals.
	index := capacityTagIndex(tag)
	if index < 1 {
		return -1
	}
	return index - 1
}

func (s *capacityOfficial) handler(response http.ResponseWriter, request *http.Request) {
	secret := strings.TrimPrefix(request.Header.Get("Authorization"), "Bearer ")
	key, known := s.keyOf(secret)
	if !known {
		response.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(response, `{"reason":"unknown-key"}`)
		return
	}
	path := request.URL.EscapedPath()
	response.Header().Set("Content-Type", "application/json")
	switch {
	case path == "/v1/locations/global/rankings/players":
		s.mu.Lock()
		s.hits++
		s.arrivals = append(s.arrivals, capacityArrival{at: time.Now(), key: key, endpoint: "global_player_rankings"})
		body := s.ranking
		s.bodyBytes += int64(len(body))
		s.mu.Unlock()
		_, _ = response.Write(body)
		return
	case strings.HasPrefix(path, "/v1/players/"):
		rest := strings.TrimPrefix(path, "/v1/players/")
		tag := rest
		battleLog := false
		if strings.HasSuffix(rest, "/battlelog") {
			battleLog = true
			tag = strings.TrimSuffix(rest, "/battlelog")
		}
		// EscapedPath leaves %23 for '#'; decode the tag manually.
		tag = strings.ReplaceAll(tag, "%23", "#")
		index := s.playerIndex(tag)
		ep := "profile"
		if battleLog {
			ep = "battle_log"
		}
		s.mu.Lock()
		s.hits++
		s.arrivals = append(s.arrivals, capacityArrival{at: time.Now(), key: key, endpoint: ep})
		if battleLog && index >= 0 && index%capacityRetryEvery == 0 && !s.failedOnce[tag] {
			s.failedOnce[tag] = true
			s.injected503++
			s.bodyBytes += int64(len(`{"reason":"maintenance"}`))
			s.mu.Unlock()
			response.WriteHeader(http.StatusServiceUnavailable)
			_, _ = io.WriteString(response, `{"reason":"maintenance"}`)
			return
		}
		var body []byte
		if battleLog {
			body = s.battle
		} else {
			body = bytesReplaceTag(s.profileTmpl, tag)
		}
		s.bodyBytes += int64(len(body))
		s.mu.Unlock()
		_, _ = response.Write(body)
		return
	default:
		http.NotFound(response, request)
	}
}

func bytesReplaceTag(tmpl []byte, tag string) []byte {
	return []byte(strings.Replace(string(tmpl), "#2PP", tag, 1))
}

// capacityHostID labels local calibration receipts so they can never be
// mistaken for the final isolated qualification run. Bounded short string:
// hostname, CPU count, and total memory.
func capacityHostID() string {
	host, err := os.Hostname()
	if err != nil || host == "" {
		host = "unknown-host"
	}
	if len(host) > 64 {
		host = host[:64]
	}
	var memMB int64 = -1
	if data, err := os.ReadFile("/proc/meminfo"); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			var kb int64
			if _, err := fmt.Sscanf(line, "MemTotal: %d kB", &kb); err == nil && kb > 0 {
				memMB = kb / 1024
				break
			}
		}
	}
	return fmt.Sprintf("%s|cpu%d|mem%dMB", host, runtime.NumCPU(), memMB)
}

// capacityHTTPTotals counts loopback attempts per endpoint from arrivals.
func capacityHTTPTotals(arrivals []capacityArrival) map[string]int {
	totals := map[string]int{}
	for _, arrival := range arrivals {
		totals[arrival.endpoint]++
	}
	return totals
}

// capacityBucketMaxima returns the largest arrival count inside any rolling
// one-second window, per key and aggregate.
func capacityBucketMaxima(arrivals []capacityArrival, keys int) (map[int]int, int) {
	byKey := make(map[int][]time.Time, keys+1)
	var all []time.Time
	for _, arrival := range arrivals {
		byKey[arrival.key] = append(byKey[arrival.key], arrival.at)
		all = append(all, arrival.at)
	}
	maxima := make(map[int]int, keys+1)
	for key, times := range byKey {
		sort.Slice(times, func(i, j int) bool { return times[i].Before(times[j]) })
		best, left := 0, 0
		for right := range times {
			for times[right].Sub(times[left]) >= time.Second {
				left++
			}
			if width := right - left + 1; width > best {
				best = width
			}
		}
		maxima[key] = best
	}
	sort.Slice(all, func(i, j int) bool { return all[i].Before(all[j]) })
	aggregate, left := 0, 0
	for right := range all {
		for all[right].Sub(all[left]) >= time.Second {
			left++
		}
		if width := right - left + 1; width > aggregate {
			aggregate = width
		}
	}
	return maxima, aggregate
}

// TestIssue92CapacityProbe executes the fixed Slice D workload through the
// production collector path: scheduler claims, key-pool admissions, loopback
// official fetches, archive commits, and disposable-DB rows. It is opt-in
// (skipped without a database) and emits one bounded aggregate marker.
//
// Isolation: the runner supplies a disposable schema via
// CLASHLENS_CAPACITY_DATABASE_URL. For local calibration only,
// CLASHLENS_CAPACITY_EMBEDDED=1 starts a throwaway embedded PostgreSQL in a
// temp dir, applies the committed migrations, and stops it on cleanup. No
// official, remote-archive, production, or issue82 traffic is possible: the
// only HTTP servers are loopback httptest fixtures.
// TestIssue92CapacityUnknownSecretRejected proves unknown API secrets are
// rejected before admission: no body is served, no arrival is recorded, and
// the unknown counter trips the terminal gate. No database is required.
func TestIssue92CapacityUnknownSecretRejected(t *testing.T) {
	official := &capacityOfficial{
		secrets:    map[string]int{"capacity-probe-secret-0": 0},
		failedOnce: map[string]bool{},
	}
	server := httptest.NewServer(http.HandlerFunc(official.handler))
	defer server.Close()

	// Unknown secret: rejected, unattributed.
	response, err := http.Get(server.URL + "/v1/locations/global/rankings/players")
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unknown secret status = %d, want 401", response.StatusCode)
	}
	official.mu.Lock()
	unknown := official.unknownSecrets
	hits := official.hits
	official.mu.Unlock()
	if unknown != 1 {
		t.Fatalf("unknown counter = %d, want 1", unknown)
	}
	if hits != 0 {
		t.Fatalf("rejected request was counted as a hit")
	}

	// Known secret: served and attributed to its key.
	request, err := http.NewRequest(http.MethodGet, server.URL+"/v1/locations/global/rankings/players", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer capacity-probe-secret-0")
	response, err = http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("known secret status = %d, want 200", response.StatusCode)
	}
	official.mu.Lock()
	defer official.mu.Unlock()
	if len(official.arrivals) != 1 || official.arrivals[0].key != 0 {
		t.Fatalf("known request was not attributed to key 0: %+v", official.arrivals)
	}
}

func TestIssue92CapacityProbe(t *testing.T) {
	// This historical probe assumes immediate regular retries and a fixed
	// 300-second batch. Neither is the current rolling-poll contract.
	if os.Getenv("CLASHLENS_CAPACITY_DATABASE_URL") != "" || os.Getenv("CLASHLENS_CAPACITY_EMBEDDED") == "1" {
		t.Fatal("fixed-window capacity probe retired; see docs/collector-polling.md")
	}
	databaseURL := os.Getenv("CLASHLENS_CAPACITY_DATABASE_URL")
	if databaseURL == "" {
		if os.Getenv("CLASHLENS_CAPACITY_EMBEDDED") != "1" {
			t.Skip("issue92 capacity probe is not enabled")
		}
		databaseURL = testsupport.StartPostgres(t)
		applyCapacityMigrations(t, databaseURL)
	}
	budget := 4333
	if raw := os.Getenv("CLASHLENS_CAPACITY_RETRY_BUDGET"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 0 || value > 4333 {
			t.Fatal("invalid issue92 capacity retry budget")
		}
		budget = value
	}
	spoolRoot := os.Getenv("CLASHLENS_CAPACITY_SPOOL_DIR")
	if spoolRoot == "" {
		spoolRoot = filepath.Join(t.TempDir(), "spool")
	}
	// Calibration overrides for scaling experiments; qualification requires
	// the exact defaults (enforced Python-side: players must be 12833 and
	// lanes 32 or the marker is rejected).
	players := capacityPlayersDefault
	if raw := os.Getenv("CLASHLENS_CAPACITY_PLAYERS"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 1 || value > capacityPlayersDefault {
			t.Fatal("invalid issue92 capacity player count")
		}
		players = value
	}
	lanes := capacityWorkersDefault
	if raw := os.Getenv("CLASHLENS_CAPACITY_WORKERS"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 1 || value > 64 {
			t.Fatal("invalid issue92 capacity lane count")
		}
		lanes = value
	}

	ctx, cancel := context.WithTimeout(context.Background(), 560*time.Second)
	defer cancel()
	probeStart := time.Now()

	store, err := openStoreWithPoolSize(ctx, databaseURL, 5, 40)
	if err != nil {
		t.Fatal("open capacity probe store")
	}
	defer store.close()
	// Production wires the archive instance ID in app startup (app.go); the
	// probe must do the same or catalogue commits fail on contract v3+.
	store.archiveInstanceID = "issue92-capacity"
	if err := store.ready(ctx); err != nil {
		t.Fatal("capacity probe store is not ready")
	}
	store.setEndpointBudget(endpointBudgetConfig{
		enabled: true,
		runID:   "issue92-capacity",
		caps: map[endpointName]int{
			profileEndpoint:              capacityPlayersDefault,
			battleLogEndpoint:            capacityPlayersDefault + (capacityPlayersDefault+capacityRetryEvery-1)/capacityRetryEvery,
			globalPlayerRankingsEndpoint: 1,
		},
		// PostgreSQL timestamptz keeps microsecond precision.
		deadline: time.Now().UTC().Add(590 * time.Second).Truncate(time.Microsecond),
	})

	// Dry reset exclusion first, reusing the accepted no-HTTP boundary seam
	// with a two-player scenario: full admission semantics, not a full sweep.
	// The boundary is the most recent 05:00 UTC instant so the admission
	// gate takes its main path instead of the pre-reset path.
	nowUTC := time.Now().UTC()
	boundary := time.Date(nowUTC.Year(), nowUTC.Month(), nowUTC.Day(), 5, 0, 0, 0, time.UTC)
	if boundary.After(nowUTC) {
		boundary = boundary.Add(-24 * time.Hour)
	}
	dryTags := []string{capacityTag(900001), capacityTag(900002)}
	for i, tag := range dryTags {
		if _, err := store.pool.Exec(ctx, `
			INSERT INTO players (normalized_tag, active, next_due_at)
			VALUES ($1, true, $2)
		`, tag, boundary.Add(-time.Hour)); err != nil {
			t.Fatalf("seed dry exclusion player %d: %v", i, err)
		}
	}
	admit := step8AdmissionProbeAdmit(t, ctx, store, boundary, 2)
	if admit["regular_allowed_during_reset"] != false || admit["regular_scheduled_during_reset"] != 0 {
		t.Fatal("dry reset exclusion did not defer regular work")
	}
	var sweepID int64
	if err := store.pool.QueryRow(ctx, `
		SELECT id FROM collector_reset_sweeps ORDER BY id DESC LIMIT 1
	`).Scan(&sweepID); err != nil {
		t.Fatal("read dry reset sweep")
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET status = 'complete', updated_at = clock_timestamp()
		WHERE sweep_id = $1 AND work_type = 'reset_baseline'
	`, sweepID); err != nil {
		t.Fatal("drain dry reset roots")
	}
	handoff := step8AdmissionProbeHandoff(t, ctx, store, boundary, 2)
	if handoff["safe_handoff"] != true || handoff["regular_allowed_after_handoff"] != true {
		t.Fatal("dry reset handoff did not restore regular admission")
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE players SET active = false WHERE normalized_tag IN ($1, $2)
	`, dryTags[0], dryTags[1]); err != nil {
		t.Fatal("retire dry exclusion players")
	}
	var watermark int64
	if err := store.pool.QueryRow(ctx, `SELECT COALESCE(max(id), 0) FROM collector_jobs`).Scan(&watermark); err != nil {
		t.Fatal("record workload job watermark")
	}
	var sweepsBefore int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_reset_sweeps`).Scan(&sweepsBefore); err != nil {
		t.Fatal("read sweep baseline")
	}

	relationBefore, walBefore := capacityDBSize(t, ctx, store)

	// Seed the exact ordinary population: due players, two attempts each.
	seedStart := time.Now()
	tags := make([]string, 0, players)
	for i := 1; i <= players; i++ {
		tags = append(tags, capacityTag(i))
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		SELECT tag, true, $2 FROM unnest($1::text[]) AS tag
	`, tags, boundary.Add(-time.Hour)); err != nil {
		t.Fatal("seed capacity players")
	}
	scheduled, err := store.scheduleDueRegular(ctx, boundary, 5*time.Minute, players)
	if err != nil {
		t.Fatal("schedule capacity regular work")
	}
	if scheduled != players {
		t.Fatalf("scheduled %d regular jobs, want %d", scheduled, players)
	}
	rankingCreated, err := store.scheduleGlobalRankings(ctx, boundary, 5*time.Minute)
	if err != nil {
		t.Fatal("schedule capacity rankings")
	}
	if !rankingCreated {
		t.Fatal("global rankings cycle was not created")
	}
	seedElapsed := time.Since(seedStart)

	profileTmpl, err := os.ReadFile(filepath.Join("..", "..", "python", "testdata", "legend_i_profile_v1.json"))
	if err != nil {
		t.Fatal("read profile fixture")
	}
	battleFixture, err := os.ReadFile(filepath.Join("..", "..", "python", "testdata", "legend_i_battle_log_v1.json"))
	if err != nil {
		t.Fatal("read battle fixture")
	}
	rankingFixture, err := os.ReadFile(filepath.Join("..", "..", "python", "testdata", "global_top_200_v1.json"))
	if err != nil {
		t.Fatal("read ranking fixture")
	}
	official := &capacityOfficial{
		secrets:     map[string]int{},
		profileTmpl: profileTmpl,
		battle:      battleFixture,
		ranking:     rankingFixture,
		failedOnce:  map[string]bool{},
	}
	keys := make([]APIKey, 0, capacityKeys)
	for i := 0; i < capacityKeys; i++ {
		secret := fmt.Sprintf("capacity-probe-secret-%d", i)
		official.secrets[secret] = i
		keys = append(keys, APIKey{
			Label:  fmt.Sprintf("capacity-normal-%d", i),
			Secret: secret,
			Pool:   normalPool,
		})
	}
	keyPool, err := newKeyPool(keys, capacityPerKeyRPS, false)
	if err != nil {
		t.Fatal("capacity key pool rejected four normal keys at 25/s")
	}
	if _, err := newKeyPool(keys, capacityPerKeyRPS, true); err == nil {
		t.Fatal("capacity pool accepted unsafe normal fallback")
	}
	apiServer := httptest.NewServer(http.HandlerFunc(official.handler))
	defer apiServer.Close()
	api, err := newOfficialAPIClient(officialAPIConfig{
		origin:                apiServer.URL,
		allowInsecureTestHTTP: true,
		connectionTimeout:     2 * time.Second,
		responseHeaderTimeout: 5 * time.Second,
		totalTimeout:          30 * time.Second,
		maximumResponseBytes:  1 << 20,
	})
	if err != nil {
		t.Fatal("capacity official client rejected the loopback origin")
	}
	// The runner's shared loopback archive server stays alive through the
	// Python downstream drain, so Go commits and Python reads share one
	// operation ledger instead of a short-lived fixture.
	archiveEndpoint := os.Getenv("CLASHLENS_CAPACITY_ARCHIVE_ENDPOINT")
	if archiveEndpoint == "" {
		t.Fatal("capacity archive endpoint is required")
	}
	archive, err := newS3Archive(archiveEndpoint, false, "evidence", "access", "secret")
	if err != nil {
		t.Fatal("capacity archive rejected the loopback origin")
	}
	spool, err := newEvidenceSpool(spoolConfig{
		root: spoolRoot, maxBytes: 64 << 20,
		maxObjects: 30000, staleTempAge: time.Hour,
	})
	if err != nil {
		t.Fatal("capacity spool setup failed")
	}
	defer spool.close()
	archive.spool = spool
	archive.maximumBodyBytes = 1 << 20
	// Per-operation S3 evidence split by phase: requests before ordinary
	// completion count as ordinary, later ones as retry. Early retries that
	// overlap the ordinary tail attribute to ordinary; the retry tranche
	// itself is proven separately through parent/result lineage.
	var s3phase atomic.Int64
	var s3mu sync.Mutex
	s3ordinary := map[string]int64{}
	s3retry := map[string]int64{}
	archive.observeRequest = func(operation string) {
		s3mu.Lock()
		defer s3mu.Unlock()
		if s3phase.Load() == 0 {
			s3ordinary[operation]++
		} else {
			s3retry[operation]++
		}
	}
	archive.catalogueVerified = store.verifiedCatalogue
	if store.archiveRetention {
		archive.catalogueLocation = store.catalogueLocation
	}
	markerDigest := sha256.Sum256([]byte("issue92-capacity-marker"))
	markerHash := hex.EncodeToString(markerDigest[:])
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO archive_instances (
			instance_id, endpoint, region, bucket,
			marker_key, marker_hash, marker_payload_version
		)
		VALUES ('issue92-capacity', $1, 'us-east-1', 'evidence',
			'issue92-capacity-marker', $2, 'v1')
	`, archiveEndpoint, markerHash); err != nil {
		t.Fatal("register capacity archive instance")
	}
	if err := store.validateArchiveInstance(ctx, archiveEndpoint, "us-east-1", "evidence",
		"issue92-capacity-marker", markerHash, "v1"); err != nil {
		t.Fatal("validate capacity archive instance")
	}

	// Peak monitor: lightweight in-run sampling of relation bytes, WAL, and
	// spool allocated bytes. A cap breach or a measurement error cancels
	// work immediately and fails before cleanup; the checker reports it.
	runCtx, stopWorkers := context.WithCancel(ctx)
	var peakRelation atomic.Int64
	var peakSpoolAlloc atomic.Int64
	var peakRetainedWAL atomic.Int64
	var monitorOnce sync.Once
	var monitorReason atomic.Value
	failMonitor := func(reason string) {
		monitorOnce.Do(func() {
			monitorReason.Store(reason)
			stopWorkers()
		})
	}
	peakRelation.Store(relationBefore)
	monitorDone := make(chan struct{})
	go func() {
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-monitorDone:
				return
			case <-ticker.C:
				relation, err := capacityRelationBytes(ctx, store)
				if err != nil {
					failMonitor(fmt.Sprintf("relation measurement failed: %v", err))
					return
				}
				for {
					current := peakRelation.Load()
					if relation <= current || peakRelation.CompareAndSwap(current, relation) {
						break
					}
				}
				_, allocated, _, err := capacityDirUsage(spoolRoot)
				if err != nil {
					failMonitor(fmt.Sprintf("spool measurement failed: %v", err))
					return
				}
				for {
					current := peakSpoolAlloc.Load()
					if allocated <= current || peakSpoolAlloc.CompareAndSwap(current, allocated) {
						break
					}
				}
				var walNow int64
				if err := store.pool.QueryRow(ctx, `SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), $1)::bigint`, walBefore).Scan(&walNow); err != nil {
					failMonitor(fmt.Sprintf("WAL measurement failed: %v", err))
					return
				}
				retainedNow, err := capacityRetainedWAL(ctx, store)
				if err != nil {
					failMonitor(fmt.Sprintf("retained WAL measurement failed: %v", err))
					return
				}
				for {
					current := peakRetainedWAL.Load()
					if retainedNow <= current || peakRetainedWAL.CompareAndSwap(current, retainedNow) {
						break
					}
				}
				// WAL LSN difference is cluster-wide; during this exclusive
				// run it is attributed to the workload as a ceiling.
				// Caps mirror the Python retained-artifact constants
				// CAPACITY_SPOOL_BYTES/CAPACITY_PG_PEAK_BYTES/
				// CAPACITY_TOTAL_BYTES (authorized disposable values:
				// spool 64 MiB, PG peak 320 MiB, combined 384 MiB).
				pgFootprint := (peakRelation.Load() - relationBefore) + walNow
				spoolWater := peakSpoolAlloc.Load()
				switch {
				case spoolWater > 64<<20:
					failMonitor(fmt.Sprintf("spool peak %d bytes exceeds the 64 MiB cap", spoolWater))
					return
				case pgFootprint > 320<<20:
					failMonitor(fmt.Sprintf("PostgreSQL footprint %d bytes exceeds the 320 MiB cap", pgFootprint))
					return
				case spoolWater+pgFootprint > 384<<20:
					failMonitor(fmt.Sprintf("combined footprint %d bytes exceeds the 384 MiB cap", spoolWater+pgFootprint))
					return
				}
			}
		}
	}()

	// Thirty-two collection lanes drain through runOnce until quiescence,
	// mirroring production runWorkerLoop: a failed job is recorded and the
	// lane continues; only systemic failure (errors with zero progress)
	// stops the run early.
	firstClaim := time.Now()
	debug := os.Getenv("CLASHLENS_CAPACITY_DEBUG") == "1"
	errCh := make(chan error, 4096)
	var claimedJobs atomic.Int64
	var workerErrs atomic.Int64
	var firstErrMu sync.Mutex
	var firstErr error
	errHistMu := &sync.Mutex{}
	errHist := map[string]int{}
	laneMetrics := newCollectorMetrics()
	var workers sync.WaitGroup
	for i := 0; i < lanes; i++ {
		workers.Add(1)
		go func(lane int) {
			defer workers.Done()
			worker := newWorker(store, archive, api, keyPool, workerConfig{
				owner: fmt.Sprintf("capacity-w%02d", lane),
				// Production default lease (config.go): short enough that a
				// dead lane's jobs recover inside the wall via the
				// production expiry path instead of wedging drain.
				leaseDuration:    30 * time.Second,
				collectorVersion: "issue92-capacity",
				maximumRetries:   3,
				retryPolicy:      newRetryPolicy(500*time.Millisecond, 30*time.Second, 0.2),
				metrics:          laneMetrics,
			})
			for {
				select {
				case <-runCtx.Done():
					return
				default:
				}
				claimed, err := worker.runOnce(runCtx, normalPool)
				if err != nil {
					if runCtx.Err() != nil {
						return
					}
					workerErrs.Add(1)
					errHistMu.Lock()
					hkey := fmt.Sprintf("%.120v", err)
					if len(errHist) < 8 || errHist[hkey] > 0 {
						errHist[hkey]++
					}
					errHistMu.Unlock()
					firstErrMu.Lock()
					if firstErr == nil {
						firstErr = err
					}
					firstErrMu.Unlock()
					select {
					case errCh <- err:
					default:
					}
					select {
					case <-runCtx.Done():
						return
					case <-time.After(200 * time.Millisecond):
					}
					continue
				}
				if claimed {
					claimedJobs.Add(1)
					continue
				}
				if !claimed {
					select {
					case <-runCtx.Done():
						return
					case <-time.After(100 * time.Millisecond):
					}
				}
			}
		}(i)
	}
	quiesced := false
	checkerTicks := 0
	var lastErrs, lastClaimed int64
	ordinaryTerminal := false
	var ordinaryElapsed time.Duration
	for {
		select {
		case <-ctx.Done():
			stopWorkers()
			workers.Wait()
			t.Fatal("capacity workload did not drain before the wall")
		case <-time.After(500 * time.Millisecond):
		}
		checkerTicks++
		if debug && checkerTicks%60 == 0 {
			rows, queryErr := store.pool.Query(ctx, `
				SELECT status, work_type, count(*) FROM collector_jobs
				WHERE id > $1 AND status IN ('pending', 'leased', 'waiting_retry', 'waiting_dependency')
				GROUP BY status, work_type ORDER BY status, work_type
			`, watermark)
			if queryErr == nil {
				var parts []string
				for rows.Next() {
					var status, workType string
					var count int
					if scanErr := rows.Scan(&status, &workType, &count); scanErr == nil && len(parts) < 12 {
						parts = append(parts, fmt.Sprintf("%s/%s=%d", status, workType, count))
					}
				}
				rows.Close()
				firstErrMu.Lock()
				errHistMu.Lock()
				var histParts []string
				for htext, hcount := range errHist {
					if len(histParts) >= 8 {
						break
					}
					histParts = append(histParts, fmt.Sprintf("%dx[%s]", hcount, htext))
				}
				sort.Strings(histParts)
				histText := strings.Join(histParts, "; ")
				errHistMu.Unlock()
				firstText := fmt.Sprintf("%.160v", firstErr)
				firstErrMu.Unlock()
				fmt.Printf("capacity-debug t=%ds claimed=%d workererrs=%d %s firsterr=%s\n",
					int(time.Since(probeStart).Seconds()), claimedJobs.Load(), workerErrs.Load(), strings.Join(parts, " "), firstText+" {"+histText+"}")
			} else {
				rows.Close()
			}
		}
		var nonterminal int
		if err := store.pool.QueryRow(ctx, `
			SELECT count(*) FROM collector_jobs
			WHERE id > $1 AND status IN ('pending', 'leased', 'waiting_retry', 'waiting_dependency')
		`, watermark).Scan(&nonterminal); err != nil {
			stopWorkers()
			workers.Wait()
			t.Fatal("read capacity drain state")
		}
		// Timing split: ordinary completion (regular + ranking terminal,
		// retries excluded) is recorded separately from full quiescence so
		// the <=300s ordinary acceptance cannot be masked by retry work.
		if !ordinaryTerminal {
			var ordinaryNonterminal int
			if err := store.pool.QueryRow(ctx, `
				SELECT count(*) FROM collector_jobs
				WHERE id > $1 AND work_type IN ('regular_poll', 'global_player_rankings')
				  AND status IN ('pending', 'leased', 'waiting_retry', 'waiting_dependency')
			`, watermark).Scan(&ordinaryNonterminal); err != nil {
				stopWorkers()
				workers.Wait()
				t.Fatal("read capacity ordinary drain state")
			}
			if ordinaryNonterminal == 0 {
				ordinaryTerminal = true
				ordinaryElapsed = time.Since(firstClaim)
				s3phase.Store(1)
			}
		}
		if reason := monitorReason.Load(); reason != nil {
			stopWorkers()
			workers.Wait()
			t.Fatalf("capacity in-run monitor: %s", reason)
		}
		if workerErrs.Load()-lastErrs >= int64(lanes) && claimedJobs.Load() == lastClaimed {
			stopWorkers()
			workers.Wait()
			firstErrMu.Lock()
			defer firstErrMu.Unlock()
			t.Fatalf("capacity errors with zero progress: %d errors, %d claimed, first: %.300v",
				workerErrs.Load()-lastErrs, claimedJobs.Load(), firstErr)
		}
		lastErrs = workerErrs.Load()
		lastClaimed = claimedJobs.Load()
		if nonterminal == 0 {
			quiesced = true
			stopWorkers()
			workers.Wait()
			break
		}
	}
	close(monitorDone)
	if !quiesced {
		t.Fatal("capacity drain state is invalid")
	}
	drainElapsed := time.Since(firstClaim)
	if !ordinaryTerminal {
		ordinaryElapsed = drainElapsed
	}
	close(errCh)
	var runErrs []error
	for err := range errCh {
		runErrs = append(runErrs, err)
	}
	if debug {
		laneMetrics.mu.Lock()
		type stageSum struct {
			name  string
			count uint64
			sum   float64
		}
		var stages []stageSum
		for name, histogram := range laneMetrics.stageDurations {
			stages = append(stages, stageSum{name, histogram.count, histogram.sum})
		}
		laneMetrics.mu.Unlock()
		sort.Slice(stages, func(i, j int) bool { return stages[i].sum > stages[j].sum })
		for i, stage := range stages {
			if i >= 15 {
				break
			}
			avg := 0.0
			if stage.count > 0 {
				avg = stage.sum / float64(stage.count) * 1000
			}
			fmt.Printf("capacity-debug stage %s count=%d sum=%.1fs avg=%.2fms\n",
				stage.name, stage.count, stage.sum, avg)
		}
		fmt.Printf("capacity-debug runerrs=%d claimed=%d\n", len(runErrs), claimedJobs.Load())
	}
	// Expected 503 retries resolve through resolveAttempt with nil errors;
	// any runOnce error is unexplained and fails the run.
	if workerErrs.Load() > 0 {
		firstErrMu.Lock()
		defer firstErrMu.Unlock()
		t.Fatalf("capacity workers hit %d unexplained errors, first: %.200v", workerErrs.Load(), firstErr)
	}

	// Terminal capture before any cleanup.
	s3mu.Lock()
	s3ordPut, s3ordHead, s3ordGet := s3ordinary["put"], s3ordinary["head"], s3ordinary["get"]
	s3retPut, s3retHead, s3retGet := s3retry["put"], s3retry["head"], s3retry["get"]
	s3mu.Unlock()
	official.mu.Lock()
	arrivals := append([]capacityArrival(nil), official.arrivals...)
	hits := official.hits
	injected := official.injected503
	servedBytes := official.bodyBytes
	official.mu.Unlock()
	for _, arrival := range arrivals {
		if arrival.key < 0 {
			t.Fatalf("unattributed arrival escaped admission for endpoint %s", arrival.endpoint)
		}
	}
	perKeyMaxima, aggregateMaxima := capacityBucketMaxima(arrivals, capacityKeys)
	for key := 0; key < capacityKeys; key++ {
		if perKeyMaxima[key] > capacityPerKeyRPS {
			t.Fatalf("normal key %d reached %d/s, above the 25/s cap", key, perKeyMaxima[key])
		}
	}
	if aggregateMaxima > capacityAggregateRPS {
		t.Fatalf("aggregate rate reached %d/s, above the 100/s cap", aggregateMaxima)
	}

	var profileObserved, battlelogObserved, rankingObserved, retryingTerminal int
	if err := store.pool.QueryRow(ctx, `
		SELECT
			count(*) FILTER (WHERE result.endpoint = 'profile' AND result.outcome = 'observed'),
			count(*) FILTER (WHERE result.endpoint = 'battle_log' AND result.outcome = 'observed'),
			count(*) FILTER (WHERE result.endpoint = 'global_player_rankings' AND result.outcome = 'observed'),
			count(*) FILTER (WHERE result.outcome = 'retrying')
		FROM collector_endpoint_results AS result
		JOIN collector_attempts AS attempt ON attempt.id = result.attempt_id
		JOIN collector_jobs AS job ON job.id = attempt.job_id
		WHERE job.id > $1
	`, watermark).Scan(&profileObserved, &battlelogObserved, &rankingObserved, &retryingTerminal); err != nil {
		t.Fatal("read capacity endpoint outcomes")
	}
	ordinary := profileObserved + battlelogObserved + rankingObserved
	if ordinary != 2*players+1 {
		t.Fatalf("ordinary attempts = %d, want %d", ordinary, 2*players+1)
	}
	if retryingTerminal != 0 {
		t.Fatalf("%d endpoint results never drained", retryingTerminal)
	}
	var parents, missing, duplicates, retriesExecuted int
	if err := store.pool.QueryRow(ctx, `
		SELECT
			(SELECT count(*) FROM collector_jobs
			 WHERE id > $1 AND work_type = 'endpoint_retry' AND parent_attempt_id IS NOT NULL),
			(SELECT count(*) FROM collector_jobs AS job
			 LEFT JOIN collector_attempts AS attempt ON attempt.id = job.parent_attempt_id
			 WHERE job.id > $1 AND job.work_type = 'endpoint_retry'
			   AND job.parent_attempt_id IS NOT NULL AND attempt.id IS NULL),
			(SELECT count(*) FROM (
				SELECT parent_attempt_id, required_endpoint FROM collector_jobs
				WHERE id > $1 AND work_type = 'endpoint_retry'
				GROUP BY parent_attempt_id, required_endpoint HAVING count(*) > 1
			) AS dup),
			(SELECT count(*) FROM collector_jobs
			 WHERE id > $1 AND work_type = 'endpoint_retry' AND status = 'complete')
	`, watermark).Scan(&parents, &missing, &duplicates, &retriesExecuted); err != nil {
		t.Fatalf("read capacity retry lineage: %v", err)
	}
	if parents == 0 {
		t.Fatal("capacity retry lineage has zero parents")
	}
	if parents != injected {
		t.Fatalf("retry parents = %d, want the %d injected retries", parents, injected)
	}
	if parents > budget {
		t.Fatalf("retry parents = %d, above the %d tranche budget", parents, budget)
	}
	if missing != 0 || duplicates != 0 {
		t.Fatalf("retry lineage missing = %d, duplicate = %d", missing, duplicates)
	}
	budgetCaps := map[string]int{"profile": 12833, "battle_log": 13235, "global_player_rankings": 1}
	budgetUsed := map[string]int{}
	for endpoint, cap := range budgetCaps {
		var storedCap, consumed int
		if err := store.pool.QueryRow(ctx, `
			SELECT cap, consumed FROM collector_endpoint_budgets
			WHERE run_id = 'issue92-capacity' AND endpoint = $1
		`, endpoint).Scan(&storedCap, &consumed); err != nil {
			t.Fatalf("read capacity budget for %s: %v", endpoint, err)
		}
		if storedCap != cap {
			t.Fatalf("budget cap for %s = %d, want %d", endpoint, storedCap, cap)
		}
		budgetUsed[endpoint] = consumed
	}
	httpTotals := capacityHTTPTotals(arrivals)
	for endpoint, cap := range budgetCaps {
		if budgetUsed[endpoint] != httpTotals[endpoint] {
			t.Fatalf("budget consumed for %s = %d, loopback attempts = %d", endpoint, budgetUsed[endpoint], httpTotals[endpoint])
		}
		if budgetUsed[endpoint] > cap {
			t.Fatalf("budget consumed for %s = %d exceeds cap %d", endpoint, budgetUsed[endpoint], cap)
		}
	}

	relationAfter, err := capacityRelationBytes(ctx, store)
	if err != nil {
		t.Fatal("read terminal relation bytes")
	}
	relationGrowth := relationAfter - relationBefore
	if relationAfter > peakRelation.Load() {
		peakRelation.Store(relationAfter)
	}
	_, finalAlloc, _, err := capacityDirUsage(spoolRoot)
	if err != nil {
		t.Fatal("read terminal spool usage")
	}
	ledger, err := spool.ledger()
	if err != nil {
		t.Fatal("read terminal spool ledger")
	}
	peakAlloc := peakSpoolAlloc.Load()
	if finalAlloc > peakAlloc {
		peakAlloc = finalAlloc
		peakSpoolAlloc.Store(peakAlloc)
	}
	if peakAlloc > 64<<20 {
		t.Fatalf("spool peak %d allocated bytes exceeds the 64 MiB cap", peakAlloc)
	}
	var walBytes int64
	if err := store.pool.QueryRow(ctx, `SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), $1)::bigint`, walBefore).Scan(&walBytes); err != nil {
		t.Fatal("read capacity WAL growth")
	}
	// PostgreSQL peak includes WAL: relation high-water growth plus the
	// WAL generated across the run. Combined spool+PG must fit 384 MiB
	// before output bytes are added Python-side.
	pgPeak := (peakRelation.Load() - relationBefore) + walBytes
	if peakAlloc+pgPeak > 384<<20 {
		t.Fatalf("combined spool+PG footprint %d bytes exceeds the 384 MiB cap", peakAlloc+pgPeak)
	}
	retainedWAL, err := capacityRetainedWAL(ctx, store)
	if err != nil {
		t.Fatal("read terminal retained WAL")
	}
	if retainedWAL > peakRetainedWAL.Load() {
		peakRetainedWAL.Store(retainedWAL)
	}
	official.mu.Lock()
	unknownSecrets := official.unknownSecrets
	official.mu.Unlock()
	if unknownSecrets != 0 {
		t.Fatalf("%d requests arrived with unknown API secrets", unknownSecrets)
	}
	if pgPeak > 320<<20 {
		t.Fatalf("PostgreSQL peak %d bytes exceeds the 320 MiB cap", pgPeak)
	}
	var sweepsAfter int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_reset_sweeps`).Scan(&sweepsAfter); err != nil {
		t.Fatal("read terminal sweep count")
	}
	evidence := map[string]any{
		"players":                      players,
		"lanes":                        lanes,
		"profile_attempts":             profileObserved,
		"battlelog_attempts":           battlelogObserved,
		"ranking_attempts":             rankingObserved,
		"ordinary_attempts":            ordinary,
		"retry_injected":               injected,
		"retry_parents":                parents,
		"retry_missing":                missing,
		"retry_duplicate":              duplicates,
		"retries_executed":             retriesExecuted,
		"retry_budget":                 budget,
		"worker_errors":                workerErrs.Load(),
		"keys":                         capacityKeys,
		"per_key_rps":                  capacityPerKeyRPS,
		"aggregate_rps":                capacityAggregateRPS,
		"per_key_max":                  maxPerKey(perKeyMaxima),
		"aggregate_max":                aggregateMaxima,
		"official_requests":            hits,
		"official_bytes":               servedBytes,
		"s3_ordinary_put":              s3ordPut,
		"s3_ordinary_head":             s3ordHead,
		"s3_ordinary_get":              s3ordGet,
		"s3_retry_put":                 s3retPut,
		"s3_retry_head":                s3retHead,
		"s3_retry_get":                 s3retGet,
		"budget_cap_profile":           budgetCaps["profile"],
		"budget_cap_battlelog":         budgetCaps["battle_log"],
		"budget_cap_rankings":          budgetCaps["global_player_rankings"],
		"budget_used_profile":          budgetUsed["profile"],
		"budget_used_battlelog":        budgetUsed["battle_log"],
		"budget_used_rankings":         budgetUsed["global_player_rankings"],
		"http_profile":                 httpTotals["profile"],
		"http_battlelog":               httpTotals["battle_log"],
		"http_rankings":                httpTotals["global_player_rankings"],
		"host_id":                      capacityHostID(),
		"spool_peak_bytes":             peakAlloc,
		"spool_final_bytes":            ledger.FinalBytes,
		"spool_temp_bytes":             ledger.TemporaryBytes,
		"spool_reserved_bytes":         ledger.ReservedBytes,
		"spool_final_objects":          ledger.FinalObjects,
		"spool_temp_objects":           ledger.TemporaryObjects,
		"spool_reserved_objects":       ledger.ReservedObjects,
		"spool_alloc_bytes":            finalAlloc,
		"pg_growth_bytes":              relationGrowth,
		"pg_peak_bytes":                pgPeak,
		"wal_bytes":                    walBytes,
		"wal_retained_bytes":           retainedWAL,
		"wal_retained_peak_bytes":      peakRetainedWAL.Load(),
		"seed_ms":                      seedElapsed.Milliseconds(),
		"ordinary_ms":                  ordinaryElapsed.Milliseconds(),
		"drain_ms":                     drainElapsed.Milliseconds(),
		"wall_ms":                      time.Since(probeStart).Milliseconds(),
		"sweep_delta":                  sweepsAfter - sweepsBefore,
		"regular_scheduled_dry":        0,
		"reset_members":                2,
		"regular_allowed_during_reset": false,
		"reset_gate_dry":               true,
	}
	payload, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal("encode capacity probe evidence")
	}
	if len(payload) > 4096 {
		t.Fatal("capacity probe evidence exceeds 4096 bytes")
	}
	fmt.Println(capacityProbeMarker + string(payload))
}

func maxPerKey(maxima map[int]int) int {
	best := 0
	for _, value := range maxima {
		if value > best {
			best = value
		}
	}
	return best
}

func capacityRelationBytes(ctx context.Context, store *store) (int64, error) {
	var total int64
	if err := store.pool.QueryRow(ctx, `
		SELECT COALESCE(sum(pg_total_relation_size(oid)), 0)::bigint FROM pg_class
		WHERE relnamespace = current_schema()::regnamespace AND relkind IN ('r', 'm')
	`).Scan(&total); err != nil {
		return 0, err
	}
	return total, nil
}

// capacityRetainedWAL sums retained WAL segments. This is cluster-wide,
// not schema-scoped; on a quiet qualification host it tracks this run,
// on shared infrastructure it is a ceiling. Failure to read it fails the
// run because silent WAL growth would invalidate the footprint proof.
func capacityRetainedWAL(ctx context.Context, store *store) (int64, error) {
	var total int64
	if err := store.pool.QueryRow(ctx, `
		SELECT COALESCE(sum(size), 0)::bigint FROM pg_ls_waldir()
	`).Scan(&total); err != nil {
		return 0, err
	}
	return total, nil
}

func capacityDBSize(t *testing.T, ctx context.Context, store *store) (int64, string) {
	t.Helper()
	relation, err := capacityRelationBytes(ctx, store)
	if err != nil {
		t.Fatal("read capacity relation baseline")
	}
	var wal string
	if err := store.pool.QueryRow(ctx, `SELECT pg_current_wal_insert_lsn()::text`).Scan(&wal); err != nil {
		t.Fatal("read capacity WAL baseline")
	}
	return relation, wal
}

func applyCapacityMigrations(t *testing.T, databaseURL string) {
	t.Helper()
	connection, err := pgx.Connect(context.Background(), databaseURL)
	if err != nil {
		t.Fatalf("connect embedded capacity database: %v", err)
	}
	defer func() { _ = connection.Close(context.Background()) }()
	files, err := filepath.Glob(filepath.Join("..", "..", "deploy", "migrations", "*.sql"))
	if err != nil || len(files) == 0 {
		t.Fatal("list capacity migrations")
	}
	sort.Strings(files)
	for _, file := range files {
		statement, err := os.ReadFile(file)
		if err != nil {
			t.Fatalf("read capacity migration %s: %v", file, err)
		}
		if _, err := connection.Exec(context.Background(), string(statement)); err != nil {
			t.Fatalf("apply capacity migration %s: %v", file, err)
		}
	}
}

// capacityDirUsage measures spool directory logical bytes, allocated bytes
// (st_blocks, the real disk cost the 64 MiB cap bounds), and file counts.
func capacityDirUsage(root string) (logical, allocated, objects int64, err error) {
	err = filepath.Walk(root, func(_ string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if !info.IsDir() {
			logical += info.Size()
			objects++
			if stat, ok := info.Sys().(*syscall.Stat_t); ok {
				allocated += stat.Blocks * 512
			} else {
				allocated += info.Size()
			}
		}
		return nil
	})
	return logical, allocated, objects, err
}
