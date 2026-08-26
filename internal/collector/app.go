package collector

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const dependencyReadinessInterval = 10 * time.Second

type dependencyReadiness struct {
	checkedAt time.Time
	err       error
}

type application struct {
	config  collectorConfig
	store   *store
	archive *s3Archive
	api     *officialAPIClient
	keys    *keyPool
	metrics *collectorMetrics
	logger  *slog.Logger
	owner   string

	dependencyReadiness atomic.Value
	dependencyCheckMu   sync.Mutex
	lastSpoolCleanupAt  atomic.Int64
}

type readinessReport struct {
	Ready      bool              `json:"ready"`
	Components map[string]string `json:"components"`
}

func newApplication(ctx context.Context, config collectorConfig, logger *slog.Logger) (*application, error) {
	if config.archiveRegion == "" {
		config.archiveRegion = "us-east-1"
	}
	store, err := openStoreWithPoolSize(ctx, config.databaseURL, config.schemaVersion, config.databasePoolSize)
	if err != nil {
		return nil, err
	}
	store.archiveInstanceID = config.archiveInstanceID
	if err := store.validateArchiveInstance(ctx, config.archiveEndpoint, config.archiveRegion, config.archiveBucket, config.archiveMarkerKey, config.archiveMarkerHash, config.archiveMarkerPayloadVersion); err != nil {
		store.close()
		return nil, err
	}
	if err := store.validateTrafficGateMode(ctx, config.trafficGateMode); err != nil {
		store.close()
		return nil, fmt.Errorf("validate shared traffic gate: %w", err)
	}
	archive, err := newS3ArchiveWithRegion(
		config.archiveEndpoint,
		config.archiveRegion,
		config.archiveSecure,
		config.archiveBucket,
		config.archiveAccessKey,
		config.archiveSecretKey,
	)
	if err != nil {
		store.close()
		return nil, err
	}
	archive.maximumBodyBytes = config.maximumResponseBytes
	archive.catalogueVerified = store.verifiedCatalogue
	if config.schemaVersion >= 3 && config.spoolRoot != "" {
		if config.spoolMaxBytes <= 0 {
			config.spoolMaxBytes = 16 << 30
		}
		if config.spoolMaxObjects <= 0 {
			config.spoolMaxObjects = 1000000
		}
		if config.spoolStaleTempAge <= 0 {
			config.spoolStaleTempAge = 30 * time.Minute
		}
		if config.spoolCleanupBatch <= 0 {
			config.spoolCleanupBatch = 100
		}
		if config.spoolSafetyAge <= 0 {
			config.spoolSafetyAge = 24 * time.Hour
		}
		archive.spool, err = newEvidenceSpool(spoolConfig{
			root: config.spoolRoot, maxBytes: config.spoolMaxBytes,
			maxObjects: config.spoolMaxObjects, freeSpaceFloor: config.spoolFreeSpaceFloor,
			freeInodeFloor: config.spoolFreeInodeFloor, staleTempAge: config.spoolStaleTempAge,
		})
	}
	if err != nil {
		store.close()
		return nil, err
	}
	keys, err := newKeyPool(
		config.keys,
		config.requestsPerSecondPerKey,
		false,
	)
	if err != nil {
		store.close()
		return nil, err
	}
	api, err := newOfficialAPIClient(officialAPIConfig{
		origin:                config.officialAPIOrigin,
		proxyURL:              config.officialAPIProxyURL,
		allowInsecureTestHTTP: config.allowInsecureTestHTTP,
		connectionTimeout:     config.connectionTimeout,
		responseHeaderTimeout: config.responseHeaderTimeout,
		totalTimeout:          config.totalRequestTimeout,
		maximumResponseBytes:  config.maximumResponseBytes,
	})
	if err != nil {
		store.close()
		return nil, err
	}
	hostname, err := os.Hostname()
	if err != nil {
		hostname = "unknown-host"
	}
	ownerToken, err := randomToken()
	if err != nil {
		store.close()
		return nil, err
	}
	if config.trafficGateMode == requiredTrafficGateMode {
		interactiveKey, keyError := onlyInteractiveKey(config.keys)
		if keyError != nil {
			store.close()
			return nil, keyError
		}
		if err := store.registerSharedCredential(
			ctx,
			bearerTokenFingerprint(interactiveKey.Secret),
			29,
			1,
			30,
			"collector:startup",
		); err != nil {
			store.close()
			return nil, fmt.Errorf("register shared interactive API credential: %w", err)
		}
	}
	if archive.spool == nil {
		if err := archive.verifyWriteCapability(ctx, ownerToken); err != nil {
			store.close()
			return nil, fmt.Errorf("collector startup guard failed: %w", err)
		}
	}
	metrics := newCollectorMetrics()
	archive.observeStage = metrics.recordStageDuration
	store.metrics = metrics
	app := &application{
		config:  config,
		store:   store,
		archive: archive,
		api:     api,
		keys:    keys,
		metrics: metrics,
		logger:  logger,
		owner:   hostname + "-" + strconv.Itoa(os.Getpid()) + "-" + ownerToken[:8],
	}
	if err := app.ready(ctx); err != nil {
		store.close()
		return nil, fmt.Errorf("collector startup guard failed: %w", err)
	}
	return app, nil
}

