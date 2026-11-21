package executor

// FINISH-FIX-GO-READALL-LIMIT-C (, leg C): oversize proof for the
// docker exec probe output read in executor.go containerDataBytes (~:459), bounded
// by the existing stageOutLogMaxBytes (16 MiB) log cap.
// The site sits inside a method that needs a live docker daemon, so the read was
// extracted into the smallest possible pure helper (readCappedExecStream, behaviour
// identical to the inline io.ReadAll(io.LimitReader(...)) it replaced); this file
// drives that helper with a counting generator reader, the same method leg B used
// for the staging sites. Helper/file names carry the rlxce prefix so they cannot
// collide with the other legs' test files.

import (
	"io"
	"strconv"
	"strings"
	"testing"
)

const rlxceSmallCap = 1 << 10 // 1 KiB cap for the tests (production cap is 16 MiB)

// rlxceGenBody is a counting generator reader: it yields `prefix` bytes, then
// `remaining` bytes of filler, without allocating the whole stream, and records
// how many bytes were handed out.
type rlxceGenBody struct {
	prefix    []byte
	remaining int64
	read      int64
}

func (g *rlxceGenBody) Read(p []byte) (int, error) {
	if len(g.prefix) > 0 {
		n := len(p)
		if n > len(g.prefix) {
			n = len(g.prefix)
		}
		for i := 0; i < n; i++ {
			p[i] = g.prefix[i]
		}
		g.prefix = g.prefix[n:]
		g.read += int64(n)
		return n, nil
	}
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

// rlxceFirstIntField mirrors the parse loop in containerDataBytes (executor.go
// ~:460): first whitespace-separated field that parses as an int.
func rlxceFirstIntField(out []byte) (int64, bool) {
	for _, f := range strings.Fields(string(out)) {
		if n, perr := strconv.ParseInt(f, 10, 64); perr == nil {
			return n, true
		}
	}
	return 0, false
}

// TestRlxceExecProbeOverCapTruncated: a probe stream that would be cap+10 KiB is
// stopped at the cap; the result is the truncated LEADING part (the du number
// survives, only trailing tokens are dropped).
func TestRlxceExecProbeOverCapTruncated(t *testing.T) {
	g := &rlxceGenBody{prefix: []byte("98765 "), remaining: rlxceSmallCap + 10<<10}
	out := readCappedExecStream(g, rlxceSmallCap)
	if g.read > rlxceSmallCap {
		t.Fatalf("consumed %d bytes; bounded read must stop at cap = %d", g.read, rlxceSmallCap)
	}
	if len(out) != rlxceSmallCap {
		t.Fatalf("truncated result is %d bytes; want exactly cap %d", len(out), rlxceSmallCap)
	}
	if string(out[:6]) != "98765 " {
		t.Fatalf("truncated result lost the leading token: %q", string(out[:6]))
	}
	n, ok := rlxceFirstIntField(out)
	if !ok || n != 98765 {
		t.Fatalf("leading du number not recoverable from the truncated head: %v %v", n, ok)
	}
}

// TestRlxceExecProbeNormalUnchanged: at or below the cap the whole stream comes
// back byte-for-byte (behaviour unchanged on normal sizes).
func TestRlxceExecProbeNormalUnchanged(t *testing.T) {
	for _, size := range []int64{0, 1, 512, rlxceSmallCap} {
		g := &rlxceGenBody{remaining: size}
		out := readCappedExecStream(g, rlxceSmallCap)
		if int64(len(out)) != size {
			t.Fatalf("size %d: got %d bytes back", size, len(out))
		}
		if g.read != size {
			t.Fatalf("size %d: reader consumed %d bytes", size, g.read)
		}
	}
}

// TestRlxceExecProbeProductionCap pins the cap the site passes.
func TestRlxceExecProbeProductionCap(t *testing.T) {
	if stageOutLogMaxBytes != 16<<20 {
		t.Fatalf("stageOutLogMaxBytes = %d; documented cap is 16 MiB", stageOutLogMaxBytes)
	}
}
