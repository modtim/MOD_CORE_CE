package heartbeat

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// ErrNotMeshParticipant is returned by a RehomeReceiver when this worker has no
// mesh client at all (e.g. the embedded worker, whose image ships no tailscale
// binary). It is not a failure to retry: there is nothing on this host to
// re-home, so handleMeshRehome latches the epoch in memory and logs once at
// INFO instead of logging an ERROR and retrying every heartbeat.
var ErrNotMeshParticipant = errors.New("not a mesh participant")

// lookPath is exec.LookPath, swappable in tests.
var lookPath = exec.LookPath

// RequireMeshBinary reports whether the mesh client binary is on PATH. When it
// is not, it returns an error wrapping ErrNotMeshParticipant, which a
// RehomeReceiver should return unchanged.
func RequireMeshBinary(name string) error {
	if _, err := lookPath(name); err != nil {
		return fmt.Errorf("%w: %s: %v", ErrNotMeshParticipant, name, err)
	}
	return nil
}

// MeshRehome is the manager's site-anchor re-home directive, delivered in the
// heartbeat response as `mesh_rehome` (mdns-name-discovery §3). It is the same
// pull model as `paused`/`drain`: every heartbeat carries it.
//
// A nil *MeshRehome (the field is absent / JSON null) means the manager is NOT
// driving re-homing — the field is flag-gated off on the manager — and the
// worker does nothing. Only a non-nil directive whose Epoch differs from the
// last one we acted on re-runs the site-anchor bring-up.
//
// Epoch is a STABLE short hash of the address the worker must dial (the manager
// derives it as a sha256 prefix of the site login server), so it changes EXACTLY
// when that address changes — an IP slide on a flat DHCP switch, or an IP→name
// switch. It is an opaque string; the worker never parses it, only compares it.
type MeshRehome struct {
	SiteLoginServer string `json:"site_login_server"`
	Epoch           string `json:"epoch"`
	Reason          string `json:"reason"`
}

// RehomeReceiver re-homes the site anchor to siteLoginServer. It is invoked ONLY
// when the epoch actually changes. Wired in main.go to run
//
//	tailscale --socket=<site sock> up --login-server=<siteLoginServer> \
//	          --force-reauth --accept-dns=true
//
// against the same site socket the enrollment brought the anchor up on. A
// returned error is treated as "re-home did not take": the epoch is NOT
// persisted, so the next heartbeat carrying the same epoch retries.
type RehomeReceiver func(siteLoginServer, reason string) error

// OnMeshRehome registers the site-anchor re-home executor and the file the
// last-applied epoch is persisted to (so a worker restart does not re-home
// needlessly when the address has not moved). Passing a nil fn — or never
// calling this — leaves the worker inert: it will read mesh_rehome and do
// nothing, which is the correct behaviour while the manager flag is off.
func (h *Heartbeat) OnMeshRehome(fn RehomeReceiver, statePath string) {
	h.rehomeMu.Lock()
	defer h.rehomeMu.Unlock()
	h.onRehome = fn
	h.rehomeStatePath = statePath
	h.rehomeEpoch = readRehomeEpoch(statePath)
}

// handleMeshRehome acts on the manager's directive. Guarded so it only acts on a
// real change:
//   - r == nil                 → manager isn't sending the field (flag off): no-op
//   - onRehome not wired        → nothing can re-home: no-op
//   - r.Epoch == last applied   → address unchanged since we last acted: no-op
//   - otherwise                 → re-run the site-anchor bring-up, then persist
//     the new epoch (only if the bring-up succeeded, so a
//     transient failure retries next beat)
func (h *Heartbeat) handleMeshRehome(r *MeshRehome) {
	if r == nil {
		return
	}

	h.rehomeMu.Lock()
	fn := h.onRehome
	if fn == nil {
		h.rehomeMu.Unlock()
		return
	}
	prev := h.rehomeEpoch
	if r.Epoch == prev {
		h.rehomeMu.Unlock()
		return
	}
	// Optimistically record the new epoch so a second heartbeat that arrives
	// while the (slow) bring-up is still running does not fire a second one.
	h.rehomeEpoch = r.Epoch
	statePath := h.rehomeStatePath
	h.rehomeMu.Unlock()

	h.log.Info("mesh_rehome: site-anchor address changed — re-homing",
		"prev_epoch", prev,
		"new_epoch", r.Epoch,
		"site_login_server", r.SiteLoginServer,
		"reason", r.Reason,
	)

	if err := fn(r.SiteLoginServer, r.Reason); err != nil {
		if errors.Is(err, ErrNotMeshParticipant) {
			// Nothing on this host can re-home. Keep the epoch latched in
			// memory so the same directive on the next beat is a no-op, but do
			// not persist it: if a mesh client is installed later, a restart
			// re-evaluates the directive and re-homes.
			h.log.Info("mesh_rehome: no tailscale binary; this worker is not on the mesh — ignoring re-home directives",
				"epoch", r.Epoch, "detail", err)
			return
		}
		// The bring-up did not take. Roll the applied epoch back to what it was
		// so the unchanged epoch on the next beat still reads as pending and we
		// retry, rather than latching a failed re-home.
		h.rehomeMu.Lock()
		if h.rehomeEpoch == r.Epoch { // don't clobber a newer epoch that raced in
			h.rehomeEpoch = prev
		}
		h.rehomeMu.Unlock()
		h.log.Error("mesh_rehome: site-anchor re-home failed; will retry next heartbeat",
			"epoch", r.Epoch, "error", err)
		return
	}

	writeRehomeEpoch(statePath, r.Epoch)
	h.log.Info("mesh_rehome: site anchor re-homed", "epoch", r.Epoch)
}

// readRehomeEpoch returns the last-applied epoch persisted at path, or "" when
// there is none (first ever run, or path unset). "" reads as "no epoch applied
// yet", so the first non-empty directive the manager sends is treated as a
// change and re-homes once.
func readRehomeEpoch(path string) string {
	if path == "" {
		return ""
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// writeRehomeEpoch persists the applied epoch. Best-effort: a failure to persist
// only means a restart may re-home once more, which is safe (force-reauth is
// idempotent), so it never breaks the heartbeat.
func writeRehomeEpoch(path, epoch string) {
	if path == "" {
		return
	}
	_ = os.MkdirAll(filepath.Dir(path), 0o755)
	_ = os.WriteFile(path, []byte(epoch), 0o644)
}
