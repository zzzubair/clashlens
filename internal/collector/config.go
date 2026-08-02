package collector

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

type collectorConfig struct {
	databaseURL               string
	schemaVersion             int
	archiveEndpoint           string
	archiveSecure             bool
	archiveBucket             string
	archiveAccessKey          string
	archiveSecretKey          string
	officialAPIOrigin         string
	allowInsecureTestHTTP     bool
	keys                      []APIKey
	allowInteractiveForNormal bool
	requestsPerSecondPerKey   int
	workersPerKey             int
	pollCycle                 time.Duration
	scheduleBatchSize         int
	leaseDuration             time.Duration
	maximumRetries            int
	retryBaseDelay            time.Duration
	retryMaximumDelay         time.Duration
	retryJitterFraction       float64
	interactiveCooldown       time.Duration
	schedulerInterval         time.Duration
	workerIdleInterval        time.Duration
	connectionTimeout         time.Duration
	responseHeaderTimeout     time.Duration
	totalRequestTimeout       time.Duration
	maximumResponseBytes      int64
	healthListenAddress       string
	collectorVersion          string
}

func loadConfig(getenv func(string) string) (collectorConfig, error) {
	config := collectorConfig{
		databaseURL:             strings.TrimSpace(getenv("CLASHLENS_DATABASE_URL")),
		schemaVersion:           1,
		archiveEndpoint:         strings.TrimSpace(getenv("CLASHLENS_ARCHIVE_ENDPOINT")),
		archiveBucket:           strings.TrimSpace(getenv("CLASHLENS_ARCHIVE_BUCKET")),
		archiveAccessKey:        getenv("CLASHLENS_ARCHIVE_ACCESS_KEY"),
		archiveSecretKey:        getenv("CLASHLENS_ARCHIVE_SECRET_KEY"),
		officialAPIOrigin:       strings.TrimSpace(getenv("CLASHLENS_OFFICIAL_API_ORIGIN")),
		requestsPerSecondPerKey: 30,
		workersPerKey:           8,
		pollCycle:               5 * time.Minute,
		scheduleBatchSize:       1000,
		leaseDuration:           30 * time.Second,
		maximumRetries:          4,
		retryBaseDelay:          500 * time.Millisecond,
		retryMaximumDelay:       30 * time.Second,
		retryJitterFraction:     0.2,
		interactiveCooldown:     30 * time.Second,
		schedulerInterval:       time.Second,
		workerIdleInterval:      250 * time.Millisecond,
		connectionTimeout:       3 * time.Second,
		responseHeaderTimeout:   5 * time.Second,
		totalRequestTimeout:     10 * time.Second,
		maximumResponseBytes:    4 << 20,
		healthListenAddress:     strings.TrimSpace(getenv("CLASHLENS_HEALTH_LISTEN")),
		collectorVersion:        strings.TrimSpace(getenv("CLASHLENS_COLLECTOR_VERSION")),
	}
	if config.officialAPIOrigin == "" {
		config.officialAPIOrigin = "https://api.clashofclans.com"
	}
	if config.collectorVersion == "" {
		config.collectorVersion = "prototype"
	}

	var err error
	if config.archiveSecure, err = optionalBool(getenv, "CLASHLENS_ARCHIVE_SECURE", true); err != nil {
		return collectorConfig{}, err
	}
	if config.allowInsecureTestHTTP, err = optionalBool(getenv, "CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN", false); err != nil {
		return collectorConfig{}, err
	}
	allowReducedPools, err := optionalBool(getenv, "CLASHLENS_ALLOW_REDUCED_KEY_POOLS", false)
	if err != nil {
		return collectorConfig{}, err
	}
	if config.allowInteractiveForNormal, err = optionalBool(
		getenv,
		"CLASHLENS_ALLOW_INTERACTIVE_FOR_NORMAL",
		false,
	); err != nil {
		return collectorConfig{}, err
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
	if err := optionalInt(getenv, "CLASHLENS_SCHEDULE_BATCH_SIZE", &config.scheduleBatchSize); err != nil {
		return collectorConfig{}, err
	}
	if err := optionalInt(getenv, "CLASHLENS_MAXIMUM_RETRIES", &config.maximumRetries); err != nil {
		return collectorConfig{}, err
	}
	if err := optionalInt64(getenv, "CLASHLENS_MAXIMUM_RESPONSE_BYTES", &config.maximumResponseBytes); err != nil {
		return collectorConfig{}, err
	}
	if err := optionalInt(getenv, "CLASHLENS_REQUESTS_PER_SECOND_PER_KEY", &config.requestsPerSecondPerKey); err != nil {
		return collectorConfig{}, err
	}
	if err := optionalInt(getenv, "CLASHLENS_WORKERS_PER_KEY", &config.workersPerKey); err != nil {
		return collectorConfig{}, err
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
	if config.retryMaximumDelay < config.retryBaseDelay {
		return collectorConfig{}, errors.New("retry maximum delay must not be less than retry base delay")
	}
	return config, nil
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
		secret := strings.TrimSpace(string(contents))
		if secret == "" {
			return nil, fmt.Errorf("API key file for label %q is empty", label)
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