func onlyInteractiveKey(keys []APIKey) (APIKey, error) {
	var interactive []APIKey
	for _, key := range keys {
		if key.Pool == interactivePool {
			interactive = append(interactive, key)
		}
	}
	if len(interactive) != 1 {
		return APIKey{}, errors.New("exactly one shared interactive API key is required")
	}
	return interactive[0], nil
}

func (a *application) close() {
	if a.archive != nil && a.archive.spool != nil {
		a.archive.spool.close()
	}
	a.store.close()
}

func (a *application) ready(ctx context.Context) error {
	_, err := a.readiness(ctx)
	return err
}

func (a *application) readiness(ctx context.Context) (readinessReport, error) {
	report := readinessReport{
		Ready: true,
		Components: map[string]string{
			"postgresql":           "ready",
			"archive":              "ready",
			"remote_archive":       "degraded",
			"normal_key_pool":      "ready",
			"interactive_key_pool": "ready",
		},
	}
	if a.archive.spool != nil && a.config.archiveMarkerKey != "" && a.config.archiveMarkerHash != "" {
		if err := a.archive.markerHealth(ctx, a.config.archiveMarkerKey, a.config.archiveMarkerHash); err == nil {
			report.Components["remote_archive"] = "ready"
		} else if strings.Contains(err.Error(), "marker hash mismatch") {
			report.Ready = false
			report.Components["remote_archive"] = "not_ready"
		}
	}
	checks := []struct {
		name string
		err  error
	}{
		{name: "postgresql", err: a.store.ready(ctx)},
		{name: "archive", err: a.archive.ready(ctx)},
		{name: "spool", err: a.archive.spoolReady()},
		{name: "normal_key_pool", err: a.keys.readyForPool(normalPool)},
		{name: "interactive_key_pool", err: a.keys.readyForPool(interactivePool)},
	}
	var failures []error
	for _, check := range checks {
		if check.err == nil {
			continue
		}
		report.Ready = false
		report.Components[check.name] = "not_ready"
		failures = append(failures, fmt.Errorf("%s: %w", check.name, check.err))
	}
	return report, errors.Join(failures...)
}

func (a *application) dependenciesReady(ctx context.Context) error {
	if cached, ok := a.dependencyReadiness.Load().(dependencyReadiness); ok && time.Since(cached.checkedAt) < dependencyReadinessInterval {
		return cached.err
	}
	a.dependencyCheckMu.Lock()
	defer a.dependencyCheckMu.Unlock()
	if cached, ok := a.dependencyReadiness.Load().(dependencyReadiness); ok && time.Since(cached.checkedAt) < dependencyReadinessInterval {
		return cached.err
	}
	var failures []error
	if err := a.store.ready(ctx); err != nil {
		failures = append(failures, fmt.Errorf("postgresql: %w", err))
	}
	// Remote marker health is deliberately not a claim dependency when the
	// shared spool is active. Keep the legacy test path strict.
	if a.archive.spool == nil {
		if err := a.archive.ready(ctx); err != nil {
			failures = append(failures, fmt.Errorf("archive: %w", err))
		}
	} else if err := a.archive.spoolReady(); err != nil {
		failures = append(failures, fmt.Errorf("spool: %w", err))
	}
	err := errors.Join(failures...)
	a.dependencyReadiness.Store(dependencyReadiness{checkedAt: time.Now(), err: err})
	return err
}

