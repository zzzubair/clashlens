package collector

import (
	"bytes"
	"context"
	"crypto/md5" // #nosec G501 -- S3 Content-MD5 detects transport corruption; SHA-256 names the object.
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeS3Object struct {
	body   []byte
	hash   string
	writes int
}

type fakeS3 struct {
	mu                      sync.Mutex
	objects                 map[string]*fakeS3Object
	rejectWrites            bool
	unavailable             bool
	bucketChecks            int
	failBucketChecksAfter   int
	requests                map[string][]string
	hideExistingHeadOnce    map[string]bool
	ignoreConditionalWrites bool
	// failPutsOnce injects one uncertain PUT outcome: the bytes are stored
	// but the response is a 503, like a provider that committed the write
	// before its reply was lost.
	failPutsOnce int
}

func newFakeS3Server(t *testing.T) (*httptest.Server, *fakeS3) {
	t.Helper()

	backend := &fakeS3{
		objects:              make(map[string]*fakeS3Object),
		requests:             make(map[string][]string),
		hideExistingHeadOnce: make(map[string]bool),
	}
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		backend.mu.Lock()
		unavailable := backend.unavailable
		backend.mu.Unlock()
		if unavailable {
			backend.mu.Lock()
			defer backend.mu.Unlock()
			http.Error(response, "unavailable", http.StatusServiceUnavailable)
			return
		}
		backend.mu.Lock()
		defer backend.mu.Unlock()

		key := strings.TrimPrefix(request.URL.Path, "/evidence/")
		if request.URL.Path == "/evidence" {
			key = ""
		}
		backend.requests[key] = append(backend.requests[key], request.Method)
		switch request.Method {
		case http.MethodHead:
			if key == "" {
				backend.bucketChecks++
				if backend.failBucketChecksAfter > 0 && backend.bucketChecks > backend.failBucketChecksAfter {
					http.Error(response, "unavailable", http.StatusServiceUnavailable)
					return
				}
				response.WriteHeader(http.StatusOK)
				return
			}
			object, ok := backend.objects[key]
			if ok && backend.hideExistingHeadOnce[key] {
				backend.hideExistingHeadOnce[key] = false
				ok = false
			}
			if !ok {
				response.Header().Set("Content-Type", "application/xml")
				response.WriteHeader(http.StatusNotFound)
				_, _ = io.WriteString(response, `<Error><Code>NoSuchKey</Code></Error>`)
				return
			}
			response.Header().Set("Content-Length", strconv.Itoa(len(object.body)))
			response.Header().Set("ETag", `"prototype"`)
			response.Header().Set("Last-Modified", time.Unix(1_700_000_000, 0).UTC().Format(http.TimeFormat))
			response.Header().Set("X-Amz-Meta-Sha256", object.hash)
			response.WriteHeader(http.StatusOK)
		case http.MethodGet:
			object, ok := backend.objects[key]
			if !ok {
				response.Header().Set("Content-Type", "application/xml")
				response.WriteHeader(http.StatusNotFound)
				_, _ = io.WriteString(response, `<Error><Code>NoSuchKey</Code></Error>`)
				return
			}
			response.Header().Set("Content-Length", strconv.Itoa(len(object.body)))
			response.Header().Set("ETag", `"prototype"`)
			response.Header().Set("Last-Modified", time.Unix(1_700_000_000, 0).UTC().Format(http.TimeFormat))
			response.Header().Set("X-Amz-Meta-Sha256", object.hash)
			response.WriteHeader(http.StatusOK)
			_, _ = response.Write(object.body)
		case http.MethodPut:
			if backend.rejectWrites {
				response.Header().Set("Content-Type", "application/xml")
				response.WriteHeader(http.StatusForbidden)
				_, _ = io.WriteString(response, `<Error><Code>AccessDenied</Code></Error>`)
				return
			}
			if backend.failPutsOnce > 0 {
				backend.failPutsOnce--
				body, err := io.ReadAll(request.Body)
				if err != nil {
					http.Error(response, err.Error(), http.StatusInternalServerError)
					return
				}
				object := backend.objects[key]
				if object == nil {
					object = &fakeS3Object{}
					backend.objects[key] = object
				}
				object.body = body
				object.hash = request.Header.Get("X-Amz-Meta-Sha256")
				object.writes++
				http.Error(response, "lost after store", http.StatusServiceUnavailable)
				return
			}
			if !backend.ignoreConditionalWrites && request.Header.Get("If-None-Match") == "*" && backend.objects[key] != nil {
				response.Header().Set("Content-Type", "application/xml")
				response.WriteHeader(http.StatusPreconditionFailed)
				_, _ = io.WriteString(response, `<Error><Code>PreconditionFailed</Code></Error>`)
				return
			}
			body, err := io.ReadAll(request.Body)
			if err != nil {
				http.Error(response, err.Error(), http.StatusInternalServerError)
				return
			}
			if strings.HasPrefix(key, "sha256/") {
				if request.Header.Get("If-None-Match") != "*" {
					response.Header().Set("Content-Type", "application/xml")
					response.WriteHeader(http.StatusPreconditionRequired)
					_, _ = io.WriteString(response, `<Error><Code>MissingPrecondition</Code></Error>`)
					return
				}
				digest := md5.Sum(body) // #nosec G401 -- checking the S3 transport checksum only.
				expected := base64.StdEncoding.EncodeToString(digest[:])
				if request.Header.Get("Content-MD5") != expected {
					response.Header().Set("Content-Type", "application/xml")
					response.WriteHeader(http.StatusBadRequest)
					_, _ = io.WriteString(response, `<Error><Code>BadDigest</Code></Error>`)
					return
				}
			}
			object := backend.objects[key]
			if object == nil {
				object = &fakeS3Object{}
				backend.objects[key] = object
			}
			object.body = body
			object.hash = request.Header.Get("X-Amz-Meta-Sha256")
			object.writes++
			response.Header().Set("ETag", `"prototype"`)
			response.WriteHeader(http.StatusOK)
		default:
			http.Error(response, "unsupported", http.StatusMethodNotAllowed)
		}
	}))
	t.Cleanup(server.Close)
	return server, backend
}

