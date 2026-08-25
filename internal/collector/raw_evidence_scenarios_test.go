package collector

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/minio/minio-go/v7"
	"github.com/zzzubair/clashlens/internal/testsupport"
)

// scenarioArchive wires a production s3Archive to a fake provider, an optional
// spool, and a catalogue stub backed by an explicit verified set.
type scenarioArchive struct {
	*s3Archive
	backend  *fakeS3
	server   *httptest.Server
	spool    *evidenceSpool
	verified map[string]bool
}

func newScenarioArchive(t *testing.T, spoolRoot string) *scenarioArchive {
	t.Helper()
	result := &scenarioArchive{verified: map[string]bool{}}
	result.server, result.backend = newFakeS3Server(t)
	archive, err := newS3Archive(strings.TrimPrefix(result.server.URL, "http://"), false, "evidence", "access", "secret")
	if err != nil {
		t.Fatalf("newS3Archive returned an error: %v", err)
	}
	archive.maximumBodyBytes = 1 << 20
	archive.catalogueVerified = func(_ context.Context, hash string, _ int64) (bool, error) {
		return result.verified[hash], nil
	}
	if spoolRoot != "" {
		archive.spool, err = newEvidenceSpool(spoolConfig{
			root: spoolRoot, maxBytes: 4 << 20, maxObjects: 100,
			freeSpaceFloor: 0, freeInodeFloor: 0, staleTempAge: time.Hour,
		})
		if err != nil {
			t.Fatalf("newEvidenceSpool returned an error: %v", err)
		}
		t.Cleanup(archive.spool.close)
		result.spool = archive.spool
	}
	result.s3Archive = archive
	t.Cleanup(result.server.Close)
	return result
}

func (s *scenarioArchive) commit(reference, hash string) error {
	if !strings.HasPrefix(reference, "s3://") {
		return errors.New("commit reference is not a bucket reference: " + reference)
	}
	s.verified[hash] = true
	return nil
}

func bodyAndHash(t *testing.T, body string) ([]byte, string) {
	t.Helper()
	digest := sha256.Sum256([]byte(body))
	return []byte(body), hex.EncodeToString(digest[:])
}

func TestRawEvidenceVerifiedDuplicateUsesZeroBucketRequests(t *testing.T) {
	scenario := newScenarioArchive(t, filepath.Join(t.TempDir(), "spool"))
	ctx := context.Background()
	body, hash := bodyAndHash(t, `{"items":[]}`)

	reservation, err := scenario.reserve(ctx)
	if err != nil {
		t.Fatal(err)
	}
	firstCommitted := false
	if err := scenario.secureAndCommit(ctx, reservation, body, func(_ context.Context, reference, committedHash string, _ int64) error {
		firstCommitted = true
		return scenario.commit(reference, committedHash)
	}, nil); err != nil {
		t.Fatal(err)
	}
	if !firstCommitted {
		t.Fatal("first new-hash store did not commit")
	}
	objectKey := "sha256/" + hash[:2] + "/" + hash
	scenario.backend.mu.Lock()
	newHashRequests := append([]string(nil), scenario.backend.requests[objectKey]...)
	scenario.backend.mu.Unlock()
	if len(newHashRequests) != 2 || newHashRequests[0] != "PUT" || newHashRequests[1] != "GET" {
		t.Fatalf("new hash requests = %v, want exactly one conditional PUT and one verification GET", newHashRequests)
	}

	// A remotely verified duplicate with safe local bytes must make zero S3
	// requests and must not rewrite or fsync identical local content.
	final := scenario.spool.finalPath(hash)
	infoBefore, statErr := os.Stat(final)
	if statErr != nil {
		t.Fatal(statErr)
	}
	for i := 0; i < 3; i++ {
		reservation, reserveErr := scenario.reserve(ctx)
		if reserveErr != nil {
			t.Fatal(reserveErr)
		}
		duplicated := false
		if err := scenario.secureAndCommit(ctx, reservation, body, func(_ context.Context, reference, committedHash string, _ int64) error {
			duplicated = true
			return scenario.commit(reference, committedHash)
		}, nil); err != nil {
			t.Fatal(err)
		}
		if !duplicated {
			t.Fatal("verified duplicate did not commit")
		}
	}
	scenario.backend.mu.Lock()
	totalRequests := len(scenario.backend.requests[objectKey])
	scenario.backend.mu.Unlock()
	if totalRequests != 2 {
		t.Fatalf("duplicate path issued %d total requests, want only the initial PUT+GET", totalRequests)
	}
	infoAfter, statErr := os.Stat(final)
	if statErr != nil {
		t.Fatal(statErr)
	}
	if !infoBefore.ModTime().Equal(infoAfter.ModTime()) {
		t.Fatal("verified duplicate rewrote or fsynced identical local content")
	}
	ledger, ledgerErr := scenario.spool.ledger()
	if ledgerErr != nil {
		t.Fatal(ledgerErr)
	}
	if ledger.FinalObjects != 1 || ledger.ReservedObjects != 0 || ledger.ReservedBytes != 0 {
		t.Fatalf("ledger after duplicates = %+v, want one final object and no live reservations", ledger)
	}
}

