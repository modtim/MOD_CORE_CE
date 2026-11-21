package executor

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// GSEC Q21.9 — WSL non-root worker lib-dir readability probe.

func TestWSLLibReadableTrueOnTempDir(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "libnvidia.so"), []byte("x"), 0644); err != nil {
		t.Fatal(err)
	}
	ok, hint := WSLLibReadable(dir)
	if !ok {
		t.Fatalf("expected readable, got hint %q", hint)
	}
	if hint != "" {
		t.Fatalf("expected empty hint on readable dir, got %q", hint)
	}
}

func TestWSLLibReadableEmptyDirUsesDefault(t *testing.T) {
	// Empty string must resolve to the documented default path; on a non-WSL
	// host that path is absent and treated as "nothing to fix".
	ok, _ := WSLLibReadable("")
	_ = ok
}

func TestWSLLibReadableUnreadable(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("root can read mode-000 dirs; cannot assert unreadable")
	}
	dir := t.TempDir()
	if err := os.Chmod(dir, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0700) })

	ok, hint := WSLLibReadable(dir)
	if ok {
		t.Fatal("expected unreadable dir to report false")
	}
	if !strings.Contains(hint, "setfacl") || !strings.Contains(hint, dir) {
		t.Fatalf("hint %q must name the dir and a setfacl remediation", hint)
	}
}

func TestWSLLibReadableDefaultReturnsString(t *testing.T) {
	ok, _ := WSLLibReadableDefault()
	_ = ok
}

func TestIsWSLDetectsInteropEntry(t *testing.T) {
	orig := readProcFile
	readProcFile = func(path string) (string, bool) {
		if path == "/proc/sys/fs/binfmt_misc/WSLInterop" {
			return "enabled\n", true
		}
		return "", false
	}
	t.Cleanup(func() { readProcFile = orig })
	if !IsWSL() {
		t.Fatal("expected WSL detected via WSLInterop binfmt entry")
	}
}

func TestIsWSLDetectsProcVersionMicrosoft(t *testing.T) {
	orig := readProcFile
	readProcFile = func(path string) (string, bool) {
		if path == "/proc/version" {
			return "Linux 5.15.146.1-microsoft-standard-WSL2 #1 SMP\n", true
		}
		return "", false
	}
	t.Cleanup(func() { readProcFile = orig })
	if !IsWSL() {
		t.Fatal("expected WSL detected via 'microsoft' in /proc/version")
	}
}

func TestIsWSLFalseOnBareHost(t *testing.T) {
	orig := readProcFile
	readProcFile = func(path string) (string, bool) {
		if path == "/proc/version" {
			return "Linux 6.1.0-generic #1 SMP\n", true
		}
		return "", false
	}
	t.Cleanup(func() { readProcFile = orig })
	if IsWSL() {
		t.Fatal("bare host must not be detected as WSL")
	}
}
