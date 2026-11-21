package executor

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/docker/docker/api/types/container"
)

func TestHardenedSandboxEnabled_DefaultOn(t *testing.T) {
	t.Setenv("DISABLE_HARDENED_SANDBOX", "")
	if !hardenedSandboxEnabled() {
		t.Fatal("expected hardened sandbox enabled by default, got disabled")
	}
}

func TestHardenedSandboxEnabled_KillSwitch(t *testing.T) {
	t.Setenv("DISABLE_HARDENED_SANDBOX", "true")
	if hardenedSandboxEnabled() {
		t.Fatal("DISABLE_HARDENED_SANDBOX=true should disable hardening")
	}
	t.Setenv("DISABLE_HARDENED_SANDBOX", "TRUE")
	if hardenedSandboxEnabled() {
		t.Fatal("DISABLE_HARDENED_SANDBOX=TRUE (caps) should disable hardening")
	}
}

func TestHardenedSandboxEnabled_OtherValuesLeaveOn(t *testing.T) {
	for _, v := range []string{"false", "0", "yes", "on", "no"} {
		t.Setenv("DISABLE_HARDENED_SANDBOX", v)
		if !hardenedSandboxEnabled() {
			t.Fatalf("DISABLE_HARDENED_SANDBOX=%q should NOT disable hardening", v)
		}
	}
}

func TestApplyHardenedSandbox_SetsAllFieldsWhenEnabled(t *testing.T) {
	hc := &container.HostConfig{}
	applyHardenedSandbox(hc, true)

	if !hc.ReadonlyRootfs {
		t.Error("ReadonlyRootfs should be true after applyHardenedSandbox(true)")
	}
	if hc.Tmpfs["/tmp"] == "" {
		t.Error("Tmpfs /tmp should be populated")
	}
	if hc.Tmpfs["/var/tmp"] == "" {
		t.Error("Tmpfs /var/tmp should be populated")
	}
	if hc.Tmpfs["/run"] == "" {
		t.Error("Tmpfs /run should be populated")
	}
	if len(hc.Ulimits) == 0 {
		t.Fatal("Ulimits should contain at least the core=0 entry")
	}
	if hc.ShmSize != 1<<30 {
		t.Errorf("ShmSize should be 1 GB (1<<30 = %d), got %d", 1<<30, hc.ShmSize)
	}
	var foundCoreZero bool
	for _, u := range hc.Ulimits {
		if u.Name == "core" && u.Hard == 0 && u.Soft == 0 {
			foundCoreZero = true
		}
	}
	if !foundCoreZero {
		t.Error("Ulimits should include core=0/0")
	}
}

func TestApplyHardenedSandbox_NoOpWhenDisabled(t *testing.T) {
	hc := &container.HostConfig{}
	applyHardenedSandbox(hc, false)

	if hc.ReadonlyRootfs {
		t.Error("ReadonlyRootfs should stay false when hardening disabled")
	}
	if hc.Tmpfs != nil {
		t.Errorf("Tmpfs should be nil when hardening disabled, got %v", hc.Tmpfs)
	}
	if hc.Ulimits != nil {
		t.Errorf("Ulimits should be nil when hardening disabled, got %v", hc.Ulimits)
	}
	if hc.ShmSize != 0 {
		t.Errorf("ShmSize should be 0 when hardening disabled, got %d", hc.ShmSize)
	}
}

func TestApplyHardenedSandbox_NilSafe(t *testing.T) {
	applyHardenedSandbox(nil, true)
}

func TestApplyHardenedSandbox_PreservesExistingFields(t *testing.T) {
	hc := &container.HostConfig{
		CapDrop:     []string{"ALL"},
		Privileged:  false,
		SecurityOpt: []string{"no-new-privileges"},
	}
	applyHardenedSandbox(hc, true)

	if len(hc.CapDrop) != 1 || hc.CapDrop[0] != "ALL" {
		t.Error("CapDrop should not be touched by applyHardenedSandbox")
	}
	if hc.Privileged {
		t.Error("Privileged should not be flipped on")
	}
	// SecurityOpt should keep the existing no-new-privileges and may also
	// contain a seccomp=<path> entry if the embedded profile materialized.
	var hasNoNew bool
	for _, s := range hc.SecurityOpt {
		if s == "no-new-privileges" {
			hasNoNew = true
		}
	}
	if !hasNoNew {
		t.Error("SecurityOpt should retain no-new-privileges")
	}
}

func TestApplyHardenedSandbox_PreservesExistingShmSize(t *testing.T) {
	hc := &container.HostConfig{ShmSize: 512 * 1024 * 1024}
	applyHardenedSandbox(hc, true)

	if hc.ShmSize != 512*1024*1024 {
		t.Errorf("ShmSize should keep caller-provided value (512 MB), got %d", hc.ShmSize)
	}
}

func TestApplyHardenedSandbox_CapsShmSizeToMemoryLimit(t *testing.T) {
	hc := &container.HostConfig{
		Resources: container.Resources{Memory: 200 * 1024 * 1024},
	}
	applyHardenedSandbox(hc, true)

	want := int64(100 * 1024 * 1024)
	if hc.ShmSize != want {
		t.Errorf("ShmSize should be capped to half the memory limit (%d), got %d", want, hc.ShmSize)
	}
}

