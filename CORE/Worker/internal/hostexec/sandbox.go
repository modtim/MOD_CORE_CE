//go:build linux

// Package hostexec: sandboxed host mode for shared workers (opsx worker-host-exec
// hx.17). A shared worker runs other tenants' jobs, so its host execs must never
// touch worker scratch, credentials, or other tenants' dirs. This wraps argv in a
// transient unprivileged systemd-run unit: DynamicUser=yes (the command NEVER runs
// as root - there is deliberately no RunAs/root option here; root is not something
// this wrapper can honour, so callers must not ask for it), ProtectSystem=strict +
// ProtectHome + PrivateTmp fence the OS, ReadWritePaths=<tenant workspace> is the
// only writable place, InaccessiblePaths keeps scratch/creds unreadable, and
// NoNewPrivileges closes the rest. On non-systemd hosts SandboxAvailable fails
// closed so the caller refuses instead of running unsandboxed.
package hostexec

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
)

// SystemdRunBin is the absolute path to systemd-run resolved from PATH at import
// time ("" when absent). Injectable: tests set it directly.
var SystemdRunBin = findSystemdRun()

// SystemUnitDir is the directory whose presence proves systemd owns PID 1.
// Injectable: tests point it at a real dir (present) or a bogus path (absent).
var SystemUnitDir = "/run/systemd/system"

// findSystemdRun scans PATH for an executable systemd-run.
func findSystemdRun() string {
	for _, dir := range strings.Split(os.Getenv("PATH"), ":") {
		if dir == "" {
			continue
		}
		cand := dir + "/systemd-run"
		st, err := os.Stat(cand)
		if err == nil && st.Mode()&0o111 != 0 {
			return cand
		}
	}
	return ""
}

// SandboxOptions describes a sandboxed host exec. There is no RunAs field:
// DynamicUser=yes always runs the command as a transient unprivileged user, so
// run-as-root is rejected by construction (no such option exists to set).
type SandboxOptions struct {
	Workspace string
	// Inaccessible: worker scratch / credential paths kept unreadable.
	// TimeoutS: RuntimeMaxSec; <=0 means no cap. Env: --setenv pairs.
	Inaccessible []string
	TimeoutS     int
	Env          map[string]string
}

// sandboxWorkspace validates the tenant workspace: non-empty, absolute, Clean()ed,
// no ".." component, and never "/" (the whole filesystem must not be writable).
func sandboxWorkspace(ws string) (string, error) {
	if ws == "" {
		return "", fmt.Errorf("workspace is empty: sandboxed host mode needs the tenant workspace")
	}
	if ws[0] != '/' {
		return "", fmt.Errorf("workspace %q is not absolute", ws)
	}
	if cleaned := filepath.Clean(ws); cleaned != ws {
		return "", fmt.Errorf("workspace %q is not Clean()ed (clean form is %q)", ws, cleaned)
	}
	for _, part := range strings.Split(ws, "/") {
		if part == ".." {
			return "", fmt.Errorf("workspace %q contains a .. component", ws)
		}
	}
	if ws == "/" {
		return "", fmt.Errorf("workspace %q is the whole filesystem: refusing to make / writable", ws)
	}
	return ws, nil
}

// sandboxPath validates an Inaccessible path: non-empty, absolute, Clean()ed,
// no ".." component.
func sandboxPath(p string) (string, error) {
	if p == "" {
		return "", fmt.Errorf("Inaccessible path is empty")
	}
	if p[0] != '/' {
		return "", fmt.Errorf("Inaccessible path %q is not absolute", p)
	}
	if cleaned := filepath.Clean(p); cleaned != p {
		return "", fmt.Errorf("Inaccessible path %q is not Clean()ed (clean form is %q)", p, cleaned)
	}
	for _, part := range strings.Split(p, "/") {
		if part == ".." {
			return "", fmt.Errorf("Inaccessible path %q contains a .. component", p)
		}
	}
	return p, nil
}

// envKeyOK reports whether k is a valid systemd --setenv variable name:
// non-empty, no '=', starts with a letter/underscore, alnum/underscore only.
func envKeyOK(k string) bool {
	if k == "" || strings.Contains(k, "=") {
		return false
	}
	first := k[0]
	if !((first >= 'a' && first <= 'z') || (first >= 'A' && first <= 'Z') || first == '_') {
		return false
	}
	for i := 0; i < len(k); i++ {
		c := k[i]
		ok := (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_'
		if !ok {
			return false
		}
	}
	return true
}

// envValueOK rejects env values containing a newline: a systemd --setenv pair is
// one argv element, so a newline would smuggle a second unit setting.
func envValueOK(v string) bool {
	return !strings.ContainsAny(v, "\r\n")
}

// SandboxAvailable fails closed: it returns an error unless systemd-run is on
// PATH AND SystemUnitDir exists. Callers MUST check it before execing, and
// refuse (never run unsandboxed) when it errors.
func SandboxAvailable() error {
	if SystemdRunBin == "" {
		return fmt.Errorf("systemd-run not found on PATH: sandboxed host mode unavailable; refusing to run unsandboxed")
	}
	_, err := os.Stat(SystemUnitDir)
	if err != nil {
		return fmt.Errorf("%s missing: host is not managed by systemd (PID 1); refusing to run unsandboxed", SystemUnitDir)
	}
	return nil
}

// SandboxArgv builds the full systemd-run argv for a sandboxed host exec. It
// does NOT exec anything. It fails closed first: when the host has no systemd-run
// (or no systemd-owned PID 1) it returns nil + error, never the raw argv, so a
// caller can never fall back to an unsandboxed exec.
func SandboxArgv(opts SandboxOptions, argv []string) ([]string, error) {
	if err := SandboxAvailable(); err != nil {
		return nil, err
	}
	if len(argv) == 0 {
		return nil, fmt.Errorf("argv is empty: nothing to sandbox")
	}
	if argv[0] == "" {
		return nil, fmt.Errorf("argv[0] is empty: nothing to exec")
	}
	ws, err := sandboxWorkspace(opts.Workspace)
	if err != nil {
		return nil, err
	}
	for _, p := range opts.Inaccessible {
		if _, err := sandboxPath(p); err != nil {
			return nil, err
		}
	}
	if opts.TimeoutS < 0 {
		return nil, fmt.Errorf("TimeoutS must be >= 0 (got %d)", opts.TimeoutS)
	}
	keys := make([]string, 0, len(opts.Env))
	for k := range opts.Env {
		if !envKeyOK(k) {
			return nil, fmt.Errorf("env key %q is not a valid systemd --setenv name", k)
		}
		if !envValueOK(opts.Env[k]) {
			return nil, fmt.Errorf("env value for key %q contains a newline", k)
		}
		keys = append(keys, k)
	}
	slices.Sort(keys)

	out := []string{
		"systemd-run", "--wait", "--pipe", "--collect", "--quiet",
		"-p", "DynamicUser=yes",
		"-p", "ProtectSystem=strict",
		"-p", "ProtectHome=yes",
		"-p", "PrivateTmp=yes",
		"-p", "NoNewPrivileges=yes",
		"-p", "ReadWritePaths=" + ws,
	}
	for _, p := range opts.Inaccessible {
		out = append(out, "-p", "InaccessiblePaths="+p)
	}
	if opts.TimeoutS > 0 {
		out = append(out, "-p", "RuntimeMaxSec="+strconv.Itoa(opts.TimeoutS))
	}
	for _, k := range keys {
		out = append(out, "--setenv="+k+"="+opts.Env[k])
	}
	out = append(out, "--working-directory="+ws, "--")
	out = append(out, argv...)
	return out, nil
}
