package transport

import (
	"bytes"
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// credsErrLogBuf is a goroutine-safe log sink for the retry loop's warnings.
type credsErrLogBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (c *credsErrLogBuf) Write(p []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.b.Write(p)
}

func (c *credsErrLogBuf) String() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.b.String()
}

// TestCredentialsNon200CarriesNoBody: a non-200 from the credentials
// endpoint must surface the status code only. The 500-byte sentinel body
// (storage credentials in production) must not appear in the returned
// error or in anything the retry loop logs about it.
func TestCredentialsNon200CarriesNoBody(t *testing.T) {
	const sentinel = "SENTINEL-CRED-BODY-"
	body := strings.Repeat(sentinel, 30) // well over the old 500-byte cap
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(body))
	}))
	defer srv.Close()

	var logs credsErrLogBuf
	lg := slog.New(slog.NewTextHandler(&logs, nil))
	tr := NewAPITransport(srv.URL, "test-worker", "tok", nil, lg)

	// The retry loop retries non-200s until its deadline; a short context
	// ends it after the first attempt, whose error is logged by the loop.
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	_, err := tr.GetCredentials(ctx, 7)
	if err == nil {
		t.Fatal("GetCredentials: want an error for a 500, got nil")
	}
	if strings.Contains(err.Error(), sentinel) {
		t.Fatalf("returned error carries the response body: %v", err)
	}
	seen := logs.String()
	if strings.Contains(seen, sentinel) {
		t.Fatalf("retry log carries the response body: %s", seen)
	}
	if !strings.Contains(seen, "get credentials: HTTP 500") {
		t.Fatalf("retry log lacks the status code: %s", seen)
	}
}
