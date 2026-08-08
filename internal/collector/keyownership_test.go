package collector

import (
	"context"
	"testing"
	"time"
)

func TestAPIKeyOwnershipAllowsOnlyOneProcessPerSecret(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	firstStore, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("open first store: %v", err)
	}
	t.Cleanup(firstStore.close)
	secondStore, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("open second store: %v", err)
	}
	t.Cleanup(secondStore.close)
	keys := []APIKey{{Label: "normal-a", Secret: "same-secret", Pool: normalPool}}

	releaseFirst, err := firstStore.acquireAPIKeyOwnership(ctx, keys)
	if err != nil {
		t.Fatalf("first ownership acquisition returned an error: %v", err)
	}
	if _, err := secondStore.acquireAPIKeyOwnership(ctx, keys); err == nil {
		t.Fatal("second process acquired the same API key ownership")
	}

	releaseFirst()
	releaseSecond, err := secondStore.acquireAPIKeyOwnership(ctx, keys)
	if err != nil {
		t.Fatalf("ownership was not available after release: %v", err)
	}
	releaseSecond()
}
