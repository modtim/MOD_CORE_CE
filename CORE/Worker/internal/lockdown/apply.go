// The exec half of the worker-managed host lockdown (design.md H2/H3, C31).
//
// rules.go renders the policy; this file owns everything that can touch the
// outside world: nft, /proc and PATH. All of it goes through three seams -
// Runner (process exec), ReadFile and LookPath - so the unit tests drive the
// whole probe/apply/verify/remove state machine without a root, an nft or a
// /proc. ExecRunner is the ONLY real-exec code in this file.
package lockdown

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"strconv"
	"strings"
)

// HostLockdown states reported in the heartbeat's egress_mediation (H3).
const (
	Enforced            = "enforced"
	Partial             = "partial"
	Unsupported         = "unsupported"
	Off                 = "off"
	Errored             = "error"
	HostScope           = "host"
	WSLScope            = "wsl-vm"
	ProbeTableName      = "mod_lockdown_probe"
	NetAdminBit         = 12
	ProcStatusPath      = "/proc/self/status"
	ProcOSReleasePath   = "/proc/sys/kernel/osrelease"
	ProcVersionPath     = "/proc/version"
	NftProgram          = "nft"
	DockerProgram       = "docker"
	DockerInfoFormat    = "--format={{.OperatingSystem}}"
	DockerDesktopMarker = "Docker Desktop"
	ApplyFileArg        = "-f"
	ApplyStdinArg       = "-"
	JSONListFlag        = "-j"
	ListWord            = "list"
	TableWord           = "table"
	ProbeTable          = TableFamily + " " + ProbeTableName
	LockdownTable       = TableFamily + " " + TableName
	NoSuchFileMarker    = "No such file or directory"
	NoSuchTableMarker   = "does not exist"
	DeleteVerb          = "delete"
	AddVerb             = "add"
)

// OSReleasePaths carry the WSL marker on either file depending on WSL version
// and distro (the same two files capabilities/platform.go reads; the check is
// copied here because that package's isWSL is unexported).
var OSReleasePaths = []string{ProcOSReleasePath, ProcVersionPath}

// WSLMarkers appear in osrelease/version on any WSL bridge, never on bare metal.
var WSLMarkers = []string{"microsoft", "wsl"}

// Status is the heartbeat's egress_mediation.host_lockdown/scope/detail triple.
type Status struct {
	HostLockdown string
	Scope        string
	Detail       string
}

// Runner is the process-exec seam: stdin is fed to the child ("" means the child
// gets no stdin at all), stdout is returned. Tests supply a fake that records
// calls and returns scripted output.
type Runner interface {
	// Run executes name+args with stdin on standard input and returns stdout.
	// argv is passed verbatim - no shell, no globbing.
	Run(ctx context.Context, stdin string, name string, args ...string) (stdout string, err error)
}

// ExecRunner is the real Runner: os/exec, nothing else.
type ExecRunner struct{}

func (r *ExecRunner) Run(ctx context.Context, stdin string, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	if stdin != "" {
		// Empty stdin leaves Cmd.Stdin nil, i.e. /dev/null: nft never blocks on a
		// pipe it was not handed one.
		cmd.Stdin = bytes.NewReader([]byte(stdin))
	}
	out, err := cmd.Output()
	if err != nil {
		// nft writes its diagnosis to stderr; surface it so callers can tell a
		// missing table from a real failure.
		if e, ok := err.(*exec.ExitError); ok && len(e.Stderr) > 0 {
			return "", errors.New(strings.TrimSpace(string(e.Stderr)))
		}
		// exec.LookPath-style failures (binary missing, spawn failure) come back
		// as plain errors, not ExitError.
		return "", err
	}
	return string(out), nil
}

// Manager is the lockdown state machine plus its seams. lastHash is the
// normalised listing of the last policy this manager applied, and expectPolicy
// is the digest of Render(that same policy); Verify compares the live table
// against lastHash AND the policy it was handed against expectPolicy, which is
// how drift is detected (C32a). Both are process-local caches, never the source
// of truth: the live ruleset is re-read on every Verify, and a fresh process
// (empty state) fails closed rather than reporting OK from memory.
type Manager struct {
	R Runner
	// ReadFile is the /proc seam (never a real /proc in tests).
	ReadFile func(string) ([]byte, error)
	// LookPath is the PATH seam (where is nft?).
	LookPath func(string) (string, error)
	lastHash string
	// expectPolicy is the digest of Render(p) for the last p this manager
	// applied; Verify checks its policy argument against it so a caller cannot
	// get "enforced" for a policy that was never applied.
	expectPolicy string
}

// DefaultManager wires the real seams: os.ReadFile, exec.LookPath, ExecRunner.
func DefaultManager() *Manager {
	return &Manager{
		R:        new(ExecRunner),
		ReadFile: func(p string) ([]byte, error) { return os.ReadFile(p) },
		LookPath: func(name string) (string, error) { return exec.LookPath(name) },
	}
}