func TestApplyHardenedSandbox_NoMemoryCapAboveOneGB(t *testing.T) {
	hc := &container.HostConfig{
		Resources: container.Resources{Memory: 4 * 1024 * 1024 * 1024},
	}
	applyHardenedSandbox(hc, true)

	if hc.ShmSize != 1<<30 {
		t.Errorf("ShmSize should stay at the 1 GB default when memory limit exceeds it, got %d", hc.ShmSize)
	}
}

func TestSeccompProfile_Embedded(t *testing.T) {
	if len(embeddedSeccompProfile) == 0 {
		t.Fatal("embeddedSeccompProfile is empty — go:embed directive broken")
	}

	var profile map[string]any
	if err := json.Unmarshal(embeddedSeccompProfile, &profile); err != nil {
		t.Fatalf("embedded seccomp profile is not valid JSON: %v", err)
	}

	// GSEC FX.17 / R-068: the profile flipped from subtract-from-allow to a
	// default-deny allowlist derived from Docker/moby's default profile.
	// The posture assertion lives here; the allowlist/deny-set checks live
	// in seccomp_node_profile_test.go.
	if profile["defaultAction"] != "SCMP_ACT_ERRNO" {
		t.Errorf("expected defaultAction=SCMP_ACT_ERRNO (default-deny allowlist), got %v", profile["defaultAction"])
	}

	raw, _ := json.Marshal(profile["syscalls"])
	dump := string(raw)
	// Spot-check the deny set. If any of these go missing the sandbox
	// loses a known-bad escape syscall; fail loudly on regression.
	for _, syscallName := range []string{
		"keyctl", "add_key", "request_key",
		"bpf", "userfaultfd",
		"io_uring_setup", "io_uring_enter", "io_uring_register",
		"ptrace", "perf_event_open",
		"unshare", "clone",
	} {
		if !strings.Contains(dump, `"`+syscallName+`"`) {
			t.Errorf("seccomp profile missing deny for %q", syscallName)
		}
	}
}

func TestSeccompProfilePath_MaterializesFile(t *testing.T) {
	// Force re-materialization onto a tmpdir we control; safe because
	// the sync.Once is per-test-process in CI but this test asserts
	// file-contents correctness only on first observation anyway.
	path := seccompProfilePath()
	if path == "" {
		t.Skip("seccomp profile path unavailable in this test env (read-only fs)")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("cannot read materialized seccomp profile at %s: %v", path, err)
	}
	if len(data) == 0 {
		t.Fatal("materialized seccomp profile is empty")
	}
	if string(data) != string(embeddedSeccompProfile) {
		t.Error("materialized seccomp profile does not match the embedded bytes")
	}
}

func TestSelectRuntime_DefaultWhenNotRequested(t *testing.T) {
	t.Setenv("GVISOR_AVAILABLE", "true")
	rt, reason := SelectRuntime(false)
	if rt != "" {
		t.Errorf("runtime should be empty (default runc) when not requested; got %q", rt)
	}
	if reason == "" {
		t.Error("reason string should be populated for the audit log")
	}
}

func TestSelectRuntime_RequestedAndAvailable(t *testing.T) {
	t.Setenv("GVISOR_AVAILABLE", "true")
	rt, _ := SelectRuntime(true)
	if rt != "runsc" {
		t.Errorf("expected runsc when requested + available; got %q", rt)
	}
}

func TestSelectRuntime_RequestedButUnavailableFallsBackSilently(t *testing.T) {
	t.Setenv("GVISOR_AVAILABLE", "false")
	rt, reason := SelectRuntime(true)
	if rt != "" {
		t.Errorf("expected fallback to default runtime when gVisor unavailable; got %q", rt)
	}
	if reason == "" {
		t.Error("reason should explain the downgrade for operator visibility")
	}
}

func TestApplyHardenedSandbox_AppendsSeccompWhenAvailable(t *testing.T) {
	if len(embeddedSeccompProfile) == 0 {
		t.Skip("embedded seccomp profile is empty (embed broken in this env)")
	}
	hc := &container.HostConfig{SecurityOpt: []string{"no-new-privileges"}}
	applyHardenedSandbox(hc, true)

	// The seccomp entry must carry the JSON CONTENTS of the profile,
	// not a filesystem path. Docker's SDK parses the value after
	// `seccomp=` as JSON; a path like `/var/lib/…/seccomp-node.json`
	// produces:
	//   `Decoding seccomp profile failed: invalid character '/'
	//    looking for beginning of value`
	// (observed on staging task 6716, 2026-04-22).
	wantPrefix := "seccomp={"
	var foundSeccomp bool
	for _, s := range hc.SecurityOpt {
		if strings.HasPrefix(s, "seccomp=") {
			foundSeccomp = true
			if !strings.HasPrefix(s, wantPrefix) {
				t.Errorf("seccomp entry must start with %q (JSON content, not a path). Got %q", wantPrefix, s[:min(len(s), 60)])
			}
			// Must be the full embedded profile.
			if s != "seccomp="+string(embeddedSeccompProfile) {
				t.Errorf("seccomp payload should match the embedded profile bytes exactly")
			}
			// Path regression guard — no forward slash after `seccomp=` or
			// we're back to the broken behaviour.
			if strings.HasPrefix(s, "seccomp=/") {
				t.Error("seccomp entry must not be a filesystem path; Docker SDK requires JSON content")
			}
		}
	}
	if !foundSeccomp {
		t.Error("expected seccomp= entry in SecurityOpt after hardening")
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
