package heartbeat

import (
	"bytes"
	"errors"
	"log/slog"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// recordingRehomer captures the calls handleMeshRehome makes to the executor,
// so a test can assert whether a re-home fired and with what address.
type recordingRehomer struct {
	mu    sync.Mutex
	calls []MeshRehome // one entry per invocation (Reason carried through)
	err   error        // returned to simulate a failed bring-up
}

func (r *recordingRehomer) fn(siteLoginServer, reason string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls = append(r.calls, MeshRehome{SiteLoginServer: siteLoginServer, Reason: reason})
	return r.err
}

func (r *recordingRehomer) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.calls)
}

// newRehomeHarness builds a minimal Heartbeat wired for re-home, persisting its
// epoch under the test's temp dir so nothing touches the host.
func newRehomeHarness(t *testing.T, rec *recordingRehomer) *Heartbeat {
	t.Helper()
	h := &Heartbeat{log: testLogger()}
	statePath := filepath.Join(t.TempDir(), "mesh_rehome_epoch")
	h.OnMeshRehome(rec.fn, statePath)
	return h
}

// A changed epoch must re-home the site anchor exactly once, dialing the address
// the directive carried. This is the whole point of the feature: the manager's
// site-anchor address slid, and the worker follows it with no re-enrollment.
func TestMeshRehome_EpochChangeTriggers(t *testing.T) {
	rec := &recordingRehomer{}
	h := newRehomeHarness(t, rec)

	h.handleMeshRehome(&MeshRehome{
		SiteLoginServer: "https://mod-manager.local:8443",
		Epoch:           "a1b2c3d4e5f60718",
		Reason:          "site-anchor-address",
	})

	if got := rec.count(); got != 1 {
		t.Fatalf("epoch change should re-home once, got %d calls", got)
	}
	if rec.calls[0].SiteLoginServer != "https://mod-manager.local:8443" {
		t.Fatalf("re-homed to wrong address: %q", rec.calls[0].SiteLoginServer)
	}
	if rec.calls[0].Reason != "site-anchor-address" {
		t.Fatalf("reason not carried through: %q", rec.calls[0].Reason)
	}
}

// An unchanged epoch must be a no-op. The manager sends the directive on EVERY
// heartbeat; re-homing each time would tear the site anchor down and back up
// every beat, which is exactly the churn the epoch guard exists to prevent.
func TestMeshRehome_UnchangedEpochNoOp(t *testing.T) {
	rec := &recordingRehomer{}
	h := newRehomeHarness(t, rec)

	dir := &MeshRehome{
		SiteLoginServer: "https://mod-manager.local:8443",
		Epoch:           "a1b2c3d4e5f60718",
		Reason:          "site-anchor-address",
	}
	h.handleMeshRehome(dir) // first time: re-homes
	h.handleMeshRehome(dir) // same epoch: no-op
	h.handleMeshRehome(dir) // same epoch: no-op

	if got := rec.count(); got != 1 {
		t.Fatalf("unchanged epoch must not re-home again; got %d calls", got)
	}
}

// A nil directive means the manager isn't driving re-homing (its flag is off, so
// it sends JSON null). The worker must do nothing — never touch the anchor.
func TestMeshRehome_NilDirectiveNoOp(t *testing.T) {
	rec := &recordingRehomer{}
	h := newRehomeHarness(t, rec)

	h.handleMeshRehome(nil)

	if got := rec.count(); got != 0 {
		t.Fatalf("nil mesh_rehome must be inert; got %d calls", got)
	}
}

// With no executor wired (OnMeshRehome never called), a directive must not panic
// and must do nothing — the pre-wiring / legacy state.
func TestMeshRehome_NoExecutorNoOp(t *testing.T) {
	h := &Heartbeat{log: testLogger()}
	h.handleMeshRehome(&MeshRehome{
		SiteLoginServer: "https://mod-manager.local:8443",
		Epoch:           "deadbeefdeadbeef",
	})
	// Reaching here without a panic is the assertion.
}

