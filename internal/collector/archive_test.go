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
		defer backend.mu.Unlock()
		if backend.unavailable {
			http.Error(response, "unavailable", http.StatusServiceUnavailable)
			return
		}

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
	reference, err := archive.store(context.Background(), hash, body)
	if err != nil {
		t.Fatalf("first duplicate store returned an error: %v", err)
	}
	for index := 1; index < duplicates; index++ {
		repeat, err := archive.store(context.Background(), hash, body)
		if err != nil {
			t.Fatalf("repeated store %d returned an error: %v", index, err)
		}
		if repeat != reference {
			t.Fatalf("repeated store reference = %q, want %q", repeat, reference)
		}
	}

	backend.mu.Lock()
	defer backend.mu.Unlock()
	wantSequence := []string{http.MethodHead, http.MethodPut}
	for index := 1; index < duplicates; index++ {
		wantSequence = append(wantSequence, http.MethodHead, http.MethodGet)
	}
	if got := backend.requests[objectKey]; !slices.Equal(got, wantSequence) {
		t.Fatalf("duplicate archive requests = %v, want %v", got, wantSequence)
	}
	if object := backend.objects[objectKey]; object == nil || object.writes != 1 {
		t.Fatalf("content-addressed object was conditionally written more than once")
	}
	totals := map[string]int{"count": duplicates, "head": 0, "get": 0, "put": 0}
	for _, method := range backend.requests[objectKey] {
		switch method {
		case http.MethodHead:
			totals["head"]++
		case http.MethodGet:
			totals["get"]++
		case http.MethodPut:
			totals["put"]++
		}
	}
	if totals["head"] != duplicates || totals["get"] != duplicates-1 || totals["put"] != 1 {
		t.Fatalf("duplicate archive operation totals = %v, want head=%d get=%d put=1", totals, duplicates, duplicates-1)
	}
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
