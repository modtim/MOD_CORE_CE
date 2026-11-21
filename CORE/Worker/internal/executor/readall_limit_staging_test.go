package executor

// FINISH-FIX-GO-READALL-LIMIT-B: oversize tests for the two
// executor staging sites that carry a bounded read.
//   (1) native_staging.go fetchAndVerifyObject -> readCappedObjectBody
//       (cap nativeObjectMaxBytes = 256 MiB; tested with a small cap parameter).
//   (2) ce_inprocess_staging.go ceMinIOFromCredsFile (cap ceCredsFileMaxBytes = 64 KiB).
// Helper and file names carry the rlsb prefix so they cannot collide with the
// other legs' test files.

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const rlsbSmallCap = 1 << 10 // 1 KiB cap for the native helper tests

// rlsbGenBody is a counting generator: it yields `remaining` bytes of filler
// without allocating them, and records how many bytes were handed out.
type rlsbGenBody struct {
	remaining int64
	read      int64
}

func (g *rlsbGenBody) Read(p []byte) (int, error) {
	if g.remaining <= 0 {
		return 0, io.EOF
	}
	n := int64(len(p))
	if n > g.remaining {
		n = g.remaining
	}
	for i := int64(0); i < n; i++ {
		p[i] = 'x'
	}
	g.remaining -= n
	g.read += n
	return int(n), nil
}

// TestRlsbNativeOverCapRefused: a body of cap+10 KiB is refused, returns no
// bytes, and the reader is never drained past cap+1.
func TestRlsbNativeOverCapRefused(t *testing.T) {
	body := &rlsbGenBody{remaining: rlsbSmallCap + 10<<10}
	buf, err := readCappedObjectBody(body, rlsbSmallCap)
	if err == nil {
		t.Fatalf("over-cap body accepted (%d bytes returned)", len(buf))
	}
	if buf != nil {
		t.Fatalf("over-cap body returned %d bytes with the error; want none", len(buf))
	}
	if !strings.Contains(err.Error(), "exceeds") {
		t.Fatalf("error %q does not name the cap", err)
	}
	if body.read > rlsbSmallCap+1 {
		t.Fatalf("consumed %d bytes; bounded read must stop at cap+1 = %d", body.read, rlsbSmallCap+1)
	}
}

// TestRlsbNativeBoundaryAndNormal: exactly-cap and small bodies come back
// byte-identical (behaviour unchanged below the cap).
func TestRlsbNativeBoundaryAndNormal(t *testing.T) {
	for _, size := range []int64{0, 1, 512, rlsbSmallCap} {
		body := &rlsbGenBody{remaining: size}
		buf, err := readCappedObjectBody(body, rlsbSmallCap)
		if err != nil {
			t.Fatalf("size %d: unexpected error %v", size, err)
		}
		if int64(len(buf)) != size {
			t.Fatalf("size %d: got %d bytes back", size, len(buf))
		}
		if body.read != size {
			t.Fatalf("size %d: reader consumed %d bytes", size, body.read)
		}
	}
}

// TestRlsbNativeProductionCap pins the production cap the fetch site passes.
func TestRlsbNativeProductionCap(t *testing.T) {
	if nativeObjectMaxBytes != 256<<20 {
		t.Fatalf("nativeObjectMaxBytes = %d; documented cap is 256 MiB", nativeObjectMaxBytes)
	}
}

// rlsbCredsJSON is a valid creds document with fake values only.
const rlsbCredsJSON = `{"task_id":1,"endpoint":"minio.test:9000","access_key":"FAKEACCESS","secret_key":"FAKESECRET"}`

// rlsbWriteCreds writes rlsbCredsJSON padded with trailing spaces to
// exactly size bytes (valid JSON stays valid with trailing whitespace).
func rlsbWriteCreds(t *testing.T, size int) string {
	t.Helper()
	pad := size - len(rlsbCredsJSON)
	if pad < 0 {
		t.Fatalf("size %d smaller than the creds document", size)
	}
	path := filepath.Join(t.TempDir(), "creds.json")
	if err := os.WriteFile(path, []byte(rlsbCredsJSON+strings.Repeat(" ", pad)), 0o600); err != nil {
		t.Fatalf("write creds: %v", err)
	}
	return path
}

// TestRlsbCredsOverCapRefused: a valid creds file padded past 64 KiB is refused
// with an error and is never parsed from a truncated read.
func TestRlsbCredsOverCapRefused(t *testing.T) {
	path := rlsbWriteCreds(t, ceCredsFileMaxBytes+10<<10)
	got, err := ceMinIOFromCredsFile(path)
	if err == nil {
		t.Fatalf("over-cap creds file accepted (endpoint %q)", got.Endpoint)
	}
	if !strings.Contains(err.Error(), "exceeds") {
		t.Fatalf("error %q does not name the cap", err)
	}
}

// TestRlsbCredsBoundaryAndNormal: a file of exactly the cap is still parsed, and
// a small valid file parses as before.
func TestRlsbCredsBoundaryAndNormal(t *testing.T) {
	atCap := rlsbWriteCreds(t, ceCredsFileMaxBytes)
	if _, err := ceMinIOFromCredsFile(atCap); err != nil {
		t.Fatalf("file of exactly cap bytes rejected: %v", err)
	}
	small := rlsbWriteCreds(t, len(rlsbCredsJSON))
	got, err := ceMinIOFromCredsFile(small)
	if err != nil {
		t.Fatalf("small valid creds file rejected: %v", err)
	}
	if got.AccessKey != "FAKEACCESS" || got.SecretKey != "FAKESECRET" || got.Endpoint != "minio.test:9000" {
		t.Fatalf("small creds parsed wrong: access %q endpoint %q", got.AccessKey, got.Endpoint)
	}
}