func TestRawEvidenceUncertainPutReconcilesByVerificationGet(t *testing.T) {
	scenario := newScenarioArchive(t, filepath.Join(t.TempDir(), "spool"))
	ctx := context.Background()
	body, hash := bodyAndHash(t, `{"uncertain":true}`)

	scenario.backend.mu.Lock()
	scenario.backend.failPutsOnce = 1
	scenario.backend.mu.Unlock()

	reservation, err := scenario.reserve(ctx)
	if err != nil {
		t.Fatal(err)
	}
	committed := false
	if err := scenario.secureAndCommit(ctx, reservation, body, func(_ context.Context, reference, committedHash string, _ int64) error {
		committed = true
		return scenario.commit(reference, committedHash)
	}, nil); err != nil {
		t.Fatalf("uncertain PUT did not reconcile by GET: %v", err)
	}
	if !committed {
		t.Fatal("reconciled uncertain PUT did not commit")
	}
	objectKey := "sha256/" + hash[:2] + "/" + hash
	scenario.backend.mu.Lock()
	requests := append([]string(nil), scenario.backend.requests[objectKey]...)
	writes := scenario.backend.objects[objectKey].writes
	scenario.backend.mu.Unlock()
	// The provider stored the bytes but lost its reply. minio may retry the
	// conditional PUT (the retained object then answers 412); either way the
	// module may only commit after a verification GET proves exact bytes,
	// never through HEAD and never through a second unconditional write.
	if countMethod(requests, "HEAD") != 0 {
		t.Fatalf("requests = %v, want no HEAD", requests)
	}
	if countMethod(requests, "PUT") < 1 || len(requests) < 2 || requests[len(requests)-1] != "GET" {
		t.Fatalf("requests = %v, want conditional PUT(s) resolved by a final verification GET", requests)
	}
	if writes > 2 || !bytes.Equal(scenario.backend.objects[objectKey].body, body) {
		t.Fatalf("provider object writes = %d body mismatch, want bounded conditional writes of exact bytes", writes)
	}
}

func TestRawEvidencePersistentProviderFailurePersistsPendingWithoutCommit(t *testing.T) {
	scenario := newScenarioArchive(t, filepath.Join(t.TempDir(), "spool"))
	ctx := context.Background()
	body, hash := bodyAndHash(t, `{"pending":true}`)
	scenario.backend.mu.Lock()
	scenario.backend.unavailable = true
	scenario.backend.mu.Unlock()

	var pendingHash, pendingReference string
	var pendingSize int64
	pendingCalled := false
	reservation, err := scenario.reserve(ctx)
	if err != nil {
		t.Fatal(err)
	}
	uncertainResponse := officialResponse{statusCode: 200}
	err = scenario.secureAndCommit(ctx, reservation, body, func(context.Context, string, string, int64) error {
		t.Error("commit ran without remote verification")
		return nil
	}, func(_ context.Context, pendingDigest, reference string, size int64, _ officialResponse, _ string) error {
		pendingCalled = true
		pendingHash, pendingReference, pendingSize = pendingDigest, reference, size
		return nil
	}, uncertainResponse)
	if err == nil || !errors.Is(err, errPendingRemoteVerification) {
		t.Fatalf("secureAndCommit error = %v, want errPendingRemoteVerification", err)
	}
	if !pendingCalled || pendingHash != hash || pendingSize != int64(len(body)) || !strings.Contains(pendingReference, hash) {
		t.Fatalf("pending handoff = called=%v hash=%q reference=%q size=%d", pendingCalled, pendingHash, pendingReference, pendingSize)
	}
}

func TestRawEvidenceTerminalProviderErrorsNeverPersistPending(t *testing.T) {
	for name, reject := range map[string]func(*fakeS3){
		"permission_denied": func(backend *fakeS3) { backend.rejectWrites = true },
	} {
		t.Run(name, func(t *testing.T) {
			scenario := newScenarioArchive(t, filepath.Join(t.TempDir(), "spool"))
			ctx := context.Background()
			body, _ := bodyAndHash(t, `{"terminal":"case"}`)
			scenario.backend.mu.Lock()
			reject(scenario.backend)
			scenario.backend.mu.Unlock()

			pendingCalled := false
			reservation, err := scenario.reserve(ctx)
			if err != nil {
				t.Fatal(err)
			}
			err = scenario.secureAndCommit(ctx, reservation, body, func(context.Context, string, string, int64) error {
				t.Error("commit ran after terminal provider rejection")
				return nil
			}, func(context.Context, string, string, int64, officialResponse, string) error {
				pendingCalled = true
				return nil
			}, officialResponse{statusCode: 200})
			if err == nil || !errors.Is(err, errArchiveTerminal) {
				t.Fatalf("secureAndCommit error = %v, want errArchiveTerminal", err)
			}
			if pendingCalled {
				t.Fatal("terminal provider rejection persisted pending-remote-verification state")
			}
		})
	}
}

func TestArchiveErrorIsTerminalClassifications(t *testing.T) {
	for _, tc := range []struct {
		status   int
		code     string
		terminal bool
	}{
		{http.StatusUnauthorized, "", true},
		{http.StatusForbidden, "", true},
		{http.StatusBadRequest, "", true},
		{http.StatusMethodNotAllowed, "", true},
		{http.StatusNotImplemented, "", true},
		{http.StatusOK, "AccessDenied", true},
		{http.StatusOK, "InvalidAccessKeyId", true},
		{http.StatusOK, "SignatureDoesNotMatch", true},
		{http.StatusOK, "NotImplemented", true},
		{http.StatusOK, "InvalidRegion", true},
		{http.StatusInternalServerError, "InternalError", false},
		{http.StatusServiceUnavailable, "ServiceUnavailable", false},
		{http.StatusRequestTimeout, "RequestTimeout", false},
	} {
		err := minio.ErrorResponse{StatusCode: tc.status, Code: tc.code}
		if got := archiveErrorIsTerminal(err); got != tc.terminal {
			t.Errorf("archiveErrorIsTerminal(%d %q) = %v, want %v", tc.status, tc.code, got, tc.terminal)
		}
	}
}