// nft runs one nft invocation through the Runner seam.
func (m *Manager) nft(ctx context.Context, stdin string, args []string) (string, error) {
	return m.R.Run(ctx, stdin, NftProgram, args...)
}

// hasNetAdmin reports CAP_NET_ADMIN (bit 12) in /proc/self/status CapEff.
func (m *Manager) hasNetAdmin(ctx context.Context) bool {
	data, err := m.ReadFile(ProcStatusPath)
	if err != nil {
		// No /proc (containerised worker, odd chroot): not a capability grant.
		return false
	}
	for _, line := range strings.Split(string(data), "\n") {
		trimmed := strings.TrimSpace(line)
		rest := strings.TrimPrefix(trimmed, "CapEff:")
		if rest == trimmed {
			continue
		}
		mask, err := strconv.ParseUint(strings.TrimSpace(rest), 16, 64)
		if err != nil {
			// A non-hex CapEff (kernel oddity) is not a capability grant.
			return false
		}
		return (mask>>NetAdminBit)&1 == 1
	}
	return false
}

// wslBridge is the copied ~10-line osrelease check: capabilities/platform.go has
// the same logic but its isWSL is unexported, so it is re-implemented here rather
// than reached into.
func (m *Manager) wslBridge(ctx context.Context) bool {
	for _, path := range OSReleasePaths {
		data, err := m.ReadFile(path)
		if err != nil {
			continue
		}
		lower := strings.ToLower(string(data))
		for _, marker := range WSLMarkers {
			if strings.Contains(lower, marker) {
				return true
			}
		}
	}
	return false
}

// dockerDesktop reports whether the daemon this worker talks to runs on a
// Windows/Mac host (H3: that host is unfirewallable, so lockdown is
// unsupported regardless of what the WSL/bare-metal probe says).
func (m *Manager) dockerDesktop(ctx context.Context) bool {
	out, err := m.R.Run(ctx, "", DockerProgram, "info", DockerInfoFormat)
	if err != nil {
		// No daemon reachable (k8s backend, or docker not installed): not
		// Docker Desktop.
		return false
	}
	return strings.Contains(out, DockerDesktopMarker)
}

// policyDigest is the sha256 (hex) of a rendered nft script. It is the identity
// of a Policy: two policies digest equal iff Render(p) is byte-identical, so
// Verify can honour its policy argument without a second rule generator (the
// apply path's renderer is the only source of rules).
func policyDigest(p Policy) string {
	sum := sha256.Sum256([]byte(Render(p)))
	return "sha256:" + hex.EncodeToString(sum[:])
}

// listingHash is the sha256 (hex) of a normalised nft listing.
func listingHash(listing string) string {
	sum := sha256.Sum256([]byte(listing))
	return "sha256:" + hex.EncodeToString(sum[:])
}

// normaliseListing makes a live `nft -j list table` dump comparable to itself
// across runs: nft stamps per-object handles and a metainfo block (which carry
// the table's creation bookkeeping) into every listing, so they are dropped and
// the remainder is re-encoded with sorted keys. Handles-only differences then
// compare equal - that is NOT drift.
func normaliseListing(raw string) string {
	var v any
	if err := json.Unmarshal([]byte(strings.TrimSpace(raw)), &v); err != nil {
		// Not JSON (older nft, or a wrapper script): fall back to the trimmed text so
		// the comparison is still self-consistent.
		return strings.TrimSpace(raw)
	}
	stripped := stripListing(v)
	out, err := json.Marshal(stripped)
	if err != nil {
		return strings.TrimSpace(raw)
	}
	return string(out)
}

// stripListing drops handle fields and the metainfo block recursively.
func stripListing(x any) any {
	if m, ok := x.(map[string]any); ok {
		for k, v := range m {
			if k == "handle" || k == "metainfo" {
				delete(m, k)
				continue
			}
			m[k] = stripListing(v)
		}
		return m
	}
	if l, ok := x.([]any); ok {
		for i := range len(l) {
			l[i] = stripListing(l[i])
		}
		return l
	}
	return x
}

// argvFor builds one nft invocation: verb(s) + the table spec.
func argvFor(verb string) []string { return []string{verb, TableWord, TableFamily, TableName} }

// probeArgvFor builds one nft invocation against the scratch probe table.
func probeArgvFor(verb string) []string {
	return []string{verb, TableWord, TableFamily, ProbeTableName}
}

// applyArgv is `nft -f -`: the script comes in on stdin.
func applyArgv() []string { return []string{ApplyFileArg, ApplyStdinArg} }

