package executor

import (
	"encoding/json"
	"testing"
)

// GSEC FX.17 / R-068 — the embedded node seccomp profile must be a
// default-DENY allowlist (Docker/moby default derived), not a
// subtract-from-allow profile. These tests pin that posture so a
// regression back to SCMP_ACT_ALLOW fails CI.

// modHardDenied is the unconditional (no args) part of MOD's 8 deny
// groups. These names must appear in NO SCMP_ACT_ALLOW rule. unshare and
// clone are NOT here: they are only denied with the CLONE_NEWUSER flag
// (args-matched), and Docker's default allowlist legitimately allows the
// flag-free form (thread creation).
var modHardDenied = map[string]bool{
	"keyctl":            true,
	"add_key":           true,
	"request_key":       true,
	"bpf":               true,
	"userfaultfd":       true,
	"io_uring_setup":    true,
	"io_uring_enter":    true,
	"io_uring_register": true,
	"ptrace":            true,
	"perf_event_open":   true,
}

// modDeniedGroups is the full 8-group deny set: every name must have at
// least one explicit SCMP_ACT_ERRNO rule (the arg-matched unshare/clone
// entries included), so the denials survive even if the default-action
// semantics ever change.
var modDeniedGroups = [][]string{
	{"keyctl", "add_key", "request_key"},
	{"bpf"},
	{"userfaultfd"},
	{"io_uring_setup", "io_uring_enter", "io_uring_register"},
	{"ptrace"},
	{"perf_event_open"},
	{"unshare"},
	{"clone"},
}

type seccompRule struct {
	Names  []string `json:"names"`
	Action string   `json:"action"`
}

func loadEmbeddedNodeProfile(t *testing.T) (defaultAction string, archs map[string]bool, rules []seccompRule) {
	t.Helper()
	if len(embeddedSeccompProfile) == 0 {
		t.Fatal("embeddedSeccompProfile is empty — go:embed directive broken")
	}
	var profile struct {
		DefaultAction string `json:"defaultAction"`
		ArchMap       []struct {
			Architecture string `json:"architecture"`
		} `json:"archMap"`
		Syscalls []seccompRule `json:"syscalls"`
	}
	if err := json.Unmarshal(embeddedSeccompProfile, &profile); err != nil {
		t.Fatalf("embedded node seccomp profile is not valid JSON: %v", err)
	}
	a := map[string]bool{}
	for _, am := range profile.ArchMap {
		a[am.Architecture] = true
	}
	return profile.DefaultAction, a, profile.Syscalls
}

func TestSeccompNodeProfile_DefaultDeny(t *testing.T) {
	defaultAction, _, _ := loadEmbeddedNodeProfile(t)
	if defaultAction != "SCMP_ACT_ERRNO" {
		t.Errorf("node seccomp profile must be default-deny (SCMP_ACT_ERRNO), got %q", defaultAction)
	}
}

func TestSeccompNodeProfile_ArchMapCoversWorkerArches(t *testing.T) {
	_, archs, _ := loadEmbeddedNodeProfile(t)
	for _, arch := range []string{"SCMP_ARCH_X86_64", "SCMP_ARCH_AARCH64"} {
		if !archs[arch] {
			t.Errorf("archMap must cover %s (worker builds are amd64/arm64)", arch)
		}
	}
}

func TestSeccompNodeProfile_ModDeniedAbsentFromAllowRules(t *testing.T) {
	_, _, rules := loadEmbeddedNodeProfile(t)
	for _, r := range rules {
		if r.Action != "SCMP_ACT_ALLOW" {
			continue
		}
		for _, name := range r.Names {
			if modHardDenied[name] {
				t.Errorf("MOD-denied syscall %q appears in an ALLOW rule — it must stay denied", name)
			}
		}
	}
}

func TestSeccompNodeProfile_ModDenyEntriesPresent(t *testing.T) {
	_, _, rules := loadEmbeddedNodeProfile(t)
	explicitlyDenied := map[string]bool{}
	for _, r := range rules {
		if r.Action != "SCMP_ACT_ERRNO" {
			continue
		}
		for _, name := range r.Names {
			explicitlyDenied[name] = true
		}
	}
	for _, group := range modDeniedGroups {
		for _, name := range group {
			if !explicitlyDenied[name] {
				t.Errorf("no explicit SCMP_ACT_ERRNO entry for MOD-denied syscall %q (deny group %v)", name, group)
			}
		}
	}
}

func TestSeccompNodeProfile_CoreSyscallsAllowed(t *testing.T) {
	_, _, rules := loadEmbeddedNodeProfile(t)
	allowed := map[string]bool{}
	for _, r := range rules {
		if r.Action != "SCMP_ACT_ALLOW" {
			continue
		}
		for _, name := range r.Names {
			allowed[name] = true
		}
	}
	for _, core := range []string{"read", "write", "openat", "mmap", "clone", "futex", "execve", "ioctl"} {
		if !allowed[core] {
			t.Errorf("core syscall %q must be in the allowlist (ML/CUDA/ROCm node workloads need it)", core)
		}
	}
}
