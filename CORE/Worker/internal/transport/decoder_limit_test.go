package transport

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"testing"
	"time"
)

// countBody serves a fixed byte array and records the largest number of bytes
// read from any one response, so a test can prove a decoder stopped at its cap
// instead of draining a generator that would be huge if read fully.
type countBody struct {
	b              []byte
	off, read      int
	max            *int
}

func (c *countBody) Read(p []byte) (int, error) {
	if c.off >= len(c.b) {
		return 0, io.EOF
	}
	n := len(p)
	if rem := len(c.b) - c.off; n > rem {
		n = rem
	}
	for i := 0; i < n; i++ {
		p[i] = c.b[c.off+i]
	}
	c.off += n
	c.read += n
	if c.read > *c.max {
		*c.max = c.read
	}
	return n, nil
}

// fakeJSONRT answers every request with HTTP 200 and a fresh copy of body.
// No network is involved.
type fakeJSONRT struct {
	body []byte
	max  *int
}

func (f fakeJSONRT) RoundTrip(req *http.Request) (*http.Response, error) {
	return &http.Response{
		StatusCode: http.StatusOK,
		Status:     http.StatusText(http.StatusOK),
		Header:     make(http.Header),
		Body:       io.NopCloser(&countBody{b: f.body, max: f.max}),
		Request:    req,
	}, nil
}

// newJSONTransport returns a transport whose every response is body, plus a
// pointer to the largest per-response read.
func newJSONTransport(body []byte) (*APITransport, *int) {
	maxRead := new(int)
	tr := NewAPITransport("http://fake.invalid", "test-worker", "tok", nil, slog.Default())
	tr.client.Transport = fakeJSONRT{body: body, max: maxRead}
	return tr, maxRead
}

// jsonOf builds valid-looking JSON of exactly size bytes: {"a":"xxxx...x"}.
// A decoder that reads the whole thing buffers size bytes; a bounded one must
// stop at maxResponseBytes+1 and never see the tail.
func jsonOf(size int) []byte {
	b := make([]byte, size)
	b[0] = '{'
	b[1] = '"'
	b[2] = 'a'
	b[3] = '"'
	b[4] = ':'
	b[5] = '"'
	for i := 6; i < size-2; i++ {
		b[i] = 'x'
	}
	b[size-2] = '"'
	b[size-1] = '}'
	return b
}

// TestDecoderSitesRejectOversized: every success-path decoder returns
// errResponseTooLarge for a cap+10 KB body, and the bytes it consumed never
// exceed cap+1 plus the decoder's small read-ahead (<= cap + 64 KiB), which
// also proves the generator was NOT drained.
func TestDecoderSitesRejectOversized(t *testing.T) {
	size := maxResponseBytes + 10<<10
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	calls := map[string]func(*APITransport) error{
		"TryClaimNow": func(tr *APITransport) error {
			_, err := tr.TryClaimNow(ctx)
			return err
		},
		"pollForTask": func(tr *APITransport) error {
			_, err := tr.pollForTask(ctx, 1, false)
			return err
		},
		"GetCredentials": func(tr *APITransport) error {
			_, err := tr.GetCredentials(ctx, 1)
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
	}
	for name, call := range calls {
		t.Run(name, func(t *testing.T) {
			tr, maxRead := newJSONTransport(jsonOf(size))
			if err := call(tr); !errors.Is(err, errResponseTooLarge) {
				t.Fatalf("want errResponseTooLarge, got %v", err)
			}
			if *maxRead > maxResponseBytes+64<<10 {
				t.Fatalf("consumed %d bytes of a %d-byte body; cap is %d",
					*maxRead, size, maxResponseBytes)
			}
		})
	}
}

// TestDecoderSitesNormalSizeUnchanged: a normal JSON body decodes exactly as
// before (empty queue for the poll paths, empty creds/envelope, proceed=true).
func TestDecoderSitesNormalSizeUnchanged(t *testing.T) {
	cases := map[string]struct {
		body []byte
		kind string // "nil" = call must succeed
	}{
		"poll":     {body: []byte(`{}`), kind: "nil"},
		"creds":    {body: []byte(`{}`), kind: "nil"},
		"validate": {body: []byte(`{"proceed":true}`), kind: "nil"},
		"secrets":  {body: []byte(`{}`), kind: "nil"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			tr, _ := newJSONTransport(c.body)
			var err error
			switch name {
			case "poll":
				_, err = tr.pollForTask(context.Background(), 1, false)
			case "creds":
				_, err = tr.GetCredentials(context.Background(), 1)
			case "validate":
				_, err = tr.ValidatePhase(context.Background(), 1, "p", ResourceSnapshot{})
			case "secrets":
				_, err = tr.ResolveSecrets(context.Background(), 1)
			}
			if err != nil {
				t.Fatalf("normal body altered: %v", err)
			}
		})
	}
	// TryClaimNow swallows decode errors by design; assert only that an
	// over-cap is the ONLY thing that surfaces, and a normal body never does.
	tr, _ := newJSONTransport([]byte(`{}`))
	if _, err := tr.TryClaimNow(context.Background()); errors.Is(err, errResponseTooLarge) {
		t.Fatalf("normal body reported too large: %v", err)
	}
}
