package transport

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"testing"
	"time"
)

// genBody yields size bytes of filler and records the largest number of bytes
// read from any one response, so a test can prove a call site never read past
// its cap without buffering the body.
type genBody struct {
	size, off, read int
	max             *int
}

func (g *genBody) Read(p []byte) (int, error) {
	if g.off >= g.size {
		return 0, io.EOF
	}
	n := len(p)
	if rem := g.size - g.off; n > rem {
		n = rem
	}
	for i := 0; i < n; i++ {
		p[i] = 'x'
	}
	g.off += n
	g.read += n
	if g.read > *g.max {
		*g.max = g.read
	}
	return n, nil
}

// fakeRT answers every request with a fresh body of size bytes and a fixed
// status. No network is involved. Each response gets its own genBody, so a
// retrying call site is measured per response, not cumulatively.
type fakeRT struct {
	status, size int
	max          *int
}

func (f fakeRT) RoundTrip(req *http.Request) (*http.Response, error) {
	return &http.Response{
		StatusCode: f.status,
		Status:     http.StatusText(f.status),
		Header:     make(http.Header),
		Body:       io.NopCloser(&genBody{size: f.size, max: f.max}),
		Request:    req,
	}, nil
}

// newFakeTransport returns a transport whose every response has the given
// status and body size, plus a pointer to the largest per-response read.
func newFakeTransport(status, size int) (*APITransport, *int) {
	maxRead := new(int)
	tr := NewAPITransport("http://fake.invalid", "test-worker", "tok", nil, slog.Default())
	tr.client.Transport = fakeRT{status: status, size: size, max: maxRead}
	return tr, maxRead
}

// TestErrorBodySitesAreBounded: every error-body read is capped at
// maxErrorBodyBytes. The body is cap + 10 KiB, so an unbounded io.ReadAll at any
// one site reads well over cap+1 bytes and fails that subtest only.
func TestErrorBodySitesAreBounded(t *testing.T) {
	size := maxErrorBodyBytes + 10<<10
	// Short deadline: ResolveSecrets retries on HTTP errors; each attempt is
	// measured on its own, the retries only add wall time.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	calls := map[string]func(*APITransport) error{
		"pollForTask": func(tr *APITransport) error {
			_, err := tr.pollForTask(ctx, 1, false)
			return err
		},
		"ValidatePhase": func(tr *APITransport) error {
			_, err := tr.ValidatePhase(ctx, 1, "p", ResourceSnapshot{})
			return err
		},
		"ResolveSecrets": func(tr *APITransport) error {
			_, err := tr.ResolveSecrets(ctx, 1)
			return err
		},
		"post": func(tr *APITransport) error {
			return tr.post(ctx, "/x", []byte("{}"))
		},
		"postWithResponse": func(tr *APITransport) error {
			_, err := tr.postWithResponse(ctx, "/x", []byte("{}"))
			return err
		},
	}
	for name, call := range calls {
		t.Run(name, func(t *testing.T) {
			tr, maxRead := newFakeTransport(http.StatusInternalServerError, size)
			if err := call(tr); err == nil {
				t.Fatal("want an error on HTTP 500, got nil")
			}
			if *maxRead > maxErrorBodyBytes+1 {
				t.Fatalf("read %d bytes of a %d-byte error body; cap is %d", *maxRead, size, maxErrorBodyBytes)
			}
		})
	}
}

// TestPostWithResponseTooLarge: an oversized success body is an explicit
// errResponseTooLarge, never a silently truncated body, and the read stops at
// maxResponseBytes+1.
func TestPostWithResponseTooLarge(t *testing.T) {
	size := maxResponseBytes + 10<<10
	tr, maxRead := newFakeTransport(http.StatusOK, size)
	_, err := tr.postWithResponse(context.Background(), "/x", []byte("{}"))
	if !errors.Is(err, errResponseTooLarge) {
		t.Fatalf("want errResponseTooLarge, got %v", err)
	}
	if *maxRead > maxResponseBytes+1 {
		t.Fatalf("read %d bytes of a %d-byte success body; cap is %d", *maxRead, size, maxResponseBytes)
	}
}

// TestPostWithResponseAtCapAccepted: a success body of exactly maxResponseBytes
// is still returned whole (the cap is inclusive).
func TestPostWithResponseAtCapAccepted(t *testing.T) {
	tr, _ := newFakeTransport(http.StatusOK, maxResponseBytes)
	b, err := tr.postWithResponse(context.Background(), "/x", []byte("{}"))
	if err != nil {
		t.Fatalf("body at cap must be accepted, got %v", err)
	}
	if len(b) != maxResponseBytes {
		t.Fatalf("got %d bytes, want %d", len(b), maxResponseBytes)
	}
}

// TestPostWithResponseNormalSizeUnchanged: a normal success body comes back
// byte-for-byte, as before the cap was added.
func TestPostWithResponseNormalSizeUnchanged(t *testing.T) {
	tr, _ := newFakeTransport(http.StatusOK, 1234)
	b, err := tr.postWithResponse(context.Background(), "/x", []byte("{}"))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(b) != 1234 || strings.Trim(string(b), "x") != "" {
		t.Fatalf("normal body altered: len=%d", len(b))
	}
}

// TestErrorBodyNormalSizeUnchanged: a normal error body reaches HTTPError.Body
// unchanged.
func TestErrorBodyNormalSizeUnchanged(t *testing.T) {
	tr, _ := newFakeTransport(http.StatusBadRequest, 42)
	err := tr.post(context.Background(), "/x", []byte("{}"))
	var he *HTTPError
	if !errors.As(err, &he) {
		t.Fatalf("want *HTTPError, got %v", err)
	}
	if he.StatusCode != http.StatusBadRequest || he.Body != strings.Repeat("x", 42) {
		t.Fatalf("normal error body altered: status=%d len=%d", he.StatusCode, len(he.Body))
	}
}