func (a *application) schedulerOnce(ctx context.Context, now time.Time) error {
	// Reset admission is prepared before regular work is considered. This
	// prevents priority ordering from allowing a regular poll to overlap the
	// reset baseline cycle.
	boundaryAvailable, err := a.store.boundaryAdmissionAvailable(ctx)
	if err != nil {
		return err
	}
	var sweepID int64
	var created bool
	if boundaryAvailable {
		sweepID, created, err = a.store.prepareBoundaryAdmission(ctx, now)
	} else {
		boundary := resetBoundaryAtOrBefore(now)
		sweepID, created, err = a.store.scheduleResetSweep(ctx, boundary)
	}
	if err != nil {
		return err
	}

	regularAdmissionAllowed, err := a.store.regularAdmissionAllowed(ctx, now)
	if err != nil {
		return err
	}
	schedulerStartedAt := time.Now()
	scheduled, err := a.store.scheduleDueRegular(ctx, now, a.config.pollCycle, a.config.scheduleBatchSize)
	a.metrics.recordStageDuration("schedule_due_regular", time.Since(schedulerStartedAt))
	if err != nil {
		return err
	}
	for range scheduled {
		a.metrics.recordJob("regular_poll", string(normalPool), "scheduled")
	}
	var globalRankingsCreated bool
	if regularAdmissionAllowed && a.config.enableGlobalRankings {
		globalRankingsCreated, err = a.store.scheduleGlobalRankings(ctx, now, 5*time.Minute)
		if err != nil {
			return err
		}
	}
	if globalRankingsCreated {
		a.metrics.recordJob("global_player_rankings", string(normalPool), "scheduled")
	}
	if a.archive != nil && a.archive.spool != nil {
		lastNanos := a.lastSpoolCleanupAt.Load()
		last := time.Unix(0, lastNanos)
		if lastNanos == 0 || now.Sub(last) >= a.config.spoolCleanupInterval {
			cleanupStartedAt := time.Now()
			_, cleanupErr := a.archive.spool.cleanup(ctx, now, a.config.spoolSafetyAge, a.config.spoolCleanupBatch, a.store.cleanupEligible)
			if cleanupErr == nil {
				_, cleanupErr = a.archive.spool.orphanSweep(ctx, now, a.config.spoolOrphanSafetyAge, a.config.spoolCleanupBatch, a.store.catalogueContains, a.store.pendingContains)
			}
			if cleanupErr == nil {
				cleanupErr = a.archive.spool.sweepStale(now)
			}
			if cleanupErr != nil && a.logger != nil {
				a.logger.WarnContext(ctx, "raw-evidence cleanup degraded", "error", cleanupErr)
			}
			a.lastSpoolCleanupAt.Store(now.UnixNano())
			a.metrics.recordStageDuration("spool_cleanup", time.Since(cleanupStartedAt))
		}
	}
	a.logger.InfoContext(
		ctx,
		"scheduler tick",
		"regular_jobs_created", scheduled,
		"global_rankings_job_created", globalRankingsCreated,
		"reset_sweep_id", sweepID,
		"reset_sweep_created", created,
	)
	return nil
}

func (a *application) configuredWorker(ownerSuffix string) *worker {
	return newWorker(a.store, a.archive, a.api, a.keys, workerConfig{
		owner:             a.owner + "-" + ownerSuffix,
		leaseDuration:     a.config.leaseDuration,
		collectorVersion:  a.config.collectorVersion,
		maximumRetries:    a.config.maximumRetries,
		dependenciesReady: a.dependenciesReady,
		retryPolicy: newRetryPolicy(
			a.config.retryBaseDelay,
			a.config.retryMaximumDelay,
			a.config.retryJitterFraction,
		),
		metrics: a.metrics,
		logger:  a.logger,
	})
}

func (a *application) drain(ctx context.Context) error {
	worker := a.configuredWorker("once")
	for {
		interactiveClaimed, err := worker.runOnce(ctx, interactivePool)
		if err != nil {
			return err
		}
		normalClaimed, err := worker.runOnce(ctx, normalPool)
		if err != nil {
			return err
		}
		recoveryClaimed := false
		if a.store.recoveryRetrySupported {
			recoveryClaimed, err = worker.runOnce(ctx, recoveryPool)
			if err != nil {
				return err
			}
		}
		if !interactiveClaimed && !normalClaimed && !recoveryClaimed {
			return nil
		}
	}
}