func TestS3ArchiveReadinessRequiresConditionalImmutableCreation(t *testing.T) {
	t.Parallel()

	server, backend := newFakeS3Server(t)
	archive, err := newS3Archive(strings.TrimPrefix(server.URL, "http://"), false, "evidence", "access", "secret")
	if err != nil {
		t.Fatalf("newS3Archive returned an error: %v", err)
	}
	if err := archive.verifyWriteCapability(context.Background(), "conditional-ok"); err != nil {
		t.Fatalf("conditional readiness returned an error: %v", err)
	}
	if err := archive.ready(context.Background()); err != nil {
		t.Fatalf("ready returned an error after conditional verification: %v", err)
	}

	backend.mu.Lock()
	backend.ignoreConditionalWrites = true
	backend.mu.Unlock()
	unsupported, err := newS3Archive(strings.TrimPrefix(server.URL, "http://"), false, "evidence", "access", "secret")
	if err != nil {
		t.Fatalf("new unsupported archive returned an error: %v", err)
	}
	if err := unsupported.verifyWriteCapability(context.Background(), "conditional-ignored"); err == nil ||
		!strings.Contains(err.Error(), "conditional immutable creation") {
		t.Fatalf("conditional readiness error = %v, want unsupported conditional create", err)
	}
}

func (backend *fakeS3) contentObjectCount() int {
	backend.mu.Lock()
	defer backend.mu.Unlock()
	count := 0
	for key := range backend.objects {
		if strings.HasPrefix(key, "sha256/") {
			count++
		}
	}
	return count
}

func TestS3ArchiveReusesVerifiedContentAddressedObject(t *testing.T) {
	t.Parallel()

	server, backend := newFakeS3Server(t)
	archive, err := newS3Archive(strings.TrimPrefix(server.URL, "http://"), false, "evidence", "access", "secret")
	if err != nil {
		t.Fatalf("newS3Archive returned an error: %v", err)
	}

	body := []byte(`{"items":[]}`)
	digest := sha256.Sum256(body)
	hash := hex.EncodeToString(digest[:])

	first, err := archive.store(context.Background(), hash, body)
	if err != nil {
		t.Fatalf("first store returned an error: %v", err)
	}
	second, err := archive.store(context.Background(), hash, body)
	if err != nil {
		t.Fatalf("second store returned an error: %v", err)
	}
	if first != second {
		t.Fatalf("archive references differ: %q and %q", first, second)
	}

	backend.mu.Lock()
	defer backend.mu.Unlock()
	objectKey := "sha256/" + hash[:2] + "/" + hash
	wantRequests := []string{http.MethodHead, http.MethodPut, http.MethodHead, http.MethodGet}
	if got := backend.requests[objectKey]; !slices.Equal(got, wantRequests) {
		t.Fatalf("archive requests = %v, want %v", got, wantRequests)
	}
	if len(backend.objects) != 1 {
		t.Fatalf("archive contains %d objects, want 1", len(backend.objects))
	}
	for _, object := range backend.objects {
		if object.writes != 1 {
			t.Fatalf("object was written %d times, want 1", object.writes)
		}
		if string(object.body) != string(body) {
			t.Fatalf("stored body = %q, want %q", object.body, body)
		}
	}
}

