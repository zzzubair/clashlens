package testsupport

import (
	"io"
	"net"
	"path/filepath"
	"strings"
	"testing"

	embeddedpostgres "github.com/fergusstrange/embedded-postgres"
)

func StartPostgres(t testing.TB) string {
	t.Helper()

	port := unusedPort(t)
	root := t.TempDir()
	config := embeddedpostgres.DefaultConfig().
		Version(embeddedpostgres.V18).
		Port(uint32(port)).
		Database("clashlens").
		Username("postgres").
		Password("postgres").
		Locale("C").
		Logger(io.Discard).
		RuntimePath(filepath.Join(root, "runtime")).
		DataPath(filepath.Join(root, "data")).
		BinariesPath(filepath.Join(root, "binaries"))

	database := embeddedpostgres.NewDatabase(config)
	if err := database.Start(); err != nil {
		t.Fatalf("start embedded PostgreSQL: %v", err)
	}
	t.Cleanup(func() {
		if err := database.Stop(); err != nil {
			t.Errorf("stop embedded PostgreSQL: %v", err)
		}
	})
	return strings.Replace(config.GetConnectionURL(), "localhost", "127.0.0.1", 1)
}

func unusedPort(t testing.TB) int {
	t.Helper()

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("find unused port: %v", err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	if err := listener.Close(); err != nil {
		t.Fatalf("release unused port: %v", err)
	}
	return port
}
