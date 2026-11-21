package heartbeat

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
)

// newTestHeartbeat builds a minimal Heartbeat pointed at srv, sufficient for
// exercising sendAPI's response decode (GS.PSR.7's `tunables` field) without
// standing up the full agent (no docker/redis/profile needed for this path).
func newTestHeartbeat(srv *httptest.Server) *Heartbeat {
	return &Heartbeat{
		cfg:    &config.Config{APIURL: srv.URL, WorkerName: "test-worker"},
		curURL: srv.URL,
		log:    discardLog(),
	}
}

func TestSendAPI_TunablesDispatchedToOnTunables(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"tunables": map[string]interface{}{
				"worker.egress_proxy_mode":   "off",
				"worker.prefer_local_images": true,
			},
		})
	}))
	defer srv.Close()

	h := newTestHeartbeat(srv)
	var got map[string]interface{}
	h.OnTunables(func(m map[string]interface{}) { got = m })

	_, _, err := h.sendAPI(context.Background(), map[string]interface{}{"worker_name": "test-worker"})
	if err != nil {
		t.Fatalf("sendAPI: %v", err)
	}

	want := map[string]interface{}{
		"worker.egress_proxy_mode":   "off",
		"worker.prefer_local_images": true,
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("OnTunables received %#v, want %#v", got, want)
	}
}

func TestSendAPI_AbsentTunablesDispatchesNil(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// No "tunables" key at all — an unconfigured platform, or an older
		// manager that predates this field.
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"status": "ok"})
	}))
	defer srv.Close()

	h := newTestHeartbeat(srv)
	called := false
	var got map[string]interface{}
	h.OnTunables(func(m map[string]interface{}) { called = true; got = m })

	_, _, err := h.sendAPI(context.Background(), map[string]interface{}{"worker_name": "test-worker"})
	if err != nil {
		t.Fatalf("sendAPI: %v", err)
	}
	if !called {
		t.Fatal("OnTunables must still be called (with nil/empty) so a cleared setting actually clears")
	}
	if len(got) != 0 {
		t.Errorf("expected empty/nil tunables, got %#v", got)
	}
}

func TestSendAPI_ChangeAppliedOnNextHeartbeat(t *testing.T) {
	// First beat: no override. Second beat: server pushes an override.
	// Simulates the "applied within 2 heartbeats" requirement end-to-end
	// through the wire decode (Store.Apply itself is unit-tested in
	// internal/tunables; this proves the heartbeat layer wires it through).
	beat := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		beat++
		resp := map[string]interface{}{}
		if beat >= 2 {
			resp["tunables"] = map[string]interface{}{"worker.prefer_local_images": true}
		}
		_ = json.NewEncoder(w).Encode(resp)
	}))
	defer srv.Close()

	h := newTestHeartbeat(srv)
	var last map[string]interface{}
	applied := 0
	h.OnTunables(func(m map[string]interface{}) {
		last = m
		applied++
	})

	for i := 0; i < 2; i++ {
		if _, _, err := h.sendAPI(context.Background(), map[string]interface{}{"worker_name": "test-worker"}); err != nil {
			t.Fatalf("sendAPI beat %d: %v", i+1, err)
		}
	}

	if applied != 2 {
		t.Fatalf("expected OnTunables called once per heartbeat, got %d calls", applied)
	}
	if v, ok := last["worker.prefer_local_images"]; !ok || v != true {
		t.Errorf("second heartbeat should have delivered the override, got %#v", last)
	}
}
