package heartbeat

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/modelcache"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/monitor"
)

// newSendTestHeartbeat builds a minimal Heartbeat sufficient to exercise the
// full send() path (not just sendAPI's response decode) against srv.
func newSendTestHeartbeat(srv *httptest.Server) *Heartbeat {
	h := &Heartbeat{
		cfg:     &config.Config{APIURL: srv.URL, WorkerName: "test-worker"},
		curURL:  srv.URL,
		log:     discardLog(),
		sysColl: monitor.NewSystemCollector(),
	}
	h.status.Store(StatusIdle)
	return h
}

func captureHeartbeatBody(t *testing.T) (*httptest.Server, *map[string]interface{}) {
	t.Helper()
	got := map[string]interface{}{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{})
	}))
	return srv, &got
}

// TestSend_ModelCacheScoresPresentWhenMeasured verifies GF.C2's heartbeat
// promise (modelcache_manager.go:46): a registered provider with a
// non-empty measurement rides the beat as `model_cache_scores`.
func TestSend_ModelCacheScoresPresentWhenMeasured(t *testing.T) {
	srv, got := captureHeartbeatBody(t)
	defer srv.Close()

	h := newSendTestHeartbeat(srv)
	h.OnModelCacheScores(func() []modelcache.Score {
		return []modelcache.Score{{Class: "upstream", Healthy: true, LatencyMS: 12.5}}
	})

	h.send(context.Background())

	if _, ok := (*got)["model_cache_scores"]; !ok {
		t.Fatalf("expected model_cache_scores present in heartbeat payload, got keys %#v", *got)
	}
}

// TestSend_ModelCacheScoresAbsentWhenEmpty verifies an empty measurement
// (provider registered but nothing measured yet) is OMITTED, not sent as an
// empty/misleading list — see ModelCacheScoresProvider's doc comment.
func TestSend_ModelCacheScoresAbsentWhenEmpty(t *testing.T) {
	srv, got := captureHeartbeatBody(t)
	defer srv.Close()

	h := newSendTestHeartbeat(srv)
	h.OnModelCacheScores(func() []modelcache.Score { return nil })

	h.send(context.Background())

	if v, ok := (*got)["model_cache_scores"]; ok {
		t.Fatalf("expected model_cache_scores omitted when empty, got %#v", v)
	}
}

// TestSend_ModelCacheScoresAbsentWithNoProvider verifies a worker that never
// registers the provider (predates GF.C2, or the manager was never built)
// sends no model_cache_scores key at all.
func TestSend_ModelCacheScoresAbsentWithNoProvider(t *testing.T) {
	srv, got := captureHeartbeatBody(t)
	defer srv.Close()

	h := newSendTestHeartbeat(srv)

	h.send(context.Background())

	if v, ok := (*got)["model_cache_scores"]; ok {
		t.Fatalf("expected model_cache_scores omitted with no provider, got %#v", v)
	}
}
