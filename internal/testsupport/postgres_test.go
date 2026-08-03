package testsupport

import (
	"context"
	"testing"

	"github.com/jackc/pgx/v5"
)

func TestStartPostgres(t *testing.T) {
	databaseURL := StartPostgres(t)
	connection, err := pgx.Connect(context.Background(), databaseURL)
	if err != nil {
		t.Fatalf("connect to embedded PostgreSQL: %v", err)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })

	var value int
	if err := connection.QueryRow(context.Background(), "SELECT 1").Scan(&value); err != nil {
		t.Fatalf("query embedded PostgreSQL: %v", err)
	}
	if value != 1 {
		t.Fatalf("SELECT 1 = %d", value)
	}
}
