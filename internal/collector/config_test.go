package collector

import (
	"strings"
	"testing"
)

func TestLoadConfigRequiresFourNormalKeysAndOneInteractiveKeyByDefault(t *testing.T) {
	t.Parallel()
	environment := validConfigEnvironment()
	environment["CLASHLENS_NORMAL_API_KEYS"] = "normal-1=one,normal-2=two,normal-3=three"

	_, err := loadConfig(func(name string) string { return environment[name] })
	if err == nil || !strings.Contains(err.Error(), "four normal") {
		t.Fatalf("loadConfig error = %v, want four-normal-key error", err)
	}
}

func TestLoadConfigSeparatesNormalAndInteractiveKeys(t *testing.T) {
	t.Parallel()
	environment := validConfigEnvironment()

	config, err := loadConfig(func(name string) string { return environment[name] })
	if err != nil {
		t.Fatalf("loadConfig returned an error: %v", err)
	}
	var normal, interactive int
	for _, key := range config.keys {
		switch key.Pool {
		case normalPool:
			normal++
		case interactivePool:
			interactive++
		}
	}
	if normal != 4 || interactive != 1 {
		t.Fatalf("key pools = %d normal and %d interactive, want 4 and 1", normal, interactive)
	}
	if config.workersPerKey != 8 {
		t.Fatalf("workersPerKey = %d, want 8", config.workersPerKey)
	}
}

func TestLoadConfigRejectsInsecureOfficialOriginWithoutExplicitTestFlag(t *testing.T) {
	t.Parallel()
	environment := validConfigEnvironment()
	environment["CLASHLENS_OFFICIAL_API_ORIGIN"] = "http://127.0.0.1:8080"

	_, err := loadConfig(func(name string) string { return environment[name] })
	if err == nil || !strings.Contains(err.Error(), "HTTPS") {
		t.Fatalf("loadConfig error = %v, want HTTPS error", err)
	}
}

func TestLoadConfigCanExplicitlyAllowInteractiveCapacityForNormalWork(t *testing.T) {
	t.Parallel()
	environment := validConfigEnvironment()
	environment["CLASHLENS_ALLOW_INTERACTIVE_FOR_NORMAL"] = "true"

	config, err := loadConfig(func(name string) string { return environment[name] })
	if err != nil {
		t.Fatalf("loadConfig returned an error: %v", err)
	}
	if !config.allowInteractiveForNormal {
		t.Fatal("allowInteractiveForNormal = false, want true")
	}
}

func TestLoadConfigRejectsPerKeyRateAboveOfficialLimit(t *testing.T) {
	t.Parallel()
	environment := validConfigEnvironment()
	environment["CLASHLENS_REQUESTS_PER_SECOND_PER_KEY"] = "31"

	_, err := loadConfig(func(name string) string { return environment[name] })
	if err == nil || !strings.Contains(err.Error(), "between 1 and 30") {
		t.Fatalf("loadConfig error = %v, want per-key rate limit error", err)
	}
}

func validConfigEnvironment() map[string]string {
	return map[string]string{
		"CLASHLENS_DATABASE_URL":         "postgres://collector@127.0.0.1/collector",
		"CLASHLENS_ARCHIVE_ENDPOINT":     "127.0.0.1:9000",
		"CLASHLENS_ARCHIVE_BUCKET":       "raw",
		"CLASHLENS_ARCHIVE_ACCESS_KEY":   "archive-access",
		"CLASHLENS_ARCHIVE_SECRET_KEY":   "archive-secret",
		"CLASHLENS_NORMAL_API_KEYS":      "normal-1=one,normal-2=two,normal-3=three,normal-4=four",
		"CLASHLENS_INTERACTIVE_API_KEYS": "interactive-1=five",
		"CLASHLENS_OFFICIAL_API_ORIGIN":  "https://api.clashofclans.com",
	}
}
