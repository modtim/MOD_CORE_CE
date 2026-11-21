package main

import (
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func newRedirectTestClient() *http.Client {
	return newSelfUpdateHTTPClient()
}

func TestRedirectCrossHostRefused(t *testing.T) {
	var bHits int32
	b := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&bHits, 1)
		w.WriteHeader(http.StatusOK)
	}))
	defer b.Close()
	a := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, b.URL+"/x", http.StatusFound)
	}))
	defer a.Close()

	req, _ := http.NewRequest("GET", a.URL+"/dl", nil)
	req.Header.Set("X-Bootstrap-Token", "test-token")
	resp, err := newRedirectTestClient().Do(req)
	if resp != nil {
		resp.Body.Close()
	}
	if err == nil {
		t.Fatal("expected cross-host redirect to be refused")
	}
	if n := atomic.LoadInt32(&bHits); n != 0 {
		t.Fatalf("redirect target received %d requests, want 0", n)
	}
}

func TestRedirectSameHostAllowedKeepsHeader(t *testing.T) {
	var gotToken atomic.Value
	a := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/dl" {
			http.Redirect(w, r, "/other", http.StatusFound)
			return
		}
		gotToken.Store(r.Header.Get("X-Bootstrap-Token"))
		w.WriteHeader(http.StatusOK)
	}))
	defer a.Close()

	req, _ := http.NewRequest("GET", a.URL+"/dl", nil)
	req.Header.Set("X-Bootstrap-Token", "test-token")
	resp, err := newRedirectTestClient().Do(req)
	if err != nil {
		t.Fatalf("same-host redirect failed: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d, want 200", resp.StatusCode)
	}
	if v, _ := gotToken.Load().(string); v != "test-token" {
		t.Fatalf("X-Bootstrap-Token not preserved on same-host redirect")
	}
}

// TestRedirectSelfUpdateClientWired guards the production wiring: the helper
// must stop an 11th redirect on the same host.
func TestRedirectLimit(t *testing.T) {
	via := make([]*http.Request, 10)
	req, _ := http.NewRequest("GET", "http://h/x", nil)
	for i := range via {
		via[i] = req
	}
	if err := refuseCrossHostRedirect(req, via); err == nil {
		t.Fatal("expected redirect limit error")
	}
}
