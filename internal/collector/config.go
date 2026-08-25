package collector

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// The collector PostgreSQL pool is explicitly bounded. The safe default of
// 16 preserves pgxpool's behavior on the 16-thread production host. The
// measured target profile explicitly raises it to 32 for 32 normal workers;
// the deployment validates 1-64.
const (
	defaultCollectorDatabasePoolSize = 16
	maximumCollectorDatabasePoolSize = 64
)

type collectorConfig struct {
	databaseURL                 string
	schemaVersion               int
	trafficGateMode             trafficGateMode
	archiveEndpoint             string
	archiveRegion               string
	archiveSecure               bool
	archiveBucket               string
	archiveInstanceID           string
	archiveMarkerKey            string
	archiveMarkerHash           string
	archiveMarkerPayloadVersion string
	spoolRoot                   string
	spoolMaxBytes               int64
	spoolMaxObjects             int64
	spoolFreeSpaceFloor         uint64
	spoolFreeInodeFloor         uint64
	spoolSafetyAge              time.Duration
	spoolOrphanSafetyAge        time.Duration
	spoolCleanupInterval        time.Duration
	spoolCleanupBatch           int
	spoolStaleTempAge           time.Duration
	archiveAccessKey            string
	archiveSecretKey            string
	officialAPIOrigin           string
	officialAPIProxyURL         string
	allowInsecureTestHTTP       bool
	enableGlobalRankings        bool
	keys                        []APIKey
	requestsPerSecondPerKey     int
	workersPerKey               int
	databasePoolSize            int
	pollCycle                   time.Duration
	scheduleBatchSize           int
	leaseDuration               time.Duration
	maximumRetries              int
	retryBaseDelay              time.Duration
	retryMaximumDelay           time.Duration
	retryJitterFraction         float64
	interactiveCooldown         time.Duration
	schedulerInterval           time.Duration
	workerIdleInterval          time.Duration
	connectionTimeout           time.Duration
	responseHeaderTimeout       time.Duration
	totalRequestTimeout         time.Duration
	maximumResponseBytes        int64
	healthListenAddress         string
	collectorVersion            string
}

type maintenanceConfig struct {
	databaseURL   string
	schemaVersion int
}

func loadMaintenanceConfig(getenv func(string) string) (maintenanceConfig, error) {
	databaseURL, err := secretSetting(getenv, "CLASHLENS_DATABASE_URL")
	if err != nil {
		return maintenanceConfig{}, err
	}
	config := maintenanceConfig{
		databaseURL:   databaseURL,
		schemaVersion: 1,
	}
	if err := optionalInt(getenv, "CLASHLENS_SCHEMA_VERSION", &config.schemaVersion); err != nil {
		return maintenanceConfig{}, err
	}
	if config.databaseURL == "" {
		return maintenanceConfig{}, errors.New("CLASHLENS_DATABASE_URL is required")
	}
	return config, nil
}

