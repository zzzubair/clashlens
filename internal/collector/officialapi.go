package collector

import (
	"bytes"
	"context"
	"encoding/json"
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
	profileEndpoint              endpointName = "profile"
	battleLogEndpoint            endpointName = "battle_log"
	globalPlayerRankingsEndpoint endpointName = "global_player_rankings"
)

type requestProvenance struct {
	method               string
	path                 string
	query                string
	sourceAdapterVersion string
}

type officialAPIConfig struct {
	origin                string
	proxyURL              string
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
	requestStartedAt        time.Time
	responseCompletedAt     time.Time
	statusCode              int
	body                    []byte
	headers                 map[string]string
	request                 requestProvenance
	pagingEnvelopeState     string
	pendingArchiveReference string // fenced retry state, never an API field
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
	var proxy func(*http.Request) (*url.URL, error)
	if config.proxyURL != "" {
		parsedProxy, parseError := url.Parse(config.proxyURL)
		if parseError != nil || parsedProxy.Host == "" || (parsedProxy.Scheme != "http" && parsedProxy.Scheme != "https") ||
			parsedProxy.User != nil || parsedProxy.Path != "" || parsedProxy.RawQuery != "" || parsedProxy.Fragment != "" {
			return nil, errors.New("official API proxy must be an HTTP or HTTPS origin without credentials, path, query, or fragment")
		}
		proxy = http.ProxyURL(parsedProxy)
	}

	transport := &http.Transport{
		Proxy: proxy,
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
	provenance, requestPath, err := officialRequest(endpoint, normalizedTag)
	if err != nil {
		return officialResponse{}, err
	}

	requestURL := c.origin.ResolveReference(&url.URL{Path: requestPath, RawQuery: provenance.query})
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, requestURL.String(), nil)
	if err != nil {
		return officialResponse{}, fmt.Errorf("create official API request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+secret)
	request.Header.Set("Accept", "application/json")

	startedAt := time.Now().UTC()
	response, err := c.client.Do(request)
	if err != nil {
		return officialResponse{requestStartedAt: startedAt, request: provenance}, fmt.Errorf("official API transport: %w", err)
	}
	defer response.Body.Close()

	body, err := io.ReadAll(io.LimitReader(response.Body, c.maximumResponseBytes+1))
	completedAt := time.Now().UTC()
	if err != nil {
		return officialResponse{requestStartedAt: startedAt, request: provenance}, fmt.Errorf("read complete official API response: %w", err)
	}
	if int64(len(body)) > c.maximumResponseBytes {
		return officialResponse{requestStartedAt: startedAt, request: provenance}, errors.New("official API response exceeds configured maximum size")
	}

	result := officialResponse{
		requestStartedAt:    startedAt,
		responseCompletedAt: completedAt,
		statusCode:          response.StatusCode,
		body:                body,
		headers:             evidenceHeaders(response.Header),
		request:             provenance,
		pagingEnvelopeState: "not_applicable",
	}
	if endpoint == globalPlayerRankingsEndpoint {
		result.pagingEnvelopeState = inspectPagingEnvelope(body)
	}
	return result, nil
}

func officialRequest(endpoint endpointName, normalizedTag string) (requestProvenance, string, error) {
	switch endpoint {
	case globalPlayerRankingsEndpoint:
		return requestProvenance{
			method:               http.MethodGet,
			path:                 "/v1/locations/global/rankings/players",
			query:                "limit=200",
			sourceAdapterVersion: "global-player-rankings-v1",
		}, "/v1/locations/global/rankings/players", nil
	case profileEndpoint, battleLogEndpoint:
		path, err := officialPlayerPath(normalizedTag)
		if err != nil {
			return requestProvenance{}, "", err
		}
		adapter := "player-profile-v1"
		if endpoint == battleLogEndpoint {
			path += "/battlelog"
			adapter = "battle-log-v1"
		}
		return requestProvenance{
			method:               http.MethodGet,
			path:                 strings.Replace(path, "#", "%23", 1),
			query:                "",
			sourceAdapterVersion: adapter,
		}, path, nil
	default:
		return requestProvenance{}, "", fmt.Errorf("unknown official API endpoint %q", endpoint)
	}
}

func inspectPagingEnvelope(body []byte) string {
	var envelope struct {
		Paging json.RawMessage `json:"paging"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil {
		return "malformed"
	}
	paging := bytes.TrimSpace(envelope.Paging)
	if len(paging) == 0 || bytes.Equal(paging, []byte("null")) || bytes.Equal(paging, []byte("{}")) {
		return "not_present"
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(paging, &fields); err != nil {
		return "malformed"
	}
	if len(fields) == 0 {
		return "not_present"
	}
	return "cursor_present"
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
