package executor

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"testing"
)

// errVerdictCeiling is returned by verdictGenBody once it has handed out its
// stopAt bytes. A body that would be gigabytes if read whole stops here, so an
// unbounded read fails the assertion instead of exhausting memory.
var errVerdictCeiling = errors.New("verdict generator ceiling reached")

// verdictGenBody yields size bytes of JSON whitespace and records the largest
// number of bytes one response handed out (max is shared across responses, so
// a retrying site is measured per response, not cumulatively).
type verdictGenBody struct {
	size, stopAt, off int
	max               *int
}

func (g *verdictGenBody) Read(p []byte) (int, error) {
	if g.off >= g.size {
		return 0, io.EOF
	}
	if g.off >= g.stopAt {
		return 0, errVerdictCeiling
	}
	n := len(p)
	if rem := g.size - g.off; n > rem {
		n = rem
	}
	if rem := g.stopAt - g.off; n > rem {
		n = rem
	}
	for i := 0; i < n; i++ {
		p[i] = ' '
	}
	g.off += n
	if g.off > *g.max {
		*g.max = g.off
	}
	return n, nil
}

// stubVerdictHTTP replaces the hostExecHTTPDo seam: every POST gets status and
// a fresh body from newBody. It returns a pointer to the POST count and restores
// the real seam on cleanup.
func stubVerdictHTTP(t *testing.T, status int, newBody func() io.Reader) *int {
	t.Helper()
	calls := new(int)
	old := hostExecHTTPDo
	hostExecHTTPDo = func(ctx context.Context, req *http.Request) (*http.Response, error) {
		*calls++
		return &http.Response{
			StatusCode: status,
			Status:     http.StatusText(status),
			Header:     make(http.Header),
			Body:       io.NopCloser(newBody()),
			Request:    req,
		}, nil
	}
	t.Cleanup(func() { hostExecHTTPDo = old })
	return calls
}

// newVerdictSink builds the smallest HostExecSink that post() can drive: no
// network (the seam above answers), no tunables, no run cancel side effects.
func newVerdictSink() *HostExecSink {
	return &HostExecSink{
		e:         &Executor{apiURL: "http://api.test"},
		log:       slog.Default(),
		workerID:  "readall-limit-worker",
		jobID:     1,
		pending:   map[string][]byte{},
		ctx:       context.Background(),
		cancelRun: func() {},
	}
}

// verdictPadded returns a JSON verdict padded with spaces to exactly n bytes.
func verdictPadded(t *testing.T, doc string, n int) string {
	t.Helper()
	if len(doc) > n {
		t.Fatalf("verdict %q is %d bytes, larger than the %d-byte target", doc, len(doc), n)
	}
	return doc + strings.Repeat(" ", n-len(doc))
}

// TestHostExecVerdictOverCapRefusedAndBounded: an ingest verdict past
// hostExecVerdictMaxBytes is never decoded. Each attempt reads at most cap+1
// bytes, the post fails after hostExecPostRetries attempts, and the sink does
// not treat the chunk as blocked.
func TestHostExecVerdictOverCapRefusedAndBounded(t *testing.T) {
	maxRead := new(int)
	calls := stubVerdictHTTP(t, http.StatusOK, func() io.Reader {
		// 4x cap is the ceiling: an unbounded io.ReadAll reads 4x cap and fails
		// the bytes-read assertion below.
		return &verdictGenBody{size: 1 << 30, stopAt: 4 * hostExecVerdictMaxBytes, max: maxRead}
	})
	s := newVerdictSink()
	err := s.post(s.ctx, "stdout", []byte("chunk"), false, nil)
	if err == nil {
		t.Fatalf("over-cap verdict must fail the post, got nil")
	}
	if !strings.Contains(err.Error(), "verdict body over") {
		t.Fatalf("over-cap verdict error = %q, want the over-cap refusal", err)
	}
	if *calls != hostExecPostRetries {
		t.Fatalf("POST attempts = %d, want %d (over-cap is retried)", *calls, hostExecPostRetries)
	}
	if *maxRead > hostExecVerdictMaxBytes+1 {
		t.Fatalf("read %d bytes of one over-cap verdict; cap+1 is %d", *maxRead, hostExecVerdictMaxBytes+1)
	}
	if s.blocked {
		t.Fatalf("an over-cap verdict must not mark the sink blocked")
	}
	if !s.failed {
		t.Fatalf("exhausted retries must mark the sink failed")
	}
}

// TestHostExecVerdictExactCapAccepted: a blocked verdict of exactly
// hostExecVerdictMaxBytes is still decoded (the cap is inclusive).
func TestHostExecVerdictExactCapAccepted(t *testing.T) {
	doc := verdictPadded(t, `{"accepted": false, "status": "failed", "blocked": true, "blocked_reason": "output_scan_blocked"}`, hostExecVerdictMaxBytes)
	calls := stubVerdictHTTP(t, http.StatusOK, func() io.Reader { return strings.NewReader(doc) })
	s := newVerdictSink()
	if err := s.post(s.ctx, "stdout", []byte("chunk"), false, nil); err != nil {
		t.Fatalf("exact-cap verdict must be accepted, got %v", err)
	}
	if *calls != 1 {
		t.Fatalf("POST attempts = %d, want 1", *calls)
	}
	if !s.blocked || s.blockedReason != hostExecBlockedReason {
		t.Fatalf("exact-cap blocked verdict not decoded: blocked=%v reason=%q", s.blocked, s.blockedReason)
	}
}

// TestHostExecVerdictNormalBodyUnchanged: a small accepted verdict behaves as
// before the cap: one POST, nil error, not blocked, sequence advanced.
func TestHostExecVerdictNormalBodyUnchanged(t *testing.T) {
	doc := `{"accepted": true, "status": "running"}`
	calls := stubVerdictHTTP(t, http.StatusOK, func() io.Reader { return strings.NewReader(doc) })
	s := newVerdictSink()
	if err := s.post(s.ctx, "stdout", []byte("chunk"), false, nil); err != nil {
		t.Fatalf("normal verdict must succeed, got %v", err)
	}
	if *calls != 1 {
		t.Fatalf("POST attempts = %d, want 1", *calls)
	}
	if s.blocked || s.failed {
		t.Fatalf("normal verdict must neither block nor fail the sink: blocked=%v failed=%v", s.blocked, s.failed)
	}
	if s.seq != 1 {
		t.Fatalf("seq = %d after one accepted chunk, want 1", s.seq)
	}
}
