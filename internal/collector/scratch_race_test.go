package collector

import (
	"testing"
	"time"

	"github.com/zzzubair/ClashLens/internal/testsupport"
)

func TestScratchRaceMeasureStartup(t *testing.T) {
	start := time.Now()
	url := testsupport.StartPostgres(t)
	t.Logf("StartPostgres took %s, url=%s", time.Since(start), url)
}