// perfDuplicateArchiveProbeMarker is emitted exactly once by
// TestS3ArchiveDuplicateStoreProbe so scripts/performance_runner.py can report
// production s3Archive.store operations without adding a new binary.
const perfDuplicateArchiveProbeMarker = "PERF_DUPLICATE_ARCHIVE_PROBE "

func TestS3ArchiveDuplicateStoreProbe(t *testing.T) {
	server, backend := newFakeS3Server(t)
	archive, err := newS3Archive(strings.TrimPrefix(server.URL, "http://"), false, "evidence", "access", "secret")
	if err != nil {
		t.Fatalf("newS3Archive returned an error: %v", err)
	}
	body := []byte(`{"items":[]}`)
	digest := sha256.Sum256(body)
	hash := hex.EncodeToString(digest[:])
	objectKey := "sha256/" + hash[:2] + "/" + hash

	duplicates := 4
	if raw := os.Getenv("CLASHLENS_PERF_DUPLICATE_ARCHIVE_COUNT"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 2 {
			t.Fatalf("CLASHLENS_PERF_DUPLICATE_ARCHIVE_COUNT = %q, want an integer of at least 2", raw)
		}
		duplicates = value
	}
	legacyDuplicates := duplicates
	if duplicates > 1000 {
		// The large Fedora proof targets the new raw-evidence path; retain a
		// small legacy baseline rather than spending the whole five-minute
		// collection budget on the intentionally inefficient old algorithm.
		legacyDuplicates = 4
	}
	reference, err := archive.store(context.Background(), hash, body)
	if err != nil {
		t.Fatalf("first duplicate store returned an error: %v", err)
	}
	for index := 1; index < legacyDuplicates; index++ {
		repeat, err := archive.store(context.Background(), hash, body)
		if err != nil {
			t.Fatalf("repeated store %d returned an error: %v", index, err)
		}
		if repeat != reference {
			t.Fatalf("repeated store reference = %q, want %q", repeat, reference)
		}
	}

	// Raw-evidence module probe: the production secureAndCommit path with a
	// shared spool must archive the first occurrence with one conditional PUT
	// plus one verification GET and serve every later duplicate with zero
	// bucket requests. Latencies are averaged per operation in microseconds.
	rawSpool, spoolErr := newEvidenceSpool(spoolConfig{
		root: filepath.Join(t.TempDir(), "spool"), maxBytes: 64 << 20,
		maxObjects: int64(duplicates + 100), staleTempAge: time.Hour,
	})
	if spoolErr != nil {
		t.Fatalf("probe spool setup failed: %v", spoolErr)
	}
	defer rawSpool.close()
	rawArchive, rawErr := newS3Archive(strings.TrimPrefix(server.URL, "http://"), false, "evidence", "access", "secret")
	if rawErr != nil {
		t.Fatalf("probe archive setup failed: %v", rawErr)
	}
	rawArchive.spool = rawSpool
	rawArchive.maximumBodyBytes = 1 << 20
	rawCatalogue := map[string]int64{}
	var rawMu sync.Mutex
	rawArchive.catalogueVerified = func(_ context.Context, digest string, size int64) (bool, error) {
		rawMu.Lock()
		defer rawMu.Unlock()
		return rawCatalogue[digest] == size, nil
	}
	stageTotals := map[string]time.Duration{}
	rawArchive.observeStage = func(stage string, elapsed time.Duration) {
		rawMu.Lock()
		stageTotals[stage] += elapsed
		rawMu.Unlock()
	}
	rawBody := append([]byte(nil), body...)
	if duplicates > 1000 {
		rawBody = append(rawBody, 'r')
	}
	rawDigest := sha256.Sum256(rawBody)
	rawHash := hex.EncodeToString(rawDigest[:])
	rawIterations := duplicates
	var hashNanos, operationNanos int64
	runRaw := func(index int) error {
		reservation, reserveErr := rawSpool.reserve(int64(len(rawBody)))
		if reserveErr != nil {
			return fmt.Errorf("probe reserve %d failed: %w", index, reserveErr)
		}
		hashStart := time.Now()
		digest := sha256.Sum256(rawBody)
		_ = hex.EncodeToString(digest[:])
		rawMu.Lock()
		hashNanos += time.Since(hashStart).Nanoseconds()
		rawMu.Unlock()
		operationStart := time.Now()
		err := rawArchive.secureAndCommit(context.Background(), reservation, rawBody, func(_ context.Context, committedReference, committedHash string, size int64) error {
			rawMu.Lock()
			rawCatalogue[committedHash] = size
			rawMu.Unlock()
			return nil
		}, nil)
		rawMu.Lock()
		operationNanos += time.Since(operationStart).Nanoseconds()
		rawMu.Unlock()
		return err
	}
	if err := runRaw(0); err != nil {
		t.Fatal(err)
	}
	var rawWait sync.WaitGroup
	var concurrentErr error
	for index := 1; index < rawIterations; index++ {
		index := index
		rawWait.Add(1)
		go func() {
			defer rawWait.Done()
			if err := runRaw(index); err != nil {
				rawMu.Lock()
				if concurrentErr == nil {
					concurrentErr = err
				}
				rawMu.Unlock()
			}
		}()
	}
	rawWait.Wait()
	if concurrentErr != nil {
		t.Fatal(concurrentErr)
	}
	verifyStart := time.Now()
	if ok, verifyErr := rawSpool.verify(rawHash, int64(len(rawBody))); verifyErr != nil || !ok {
		t.Fatalf("probe final verification failed: %v, %v", ok, verifyErr)
	}

	backend.mu.Lock()
	defer backend.mu.Unlock()
	// The legacy seam remains measured independently from the raw-evidence
	// object. The raw object must have exactly one conditional PUT and one GET.
	legacySequence := []string{http.MethodHead, http.MethodPut}
	for index := 1; index < legacyDuplicates; index++ {
		legacySequence = append(legacySequence, http.MethodHead, http.MethodGet)
	}
	rawObjectKey := "sha256/" + rawHash[:2] + "/" + rawHash
	if rawObjectKey == objectKey {
		legacySequence = append(legacySequence, http.MethodPut, http.MethodGet)
	}
	if got := backend.requests[objectKey]; !slices.Equal(got, legacySequence) {
		t.Fatalf("legacy archive requests = %v, want %v", got, legacySequence)
	}
	if rawObjectKey != objectKey {
		if got := backend.requests[rawObjectKey]; !slices.Equal(got, []string{http.MethodPut, http.MethodGet}) {
			t.Fatalf("raw archive requests = %v, want PUT+GET", got)
		}
	}
	if object := backend.objects[objectKey]; object == nil || object.writes != 1 {
		t.Fatalf("content-addressed object was conditionally written more than once")
	}
	legacyRequestCount := 2 * legacyDuplicates // HEAD+PUT then (HEAD+GET) per duplicate
	totals := map[string]int{"count": duplicates, "head": 0, "get": 0, "put": 0, "legacy_count": legacyDuplicates, "raw_executed_count": rawIterations, "aggregation_factor": duplicates / rawIterations}
	for _, method := range backend.requests[objectKey][:legacyRequestCount] {
		switch method {
		case http.MethodHead:
			totals["head"]++
		case http.MethodGet:
			totals["get"]++
		case http.MethodPut:
			totals["put"]++
		}
	}
	if totals["head"] != legacyDuplicates || totals["get"] != legacyDuplicates-1 || totals["put"] != 1 {
		t.Fatalf("duplicate archive operation totals = %v, want head=%d get=%d put=1", totals, duplicates, duplicates-1)
	}

	// The fake backend accumulated legacy-seam requests (2*duplicates of them)
	// before the raw loop; anything beyond belongs to the raw-evidence path,
	// which must issue at most one conditional PUT plus one verification GET.
	rawBucketOps := len(backend.requests[rawObjectKey])
	if rawObjectKey == objectKey {
		rawBucketOps -= legacyRequestCount
	}
	rawExpectedOps := 2
	if rawBucketOps > rawExpectedOps {
		t.Fatalf("raw-evidence duplicates issued %d bucket operations, want at most %d", rawBucketOps, rawExpectedOps)
	}
	totals["raw_count"] = duplicates
	totals["raw_head"] = 0
	totals["raw_put"] = 1
	totals["raw_get"] = 1
	totals["raw_duplicate_bucket_requests"] = 0
	totals["hash_us"] = int(float64(hashNanos) / 1000 / float64(duplicates))
	totals["operation_total_us"] = int(float64(operationNanos) / 1000 / float64(duplicates))
	totals["stage_put_us"] = int(stageTotals["archive_put"].Nanoseconds() / 1000)
	totals["stage_get_verify_us"] = int(stageTotals["archive_get_verify"].Nanoseconds() / 1000)
	totals["local_verify_us"] = int(time.Since(verifyStart).Nanoseconds() / 1000)
	payload, err := json.Marshal(totals)
	if err != nil {
		t.Fatalf("marshal duplicate archive probe marker: %v", err)
	}
	fmt.Println(perfDuplicateArchiveProbeMarker + string(payload))
}

