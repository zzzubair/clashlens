package collector

import (
	"os"
	"path/filepath"
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

func TestLoadConfigRejectsOfficialAPIProxyWithCredentialsOrPath(t *testing.T) {
	t.Parallel()
	for _, proxyURL := range []string{
		"http://user:password@100.108.3.103:3128",
		"http://100.108.3.103:3128/proxy",
	} {
		t.Run(proxyURL, func(t *testing.T) {
			environment := validConfigEnvironment()
			environment["CLASHLENS_OFFICIAL_API_PROXY_URL"] = proxyURL

			_, err := loadConfig(func(name string) string { return environment[name] })
			if err == nil || !strings.Contains(err.Error(), "CLASHLENS_OFFICIAL_API_PROXY_URL") {
				t.Fatalf("loadConfig error = %v, want safe proxy URL error", err)
			}
		})
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

func TestLoadConfigReadsDatabaseAndArchiveCredentialsFromFiles(t *testing.T) {
	t.Parallel()
	environment := validConfigEnvironment()
	directory := t.TempDir()
	files := map[string]string{
		"CLASHLENS_DATABASE_URL":       "postgres://file-user@postgres/collector",
		"CLASHLENS_ARCHIVE_ACCESS_KEY": "file-access",
		"CLASHLENS_ARCHIVE_SECRET_KEY": "file-secret",
	}
	for name, value := range files {
		path := filepath.Join(directory, name)
		if err := os.WriteFile(path, []byte(value+"\n"), 0o600); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
		delete(environment, name)
		environment[name+"_FILE"] = path
	}

	config, err := loadConfig(func(name string) string { return environment[name] })
	if err != nil {
		t.Fatalf("loadConfig returned an error: %v", err)
	}
	if config.databaseURL != files["CLASHLENS_DATABASE_URL"] ||
		config.archiveAccessKey != files["CLASHLENS_ARCHIVE_ACCESS_KEY"] ||
		config.archiveSecretKey != files["CLASHLENS_ARCHIVE_SECRET_KEY"] {
		t.Fatal("loadConfig did not read credential files")
	}
}

func TestLoadConfigRejectsDirectAndFileCredentialTogether(t *testing.T) {
	t.Parallel()
	environment := validConfigEnvironment()
	environment["CLASHLENS_DATABASE_URL_FILE"] = filepath.Join(t.TempDir(), "database-url")

	_, err := loadConfig(func(name string) string { return environment[name] })
	if err == nil || !strings.Contains(err.Error(), "must not both be set") {
		t.Fatalf("loadConfig error = %v, want direct-and-file conflict", err)
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
