// Tests for the LLM-gateway scrape (group R 27.5c). The HTTP layer is a func
// var (gatewayHTTPDo) so we point it at httptest servers without a Docker
// daemon. We assert on the raw heartbeat payload (the map shape) and on the
// StackObservation JSON, which is what crosses the wire to the manager.

package executor

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/docker/docker/api/types"
)

// workersJSON is a minimal, representative /workers body (mirrors
// CORE/API/tests/fixtures/llm_gateway/workers.json).
const workersJSON = `{"workers":[{"url":"http://vllm-0:8000","is_healthy":true,"load":0}],
	"total":1,"stats":{"regular_count":1}}`

const metricsText = "# HELP smg_worker_requests_active In-flight\nsmg_worker_requests_active{worker=\"http://vllm-0:8000\"} 0\n"

// stubHTTPDo redirects gatewayHTTPDo at the httptest server, restoring the real
// one on cleanup.
func stubHTTPDo(t *testing.T, srv *httptest.Server) {
	t.Helper()
	old := gatewayHTTPDo
	gatewayHTTPDo = func(ctx context.Context, url string) ([]byte, error) {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, &httpError{code: resp.StatusCode}
		}
		return io.ReadAll(resp.Body)
	}
	t.Cleanup(func() { gatewayHTTPDo = old })
}

type httpError struct{ code int }

func (e *httpError) Error() string { return "status " + strconv.Itoa(e.code) }

// candidateFor builds the candidate set for a test, as the production path does
// from a container IP (primary) and/or a published 8080 port (fallback).
func candidateFor(ip string, pubWorkers, pubMetrics uint16) []gatewayCandidate {
	var c types.Container
	if pubWorkers > 0 {
		c.Ports = []types.Port{{PrivatePort: gatewayWorkersContainerPort, PublicPort: pubWorkers}}
		if pubMetrics > 0 {
			c.Ports = append(c.Ports, types.Port{PrivatePort: gatewayMetricsContainerPort, PublicPort: pubMetrics})
		}
	}
	return gatewayCandidates(c, ip)
}

func port(srv *httptest.Server) uint16 {
	u := srv.URL
	idx := strings.LastIndex(u, ":")
	n, _ := strconv.Atoi(u[idx+1:])
	return uint16(n)
}

func TestScrapeGateway_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/workers":
			w.Write([]byte(workersJSON))
		case "/metrics":
			w.Write([]byte(metricsText))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()
	stubHTTPDo(t, srv)

	cands := candidateFor("127.0.0.1", port(srv), port(srv))
	gw := scrapeGatewayEndpoints(context.Background(), cands)
	if gw == nil {
		t.Fatal("expected a gateway payload, got nil")
	}
	// workers must be the parsed JSON object, not a string.
	wk, ok := gw["workers"].(map[string]interface{})
	if !ok {
		t.Fatalf("workers should be parsed JSON object, got %T", gw["workers"])
	}
	if _, has := wk["workers"]; !has {
		t.Errorf("parsed workers object missing 'workers' list: %v", wk)
	}
	m, isStr := gw["metrics"].(string)
	if !isStr || !strings.Contains(m, "smg_worker_requests_active") {
		t.Errorf("metrics should carry the prom text, got %v", gw["metrics"])
	}
}

func TestScrapeGateway_MetricsDown(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/workers" {
			w.Write([]byte(workersJSON))
			return
		}
		w.WriteHeader(http.StatusServiceUnavailable) // metrics down
	}))
	defer srv.Close()
	stubHTTPDo(t, srv)

	// metrics port present but answering 503 -> workers only.
	cands := candidateFor("127.0.0.1", port(srv), port(srv))
	gw := scrapeGatewayEndpoints(context.Background(), cands)
	if gw == nil {
		t.Fatal("expected workers payload, got nil")
	}
	if _, has := gw["workers"]; !has {
		t.Errorf("workers missing: %v", gw)
	}
	if _, has := gw["metrics"]; has {
		t.Errorf("metrics should be omitted when down, got %v", gw["metrics"])
	}
}

func TestScrapeGateway_BothDown(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer srv.Close()
	stubHTTPDo(t, srv)

	cands := candidateFor("127.0.0.1", port(srv), port(srv))
	gw := scrapeGatewayEndpoints(context.Background(), cands)
	if gw != nil {
		t.Fatalf("expected nil (no gateway key) when both down, got %v", gw)
	}
}

func TestScrapeGateway_SlowServerRespectsTimeout(t *testing.T) {
	// A handler that never answers within the budget. The context deadline
	// (short here) must cut the call off, yielding no gateway payload.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
			// connection torn down by the client — nothing to write.
		case <-time.After(5 * time.Second):
			w.Write([]byte(workersJSON))
		}
	}))
	defer srv.Close()
	stubHTTPDo(t, srv)

	cands := candidateFor("127.0.0.1", port(srv), port(srv))
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	start := time.Now()
	gw := scrapeGatewayEndpoints(ctx, cands)
	elapsed := time.Since(start)
	if gw != nil {
		t.Fatalf("expected nil on timeout, got %v", gw)
	}
	if elapsed > 2*time.Second {
		t.Fatalf("scrape ran %s — the per-call/pass deadline was not honored", elapsed)
	}
}

func TestIsGatewayImage(t *testing.T) {
	cases := map[string]bool{
		"lmsysorg/sgl-model-gateway:v0.3.2":          true,
		"registry.example.com/sgl-model-gateway:x": true,
		"lmsysorg/sglang:v0.3.2":                     false,
		"vllm/vllm-openai:latest":                    false,
		"":                                           false,
	}
	for image, want := range cases {
		if got := isGatewayImage(image); got != want {
			t.Errorf("isGatewayImage(%q) = %v, want %v", image, got, want)
		}
	}
}

// A non-gateway container with no IP and no published gateway ports yields no
// scrape candidates — i.e. it is never scraped.
func TestGatewayCandidates_NonGateway_NoCandidate(t *testing.T) {
	c := types.Container{Ports: []types.Port{{PrivatePort: 8000, PublicPort: 39999}}}
	if cands := gatewayCandidates(c, ""); len(cands) != 0 {
		t.Fatalf("expected no candidates for a non-gateway container, got %v", cands)
	}
}

// The observation JSON must include `gateway` when set and omit it when nil
// (so a project that loses its gateway reads as "no scrape", not "empty").
func TestStackObservation_JSONGatewayKey(t *testing.T) {
	withGW := StackObservation{
		Project: "modstack5g1", Running: 3, Total: 4,
		Gateway: map[string]interface{}{"workers": map[string]interface{}{"total": 1}},
	}
	b, err := json.Marshal(withGW)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), `"gateway":`) {
		t.Errorf("gateway should be present in JSON: %s", b)
	}

	without := StackObservation{Project: "modstack5g1", Running: 3, Total: 4}
	b2, err := json.Marshal(without)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b2), `"gateway"`) {
		t.Errorf("gateway should be omitted when nil: %s", b2)
	}
}