func TestRawEvidenceRepairsCorruptLocalBytesWithOneRecoveryGet(t *testing.T) {
	scenario := newScenarioArchive(t, filepath.Join(t.TempDir(), "spool"))
	ctx := context.Background()
	body, _ := bodyAndHash(t, `{"repair":"me"}`)
	remoteBody, remoteHash := bodyAndHash(t, `{"repair":"me"}`)

	// Seed the provider as previously verified evidence.
	reservation, err := scenario.reserve(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := scenario.secureAndCommit(ctx, reservation, remoteBody, func(_ context.Context, reference, committedHash string, _ int64) error {
		return scenario.commit(reference, committedHash)
	}, nil); err != nil {
		t.Fatal(err)
	}

	// Corrupt local bytes; recovery must be exactly one GET and one atomic repair.
	final := scenario.spool.finalPath(remoteHash)
	if err := os.WriteFile(final, []byte("corrupt"), 0600); err != nil {
		t.Fatal(err)
	}
	scenario.backend.mu.Lock()
	requestsBeforeRepair := len(scenario.backend.requests["sha256/"+remoteHash[:2]+"/"+remoteHash])
	scenario.backend.mu.Unlock()

	reservation, err = scenario.reserve(ctx)
	if err != nil {
		t.Fatal(err)
	}
	committed := false
	if err := scenario.secureAndCommit(ctx, reservation, remoteBody, func(_ context.Context, reference, committedHash string, _ int64) error {
		committed = true
		return scenario.commit(reference, committedHash)
	}, nil); err != nil {
		t.Fatal(err)
	}
	if !committed {
		t.Fatal("repair path did not commit")
	}
	repaired, readErr := os.ReadFile(final)
	if readErr != nil || !bytes.Equal(repaired, body) {
		t.Fatalf("repaired file = %q, %v", repaired, readErr)
	}
	scenario.backend.mu.Lock()
	allRequests := scenario.backend.requests["sha256/"+remoteHash[:2]+"/"+remoteHash]
	repairRequests := allRequests[requestsBeforeRepair:]
	scenario.backend.mu.Unlock()
	if countMethod(repairRequests, "GET") != 1 || countMethod(repairRequests, "PUT") != 0 || countMethod(repairRequests, "HEAD") != 0 {
		t.Fatalf("repair requests = %v, want exactly one recovery GET", repairRequests)
	}
}

func countMethod(requests []string, method string) int {
	count := 0
	for _, request := range requests {
		if request == method {
			count++
		}
	}
	return count
}

func TestSpoolCrashRecoveryReconcilesOperationRecordsDeadReservationsAndLedger(t *testing.T) {
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	body := []byte(`{"crash":"point"}`)
	reservation, err := spool.reserve(1024)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := spool.write(reservation, bytes.NewReader(body)); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(body)
	hash := hex.EncodeToString(digest[:])

	// Simulate death before promotion: an operation record plus its temporary
	// file remain on disk, and a dead writer left an unlocked reservation.
	tempPath := filepath.Join(root, "tmp", "abandoned.tmp")
	if err := os.WriteFile(tempPath, []byte("partial"), 0600); err != nil {
		t.Fatal(err)
	}
	operationFile, _, err := spool.beginOperation(hash, tempPath)
	if err != nil {
		t.Fatal(err)
	}
	deadReservationPath := filepath.Join(root, ".control", "reservations", "dead.json")
	if err := os.WriteFile(deadReservationPath, []byte(`{"limit":1024,"created_at":0}`), 0600); err != nil {
		t.Fatal(err)
	}

	// Simulate death between beginOperation and promotion: closing the
	// descriptor drops the flock while the record and temporary file remain.
	if err := operationFile.Close(); err != nil {
		t.Fatal(err)
	}

	// A dead reservation must produce retryable backpressure and trigger the
	// same reconciliation that startup uses, then admission resumes.
	if _, err := spool.reserve(1024); err == nil || !strings.Contains(err.Error(), "backpressure") {
		t.Fatalf("reserve with dead reservation = %v, want backpressure", err)
	}
	if _, statErr := os.Stat(deadReservationPath); !os.IsNotExist(statErr) {
		t.Fatal("reconciliation kept the dead reservation record")
	}
	if _, statErr := os.Stat(tempPath); !os.IsNotExist(statErr) {
		t.Fatal("reconciliation kept the abandoned operation temporary file")
	}
	recovered, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.close()
	ledger, err := recovered.ledger()
	if err != nil {
		t.Fatal(err)
	}
	expectedBytes := int64(len(body))
	if ledger.FinalBytes != expectedBytes || ledger.FinalObjects != 1 ||
		ledger.ReservedBytes != 0 || ledger.ReservedObjects != 0 ||
		ledger.AbandonedTempBytes != 0 || ledger.AbandonedTempObjects != 0 {
		t.Fatalf("rebuilt ledger = %+v, want exact final accounting for one object of %d bytes", ledger, expectedBytes)
	}
	if ok, verifyErr := recovered.verify(hash, expectedBytes); verifyErr != nil || !ok {
		t.Fatalf("verify after crash recovery = %v, %v", ok, verifyErr)
	}
}

func TestSpoolStaleTempSweepKeepsLiveWritersAndRemovesAgedTemps(t *testing.T) {
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	now := time.Now()

	liveReservation, err := spool.reserve(1024)
	if err != nil {
		t.Fatal(err)
	}
	agedPath := filepath.Join(root, "tmp", "aged.tmp")
	if err := os.WriteFile(agedPath, []byte("old"), 0600); err != nil {
		t.Fatal(err)
	}
	future := now.Add(2 * time.Hour)
	// While any live reservation is locked, the sweep must not run at all:
	// it cannot distinguish its own in-flight temporary files from debris.
	if err := spool.sweepStale(future); err != nil {
		t.Fatal(err)
	}
	if _, statErr := os.Stat(agedPath); statErr != nil {
		t.Fatal("stale sweep raced a live reservation")
	}
	if err := liveReservation.release(); err != nil {
		t.Fatal(err)
	}
	oldStamp := future.Add(-2 * time.Hour)
	if err := os.Chtimes(agedPath, oldStamp, oldStamp); err != nil {
		t.Fatal(err)
	}
	if err := spool.sweepStale(future); err != nil {
		t.Fatal(err)
	}
	if _, statErr := os.Stat(agedPath); !os.IsNotExist(statErr) {
		t.Fatal("aged abandoned temporary file survived the stale sweep")
	}
	ledger, err := spool.ledger()
	if err != nil {
		t.Fatal(err)
	}
	if ledger.AbandonedTempObjects != 0 || ledger.AbandonedTempBytes != 0 {
		t.Fatalf("ledger after sweep = %+v, want zero abandoned temporaries", ledger)
	}
}

func TestSpoolAdmissionBackpressureEnforcesCapacityInequality(t *testing.T) {
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 4096, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	first, err := spool.reserve(3000)
	if err != nil {
		t.Fatal(err)
	}
	defer first.release()
	// final(0) + temp(0) + reservations(3000) + requested(2048) > 4096.
	if _, err := spool.reserve(2048); err == nil || !strings.Contains(err.Error(), "backpressure") {
		t.Fatalf("over-capacity reserve = %v, want backpressure", err)
	}
	second, err := spool.reserve(1024)
	if err != nil {
		t.Fatalf("reserve inside remaining capacity failed: %v", err)
	}
	defer second.release()
}

func TestCleanupEligibilityAndOrphanSweepAgainstDatabase(t *testing.T) {
	ctx := context.Background()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close(ctx)
	for version := 1; version <= 8; version++ {
		applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", formatMigration(version)))
	}
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0009_raw_evidence.sql"))

	store, err := openStore(ctx, databaseURL, 3)
	if err != nil {
		t.Fatal(err)
	}
	defer store.close()
	if _, err := connection.Exec(ctx, `INSERT INTO archive_instances (instance_id, endpoint, region, bucket, marker_key, marker_hash, marker_payload_version) VALUES ('instance-cleanup', 'archive.example:443', 'region', 'bucket', 'marker', repeat('b', 64), 'v1')`); err != nil {
		t.Fatal(err)
	}
	var playerID int64
	if err := connection.QueryRow(ctx, `INSERT INTO players (normalized_tag, active, next_due_at) VALUES ('#2PP', true, clock_timestamp()) RETURNING id`).Scan(&playerID); err != nil {
		t.Fatal(err)
	}

	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()

	// Case A: catalogued evidence whose only processing job is terminal.
	terminalHash := publishScenarioEvidence(t, spool, "catalogued-terminal")
	jobA, attemptA := seedCompleteJobWithAttempt(t, ctx, store, playerID)
	if _, err := store.pool.Exec(ctx, `INSERT INTO archive_catalogue (response_hash, archive_reference, byte_size, archive_instance_id) VALUES ($1, $2, $3, 'instance-cleanup')`,
		terminalHash, "s3://bucket/"+terminalHash, scenarioBodySize("catalogued-terminal")); err != nil {
		t.Fatal(err)
	}
	if _, err := store.pool.Exec(ctx, `INSERT INTO collector_observations (occurrence_key, collection_job_id, attempt_id, player_id, normalized_tag, endpoint, request_started_at, response_completed_at, http_status, response_hash, archive_reference, archive_catalogue_hash, collector_version, key_label, evidence_headers) VALUES ('cleanup-terminal', $1, $2, $3, '#2PP', 'profile', clock_timestamp(), clock_timestamp(), 200, $4, $5, $4, 'test', 'key', '{}'::jsonb)`,
		jobA, attemptA, playerID, terminalHash, "s3://bucket/"+terminalHash); err != nil {
		t.Fatal(err)
	}
	if _, err := store.pool.Exec(ctx, `INSERT INTO python_processing_jobs (observation_id, status) VALUES ((SELECT id FROM collector_observations WHERE occurrence_key = 'cleanup-terminal'), 'complete')`); err != nil {
		t.Fatal(err)
	}

	// Case B: catalogued evidence still referenced by non-terminal processing work.
	activeHash := publishScenarioEvidence(t, spool, "catalogued-active")
	jobB, attemptB := seedCompleteJobWithAttempt(t, ctx, store, playerID)
	if _, err := store.pool.Exec(ctx, `INSERT INTO archive_catalogue (response_hash, archive_reference, byte_size, archive_instance_id) VALUES ($1, $2, $3, 'instance-cleanup')`,
		activeHash, "s3://bucket/"+activeHash, scenarioBodySize("catalogued-active")); err != nil {
		t.Fatal(err)
	}
	if _, err := store.pool.Exec(ctx, `INSERT INTO collector_observations (occurrence_key, collection_job_id, attempt_id, player_id, normalized_tag, endpoint, request_started_at, response_completed_at, http_status, response_hash, archive_reference, archive_catalogue_hash, collector_version, key_label, evidence_headers) VALUES ('cleanup-active', $1, $2, $3, '#2PP', 'profile', clock_timestamp(), clock_timestamp(), 200, $4, $5, $4, 'test', 'key', '{}'::jsonb)`,
		jobB, attemptB, playerID, activeHash, "s3://bucket/"+activeHash); err != nil {
		t.Fatal(err)
	}
	if _, err := store.pool.Exec(ctx, `INSERT INTO python_processing_jobs (observation_id, status) VALUES ((SELECT id FROM collector_observations WHERE occurrence_key = 'cleanup-active'), 'pending')`); err != nil {
		t.Fatal(err)
	}

	// Case C: uncatalogued unreferenced local final — crash debris.
	orphanHash := publishScenarioEvidence(t, spool, "aged-orphan")

	// Case D: uncatalogued final with a live pending_remote_verification reference.
	pendingHash := publishScenarioEvidence(t, spool, "pending-referenced")
	_, attemptD := seedCompleteJobWithAttempt(t, ctx, store, playerID)
	pendingReference := "s3://bucket/" + pendingHash
	if _, err := store.pool.Exec(ctx, `INSERT INTO collector_endpoint_results (attempt_id, endpoint, outcome, response_hash, archive_reference, http_status, request_count, pending_remote_verification) VALUES ($1::bigint, 'profile', 'pending_remote_verification', $2::text, $3::text, 200, 1, jsonb_build_object('response_hash', $2, 'archive_reference', $3, 'byte_size', to_jsonb($4::bigint), 'archive_instance_id', 'instance-cleanup', 'endpoint', 'profile', 'attempt_id', $1, 'request_count', 1, 'status_code', 200))`,
		attemptD, pendingHash, pendingReference, scenarioBodySize("pending-referenced")); err != nil {
		t.Fatal(err)
	}

	stamp := time.Now().Add(-48 * time.Hour)
	ageAllFinalFiles(t, spool, stamp)

	deleted, err := spool.cleanup(ctx, time.Now(), 24*time.Hour, 100, store.cleanupEligible)
	if err != nil {
		t.Fatal(err)
	}
	if deleted != 1 {
		t.Fatalf("cleanup deleted %d files, want exactly the terminal catalogued one", deleted)
	}
	assertFinalAbsent(t, spool, terminalHash)
	assertFinalPresent(t, spool, activeHash)
	assertFinalPresent(t, spool, orphanHash)
	assertFinalPresent(t, spool, pendingHash)

	orphanedCount, err := spool.orphanSweep(ctx, time.Now(), 24*time.Hour, 100, store.catalogueContains, store.pendingContains)
	if err != nil {
		t.Fatal(err)
	}
	if orphanedCount != 1 {
		t.Fatalf("orphan sweep removed %d files, want the single aged uncatalogued unreferenced file", orphanedCount)
	}
	assertFinalPresent(t, spool, activeHash)
	assertFinalPresent(t, spool, pendingHash)
	assertFinalAbsent(t, spool, orphanHash)
}

func seedCompleteJobWithAttempt(t *testing.T, ctx context.Context, store *store, playerID int64) (int64, int64) {
	t.Helper()
	var jobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (work_type, player_id, normalized_tag, capacity_pool, priority, due_at, coalescing_key, status)
		VALUES ('regular_poll', $1, '#2PP', 'normal', 100, clock_timestamp(), 'cleanup-' || ($1::bigint)::text || '-' || nextval('collector_jobs_id_seq'::regclass), 'complete')
		RETURNING id
	`, playerID).Scan(&jobID); err != nil {
		t.Fatalf("seed cleanup collector job: %v", err)
	}
	var attemptID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', clock_timestamp(), clock_timestamp())
		RETURNING id
	`, jobID).Scan(&attemptID); err != nil {
		t.Fatalf("seed cleanup attempt: %v", err)
	}
	return jobID, attemptID
}

