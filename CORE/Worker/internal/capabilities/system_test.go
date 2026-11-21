package capabilities

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParseOSRelease_Ubuntu(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "os-release")
	contents := `NAME="Ubuntu"
VERSION="22.04.3 LTS (Jammy Jellyfish)"
ID=ubuntu
ID_LIKE=debian
PRETTY_NAME="Ubuntu 22.04.3 LTS"
VERSION_ID="22.04"
HOME_URL="https://www.ubuntu.com/"
`
	if err := os.WriteFile(path, []byte(contents), 0644); err != nil {
		t.Fatalf("setup: %v", err)
	}

	rel := parseOSRelease(path)
	if rel["ID"] != "ubuntu" {
		t.Errorf("ID = %q, want %q", rel["ID"], "ubuntu")
	}
	if rel["VERSION_ID"] != "22.04" {
		t.Errorf("VERSION_ID = %q, want %q", rel["VERSION_ID"], "22.04")
	}
	if rel["NAME"] != "Ubuntu" {
		t.Errorf("NAME = %q, want %q", rel["NAME"], "Ubuntu")
	}
}

func TestParseOSRelease_RHEL(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "os-release")
	contents := `NAME="Red Hat Enterprise Linux"
VERSION="9.3 (Plow)"
ID="rhel"
ID_LIKE="fedora"
VERSION_ID="9.3"
PRETTY_NAME="Red Hat Enterprise Linux 9.3 (Plow)"
`
	if err := os.WriteFile(path, []byte(contents), 0644); err != nil {
		t.Fatalf("setup: %v", err)
	}

	rel := parseOSRelease(path)
	if rel["ID"] != "rhel" {
		t.Errorf("ID = %q, want rhel", rel["ID"])
	}
	if rel["VERSION_ID"] != "9.3" {
		t.Errorf("VERSION_ID = %q, want 9.3", rel["VERSION_ID"])
	}
}

func TestParseOSRelease_Alpine(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "os-release")
	contents := `NAME="Alpine Linux"
ID=alpine
VERSION_ID=3.19.0
PRETTY_NAME="Alpine Linux v3.19"
`
	if err := os.WriteFile(path, []byte(contents), 0644); err != nil {
		t.Fatalf("setup: %v", err)
	}

	rel := parseOSRelease(path)
	if rel["ID"] != "alpine" {
		t.Errorf("ID = %q, want alpine", rel["ID"])
	}
	if rel["VERSION_ID"] != "3.19.0" {
		t.Errorf("VERSION_ID = %q, want 3.19.0", rel["VERSION_ID"])
	}
}

func TestParseOSRelease_Missing(t *testing.T) {
	rel := parseOSRelease("/nonexistent/path/os-release")
	if len(rel) != 0 {
		t.Errorf("missing file should return empty map, got %v", rel)
	}
}

func TestParseOSRelease_Comments(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "os-release")
	contents := `# leading comment
ID=debian

# blank above and below

VERSION_ID=12
`
	if err := os.WriteFile(path, []byte(contents), 0644); err != nil {
		t.Fatalf("setup: %v", err)
	}

	rel := parseOSRelease(path)
	if rel["ID"] != "debian" {
		t.Errorf("ID = %q, want debian", rel["ID"])
	}
	if rel["VERSION_ID"] != "12" {
		t.Errorf("VERSION_ID = %q, want 12", rel["VERSION_ID"])
	}
}

func TestDetectAirGapped_Truthy(t *testing.T) {
	cases := []string{"1", "true", "TRUE", "True", "yes", "YES", "on", "ON"}
	for _, val := range cases {
		t.Run(val, func(t *testing.T) {
			t.Setenv("WORKER_AIR_GAPPED", val)
			if !detectAirGapped() {
				t.Errorf("WORKER_AIR_GAPPED=%q should be truthy", val)
			}
		})
	}
}

func TestDetectAirGapped_Falsy(t *testing.T) {
	cases := []string{"", "0", "false", "no", "off", "garbage", "FALSE"}
	for _, val := range cases {
		t.Run(val, func(t *testing.T) {
			t.Setenv("WORKER_AIR_GAPPED", val)
			if detectAirGapped() {
				t.Errorf("WORKER_AIR_GAPPED=%q should be falsy", val)
			}
		})
	}
}

func TestDetectAirGapped_Whitespace(t *testing.T) {
	t.Setenv("WORKER_AIR_GAPPED", "  true  ")
	if !detectAirGapped() {
		t.Error("WORKER_AIR_GAPPED with surrounding whitespace should be parsed as truthy")
	}
}

func TestDetectAirGapped_Unset(t *testing.T) {
	t.Setenv("WORKER_AIR_GAPPED", "")
	os.Unsetenv("WORKER_AIR_GAPPED")
	if detectAirGapped() {
		t.Error("unset WORKER_AIR_GAPPED should be falsy")
	}
}

// runCount and detectPackageCount integrate with the real OS — the unit
// tests confirm the contract that 0 is returned cleanly for non-Linux or
// when no package manager is available, rather than panicking. Real
// counts are exercised in the integration smoke (manual test doc).
func TestRunCount_FailingCommand(t *testing.T) {
	n := runCount("/bin/nonexistent-binary", "anything")
	if n != 0 {
		t.Errorf("nonexistent binary should return 0, got %d", n)
	}
}

func TestRunCount_NonNumericOutput(t *testing.T) {
	n := runCount("sh", "-c", "echo not-a-number")
	if n != 0 {
		t.Errorf("non-numeric output should return 0, got %d", n)
	}
}

func TestRunCount_ValidNumber(t *testing.T) {
	n := runCount("sh", "-c", "echo 42")
	if n != 42 {
		t.Errorf("'42' should parse to 42, got %d", n)
	}
}

func TestDetectSystemState_DoesNotPanic(t *testing.T) {
	// Smoke: just confirm DetectSystemState returns without panicking on
	// the test runner's host (which has /etc/os-release on Linux but
	// might not in some CI environments).
	st := DetectSystemState()
	// All fields are valid zero values when undetected.
	_ = st.KernelVersion
	_ = st.OSDistro
	_ = st.OSVersion
	_ = st.PackageCount
	_ = st.AirGapped
}
