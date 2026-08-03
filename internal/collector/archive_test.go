package collector

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
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
	mu                    sync.Mutex
	objects               map[string]*fakeS3Object
	rejectWrites          bool
	unavailable           bool
	bucketChecks          int
	failBucketChecksAfter int
}

func newFakeS3Server(t *testing.T) (*httptest.Server, *fakeS3) {
	t.Helper()

	backend := &fakeS3{objects: make(map[string]*fakeS3Object)}
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		backend.mu.Lock()
		defer backend.mu.Unlock()
		if backend.unavailable {
			http.Error(response, "unavailable", http.StatusServiceUnavailable)
			return
		}

		key := strings.TrimPrefix(request.URL.Path, "/evidence/")
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
			response.Header().Set("ETag", `"prototype"`)
			response.WriteHeader(http.StatusOK)
		default:
			http.Error(response, "unsupported", http.StatusMethodNotAllowed)
		}
	}))
	t.Cleanup(server.Close)
	return server, backend
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
