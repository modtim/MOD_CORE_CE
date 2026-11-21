package capabilities

import (
	"errors"
	"os/exec"
	"testing"
	"unicode/utf16"
)

// TestDetectWslc — GP.17c read-only wslc capability probe.
//
// Pure: the command runner and path lookup are injected, so nothing here
// shells out or touches PATH. Covers UTF-16LE output (with and without BOM),
// an old WSL version, a missing wslc.exe, a failing command, and non-windows.

func wslVersionText(v string) string {
	return "Windows Subsystem for Linux\n" +
		"WSL version: " + v + "\n" +
		"Kernel version: 6.6.87.2\n" +
		"WSLg version: 1.0.69\n"
}

// utf16le encodes s as UTF-16LE; withBOM prepends FF FE.
func utf16le(s string, withBOM bool) []byte {
	u := utf16.Encode([]rune(s))
	out := make([]byte, len(u)*2)
	for i, r := range u {
		out[2*i] = byte(r)
		out[2*i+1] = byte(r >> 8)
	}
	if withBOM {
		return append([]byte{0xFF, 0xFE}, out...)
	}
	return out
}

func runConst(out []byte, err error) commandRunner {
	return func(*exec.Cmd) ([]byte, error) { return out, err }
}

func lookFound(path string) pathLookup {
	return func(string) (string, error) { return path, nil }
}

func lookMissing() pathLookup {
	return func(string) (string, error) { return "", errors.New("not found") }
}

func TestDetectWslc_AvailableUTF16BOM(t *testing.T) {
	got := DetectWslc("windows", runConst(utf16le(wslVersionText("2.9.3"), true), nil), lookFound(`C:\Windows\System32\wslc.exe`))
	if got == nil {
		t.Fatal("expected capability, got nil")
	}
	if !got.Available {
		t.Fatalf("expected available, got %+v", got)
	}
	if got.WSLVersion != "2.9.3" {
		t.Fatalf("version = %q, want 2.9.3", got.WSLVersion)
	}
}

func TestDetectWslc_AvailableUTF16NoBOM(t *testing.T) {
	got := DetectWslc("windows", runConst(utf16le(wslVersionText("2.9.4"), false), nil), lookFound(`C:\Windows\System32\wslc.exe`))
	if got == nil || !got.Available {
		t.Fatalf("expected available from BOM-less UTF-16, got %+v", got)
	}
	if got.WSLVersion != "2.9.4" {
		t.Fatalf("version = %q, want 2.9.4", got.WSLVersion)
	}
}

func TestDetectWslc_AvailableUTF8(t *testing.T) {
	got := DetectWslc("windows", runConst([]byte(wslVersionText("3.0.1")), nil), lookFound(`wslc`))
	if got == nil || !got.Available || got.WSLVersion != "3.0.1" {
		t.Fatalf("expected available 3.0.1 from UTF-8, got %+v", got)
	}
}

func TestDetectWslc_OldVersion(t *testing.T) {
	got := DetectWslc("windows", runConst(utf16le(wslVersionText("2.1.5"), true), nil), lookFound(`wslc`))
	if got == nil {
		t.Fatal("expected capability, got nil")
	}
	if got.Available {
		t.Fatalf("expected unavailable for 2.1.5, got %+v", got)
	}
	if got.WSLVersion != "2.1.5" {
		t.Fatalf("version = %q, want 2.1.5", got.WSLVersion)
	}
	if got.Reason == "" {
		t.Fatal("expected a reason")
	}
}

func TestDetectWslc_MissingWslc(t *testing.T) {
	got := DetectWslc("windows", runConst(utf16le(wslVersionText("2.9.3"), true), nil), lookMissing())
	if got == nil || got.Available {
		t.Fatalf("expected unavailable when wslc.exe is missing, got %+v", got)
	}
	if got.WSLVersion != "2.9.3" {
		t.Fatalf("version = %q, want 2.9.3 (known even though wslc is missing)", got.WSLVersion)
	}
}

func TestDetectWslc_CommandError(t *testing.T) {
	got := DetectWslc("windows", runConst(nil, errors.New("exec: wsl.exe not found")), lookFound(`wslc`))
	if got == nil || got.Available {
		t.Fatalf("expected unavailable on command error, got %+v", got)
	}
	if got.Reason == "" {
		t.Fatal("expected a reason naming the failure")
	}
}

func TestDetectWslc_NoVersionLine(t *testing.T) {
	got := DetectWslc("windows", runConst([]byte("something went wrong"), nil), lookFound(`wslc`))
	if got == nil || got.Available {
		t.Fatalf("expected unavailable when the version line is absent, got %+v", got)
	}
}

func TestDetectWslc_NonWindows(t *testing.T) {
	if got := DetectWslc("linux", runConst(nil, nil), lookFound(`wslc`)); got != nil {
		t.Fatalf("expected nil on linux, got %+v", got)
	}
	if got := DetectWslc("darwin", runConst(nil, nil), lookFound(`wslc`)); got != nil {
		t.Fatalf("expected nil on darwin, got %+v", got)
	}
}

func TestDecodingWSLOutput(t *testing.T) {
	cases := []struct {
		name string
		in   []byte
		want string
	}{
		{"utf16 BOM", append([]byte{0xFF, 0xFE}, utf16le("WSL version: 2.9.3", false)...), "WSL version: 2.9.3"},
		{"utf16 no BOM", utf16le("WSL version: 2.9.3", false), "WSL version: 2.9.3"},
		{"utf8", []byte("WSL version: 2.9.3"), "WSL version: 2.9.3"},
		{"utf8 BOM", append([]byte{0xEF, 0xBB, 0xBF}, "WSL version: 2.9.3"...), "WSL version: 2.9.3"},
	}
	for _, c := range cases {
		if got := decodingWSLOutput(c.in); got != c.want {
			t.Errorf("%s: %q, want %q", c.name, got, c.want)
		}
	}
}

func TestCompareVersions(t *testing.T) {
	cases := []struct {
		a, b string
		want int
	}{
		{"2.9.3", "2.9.3", 0},
		{"2.9.4", "2.9.3", 1},
		{"2.9.2", "2.9.3", -1},
		{"2.1.5", "2.9.3", -1},
		{"3.0", "2.9.3", 1},
		{"2.9", "2.9.3", -1}, // missing component counts as 0
		{"2.10.0", "2.9.9", 1}, // numeric, not lexicographic
	}
	for _, c := range cases {
		got, err := compareVersions(c.a, c.b)
		if err != nil {
			t.Errorf("%s vs %s: unexpected error %v", c.a, c.b, err)
			continue
		}
		if got != c.want {
			t.Errorf("compareVersions(%q, %q) = %d, want %d", c.a, c.b, got, c.want)
		}
	}
	if _, err := compareVersions("2.9.x", "2.9.3"); err == nil {
		t.Error("expected an error for a non-numeric component")
	}
}