func loadConfig(getenv func(string) string) (collectorConfig, error) {
	databaseURL, err := secretSetting(getenv, "CLASHLENS_DATABASE_URL")
	if err != nil {
		return collectorConfig{}, err
	}
	archiveAccessKey, err := secretSetting(getenv, "CLASHLENS_ARCHIVE_ACCESS_KEY")
	if err != nil {
		return collectorConfig{}, err
	}
	archiveSecretKey, err := secretSetting(getenv, "CLASHLENS_ARCHIVE_SECRET_KEY")
	if err != nil {
		return collectorConfig{}, err
	}
	config := collectorConfig{
		databaseURL:                 databaseURL,
		schemaVersion:               1,
		trafficGateMode:             bridgeTrafficGateMode,
		archiveEndpoint:             strings.TrimSpace(getenv("CLASHLENS_ARCHIVE_ENDPOINT")),
		archiveRegion:               strings.TrimSpace(getenv("CLASHLENS_ARCHIVE_REGION")),
		archiveBucket:               strings.TrimSpace(getenv("CLASHLENS_ARCHIVE_BUCKET")),
		archiveInstanceID:           strings.TrimSpace(getenv("CLASHLENS_ARCHIVE_INSTANCE_ID")),
		archiveMarkerKey:            strings.TrimSpace(getenv("CLASHLENS_ARCHIVE_MARKER_KEY")),
		archiveMarkerHash:           strings.TrimSpace(getenv("CLASHLENS_ARCHIVE_MARKER_HASH")),
		archiveMarkerPayloadVersion: strings.TrimSpace(getenv("CLASHLENS_ARCHIVE_MARKER_PAYLOAD_VERSION")),
		spoolRoot:                   strings.TrimSpace(getenv("CLASHLENS_SPOOL_ROOT")),
		spoolMaxBytes:               16 << 30,
		spoolMaxObjects:             1000000,
		spoolFreeSpaceFloor:         1 << 30,
		spoolFreeInodeFloor:         10000,
		spoolSafetyAge:              24 * time.Hour,
		spoolOrphanSafetyAge:        24 * time.Hour,
		spoolCleanupInterval:        10 * time.Minute,
		spoolCleanupBatch:           100,
		spoolStaleTempAge:           30 * time.Minute,
		archiveAccessKey:            archiveAccessKey,
		archiveSecretKey:            archiveSecretKey,
		officialAPIOrigin:           strings.TrimSpace(getenv("CLASHLENS_OFFICIAL_API_ORIGIN")),
		officialAPIProxyURL:         strings.TrimSpace(getenv("CLASHLENS_OFFICIAL_API_PROXY_URL")),
		requestsPerSecondPerKey:     30,
		workersPerKey:               8,
		databasePoolSize:            defaultCollectorDatabasePoolSize,
		pollCycle:                   5 * time.Minute,
		scheduleBatchSize:           1000,
		leaseDuration:               30 * time.Second,
		maximumRetries:              4,
		retryBaseDelay:              500 * time.Millisecond,
		retryMaximumDelay:           30 * time.Second,
		retryJitterFraction:         0.2,
		interactiveCooldown:         30 * time.Second,
		schedulerInterval:           time.Second,
		workerIdleInterval:          250 * time.Millisecond,
		connectionTimeout:           3 * time.Second,
		responseHeaderTimeout:       5 * time.Second,
		totalRequestTimeout:         10 * time.Second,
		maximumResponseBytes:        4 << 20,
		healthListenAddress:         strings.TrimSpace(getenv("CLASHLENS_HEALTH_LISTEN")),
		collectorVersion:            strings.TrimSpace(getenv("CLASHLENS_COLLECTOR_VERSION")),
	}
	if config.officialAPIOrigin == "" {
		config.officialAPIOrigin = "https://api.clashofclans.com"
	}
	if config.collectorVersion == "" {
		config.collectorVersion = "prototype"
	}

	if config.archiveSecure, err = optionalBool(getenv, "CLASHLENS_ARCHIVE_SECURE", true); err != nil {
		return collectorConfig{}, err
	}
	if config.allowInsecureTestHTTP, err = optionalBool(getenv, "CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN", false); err != nil {
		return collectorConfig{}, err
	}
	if config.enableGlobalRankings, err = optionalBool(getenv, "CLASHLENS_ENABLE_GLOBAL_RANKINGS", false); err != nil {
		return collectorConfig{}, err
	}
	allowReducedPools, err := optionalBool(getenv, "CLASHLENS_ALLOW_REDUCED_KEY_POOLS", false)
	if err != nil {
		return collectorConfig{}, err
	}
	if strings.TrimSpace(getenv("CLASHLENS_ALLOW_INTERACTIVE_FOR_NORMAL")) != "" {
		return collectorConfig{}, errors.New("CLASHLENS_ALLOW_INTERACTIVE_FOR_NORMAL is not supported")
	}

	for _, setting := range []struct {
		name   string
		target *time.Duration
	}{
		{name: "CLASHLENS_POLL_CYCLE", target: &config.pollCycle},
		{name: "CLASHLENS_LEASE_DURATION", target: &config.leaseDuration},
		{name: "CLASHLENS_RETRY_BASE_DELAY", target: &config.retryBaseDelay},
		{name: "CLASHLENS_RETRY_MAXIMUM_DELAY", target: &config.retryMaximumDelay},
		{name: "CLASHLENS_INTERACTIVE_COOLDOWN", target: &config.interactiveCooldown},
		{name: "CLASHLENS_SCHEDULER_INTERVAL", target: &config.schedulerInterval},
		{name: "CLASHLENS_WORKER_IDLE_INTERVAL", target: &config.workerIdleInterval},
		{name: "CLASHLENS_CONNECTION_TIMEOUT", target: &config.connectionTimeout},
		{name: "CLASHLENS_RESPONSE_HEADER_TIMEOUT", target: &config.responseHeaderTimeout},
		{name: "CLASHLENS_TOTAL_REQUEST_TIMEOUT", target: &config.totalRequestTimeout},
	} {
		if value := strings.TrimSpace(getenv(setting.name)); value != "" {
			parsed, parseError := time.ParseDuration(value)
			if parseError != nil || parsed <= 0 {
				return collectorConfig{}, fmt.Errorf("%s must be a positive duration", setting.name)
			}
			*setting.target = parsed
		}
	}
	if err := optionalInt(getenv, "CLASHLENS_SCHEMA_VERSION", &config.schemaVersion); err != nil {
		return collectorConfig{}, err
	}
	if config.schemaVersion >= 2 {
		config.trafficGateMode = requiredTrafficGateMode
	}
	if value := strings.TrimSpace(getenv("CLASHLENS_SHARED_TRAFFIC_GATE_MODE")); value != "" {
		mode := trafficGateMode(value)
		if mode != bridgeTrafficGateMode && mode != requiredTrafficGateMode {
			return collectorConfig{}, errors.New("CLASHLENS_SHARED_TRAFFIC_GATE_MODE must be bridge or required")
		}
		config.trafficGateMode = mode
	}
	if err := optionalInt(getenv, "CLASHLENS_SCHEDULE_BATCH_SIZE", &config.scheduleBatchSize); err != nil {
		return collectorConfig{}, err
	}
	if err := optionalInt(getenv, "CLASHLENS_MAXIMUM_RETRIES", &config.maximumRetries); err != nil {
		return collectorConfig{}, err
	}
	if value := strings.TrimSpace(getenv("CLASHLENS_MAX_BODY_BYTES")); value != "" {
		if err := optionalInt64(getenv, "CLASHLENS_MAX_BODY_BYTES", &config.maximumResponseBytes); err != nil {
			return collectorConfig{}, err
		}
	} else if err := optionalInt64(getenv, "CLASHLENS_MAXIMUM_RESPONSE_BYTES", &config.maximumResponseBytes); err != nil {
		return collectorConfig{}, err
	}
	config.spoolRoot = strings.TrimSpace(config.spoolRoot)
	if config.archiveRegion == "" {
		config.archiveRegion = "us-east-1"
	}
	for _, setting := range []struct {
		name   string
		target *time.Duration
	}{
		{name: "CLASHLENS_SPOOL_SAFETY_AGE_SECONDS", target: &config.spoolSafetyAge},
		{name: "CLASHLENS_SPOOL_ORPHAN_SAFETY_AGE_SECONDS", target: &config.spoolOrphanSafetyAge},
		{name: "CLASHLENS_SPOOL_CLEANUP_INTERVAL_SECONDS", target: &config.spoolCleanupInterval},
		{name: "CLASHLENS_SPOOL_STALE_TEMP_AGE_SECONDS", target: &config.spoolStaleTempAge},
	} {
		if value := strings.TrimSpace(getenv(setting.name)); value != "" {
			seconds, parseError := strconv.Atoi(value)
			if parseError != nil || seconds <= 0 {
				return collectorConfig{}, fmt.Errorf("%s must be a positive integer", setting.name)
			}
			*setting.target = time.Duration(seconds) * time.Second
		}
	}
	if err := optionalInt64(getenv, "CLASHLENS_SPOOL_MAX_BYTES", &config.spoolMaxBytes); err != nil {
		return collectorConfig{}, err
	}
	if err := optionalInt64(getenv, "CLASHLENS_SPOOL_MAX_OBJECTS", &config.spoolMaxObjects); err != nil {
		return collectorConfig{}, err
	}
	var freeSpaceFloor, freeInodeFloor int64
	freeSpaceFloor, freeInodeFloor = int64(config.spoolFreeSpaceFloor), int64(config.spoolFreeInodeFloor)
	if err := optionalInt64(getenv, "CLASHLENS_SPOOL_FREE_SPACE_FLOOR", &freeSpaceFloor); err != nil {
		return collectorConfig{}, err
	}
	if err := optionalInt64(getenv, "CLASHLENS_SPOOL_FREE_INODE_FLOOR", &freeInodeFloor); err != nil {
		return collectorConfig{}, err
	}
	config.spoolFreeSpaceFloor, config.spoolFreeInodeFloor = uint64(freeSpaceFloor), uint64(freeInodeFloor)
	if err := optionalInt(getenv, "CLASHLENS_SPOOL_CLEANUP_BATCH", &config.spoolCleanupBatch); err != nil {
		return collectorConfig{}, err
	}
	if config.schemaVersion >= 3 {
		if config.spoolRoot == "" {
			return collectorConfig{}, errors.New("CLASHLENS_SPOOL_ROOT is required for schema version 3")
		}
		if config.archiveInstanceID == "" || config.archiveMarkerKey == "" || config.archiveMarkerPayloadVersion == "" || len(config.archiveMarkerHash) != sha256HexLength {
			return collectorConfig{}, errors.New("archive instance ID and marker contract are required for schema version 3")
		}
		if _, err := hex.DecodeString(config.archiveMarkerHash); err != nil {
			return collectorConfig{}, errors.New("archive marker hash must be lowercase SHA-256")
		}
	}
	if config.spoolRoot != "" {
		if err := validateSpoolRoot(config.spoolRoot); err != nil {
			return collectorConfig{}, err
		}
		if config.spoolOrphanSafetyAge < config.spoolStaleTempAge || config.spoolOrphanSafetyAge < config.leaseDuration*time.Duration(config.maximumRetries+1) {
			return collectorConfig{}, errors.New("spool orphan safety age must cover retry and stale-temporary windows")
		}
	}
	if err := optionalInt(getenv, "CLASHLENS_REQUESTS_PER_SECOND_PER_KEY", &config.requestsPerSecondPerKey); err != nil {
		return collectorConfig{}, err
	}
	if config.requestsPerSecondPerKey < 1 || config.requestsPerSecondPerKey > 30 {
		return collectorConfig{}, errors.New("CLASHLENS_REQUESTS_PER_SECOND_PER_KEY must be between 1 and 30")
	}
	if err := optionalInt(getenv, "CLASHLENS_WORKERS_PER_KEY", &config.workersPerKey); err != nil {
		return collectorConfig{}, err
	}
	if err := optionalInt(getenv, "CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE", &config.databasePoolSize); err != nil {
		return collectorConfig{}, err
	}
	if config.databasePoolSize < 1 || config.databasePoolSize > maximumCollectorDatabasePoolSize {
		return collectorConfig{}, fmt.Errorf(
			"CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE must be between 1 and %d",
			maximumCollectorDatabasePoolSize,
		)
	}
	if value := strings.TrimSpace(getenv("CLASHLENS_RETRY_JITTER_FRACTION")); value != "" {
		parsed, parseError := strconv.ParseFloat(value, 64)
		if parseError != nil || parsed < 0 || parsed > 1 {
			return collectorConfig{}, errors.New("CLASHLENS_RETRY_JITTER_FRACTION must be between 0 and 1")
		}
		config.retryJitterFraction = parsed
	}

	normalKeys, err := parseConfiguredKeys(
		getenv("CLASHLENS_NORMAL_API_KEYS"),
		getenv("CLASHLENS_NORMAL_API_KEY_FILES"),
		normalPool,
	)
	if err != nil {
		return collectorConfig{}, fmt.Errorf("configure normal API keys: %w", err)
	}
	interactiveKeys, err := parseConfiguredKeys(
		getenv("CLASHLENS_INTERACTIVE_API_KEYS"),
		getenv("CLASHLENS_INTERACTIVE_API_KEY_FILES"),
		interactivePool,
	)
	if err != nil {
		return collectorConfig{}, fmt.Errorf("configure interactive API keys: %w", err)
	}
	if !allowReducedPools && len(normalKeys) != 4 {
		return collectorConfig{}, errors.New("configure exactly four normal API keys or explicitly allow reduced key pools")
	}
	if !allowReducedPools && len(interactiveKeys) != 1 {
		return collectorConfig{}, errors.New("configure exactly one interactive API key or explicitly allow reduced key pools")
	}
	if len(interactiveKeys) != 1 {
		return collectorConfig{}, errors.New("configure exactly one shared interactive API key")
	}
	if len(normalKeys) == 0 || len(interactiveKeys) == 0 {
		return collectorConfig{}, errors.New("normal and interactive API key pools must each contain at least one key")
	}
	config.keys = append(normalKeys, interactiveKeys...)
	labels := make(map[string]struct{}, len(config.keys))
	for _, key := range config.keys {
		if _, duplicate := labels[key.Label]; duplicate {
			return collectorConfig{}, fmt.Errorf("API key label %q is duplicated", key.Label)
		}
		labels[key.Label] = struct{}{}
	}

	if config.databaseURL == "" {
		return collectorConfig{}, errors.New("CLASHLENS_DATABASE_URL is required")
	}
	if config.archiveEndpoint == "" || config.archiveBucket == "" || config.archiveAccessKey == "" || config.archiveSecretKey == "" {
		return collectorConfig{}, errors.New("archive endpoint, bucket, access key, and secret key are required")
	}
	origin, err := url.Parse(config.officialAPIOrigin)
	if err != nil || origin.Host == "" || origin.Path != "" {
		return collectorConfig{}, errors.New("CLASHLENS_OFFICIAL_API_ORIGIN must be an absolute origin without a path")
	}
	if origin.Scheme != "https" && !config.allowInsecureTestHTTP {
		return collectorConfig{}, errors.New("CLASHLENS_OFFICIAL_API_ORIGIN must use HTTPS")
	}
	if config.officialAPIProxyURL != "" {
		proxyURL, parseError := url.Parse(config.officialAPIProxyURL)
		if parseError != nil || proxyURL.Host == "" || (proxyURL.Scheme != "http" && proxyURL.Scheme != "https") ||
			proxyURL.User != nil || proxyURL.Path != "" || proxyURL.RawQuery != "" || proxyURL.Fragment != "" {
			return collectorConfig{}, errors.New("CLASHLENS_OFFICIAL_API_PROXY_URL must be an HTTP or HTTPS origin without credentials, path, query, or fragment")
		}
	}
	if config.retryMaximumDelay < config.retryBaseDelay {
		return collectorConfig{}, errors.New("retry maximum delay must not be less than retry base delay")
	}
	return config, nil
}