func (a *application) run(ctx context.Context, role string) error {
	if role != "both" && role != "scheduler" && role != "worker" {
		return fmt.Errorf("unknown role %q", role)
	}
	if role == "both" || role == "worker" {
		releaseKeyOwnership, err := a.store.acquireAPIKeyOwnership(ctx, a.config.keys)
		if err != nil {
			return err
		}
		defer releaseKeyOwnership()
	}
	runContext, cancelRun := context.WithCancel(ctx)
	defer cancelRun()

	var wait sync.WaitGroup
	errorsByLoop := make(chan error, 32)
	if role == "both" || role == "scheduler" {
		wait.Add(1)
		go func() {
			defer wait.Done()
			a.runSchedulerLoop(runContext, errorsByLoop)
		}()
	}
	if role == "both" || role == "worker" {
		normalWorkers := 0
		interactiveWorkers := 0
		for _, key := range a.config.keys {
			if key.Pool == normalPool {
				normalWorkers += a.config.workersPerKey
			} else {
				interactiveWorkers += a.config.workersPerKey
			}
		}
		for index := range normalWorkers {
			wait.Add(1)
			go func(index int) {
				defer wait.Done()
				a.runWorkerLoop(runContext, normalPool, "normal-"+strconv.Itoa(index), errorsByLoop)
			}(index)
		}
		for index := range interactiveWorkers {
			wait.Add(1)
			go func(index int) {
				defer wait.Done()
				a.runWorkerLoop(runContext, interactivePool, "interactive-"+strconv.Itoa(index), errorsByLoop)
			}(index)
		}
		if a.store.recoveryRetrySupported {
			// Recovery has one deliberately separate worker. It cannot occupy
			// an interactive worker slot, and its database permit is capped at
			// one request per second.
			wait.Add(1)
			go func() {
				defer wait.Done()
				a.runWorkerLoop(runContext, recoveryPool, "recovery-0", errorsByLoop)
			}()
		}
	}

	serverErrors := make(chan error, 1)
	server := a.startHealthServer(runContext, serverErrors)
	waitComplete := make(chan struct{})
	go func() {
		wait.Wait()
		close(waitComplete)
	}()

	shutdown := func() {
		cancelRun()
		if server != nil {
			shutdownContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			_ = server.Shutdown(shutdownContext)
			cancel()
		}
		<-waitComplete
	}

	select {
	case <-ctx.Done():
		shutdown()
		return nil
	case err := <-errorsByLoop:
		shutdown()
		return err
	case err := <-serverErrors:
		shutdown()
		return err
	case <-waitComplete:
		cancelRun()
		return nil
	}
}

func (a *application) runSchedulerLoop(ctx context.Context, errorsByLoop chan<- error) {
	ticker := time.NewTicker(a.config.schedulerInterval)
	defer ticker.Stop()
	for {
		if err := a.schedulerOnce(ctx, time.Now().UTC()); err != nil {
			if ctx.Err() != nil {
				return
			}
			select {
			case errorsByLoop <- fmt.Errorf("scheduler loop: %w", err):
			default:
			}
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (a *application) runWorkerLoop(
	ctx context.Context,
	pool capacityPool,
	ownerSuffix string,
	errorsByLoop chan<- error,
) {
	worker := a.configuredWorker(ownerSuffix)
	defer func() {
		releaseContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := a.store.releaseOwnerLeases(releaseContext, worker.config.owner, time.Now().UTC()); err != nil {
			a.logger.Error("release worker leases", "owner", worker.config.owner, "error", err)
		}
	}()
	for {
		claimed, err := worker.runOnce(ctx, pool)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			a.logger.ErrorContext(ctx, "worker job failed", "pool", pool, "error", err)
			if !waitForInterval(ctx, a.config.workerIdleInterval) {
				return
			}
			continue
		}
		if claimed {
			continue
		}
		if !waitForInterval(ctx, a.config.workerIdleInterval) {
			return
		}
	}
}

func waitForInterval(ctx context.Context, interval time.Duration) bool {
	timer := time.NewTimer(interval)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func (a *application) operationalHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /livez", func(response http.ResponseWriter, _ *http.Request) {
		response.WriteHeader(http.StatusOK)
		_, _ = response.Write([]byte("ok\n"))
	})
	mux.HandleFunc("GET /readyz", func(response http.ResponseWriter, request *http.Request) {
		report, _ := a.readiness(request.Context())
		response.Header().Set("Content-Type", "application/json")
		if !report.Ready {
			response.WriteHeader(http.StatusServiceUnavailable)
		}
		_ = json.NewEncoder(response).Encode(report)
	})
	mux.HandleFunc("GET /metrics", func(response http.ResponseWriter, request *http.Request) {
		metrics, err := a.metrics.render(request.Context(), a.store, a.keys, time.Now().UTC(), a.archive.spool)
		if err != nil {
			http.Error(response, "metrics unavailable", http.StatusServiceUnavailable)
			return
		}
		response.Header().Set("Content-Type", "text/plain; version=0.0.4")
		_, _ = response.Write([]byte(metrics))
	})
	return mux
}

func (a *application) startHealthServer(ctx context.Context, errorsByServer chan<- error) *http.Server {
	if a.config.healthListenAddress == "" {
		return nil
	}
	server := &http.Server{
		Addr:              a.config.healthListenAddress,
		Handler:           a.operationalHandler(),
		ReadHeaderTimeout: 2 * time.Second,
	}
	go func() {
		a.logger.InfoContext(ctx, "health server started", "address", a.config.healthListenAddress)
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			select {
			case errorsByServer <- fmt.Errorf("health server: %w", err):
			default:
			}
		}
	}()
	return server
}
