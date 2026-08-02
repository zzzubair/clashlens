package collector

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestOfficialAPIClientDoesNotUseTransparentCompression(t *testing.T) {
	t.Parallel()
	expectedBody := []byte{0x00, 0x01, 0x02, '\n', 0xff}
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if encoding := request.Header.Get("Accept-Encoding"); encoding != "" {
			t.Errorf("Accept-Encoding = %q, want empty to preserve exact response bytes", encoding)
		}
		response.Header().Set("Content-Type", "application/octet-stream")
		response.WriteHeader(http.StatusOK)
		_, _ = response.Write(expectedBody)
	}))
	t.Cleanup(server.Close)

	client := newTestOfficialAPIClient(t, server.URL, 1024)
	result, err := client.fetch(context.Background(), profileEndpoint, "#2PP", "test-secret")
	if err != nil {
		t.Fatalf("fetch returned an error: %v", err)
	}
	if !bytes.Equal(result.body, expectedBody) {
		t.Fatalf("response body = %x, want exact bytes %x", result.body, expectedBody)
	}
}

func TestOfficialAPIClientEscapesPlayerTagExactlyOnceOnWire(t *testing.T) {
	t.Parallel()
	requestURI := make(chan string, 1)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requestURI <- request.RequestURI
		_, _ = response.Write([]byte(`{"tag":"#2PP"}`))
	}))
	t.Cleanup(server.Close)

	client := newTestOfficialAPIClient(t, server.URL, 1024)
	if _, err := client.fetch(context.Background(), profileEndpoint, "#2PP", "test-secret"); err != nil {
		t.Fatalf("fetch returned an error: %v", err)
	}
	if got := <-requestURI; got != "/v1/players/%232PP" {
		t.Fatalf("RequestURI = %q, want %q", got, "/v1/players/%232PP")
	}
}

func TestOfficialAPIClientRejectsCrossOriginRedirect(t *testing.T) {
	t.Parallel()
	targetRequests := 0
	target := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		targetRequests++
	}))
	t.Cleanup(target.Close)
	origin := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		response.Header().Set("Location", target.URL)
		response.WriteHeader(http.StatusFound)
	}))
	t.Cleanup(origin.Close)

	client := newTestOfficialAPIClient(t, origin.URL, 1024)
	_, err := client.fetch(context.Background(), profileEndpoint, "#2PP", "test-secret")
	if err == nil || !strings.Contains(err.Error(), "left the configured origin") {
		t.Fatalf("fetch error = %v, want cross-origin redirect rejection", err)
	}
	if targetRequests != 0 {
		t.Fatalf("redirect target received %d requests, want 0", targetRequests)
	}
}

func TestOfficialAPIClientRejectsOversizeAndTruncatedBodies(t *testing.T) {
	t.Parallel()
	t.Run("oversize", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
			_, _ = response.Write([]byte("123456789"))
		}))
		t.Cleanup(server.Close)
		client := newTestOfficialAPIClient(t, server.URL, 8)
		_, err := client.fetch(context.Background(), profileEndpoint, "#2PP", "test-secret")
		if err == nil || !strings.Contains(err.Error(), "maximum size") {
			t.Fatalf("oversize fetch error = %v, want maximum-size error", err)
		}
	})

	t.Run("truncated", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
			response.Header().Set("Content-Length", "10")
			_, _ = response.Write([]byte("123"))
		}))
		t.Cleanup(server.Close)
		client := newTestOfficialAPIClient(t, server.URL, 1024)
		_, err := client.fetch(context.Background(), profileEndpoint, "#2PP", "test-secret")
		if !errors.Is(err, io.ErrUnexpectedEOF) {
			t.Fatalf("truncated fetch error = %v, want io.ErrUnexpectedEOF", err)
		}
	})
}

func newTestOfficialAPIClient(t *testing.T, origin string, maximumResponseBytes int64) *officialAPIClient {
	t.Helper()
	client, err := newOfficialAPIClient(officialAPIConfig{
		origin:                origin,
		allowInsecureTestHTTP: true,
		connectionTimeout:     time.Second,
		responseHeaderTimeout: time.Second,
		totalTimeout:          time.Second,
		maximumResponseBytes:  maximumResponseBytes,
	})
	if err != nil {
		t.Fatalf("newOfficialAPIClient returned an error: %v", err)
	}
	return client
}