// A second, DIFFERENT epoch re-homes again: the address moved twice (e.g. an IP
// slide, then the IP→name switch), and each move must be followed.
func TestMeshRehome_SubsequentChangeReTriggers(t *testing.T) {
	rec := &recordingRehomer{}
	h := newRehomeHarness(t, rec)

	h.handleMeshRehome(&MeshRehome{SiteLoginServer: "https://10.0.0.5:8443", Epoch: "1111111111111111"})
	h.handleMeshRehome(&MeshRehome{SiteLoginServer: "https://mod-manager.local:8443", Epoch: "2222222222222222"})

	if got := rec.count(); got != 2 {
		t.Fatalf("two distinct epochs should re-home twice; got %d calls", got)
	}
	if rec.calls[1].SiteLoginServer != "https://mod-manager.local:8443" {
		t.Fatalf("second re-home dialed wrong address: %q", rec.calls[1].SiteLoginServer)
	}
}

// A failed bring-up must NOT latch the epoch: the next heartbeat carrying the
// same (still-current) epoch has to retry, or a transient tailscale failure
// would strand the worker on the old address forever.
func TestMeshRehome_FailureRetriesNextBeat(t *testing.T) {
	rec := &recordingRehomer{err: errors.New("tailscaled not reachable")}
	h := newRehomeHarness(t, rec)

	dir := &MeshRehome{SiteLoginServer: "https://mod-manager.local:8443", Epoch: "aaaabbbbccccdddd"}
	h.handleMeshRehome(dir) // fails
	if got := rec.count(); got != 1 {
		t.Fatalf("expected one attempt, got %d", got)
	}

	rec.err = nil           // tailscaled recovers
	h.handleMeshRehome(dir) // same epoch — must retry because the first failed
	if got := rec.count(); got != 2 {
		t.Fatalf("failed re-home must retry on the next beat; got %d calls", got)
	}

	// Now that it succeeded, the epoch is latched: a further identical beat no-ops.
	h.handleMeshRehome(dir)
	if got := rec.count(); got != 2 {
		t.Fatalf("succeeded epoch must latch; got %d calls", got)
	}
}

// The applied epoch survives a restart: a worker that already re-homed to the
// current address must not re-home again just because its process restarted.
func TestMeshRehome_EpochPersistsAcrossRestart(t *testing.T) {
	statePath := filepath.Join(t.TempDir(), "mesh_rehome_epoch")
	dir := &MeshRehome{SiteLoginServer: "https://mod-manager.local:8443", Epoch: "persist0persist1"}

	rec1 := &recordingRehomer{}
	h1 := &Heartbeat{log: testLogger()}
	h1.OnMeshRehome(rec1.fn, statePath)
	h1.handleMeshRehome(dir)
	if rec1.count() != 1 {
		t.Fatalf("first process should re-home once; got %d", rec1.count())
	}

	// New process, same persisted state file.
	rec2 := &recordingRehomer{}
	h2 := &Heartbeat{log: testLogger()}
	h2.OnMeshRehome(rec2.fn, statePath)
	h2.handleMeshRehome(dir)
	if rec2.count() != 0 {
		t.Fatalf("restart with unchanged epoch must not re-home; got %d", rec2.count())
	}
}

// stubLookPath swaps the package lookPath for the duration of a test.
func stubLookPath(t *testing.T, fn func(string) (string, error)) {
	t.Helper()
	orig := lookPath
	lookPath = fn
	t.Cleanup(func() { lookPath = orig })
}

// meshRehomer mirrors main.go's wiring: check for the tailscale binary first,
// then "run" the bring-up, which returns upErr.
type meshRehomer struct {
	mu      sync.Mutex
	upCalls int
	upErr   error
}

func (m *meshRehomer) fn(siteLoginServer, reason string) error {
	if err := RequireMeshBinary("tailscale"); err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.upCalls++
	return m.upErr
}

