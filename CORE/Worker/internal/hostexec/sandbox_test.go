//go:build linux

// Pre-existing gap fixed under 992f: this file exercises SandboxArgv's
// linux-only systemd-run wrapper (SystemdRunBin, sandbox.go), which has no
// counterpart on darwin/windows (sandbox_other.go's stub takes different
// inputs), so without this tag `go vet ./internal/hostexec/...` for those
// GOOS values fails on undefined: SystemdRunBin before it ever reaches the
// new runner_windows.go / runner_darwin.go files.
package hostexec

import (
	"strconv"
	"testing"
)

// sandboxWant builds the expected wrapper argv so tests assert the exact argv
// rather than substrings. envEntries are pre-formatted "--setenv=K=V" strings in
// the order they must appear.
func sandboxWant(ws string, inaccessible []string, timeoutS int, envEntries []string, argv []string) []string {
	out := []string{
		"systemd-run", "--wait", "--pipe", "--collect", "--quiet",
		"-p", "DynamicUser=yes",
		"-p", "ProtectSystem=strict",
		"-p", "ProtectHome=yes",
		"-p", "PrivateTmp=yes",
		"-p", "NoNewPrivileges=yes",
		"-p", "ReadWritePaths=" + ws,
	}
	for _, p := range inaccessible {
		out = append(out, "-p", "InaccessiblePaths="+p)
	}
	if timeoutS > 0 {
		out = append(out, "-p", "RuntimeMaxSec="+strconv.Itoa(timeoutS))
	}
	for _, e := range envEntries {
		out = append(out, e)
	}
	out = append(out, "--working-directory="+ws, "--")
	out = append(out, argv...)
	return out
}

func sameArgv(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// sandboxReady puts the package vars into the "host is systemd-managed" state so
// the building tests exercise the happy path.
func sandboxReady() {
	SystemdRunBin = "/usr/bin/systemd-run"
	SystemUnitDir = "/tmp"
}

func TestSandboxArgv_Full(t *testing.T) {
	sandboxReady()
	opts := SandboxOptions{
		Workspace:    "/srv/mod/tenant-1/ws",
		Inaccessible: []string{"/srv/mod/scratch", "/etc/mod/creds"},
		TimeoutS:     60,
		Env:          map[string]string{"B": "2", "A": "1"},
	}
	argv := []string{"ffmpeg", "-i", "in.mp4"}
	got, err := SandboxArgv(opts, argv)
	if err != nil {
		t.Fatalf("full opts must build: %v", err)
	}
	want := sandboxWant("/srv/mod/tenant-1/ws",
		[]string{"/srv/mod/scratch", "/etc/mod/creds"}, 60,
		[]string{"--setenv=A=1", "--setenv=B=2"}, argv)
	if !sameArgv(got, want) {
		t.Errorf("argv mismatch:\n got %v\nwant %v", got, want)
	}
}

func TestSandboxArgv_EnvSorted(t *testing.T) {
	sandboxReady()
	opts := SandboxOptions{Workspace: "/srv/mod/ws", Env: map[string]string{"Z": "z", "A": "a", "M": "m"}}
	got, err := SandboxArgv(opts, []string{"true"})
	if err != nil {
		t.Fatalf("env-only opts must build: %v", err)
	}
	seen := []string{}
	for _, a := range got {
		if len(a) > 9 && a[:9] == "--setenv=" {
			seen = append(seen, a)
		}
	}
	if !sameArgv(seen, []string{"--setenv=A=a", "--setenv=M=m", "--setenv=Z=z"}) {
		t.Errorf("env not sorted by key: %v", seen)
	}
}

func TestSandboxArgv_BadWorkspace(t *testing.T) {
	sandboxReady()
	bad := []string{"", "ws/relative", "/", "/srv/mod/../etc", "/srv/mod//ws", "/srv/mod/ws/"}
	for _, ws := range bad {
		got, err := SandboxArgv(SandboxOptions{Workspace: ws}, []string{"true"})
		if err == nil {
			t.Errorf("workspace %q must be rejected", ws)
		}
		if got != nil {
			t.Errorf("workspace %q must return nil argv, got %v", ws, got)
		}
	}
}

func TestSandboxArgv_EmptyArgv(t *testing.T) {
	sandboxReady()
	got, err := SandboxArgv(SandboxOptions{Workspace: "/srv/mod/ws"}, []string{})
	if err == nil {
		t.Errorf("empty argv must be rejected")
	}
	if got != nil {
		t.Errorf("empty argv must return nil argv, got %v", got)
	}
}

func TestSandboxArgv_FailClosedNoBinary(t *testing.T) {
	SystemdRunBin = ""
	SystemUnitDir = "/tmp"
	got, err := SandboxArgv(SandboxOptions{Workspace: "/srv/mod/ws"}, []string{"true"})
	if err == nil {
		t.Errorf("no systemd-run must fail closed")
	}
	if got != nil {
		t.Errorf("fail-closed must return nil argv, got %v", got)
	}
}

func TestSandboxArgv_FailClosedNoUnitDir(t *testing.T) {
	SystemdRunBin = "/usr/bin/systemd-run"
	SystemUnitDir = "/nonexistent"
	got, err := SandboxArgv(SandboxOptions{Workspace: "/srv/mod/ws"}, []string{"true"})
	if err == nil {
		t.Errorf("non-systemd host must fail closed")
	}
	if got != nil {
		t.Errorf("fail-closed must return nil argv, got %v", got)
	}
}

func TestSandboxArgv_ArgvAfterDashVerbatim(t *testing.T) {
	sandboxReady()
	argv := []string{"tar", "-p", "--wait", "-p", "ProtectSystem=nonexistent"}
	got, err := SandboxArgv(SandboxOptions{Workspace: "/srv/mod/ws"}, argv)
	if err != nil {
		t.Fatalf("argv after -- must build: %v", err)
	}
	dash := -1
	for i, a := range got {
		if a == "--" {
			dash = i
		}
	}
	if dash < 0 {
		t.Fatalf("no -- separator in %v", got)
	}
	if !sameArgv(got[dash+1:], argv) {
		t.Errorf("argv after -- must be verbatim, got %v", got[dash+1:])
	}
}
