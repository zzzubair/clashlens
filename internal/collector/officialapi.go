package collector

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type endpointName string

const (
	profileEndpoint   endpointName = "profile"
	battleLogEndpoint endpointName = "battle_log"
)

type officialAPIConfig struct {
	origin                string
	allowInsecureTestHTTP bool
	connectionTimeout     time.Duration
	responseHeaderTimeout time.Duration
	totalTimeout          time.Duration
	maximumResponseBytes  int64
}

type officialAPIClient struct {
	origin               *url.URL
	client               *http.Client
	maximumResponseBytes int64
}

type officialResponse struct {
	requestStartedAt    time.Time
	responseCompletedAt time.Time
	statusCode          int
	body                []byte
	headers             map[string]string
}

func newOfficialAPIClient(config officialAPIConfig) (*officialAPIClient, error) {
	origin, err := url.Parse(config.origin)
	if err != nil {
		return nil, fmt.Errorf("parse official API origin: %w", err)
	}
	if origin.Host == "" || origin.User != nil || origin.RawQuery != "" || origin.Fragment != "" {
		return nil, errors.New("official API origin must contain only a scheme and host")
	}
	if origin.Scheme != "https" && !(config.allowInsecureTestHTTP && origin.Scheme == "http") {
		return nil, errors.New("official API origin must use HTTPS")
	}
	if origin.Path != "" && origin.Path != "/" {
		return nil, errors.New("official API origin must not contain a path")
	}
	if config.connectionTimeout <= 0 || config.responseHeaderTimeout <= 0 || config.totalTimeout <= 0 {
		return nil, errors.New("official API timeouts must be positive")
	}
	if config.maximumResponseBytes < 1 {
		return nil, errors.New("official API maximum response size must be positive")
	}

	transport := &http.Transport{
		DialContext: (&net.Dialer{
			Timeout:   config.connectionTimeout,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		ForceAttemptHTTP2:     true,
		DisableCompression:    true,
		ResponseHeaderTimeout: config.responseHeaderTimeout,
		TLSHandshakeTimeout:   config.connectionTimeout,
		IdleConnTimeout:       90 * time.Second,
	}
	client := &http.Client{
		Transport: transport,
		Timeout:   config.totalTimeout,
	}
	client.CheckRedirect = func(request *http.Request, via []*http.Request) error {
		if len(via) >= 3 {
			return errors.New("official API redirect limit exceeded")
		}
		if request.URL.Scheme != origin.Scheme || !strings.EqualFold(request.URL.Host, origin.Host) {
			return errors.New("official API redirect left the configured origin")
		}
		return nil
	}

	return &officialAPIClient{
		origin:               origin,
		client:               client,
		maximumResponseBytes: config.maximumResponseBytes,
	}, nil
}

func (c *officialAPIClient) fetch(ctx context.Context, endpoint endpointName, normalizedTag, secret string) (officialResponse, error) {
	path, err := officialPlayerPath(normalizedTag)
	if err != nil {
		return officialResponse{}, err
	}
	switch endpoint {
	case profileEndpoint:
	case battleLogEndpoint:
		path += "/battlelog"
	default:
		return officialResponse{}, fmt.Errorf("unknown official API endpoint %q", endpoint)
	}

	requestURL := c.origin.ResolveReference(&url.URL{Path: path})
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, requestURL.String(), nil)
	if err != nil {
		return officialResponse{}, fmt.Errorf("create official API request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+secret)
	request.Header.Set("Accept", "application/json")

	startedAt := time.Now().UTC()
	response, err := c.client.Do(request)
	if err != nil {
		return officialResponse{requestStartedAt: startedAt}, fmt.Errorf("official API transport: %w", err)
	}
	defer response.Body.Close()

	body, err := io.ReadAll(io.LimitReader(response.Body, c.maximumResponseBytes+1))
	completedAt := time.Now().UTC()
	if err != nil {
		return officialResponse{requestStartedAt: startedAt}, fmt.Errorf("read complete official API response: %w", err)
	}
	if int64(len(body)) > c.maximumResponseBytes {
		return officialResponse{requestStartedAt: startedAt}, errors.New("official API response exceeds configured maximum size")
	}

	return officialResponse{
		requestStartedAt:    startedAt,
		responseCompletedAt: completedAt,
		statusCode:          response.StatusCode,
		body:                body,
		headers:             evidenceHeaders(response.Header),
	}, nil
}

func evidenceHeaders(headers http.Header) map[string]string {
	result := make(map[string]string)
	for _, name := range []string{"Cache-Control", "Content-Type", "Date", "ETag", "Last-Modified", "Retry-After"} {
		if value := headers.Get(name); value != "" {
			result[name] = value
		}
	}
	return result
}
