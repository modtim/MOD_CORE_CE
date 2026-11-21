package hostexec

import (
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"strings"
	"testing"
)

// ScrubFrame must never invent or drop bytes around newlines: every test below
// asserts the exact output, including the trailing newline state.

func TestScrubFrame_BearerToken(t *testing.T) {
	in := "level: Authorization: Bearer supersecretvalue\n"
	got := string(ScrubFrame([]byte(in), ""))
	want := "level: Authorization: Bearer <REDACTED>\n"
	if got != want {
		t.Fatalf("bearer: got %q want %q", got, want)
	}
}

func TestScrubFrame_ScratchRootPath(t *testing.T) {
	ws := "/scratch/tenant-77/task-9"
	in := "open " + ws + "/inputs/video.mp4 failed\n"
	got := string(ScrubFrame([]byte(in), ws))
	want := "open <SCRATCH>/inputs/video.mp4 failed\n"
	if got != want {
		t.Fatalf("scratch root: got %q want %q", got, want)
	}
}

func TestScrubFrame_AWSKey(t *testing.T) {
	in := "export AWS_SECRET_ACCESS_KEY=" + "AKIA" + "IOSFODNN7EXAMPLE" + "w37656151234567890abcdefgHIJKL" + "\n"
	got := string(ScrubFrame([]byte(in), ""))
	if strings.Contains(got, "AKIA" + "IOSFODNN7EXAMPLE") || strings.Contains(got, "w37656151234567890abcdefgHIJKL") {
		t.Fatalf("aws key leaked: %q", got)
	}
	if !strings.Contains(got, "<REDACTED>") {
		t.Fatalf("aws key not redacted with a placeholder: %q", got)
	}
}

func TestScrubFrame_SecretKV(t *testing.T) {
	in := "password=hunter2 token=abc.def.ghi secret=  s3cretvalue\n"
	got := string(ScrubFrame([]byte(in), ""))
	want := "password=<REDACTED> token=<REDACTED> secret=  <REDACTED>\n"
	if got != want {
		t.Fatalf("secret kv: got %q want %q", got, want)
	}
}

func TestScrubFrame_PEMBlock(t *testing.T) {
	in := "before\n-----BEGIN RSA PRIVATE KEY-----\nQUO7dGVk\nYWx5c=\n-----END RSA PRIVATE KEY-----\nafter\n"
	got := string(ScrubFrame([]byte(in), ""))
	want := "before\n<REDACTED PRIVATE KEY>\nafter\n"
	if got != want {
		t.Fatalf("pem block: got %q want %q", got, want)
	}
}

// A frame cut mid-PEM-block must swallow everything it has (fail closed): the
// body never reaches the outbox even though the END marker is in a later frame.
func TestScrubFrame_PEMTruncatedMidBlock(t *testing.T) {
	in := "key:\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3l1a\n"
	got := string(ScrubFrame([]byte(in), ""))
	want := "key:\n<REDACTED PRIVATE KEY>"
	if got != want {
		t.Fatalf("pem truncated: got %q want %q", got, want)
	}
}

// Partial trailing line: the frame ends mid-line, so ScrubFrame must NOT append
// a newline — the next frame continues the same line.
func TestScrubFrame_PartialTrailingLine(t *testing.T) {
	in := "first line\nsecond line is cut"
	got := string(ScrubFrame([]byte(in), ""))
	want := "first line\nsecond line is cut"
	if got != want {
		t.Fatalf("partial line: got %q want %q", got, want)
	}
}

// A credential that ends exactly at a frame boundary must still be redacted:
// scrubbing the partial line is required, deferring it to the next frame is not
// allowed.
func TestScrubFrame_PartialLineCarriesSecret(t *testing.T) {
	in := "ok\nAuthorization: Bearer deadbeefcafe"
	got := string(ScrubFrame([]byte(in), ""))
	want := "ok\nAuthorization: Bearer <REDACTED>"
	if got != want {
		t.Fatalf("partial secret: got %q want %q", got, want)
	}
}

// Clean output must pass through byte-identical, including a trailing newline
// and an empty frame.
func TestScrubFrame_CleanPassthrough(t *testing.T) {
	in := "hello\nworld\n"
	if string(ScrubFrame([]byte(in), "/scratch/x")) != in {
		t.Fatalf("clean passthrough changed: %q", string(ScrubFrame([]byte(in), "/scratch/x")))
	}
	if len(ScrubFrame([]byte(""), "/scratch/x")) != 0 {
		t.Fatalf("empty frame must stay empty")
	}
}

// ScrubLogLine is the exported native rule set; the host-exec stream must agree
// with it exactly (one definition, two callers).
func TestScrubLogLine_MatchesNative(t *testing.T) {
	got := ScrubFrame([]byte("Bearer zzz\n"), "")
	want := executor.ScrubLogLine("Bearer zzz", "") + "\n"
	if string(got) != want {
		t.Fatalf("exported wrapper drift: got %q want %q", string(got), want)
	}
}