func scenarioBodySize(marker string) int64 {
	return int64(len(marker + "-bytes!"))
}

func publishScenarioEvidence(t *testing.T, spool *evidenceSpool, marker string) string {
	t.Helper()
	body := []byte(marker + "-bytes!")
	reservation, err := spool.reserve(int64(len(body)))
	if err != nil {
		t.Fatal(err)
	}
	evidence, err := spool.write(reservation, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	return evidence.Hash
}

func ageAllFinalFiles(t *testing.T, spool *evidenceSpool, stamp time.Time) {
	t.Helper()
	prefixes, err := os.ReadDir(filepath.Join(spool.cfg.root, "sha256"))
	if err != nil {
		t.Fatal(err)
	}
	for _, prefix := range prefixes {
		files, listErr := os.ReadDir(filepath.Join(spool.cfg.root, "sha256", prefix.Name()))
		if listErr != nil {
			continue
		}
		for _, file := range files {
			path := filepath.Join(spool.cfg.root, "sha256", prefix.Name(), file.Name())
			if err := os.Chtimes(path, stamp, stamp); err != nil {
				t.Fatal(err)
			}
		}
	}
}

func assertFinalAbsent(t *testing.T, spool *evidenceSpool, hash string) {
	t.Helper()
	if _, statErr := os.Stat(spool.finalPath(hash)); !os.IsNotExist(statErr) {
		t.Fatalf("final file %s should have been deleted", hash)
	}
}

func assertFinalPresent(t *testing.T, spool *evidenceSpool, hash string) {
	t.Helper()
	if _, statErr := os.Stat(spool.finalPath(hash)); statErr != nil {
		t.Fatalf("final file %s should have been kept: %v", hash, statErr)
	}
}

func startContractV3Database(t *testing.T) string {
	t.Helper()
	ctx := context.Background()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close(ctx)
	for version := 1; version <= 9; version++ {
		path := filepath.Join("..", "..", "deploy", "migrations", formatMigration(version))
		if version == 9 {
			path = filepath.Join("..", "..", "deploy", "migrations", "0009_raw_evidence.sql")
		}
		applySQLFile(t, ctx, connection, path)
	}
	return databaseURL
}

func newRawEvidenceTestWorker(
	t *testing.T,
	ctx context.Context,
	databaseURL string,
	scenario *scenarioArchive,
	apiRequests *atomic.Int64,
) (*store, *worker) {
	t.Helper()
	store, err := openStore(ctx, databaseURL, 3)
	if err != nil {
		t.Fatalf("openStore v3 returned an error: %v", err)
	}
	t.Cleanup(store.close)
	const instanceID = "instance-worker-test"
	store.archiveInstanceID = instanceID
	scenario.catalogueVerified = func(catalogueCtx context.Context, hash string, size int64) (bool, error) {
		return store.verifiedCatalogue(catalogueCtx, hash, size)
	}
	if _, err := store.pool.Exec(ctx, `INSERT INTO archive_instances (instance_id, endpoint, region, bucket, marker_key, marker_hash, marker_payload_version) VALUES ($1, 'archive.test:443', 'us-east-1', 'evidence', 'marker', repeat('b', 64), 'v1')`, instanceID); err != nil {
		t.Fatalf("seed archive instance: %v", err)
	}
	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 100); err != nil {
		t.Fatalf("schedule due player: %v", err)
	}

	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		apiRequests.Add(1)
		response.Header().Set("Content-Type", "application/json")
		playerTag := strings.TrimSuffix(strings.TrimPrefix(request.URL.Path, "/v1/players/"), "/battlelog")
		if strings.HasSuffix(request.URL.Path, "/battlelog") {
			_, _ = fmt.Fprintf(response, `{"items":[],"tag":%q}`, playerTag)
			return
		}
		_, _ = fmt.Fprintf(response, `{"tag":%q}`, playerTag)
	}))
	t.Cleanup(api.Close)
	keys, err := newKeyPool([]APIKey{{Label: "normal-a", Secret: "secret-a", Pool: normalPool}}, 30, false)
	if err != nil {
		t.Fatalf("newKeyPool returned an error: %v", err)
	}
	official, err := newOfficialAPIClient(officialAPIConfig{
		origin:                api.URL,
		allowInsecureTestHTTP: true,
		connectionTimeout:     time.Second,
		responseHeaderTimeout: time.Second,
		totalTimeout:          3 * time.Second,
		maximumResponseBytes:  1 << 20,
	})
	if err != nil {
		t.Fatalf("newOfficialAPIClient returned an error: %v", err)
	}
	worker := newWorker(store, scenario.s3Archive, official, keys, workerConfig{
		owner:            "raw-evidence-worker",
		leaseDuration:    time.Minute,
		collectorVersion: "test",
		maximumRetries:   4,
		retryPolicy:      newRetryPolicy(0, 0, 0),
	})
	return store, worker
}