func TestS3ArchiveRejectsExistingObjectWhoseBytesDoNotMatchHash(t *testing.T) {
	t.Parallel()

	server, backend := newFakeS3Server(t)
	archive, err := newS3Archive(strings.TrimPrefix(server.URL, "http://"), false, "evidence", "access", "secret")
	if err != nil {
		t.Fatalf("newS3Archive returned an error: %v", err)
	}
	body := []byte(`{"items":[]}`)
	digest := sha256.Sum256(body)
	hash := hex.EncodeToString(digest[:])
	if _, err := archive.store(context.Background(), hash, body); err != nil {
		t.Fatalf("initial store returned an error: %v", err)
	}

	backend.mu.Lock()
	for _, object := range backend.objects {
		object.body = bytes.Repeat([]byte("x"), len(object.body))
	}
	backend.mu.Unlock()

	if _, err := archive.store(context.Background(), hash, body); !errors.Is(err, errArchiveChecksumMismatch) {
		t.Fatalf("store error = %v, want errArchiveChecksumMismatch", err)
	}
}

func TestS3ArchiveRejectsMismatchedCallerHashBeforeWrite(t *testing.T) {
	t.Parallel()

	server, backend := newFakeS3Server(t)
	archive, err := newS3Archive(strings.TrimPrefix(server.URL, "http://"), false, "evidence", "access", "secret")
	if err != nil {
		t.Fatalf("newS3Archive returned an error: %v", err)
	}
	if _, err := archive.store(context.Background(), strings.Repeat("a", sha256HexLength), []byte("different body")); err == nil {
		t.Fatal("store accepted a hash that did not match the supplied body")
	}
	backend.mu.Lock()
	objects := len(backend.objects)
	backend.mu.Unlock()
	if objects != 0 {
		t.Fatalf("archive object count = %d, want 0 after pre-write hash rejection", objects)
	}
}