// capturingHarness builds a re-home harness whose logs are captured.
func capturingHarness(t *testing.T, fn RehomeReceiver) (*Heartbeat, *bytes.Buffer) {
	t.Helper()
	buf := &bytes.Buffer{}
	h := &Heartbeat{log: slog.New(slog.NewTextHandler(buf, nil))}
	h.OnMeshRehome(fn, filepath.Join(t.TempDir(), "mesh_rehome_epoch"))
	return h, buf
}

// A worker with no tailscale binary (the embedded worker) is not on the mesh.
// The directive must be a quiet no-op: no ERROR, no retry every heartbeat, and
// the "not on the mesh" line logged once — not ~6 times a minute.
func TestMeshRehome_NoTailscaleBinaryIsQuietNoOp(t *testing.T) {
	stubLookPath(t, func(string) (string, error) { return "", exec.ErrNotFound })
	m := &meshRehomer{}
	h, logs := capturingHarness(t, m.fn)

	dir := &MeshRehome{SiteLoginServer: "https://mod-manager.local:8443", Epoch: "nobinary00000000"}
	for i := 0; i < 5; i++ {
		h.handleMeshRehome(dir)
	}

	if m.upCalls != 0 {
		t.Fatalf("tailscale up must not run without the binary; got %d calls", m.upCalls)
	}
	out := logs.String()
	if strings.Contains(out, "level=ERROR") {
		t.Fatalf("missing binary must not log ERROR:\n%s", out)
	}
	if n := strings.Count(out, "not on the mesh"); n != 1 {
		t.Fatalf("not-on-mesh line should be logged once, got %d:\n%s", n, out)
	}
	if n := strings.Count(out, "re-homing"); n != 1 {
		t.Fatalf("directive should be evaluated once, not every beat; got %d:\n%s", n, out)
	}
	h.rehomeMu.Lock()
	epoch := h.rehomeEpoch
	h.rehomeMu.Unlock()
	if epoch != dir.Epoch {
		t.Fatalf("epoch should be latched in memory; got %q", epoch)
	}
	if got := readRehomeEpoch(h.rehomeStatePath); got != "" {
		t.Fatalf("epoch must not be persisted when nothing re-homed; got %q", got)
	}
}

// With the binary present, a failing `tailscale up` is a real failure: it must
// still log ERROR and retry on the next heartbeat, as before.
func TestMeshRehome_BinaryPresentFailureStillRetries(t *testing.T) {
	stubLookPath(t, func(string) (string, error) { return "/usr/bin/tailscale", nil })
	m := &meshRehomer{upErr: errors.New("tailscale up (site re-home): exit status 1")}
	h, logs := capturingHarness(t, m.fn)

	dir := &MeshRehome{SiteLoginServer: "https://mod-manager.local:8443", Epoch: "failing000000000"}
	h.handleMeshRehome(dir)
	h.handleMeshRehome(dir)

	if m.upCalls != 2 {
		t.Fatalf("a failed tailscale up must retry next beat; got %d calls", m.upCalls)
	}
	out := logs.String()
	if n := strings.Count(out, "level=ERROR"); n != 2 {
		t.Fatalf("each failed attempt should log ERROR; got %d:\n%s", n, out)
	}
	if strings.Contains(out, "not on the mesh") {
		t.Fatalf("binary present must not be reported as not-on-mesh:\n%s", out)
	}
}

func TestRequireMeshBinary(t *testing.T) {
	stubLookPath(t, func(string) (string, error) { return "", exec.ErrNotFound })
	if err := RequireMeshBinary("tailscale"); !errors.Is(err, ErrNotMeshParticipant) {
		t.Fatalf("missing binary should wrap ErrNotMeshParticipant, got %v", err)
	}
	stubLookPath(t, func(string) (string, error) { return "/usr/bin/tailscale", nil })
	if err := RequireMeshBinary("tailscale"); err != nil {
		t.Fatalf("present binary should return nil, got %v", err)
	}
}