func TestWorkerResumesPendingRemoteVerificationAndSurvivesLocalLoss(t *testing.T) {
	databaseURL := startContractV3Database(t)
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Second)
	defer cancel()

	scenario := newScenarioArchive(t, filepath.Join(t.TempDir(), "spool"))
	var apiRequests atomic.Int64
	store, worker := newRawEvidenceTestWorker(t, ctx, databaseURL, scenario, &apiRequests)

	resetDueJobs := func() {
		if _, err := store.pool.Exec(ctx, `
			UPDATE collector_jobs
			SET due_at = clock_timestamp() - interval '1 second'
			WHERE status IN ('pending', 'waiting_dependency', 'waiting_retry')
		`); err != nil {
			t.Fatal(err)
		}
	}
	countRows := func(query string) int {
		t.Helper()
		var count int
		if err := store.pool.QueryRow(ctx, query).Scan(&count); err != nil {
			t.Fatal(err)
		}
		return count
	}

	// Phase 1: provider outage after a fresh source request persists fenced
	// pending-remote-verification state for both endpoints without any commit.
	scenario.backend.mu.Lock()
	scenario.backend.unavailable = true
	scenario.backend.mu.Unlock()
	claimed, err := worker.runOnce(ctx, normalPool)
	if !claimed || err == nil {
		t.Fatalf("outage run = claimed=%v err=%v, want claimed work with pending handoff error", claimed, err)
	}
	if observations := countRows(`SELECT count(*) FROM collector_observations`); observations != 0 {
		t.Fatalf("observations after outage run = %d, want 0", observations)
	}
	pendingAfterOutage := countRows(`
		SELECT count(*) FROM collector_endpoint_results WHERE outcome = 'pending_remote_verification'
	`)
	if pendingAfterOutage != 2 {
		t.Fatalf("pending endpoint results after outage run = %d, want both endpoints", pendingAfterOutage)
	}
	var payloadSample []byte
	if err := store.pool.QueryRow(ctx, `
		SELECT pending_remote_verification FROM collector_endpoint_results
		WHERE outcome = 'pending_remote_verification' LIMIT 1
	`).Scan(&payloadSample); err != nil {
		t.Fatal(err)
	}
	payloadText := string(payloadSample)
	for _, forbidden := range []string{"body", "authorization", "secret"} {
		if strings.Contains(payloadText, forbidden) {
			t.Fatalf("pending payload contains %q: %s", forbidden, payloadText)
		}
	}
	sourceRequestsAfterPhaseOne := apiRequests.Load()
	if sourceRequestsAfterPhaseOne != 2 {
		t.Fatalf("official requests after phase one = %d, want exactly profile+battlelog", sourceRequestsAfterPhaseOne)
	}

	// Phase 2: provider recovery resumes the exact spooled bytes with zero
	// additional official API requests and commits the full verified handoff.
	scenario.backend.mu.Lock()
	scenario.backend.unavailable = false
	scenario.backend.mu.Unlock()
	resetDueJobs()
	claimed, err = worker.runOnce(ctx, normalPool)
	if !claimed || err != nil {
		var dbgObs, dbgPending int
		var dbgJob string
		_ = store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&dbgObs)
		_ = store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_endpoint_results WHERE outcome = 'pending_remote_verification'`).Scan(&dbgPending)
		_ = store.pool.QueryRow(ctx, `SELECT string_agg(DISTINCT outcome || ':' || COALESCE(execution_token,'-'), ', ') FROM collector_endpoint_results`).Scan(&dbgJob)
		t.Fatalf("recovery resume run = claimed=%v err=%v obs=%d pending=%d endpoints[%s]", claimed, err, dbgObs, dbgPending, dbgJob)
	}
	if resumed := apiRequests.Load(); resumed != sourceRequestsAfterPhaseOne {
		t.Fatalf("official requests after resume = %d, want unchanged %d (no new source request)", resumed, sourceRequestsAfterPhaseOne)
	}
	if observations := countRows(`SELECT count(*) FROM collector_observations`); observations != 2 {
		t.Fatalf("observations after resume = %d, want 2", observations)
	}
	if processingJobs := countRows(`SELECT count(*) FROM python_processing_jobs`); processingJobs != 2 {
		t.Fatalf("python processing jobs after resume = %d, want 2", processingJobs)
	}
	if catalogue := countRows(`SELECT count(*) FROM archive_catalogue`); catalogue != 2 {
		t.Fatalf("catalogue rows after resume = %d, want 2", catalogue)
	}
	if pending := countRows(`SELECT count(*) FROM collector_endpoint_results WHERE outcome = 'pending_remote_verification'`); pending != 0 {
		t.Fatalf("cleared pending rows after resume = %d outstanding, want 0", pending)
	}

	// Phase 3: Fedora loses the pending bytes of a later response. The next
	// claim clears only the operational pointer under its lease fence, makes
	// one fresh official request, and never invents catalogue evidence.
	scenario.backend.mu.Lock()
	scenario.backend.unavailable = true
	scenario.backend.mu.Unlock()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PD', true, clock_timestamp() - interval '1 hour')
	`); err != nil {
		t.Fatal(err)
	}
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 100); err != nil {
		t.Fatal(err)
	}
	claimed, err = worker.runOnce(ctx, normalPool)
	if !claimed || err == nil {
		t.Fatalf("second outage run = claimed=%v err=%v, want pending handoff again", claimed, err)
	}
	beforeLoss := apiRequests.Load()
	// Remove every locally spooled final file: total Fedora loss before
	// remote verification is explicitly permitted by #31.
	finalRoot := filepath.Join(scenario.spool.cfg.root, "sha256")
	if err := os.RemoveAll(finalRoot); err != nil {
		t.Fatal(err)
	}
	scenario.backend.mu.Lock()
	scenario.backend.unavailable = false
	scenario.backend.mu.Unlock()
	resetDueJobs()
	claimed, err = worker.runOnce(ctx, normalPool)
	if !claimed || err != nil {
		entries, _ := os.ReadDir(filepath.Join(scenario.spool.cfg.root, ".control", "reservations"))
		var details string
		for _, entry := range entries {
			details += entry.Name() + " "
		}
		ledger, ledgerErr := scenario.spool.ledger()
		if ledgerErr == nil {
			t.Fatalf("local-loss recovery run = claimed=%v err=%v ledger=%+v files=[%s]", claimed, err, ledger, details)
		}
		t.Fatalf("local-loss recovery run = claimed=%v err=%v", claimed, err)
	}
	if refetched := apiRequests.Load(); refetched != beforeLoss+2 {
		t.Fatalf("official requests after local loss = %d, want %d (one fresh request per endpoint)", refetched, beforeLoss+2)
	}
	if pending := countRows(`SELECT count(*) FROM collector_endpoint_results WHERE outcome = 'pending_remote_verification'`); pending != 0 {
		t.Fatalf("pending rows after local-loss recovery = %d outstanding, want all cleared", pending)
	}
	if observations := countRows(`SELECT count(*) FROM collector_observations`); observations != 4 {
		var debugEndpoints string
		if err := store.pool.QueryRow(ctx, `
			SELECT string_agg(endpoint || ':' || outcome || ':req' || request_count || ':hash' || COALESCE(response_hash,'-') || ':obs' || COALESCE(observation_id::text,'-'), ', ')
			FROM collector_endpoint_results
		`).Scan(&debugEndpoints); err != nil {
			debugEndpoints = err.Error()
		}
		t.Fatalf("total observations after local-loss recovery = %d, want 4 endpoints[%s]", observations, debugEndpoints)
	}
	if catalogue := countRows(`SELECT count(*) FROM archive_catalogue`); catalogue != 4 {
		t.Fatalf("total catalogue rows = %d, want 4", catalogue)
	}
}