// listArgv is `nft -j list table inet mod_lockdown`.
func listArgv() []string { return []string{JSONListFlag, ListWord, TableWord, TableFamily, TableName} }

// deleteTableArgv is the break-glass `nft delete table inet mod_lockdown`.
func deleteTableArgv() []string { return argvFor(DeleteVerb) }

// probeCreateArgv / probeDeleteArgv are the scratch-table probe pair.
func probeCreateArgv() []string { return probeArgvFor(AddVerb) }
func probeDeleteArgv() []string { return probeArgvFor(DeleteVerb) }

// missingTableError recognises nft's "the table isn't there" diagnosis, which
// Remove treats as success (idempotent) and Verify as "not applied".
func missingTableError(err error) bool {
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, strings.ToLower(NoSuchFileMarker)) ||
		strings.Contains(msg, "no such table") ||
		strings.Contains(msg, "table does not exist")
}

// Probe answers "can this worker enforce host lockdown, and where does it act?"
// (H3). It never panics: every failure mode becomes a Status.
func (m *Manager) Probe(ctx context.Context) Status {
	if m.dockerDesktop(ctx) {
		return Status{
			HostLockdown: Unsupported,
			Scope:        HostScope,
			Detail:       "docker daemon reports Docker Desktop: the OS host is not firewalled from the worker",
		}
	}
	if !m.hasNetAdmin(ctx) {
		return Status{
			HostLockdown: Unsupported,
			Scope:        HostScope,
			Detail:       "no CAP_NET_ADMIN (bit 12) in " + ProcStatusPath,
		}
	}
	if _, err := m.LookPath(NftProgram); err != nil {
		return Status{
			HostLockdown: Unsupported,
			Scope:        HostScope,
			Detail:       "nft not on PATH",
		}
	}
	inWSL := m.wslBridge(ctx)
	scope := HostScope
	if inWSL {
		scope = WSLScope
	}
	if _, err := m.nft(ctx, "", probeCreateArgv()); err != nil {
		return Status{HostLockdown: Errored, Scope: scope, Detail: "probe create failed: " + err.Error()}
	}
	if _, err := m.nft(ctx, "", probeDeleteArgv()); err != nil {
		return Status{HostLockdown: Errored, Scope: scope, Detail: "probe delete failed: " + err.Error()}
	}
	if inWSL {
		return Status{
			HostLockdown: Partial,
			Scope:        WSLScope,
			Detail:       "nftables covers the WSL VM only; the Windows host is out of scope",
		}
	}
	return Status{HostLockdown: Enforced, Scope: HostScope, Detail: "CAP_NET_ADMIN + nft + scratch table ok"}
}

// Apply installs the policy atomically: Render already carries the delete+add
// preamble in one transaction, so a crash leaves the old table or the new one,
// never nothing (fail closed). The script is handed to `nft -f -` on stdin
// exactly once.
func (m *Manager) Apply(ctx context.Context, p Policy) error {
	if _, err := m.nft(ctx, Render(p), applyArgv()); err != nil {
		return err
	}
	listing, err := m.nft(ctx, "", listArgv())
	if err != nil {
		return err
	}
	m.lastHash = listingHash(normaliseListing(listing))
	m.expectPolicy = policyDigest(p)
	return nil
}

// Verify is the drift check against the policy the caller intends: the live
// table's normalised listing must hash to what this manager last applied, AND
// the policy argument must digest to the script that Apply rendered. A missing
// table is (false, nil) - not an error, just "not enforced". A policy argument
// that differs from the one applied is drift (false, nil): the live ruleset is
// not the ruleset this call asked about. lastHash/expectPolicy are caches only,
// never the source of truth: the live ruleset is re-read on every call, and a
// fresh process (empty state) fails closed instead of reporting OK from memory.
func (m *Manager) Verify(ctx context.Context, p Policy) (bool, error) {
	if m.expectPolicy == "" || policyDigest(p) != m.expectPolicy {
		// Nothing was applied, or the intended policy changed without a
		// re-Apply: drift, fail closed (no live read needed to say so).
		return false, nil
	}
	listing, err := m.nft(ctx, "", listArgv())
	if err != nil {
		if missingTableError(err) {
			return false, nil
		}
		// A live-read failure is not "enforced": surfaced as an error.
		return false, err
	}
	return m.lastHash != "" && listingHash(normaliseListing(listing)) == m.lastHash, nil
}

// Remove is the break-glass path (C28): delete the table, tolerating "it isn't
// there" so the call is idempotent.
func (m *Manager) Remove(ctx context.Context) error {
	if _, err := m.nft(ctx, "", deleteTableArgv()); err != nil {
		if missingTableError(err) {
			m.lastHash = ""
			m.expectPolicy = ""
			return nil
		}
		return err
	}
	m.lastHash = ""
	m.expectPolicy = ""
	return nil
}