func logConfigState(ctx context.Context, logger *slog.Logger, config collectorConfig) {
	if logger == nil {
		return
	}
	logger.InfoContext(
		ctx,
		"collector configuration loaded",
		"global_rankings_enabled", config.enableGlobalRankings,
	)
}

func secretSetting(getenv func(string) string, name string) (string, error) {
	direct := strings.TrimSpace(getenv(name))
	path := strings.TrimSpace(getenv(name + "_FILE"))
	if direct != "" && path != "" {
		return "", fmt.Errorf("%s and %s_FILE must not both be set", name, name)
	}
	if path == "" {
		return direct, nil
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read %s_FILE: %w", name, err)
	}
	return strings.TrimSpace(string(contents)), nil
}

func parseConfiguredKeys(inline, fileSpecs string, pool capacityPool) ([]APIKey, error) {
	var keys []APIKey
	for _, specification := range splitSpecifications(inline) {
		label, secret, ok := strings.Cut(specification, "=")
		label = strings.TrimSpace(label)
		secret = strings.TrimSpace(secret)
		if !ok || label == "" || secret == "" {
			return nil, errors.New("each inline API key must use nonempty label=secret syntax")
		}
		keys = append(keys, APIKey{Label: label, Secret: secret, Pool: pool})
	}
	for _, specification := range splitSpecifications(fileSpecs) {
		label, path, ok := strings.Cut(specification, "=")
		label = strings.TrimSpace(label)
		path = strings.TrimSpace(path)
		if !ok || label == "" || path == "" {
			return nil, errors.New("each API key file must use nonempty label=path syntax")
		}
		contents, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read API key file for label %q: %w", label, err)
		}
		secret, parseError := parseBearerTokenBytes(contents)
		if parseError != nil {
			return nil, fmt.Errorf("API key file for label %q: %w", label, parseError)
		}
		keys = append(keys, APIKey{Label: label, Secret: secret, Pool: pool})
	}
	return keys, nil
}

func splitSpecifications(value string) []string {
	var specifications []string
	for _, part := range strings.Split(value, ",") {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			specifications = append(specifications, trimmed)
		}
	}
	return specifications
}

func optionalBool(getenv func(string) string, name string, fallback bool) (bool, error) {
	value := strings.TrimSpace(getenv(name))
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return false, fmt.Errorf("%s must be a boolean", name)
	}
	return parsed, nil
}

func optionalInt(getenv func(string) string, name string, target *int) error {
	value := strings.TrimSpace(getenv(name))
	if value == "" {
		return nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		return fmt.Errorf("%s must be a positive integer", name)
	}
	*target = parsed
	return nil
}

func optionalInt64(getenv func(string) string, name string, target *int64) error {
	value := strings.TrimSpace(getenv(name))
	if value == "" {
		return nil
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	if err != nil || parsed <= 0 {
		return fmt.Errorf("%s must be a positive integer", name)
	}
	*target = parsed
	return nil
}