func TestWorkerTerminalPendingResumeClearsPointerWithoutRequeue(t *testing.T) {
	databaseURL := startContractV3Database(t)
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Second)
	defer cancel()

	scenario := newScenarioArchive(t, filepath.Join(t.TempDir(), "spool"))
	var apiRequests atomic.Int64
	store, worker := newRawEvidenceTestWorker(t, ctx, databaseURL, scenario, &apiRequests)

	resetDueJobs := func() {
		if _, err := store.pool.Exec(ctx, `
			UPDATE collector_jobs
			SET due_at = clock_timestamp() - interval '1 second'
			WHERE status IN ('pending', 'waiting_dependency', 'waiting_retry')
		`); err != nil {
			t.Fatal(err)
		}
	}
	countRows := func(query string) int {
		t.Helper()
		var count int
		if err := store.pool.QueryRow(ctx, query).Scan(&count); err != nil {
			t.Fatal(err)
		}
		return count
	}

	// Phase 1: a provider outage persists fenced pending-remote-verification
	// state for both endpoints without any observation commit.
	scenario.backend.mu.Lock()
	scenario.backend.unavailable = true
	scenario.backend.mu.Unlock()
	claimed, err := worker.runOnce(ctx, normalPool)
	if !claimed || err == nil {
		t.Fatalf("outage run = claimed=%v err=%v, want claimed work with pending handoff error", claimed, err)
	}
	pendingHashes := func() []string {
		t.Helper()
		rows, err := store.pool.Query(ctx, `
			SELECT response_hash FROM collector_endpoint_results
			WHERE outcome = 'pending_remote_verification' AND response_hash IS NOT NULL
		`)
		if err != nil {
			t.Fatal(err)
		}
		defer rows.Close()
		var hashes []string
		for rows.Next() {
			var hash string
			if err := rows.Scan(&hash); err != nil {
				t.Fatal(err)
			}
			hashes = append(hashes, hash)
		}
		if err := rows.Err(); err != nil {
			t.Fatal(err)
		}
		return hashes
	}
	pending := pendingHashes()
	if len(pending) != 2 {
		t.Fatalf("pending endpoint results after outage = %d, want both endpoints", len(pending))
	}

	// Phase 2: the provider now holds contradicting bytes for every hash.
	// The resume PUT therefore conflicts (412) and its reconciliation GET
	// fails verification: a terminal checksum mismatch.
	scenario.backend.mu.Lock()
	for _, hash := range pending {
		key := "sha256/" + hash[:2] + "/" + hash
		scenario.backend.objects[key] = &fakeS3Object{body: []byte("contradicting"), hash: "wrong"}
	}
	scenario.backend.unavailable = false
	scenario.backend.mu.Unlock()

	// Exhaust the retry budget so attempt resolution is terminal instead of
	// scheduling another endpoint_retry job (no requeue loop).
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_endpoint_results SET retry_count = 4
		WHERE outcome = 'pending_remote_verification'
	`); err != nil {
		t.Fatal(err)
	}

	resetDueJobs()
	// The terminal contradiction is reported through durable state (a
	// lease-fenced storage failure), not through a worker error.
	claimed, err = worker.runOnce(ctx, normalPool)
	if !claimed {
		t.Fatalf("terminal resume run = claimed=%v err=%v, want claimed work", claimed, err)
	}

	// The v3 CHECK requires pending_remote_verification to be cleared by the
	// same lease-fenced statement that records the storage failure.
	if outstanding := countRows(`
		SELECT count(*) FROM collector_endpoint_results WHERE outcome = 'pending_remote_verification'
	`); outstanding != 0 {
		t.Fatalf("pending pointers survived terminal failure = %d, want 0", outstanding)
	}
	outcomes := map[string]string{}
	endpointRows, err := store.pool.Query(ctx, `
		SELECT endpoint, outcome, COALESCE(failure_category, ''), pending_remote_verification IS NULL
		FROM collector_endpoint_results
	`)
	if err != nil {
		t.Fatal(err)
	}
	for endpointRows.Next() {
		var endpoint, outcome, category string
		var pendingCleared bool
		if err := endpointRows.Scan(&endpoint, &outcome, &category, &pendingCleared); err != nil {
			t.Fatal(err)
		}
		outcomes[endpoint] = outcome + ":" + category + ":" + fmt.Sprint(pendingCleared)
	}
	endpointRows.Close()
	if len(outcomes) != 2 {
		t.Fatalf("endpoint results = %v, want two", outcomes)
	}
	for endpoint, state := range outcomes {
		if state != "failed:archive_checksum_mismatch:true" {
			var debugJobs, debugAttempts, debugRetry string
			_ = store.pool.QueryRow(ctx, `SELECT string_agg(work_type || ':' || status || ':ra' || COALESCE(result_attempt_id::text,'-') || ':pa' || COALESCE(parent_attempt_id::text,'-'), ', ') FROM collector_jobs`).Scan(&debugJobs)
			_ = store.pool.QueryRow(ctx, `SELECT string_agg(id || ':' || status || ':lease' || lease_generation, ', ') FROM collector_attempts`).Scan(&debugAttempts)
			_ = store.pool.QueryRow(ctx, `SELECT string_agg(endpoint || ':' || outcome || ':rc' || retry_count, ', ') FROM collector_endpoint_results`).Scan(&debugRetry)
			_ = store.pool.QueryRow(ctx, `SELECT 'jobgen=' || j.lease_generation || ' jtok=' || COALESCE(j.lease_token,'NULL') || ' aown=' || COALESCE(a.lease_owner,'NULL') || ' atok=' || COALESCE(a.lease_token,'NULL') || ' agen=' || a.lease_generation || ' astatus=' || a.status FROM collector_jobs j JOIN collector_attempts a ON a.job_id=j.id LIMIT 1`).Scan(&debugRetry)
			t.Fatalf("endpoint %s terminal state = %q; jobs[%s] attempts[%s] endpoints[%s]", endpoint, state, debugJobs, debugAttempts, debugRetry)
		}
	}
	if requeues := countRows(`
		SELECT count(*) FROM collector_jobs WHERE work_type = 'endpoint_retry'
	`); requeues != 0 {
		t.Fatalf("terminal contradiction scheduled %d retry jobs, want no requeue loop", requeues)
	}
	if jobStatus := countRows(`SELECT count(*) FROM collector_jobs WHERE status = 'failed'`); jobStatus < 1 {
		t.Fatalf("collector jobs failed = %d, want the root job terminal", jobStatus)
	}
	if observations := countRows(`SELECT count(*) FROM collector_observations`); observations != 0 {
		t.Fatalf("unverified observations committed = %d, want 0", observations)
	}

	// A subsequent claim finds nothing: the contradiction is terminal.
	resetDueJobs()
	claimed, err = worker.runOnce(ctx, normalPool)
	if claimed {
		t.Fatal("a terminal contradictory attempt was claimed again")
	}
	_ = err // an empty queue returns claimed=false with no error
}