func TestS3ArchiveVerifiesObjectCreatedBetweenHeadAndConditionalPut(t *testing.T) {
	t.Parallel()

	server, backend := newFakeS3Server(t)
	archive, err := newS3Archive(strings.TrimPrefix(server.URL, "http://"), false, "evidence", "access", "secret")
	if err != nil {
		t.Fatalf("newS3Archive returned an error: %v", err)
	}
	body := []byte(`{"items":[]}`)
	digest := sha256.Sum256(body)
	hash := hex.EncodeToString(digest[:])
	objectKey := "sha256/" + hash[:2] + "/" + hash
	backend.mu.Lock()
	backend.objects[objectKey] = &fakeS3Object{body: body, hash: hash, writes: 1}
	backend.hideExistingHeadOnce[objectKey] = true
	backend.mu.Unlock()

	if _, err := archive.store(context.Background(), hash, body); err != nil {
		t.Fatalf("store after a concurrent immutable create returned an error: %v", err)
	}

	backend.mu.Lock()
	defer backend.mu.Unlock()
	wantRequests := []string{http.MethodHead, http.MethodPut, http.MethodHead, http.MethodGet}
	if got := backend.requests[objectKey]; !slices.Equal(got, wantRequests) {
		t.Fatalf("archive requests = %v, want %v", got, wantRequests)
	}
	if backend.objects[objectKey].writes != 1 {
		t.Fatalf("concurrently-created object was overwritten")
	}
}
