// stack-ha-reconciliation group 3: worker self-fencing.
//
// The safety property that makes re-placement correct. Without it, a manager
// that decides a worker is gone and re-places its stacks can end up with TWO
// live copies: the "gone" worker may be perfectly healthy and merely
// unreachable, still serving, still writing to its volumes. For anything
// holding data that is the failure mode that loses it.
//
// Fencing needs no fencing device, no consensus, and no inbound path. A
// partitioned worker knows it is isolated because its OWN outbound calls are
// failing — the same heartbeats it already sends. And a worker that cannot
// reach its manager cannot claim work either, so it has nothing to gain by
// staying up. It stops its stacks and waits.
//
// The ordering is the whole argument: the worker's lease must expire strictly
// BEFORE the manager's grace period, so by the time the manager is willing to
// re-place, the previous instance has already stopped itself. Those two
// numbers are therefore derived from ONE source (the manager's grace period,
// pushed down in the heartbeat response) rather than configured separately on
// each side, because two independently-set values eventually disagree, and the
// direction they disagree in decides whether you get split-brain.

package heartbeat

import (
	"errors"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"syscall"
	"time"
)

// StackFencer stops every stack this host is running and returns what it
// stopped. Wired in main.go to the executor; a func here so this package does
// not import executor.
type StackFencer func() (stopped []string, err error)

// StackResumer restarts, in place, exactly the compose projects the caller
// names — never a set it derives itself. Wired in main.go to the executor's
// `compose start` path (stackrunner.go `case "start":`), which is deliberately
// NOT `up`: start resumes the containers that `stop` left behind, keeping the
// compose project and its volumes, whereas `up`/`down` would recreate or
// destroy them. A func field here rather than an executor import, same
// rationale as StackFencer above.
type StackResumer func(projects []string) (started []string, err error)

// StackFencerScoped is the BUG-1028h J3 variant of StackFencer: it is handed
// the projects this worker ALREADY stopped earlier in the current fence
// episode, so a retry after a FAILED fence can skip re-issuing `compose stop`
// for them. Same "func field rather than an executor import" rationale as the
// two above — main.go wires whichever variant the backend can honour, and
// evaluateFence falls back to the plain StackFencer when only that is wired.
type StackFencerScoped func(skip []string) (stopped []string, err error)

// defaultFenceLeaseSeconds applies until the manager pushes a real value.
//
// Conservative on purpose, and low-risk: a worker that has never successfully
// reached a manager has never been given a stack to host either, so there is
// nothing for this default to stop prematurely.
const defaultFenceLeaseSeconds = 120

// minFenceLeaseSeconds floors whatever the manager pushes. A lease shorter
// than a couple of heartbeat intervals would fence on ordinary jitter — a
// single slow response would stop production stacks.
const minFenceLeaseSeconds = 30

// maxFenceJitterPct bounds the per-worker spread of the pushed lease
// (BUG-1028h J3). The whole-site case: one manager-derived lease expires on
// every worker at the same second, so the entire fleet fences in lockstep and
// every hosted stack on the site stops at once. Spreading each worker's lease
// staggers that. The bound exists because jitter only ever EXTENDS a lease,
// and the API clamps `lease * (1 + pct/100) <= grace - margin`
// (stack_observation.py) — the chain `lease_effective < grace < gap` is what
// guarantees the old copy stopped before re-placement. A pct past 25 would let
// a worker fence after the manager already re-placed: split-brain.
const maxFenceJitterPct = 25

// Retry pacing for a fence that FAILED outright (§3.7).
//
// A failed fence used to be terminal: `fenced` was latched before the fencer
// ran, so an error left the flag set and nothing ever tried again. Measured
// 2026-08-01 — the fencer could not enumerate containers, logged "fence FAILED
// — stacks may still be running on this host", and went quiet. The manager
// then re-placed at gap grace against stacks that had never stopped, which is
// precisely the split-brain this whole file exists to prevent.
//
// Retrying is safe in the direction that matters: `compose stop` against an
// already-stopped project is a no-op, so re-attempting costs a wasted command,
// while not re-attempting costs two live copies of a stack holding data. The
// backoff exists only so a permanently broken Docker socket does not hammer
// itself every tick — it is a courtesy to the host, not a correctness rule.
const (
	initialFenceBackoff = 15 * time.Second
	maxFenceBackoff     = 5 * time.Minute
)

// fenceBackoff grows the retry interval with consecutive failures.
func fenceBackoff(attempts int64) time.Duration {
	if attempts < 1 {
		attempts = 1
	}
	d := initialFenceBackoff
	for i := int64(1); i < attempts && d < maxFenceBackoff; i++ {
		d *= 2
	}
	if d > maxFenceBackoff {
		d = maxFenceBackoff
	}
	return d
}

// shouldFence decides whether the lease has expired.
//
// Pure so the arithmetic is testable without a partitioned network — which is
// the one thing that cannot be reproduced on a single-box deployment where the
// worker shares a network with its manager.
//
// `lastContact` zero means this worker has never reached a manager. That does
// NOT fence: it is a worker still starting up, and it cannot be hosting stacks
// a manager gave it if it has never spoken to one.
func shouldFence(lastContact, now time.Time, lease time.Duration, alreadyFenced bool) bool {
	if alreadyFenced || lease <= 0 || lastContact.IsZero() {
		return false
	}
	return now.Sub(lastContact) > lease
}

// noteContact records a successful manager round-trip.
func (h *Heartbeat) noteContact(now time.Time) {
	h.lastContactUnix.Store(now.Unix())
}

// countsAsLostContact classifies one heartbeat POST outcome for fencing
// purposes (2026-09-28 incident: a routine `docker restart my_fastapi_app`
// made the API unreachable for ~110-125s; every worker's lease expired and
// fenced every hosted stack fleet-wide).
//
// The fence clock exists to catch a REAL partition: the worker cannot reach
// the manager AT ALL, so the manager's reconciler — which needs that same
// unreachability to decide this worker is gone — might be re-placing its
// stacks elsewhere. The clock must NOT advance just because the manager's API
// PROCESS is down while its HOST still answers: the front proxy (Caddy)
// still serves a 502/503/504 while the upstream restarts, and the OS still
// completes (or actively refuses/resets) the TCP handshake even when nothing
// is listening yet. In both cases the manager host is up, its reconciler is
// either not running (nothing to re-place with) or about to come back
// exactly where it left off — there is no window for two live copies.
//
// resp != nil means an HTTP response of ANY status arrived from the manager
// URL. That is contact, full stop, regardless of status code — a 502 during
// an API restart counts exactly the same as a 200.
//
// Only genuine network-level unreachability — dial timeout, no route to
// host, i/o timeout, DNS failure for the manager host, TLS handshake
// timeout — advances the lease. A refused or reset connection does NOT: the
// manager host's kernel answered, it just has nothing listening on that port
// yet (the common shape of an API process mid-restart behind no proxy, or a
// proxy that itself dropped the connection rather than 502'ing it).
//
// Pure and independent of any live socket so it is unit-testable without a
// partitioned network, matching shouldFence's rationale above.
func countsAsLostContact(err error, resp *http.Response) bool {
	if resp != nil {
		// A response arrived — 2xx, 4xx, or 5xx — so the manager HOST answered.
		return false
	}
	if err == nil {
		return false
	}

	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		return true // DNS failure for the manager host: cannot even resolve it.
	}

	var opErr *net.OpError
	if errors.As(err, &opErr) {
		switch {
		case errors.Is(opErr.Err, syscall.ECONNREFUSED):
			// Host reachable; nothing listening (API process mid-restart). Contact.
			return false
		case errors.Is(opErr.Err, syscall.ECONNRESET):
			// Host reachable; it actively tore down the connection. Contact.
			return false
		case errors.Is(opErr.Err, syscall.EHOSTUNREACH), errors.Is(opErr.Err, syscall.ENETUNREACH):
			return true // no route to host: genuinely unreachable.
		}
		if opErr.Timeout() {
			return true // dial timeout / i/o timeout: no answer at all.
		}
	}

	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return true
	}

	// Fallback string checks for errors the stdlib does not expose as typed
	// values on every platform (notably TLS handshake timeout).
	msg := err.Error()
	switch {
	case strings.Contains(msg, "TLS handshake timeout"):
		return true
	case strings.Contains(msg, "no such host"):
		return true
	case strings.Contains(msg, "connection refused"):
		return false
	case strings.Contains(msg, "connection reset"):
		return false
	}

	// An error shape this function does not recognise. Conservative default
	// matches the PRE-fix behaviour (any transport error advanced the lease):
	// treat the unknown case as a possible real partition rather than
	// silently disabling fencing for it.
	return true
}

// applyHeartbeatResult updates fence/contact state from one heartbeat
// attempt's outcome. Split out of send() so the fencing decision — the part
// that actually matters here — is callable from a test without a live
// socket.
func (h *Heartbeat) applyHeartbeatResult(now time.Time, resp *http.Response, err error) {
	// The manager's placement confirmation for THIS beat, stashed by sendAPI
	// when the response decoded (`stack_resume_projects`, produced by the API
	//'s J2 fast path). Consumed here — and cleared here, whatever the outcome —
	// so a stale list can never be carried into a later beat.
	confirmed := h.takeResumeCandidates()
	if err == nil {
		h.noteContact(now)
		// 2400BUG-1028h J1 — the resume fast path. noteFenceRecovered returns
		// true only when it actually cleared a `fenced=true` episode, i.e. this
		// worker really did stop stacks during a real partition and contact has
		// now genuinely returned. A resume that did not clear anything (never
		// fenced, or a fence that FAILED and left `fenced` false) restarts
		// nothing: there is nothing this worker stopped, and starting what is
		// already running is not our call to make.
		if h.noteFenceRecovered() {
			h.resumeFenceStacks(confirmed)
		}
		return
	}
	if !countsAsLostContact(err, resp) {
		// Manager host reachable (its API is down/restarting, or refused/reset
		// the connection) — contact for fence purposes. Reset the clock but do
		// NOT clear an existing fenced state: only a fully successful heartbeat
		// (this worker's own round-trip actually completing) proves the manager
		// is back and reconciling, which is what noteFenceRecovered promises.
		h.noteContact(now)
		return
	}
	h.evaluateFence(now)
}

// SetFenceLease records the lease the manager derived from its own grace
// period. Clamped, never zeroed by a malformed push — a zero lease disables
// fencing, which must not be reachable by a bad server response.
func (h *Heartbeat) SetFenceLease(seconds int) {
	if seconds < minFenceLeaseSeconds {
		seconds = minFenceLeaseSeconds
	}
	h.fenceLeaseS.Store(int64(seconds))
}

// FenceLease returns the current lease window, INCLUDING this worker's jitter
// (BUG-1028h J3). Jitter only ever EXTENDS the lease — never shortens it — so
// a worker can fence LATER than the manager's base lease, never earlier.
func (h *Heartbeat) FenceLease() time.Duration {
	s := h.fenceLeaseS.Load()
	if s <= 0 {
		s = defaultFenceLeaseSeconds
	}
	return time.Duration(s + fenceJitterSeconds(s, h.fenceJitterPct.Load(),
		fenceSeedFor(h))) * time.Second
}

// SetFenceJitterPct records the per-worker lease spread the manager pushed
// (BUG-1028h J3). Clamped to [0, maxFenceJitterPct]: the manager owns the
// chain `lease_effective < grace < gap` (stack_observation.py clamps
// lease*(1+pct/100) to grace-margin), and a worker that accepted a bigger
// spread than the manager accounted for would fence after the manager had
// already re-placed — split-brain. Absent/0 = no jitter, unchanged behaviour.
func (h *Heartbeat) SetFenceJitterPct(pct int) {
	if pct < 0 {
		pct = 0
	}
	if pct > maxFenceJitterPct {
		pct = maxFenceJitterPct
	}
	h.fenceJitterPct.Store(int64(pct))
}

// fenceSeedFor is the deterministic per-worker seed: the server-assigned
// WorkerID when known (heartbeat.go), else a hash of the worker name.
// Deterministic on purpose — a restarted worker must keep the SAME jitter, so
// one host cannot fence at the base lease on one boot and late on the next.
func fenceSeedFor(h *Heartbeat) uint64 {
	name := ""
	if h.cfg != nil {
		name = h.cfg.WorkerName
	}
	return fenceSeed(name, WorkerID())
}

func fenceSeed(name string, id int64) uint64 {
	h := uint64(1469598125539394664)
	for _, c := range []byte(name) {
	 h = (h ^ uint64(c)) * 1099511628211
	}
	h ^= uint64(id)
	h ^= h >> 33
	h *= 0xff51afd7ed558ccd
	h ^= h >> 33
	return h
}

func fenceJitterSeconds(leaseSeconds, pct int64, seed uint64) int64 {
	if pct <= 0 {
		return 0
	}
	if pct > maxFenceJitterPct {
		pct = maxFenceJitterPct
	}
	span := leaseSeconds * pct / 100
	if span <= 0 {
		return 0
	}
	return int64(seed % uint64(span + 1))
}

// OnFenceStacks registers the callback that stops hosted stacks.
func (h *Heartbeat) OnFenceStacks(fn StackFencer) { h.fencer = fn }

// OnFenceStacksScoped registers the retry-aware variant (BUG-1028h J3): it is
// handed the projects this worker ALREADY stopped earlier in the current fence
// episode, so a retry after a FAILED fence acts only on what is still running
// instead of re-issuing `compose stop` over the whole host set. Wired
// INSTEAD OF (or in addition to) OnFenceStacks in main.go; when only the plain
// fencer is wired, evaluateFence keeps the old whole-host-set behaviour.
func (h *Heartbeat) OnFenceStacksScoped(fn StackFencerScoped) {
	h.ScopedFencer = fn
}

// alreadyStopped is this episode's stopped set — what the retry may skip.
func (h *Heartbeat) alreadyStopped() []string {
	cur := h.fencedStacks.Load()
	if cur == nil {
		return []string{}
	}
	return *cur
}

// OnResumeStacks registers the callback that restarts fenced stacks in place
// (2400BUG-1028h J1). Paired with OnFenceStacks in main.go; nil means this
// worker cannot resume anything and the reconciler's sweep stays the fallback.
func (h *Heartbeat) OnResumeStacks(fn StackResumer) { h.resumer = fn }

// stashResumeCandidates records the `stack_resume_projects` list the manager
// sent in the heartbeat response that is about to be applied. sendAPI decodes
// the response and applyHeartbeatResult consumes it on the SAME tick, so the
// confirmation is always exactly one beat old — never a stale list from an
// earlier contact.
func (h *Heartbeat) stashResumeCandidates(projects []string) {
	if len(projects) == 0 {
		h.resumeCandidates.Store(nil)
		return
	}
	var copy []string
	for _, p := range projects {
		p = strings.TrimSpace(p)
		if p != "" {
			copy = append(copy, p)
		}
	}
	if len(copy) > 0 {
		h.resumeCandidates.Store(&copy)
		return
	}
	h.resumeCandidates.Store(nil)
}

// takeResumeCandidates returns and clears this beat's confirmation. Absent/
// empty ⇒ nil, which resumeFenceStacks treats as "the manager named nothing"
// — the fail-closed default.
func (h *Heartbeat) takeResumeCandidates() []string {
	p := h.resumeCandidates.Swap(nil)
	if p == nil {
		return []string{}
	}
	return *p
}

// Fenced reports whether this worker has fenced and not yet re-established
// contact.
func (h *Heartbeat) Fenced() bool { return h.fenced.Load() }

// evaluateFence runs after every heartbeat attempt, successful or not.
//
// Deliberately driven by the heartbeat loop rather than its own timer: the
// thing being measured is heartbeat success, so evaluating on that same tick
// means there is no second clock to fall out of step with.
func (h *Heartbeat) evaluateFence(now time.Time) {
	last := time.Unix(h.lastContactUnix.Load(), 0)
	if h.lastContactUnix.Load() == 0 {
		last = time.Time{}
	}
	if !shouldFence(last, now, h.FenceLease(), h.fenced.Load()) {
		return
	}

	// Space out retries after a failure. Zero on the first attempt, so the
	// original behaviour — fence the moment the lease expires — is unchanged.
	if next := h.nextFenceAttemptUnix.Load(); next > 0 && now.Unix() < next {
		return
	}

	// Stamped at the START of a fence EPISODE — the first attempt after contact
	// was lost. Deliberately neither of the two easy alternatives: restamping
	// every attempt would move the outage time forward on each retry and lose
	// when it actually began, while stamping only once for the worker's whole
	// lifetime would report a partition from months ago as the cause of
	// today's. An episode ends at noteFenceRecovered, which resets `attempts`.
	if h.fenceAttempts.Load() == 0 && !h.fenced.Load() {
		h.fencedAtUnix.Store(now.Unix())
		h.fencedStacks.Store(nil) // previous episode's list is not this one's
	}

	// BUG-1028h J3 — the retry guard. A SUCCESSFUL fence already returns early
	// above (`shouldFence` short-circuits on `alreadyFenced`), so the only way
	// here is the first attempt of an episode or a retry after a FAILED one.
	// On a retry, the projects this worker already stopped this episode are no
	// longer running; the plain fencer would re-enumerate the whole host set
	// and re-issue `compose stop` for them. When the backend can honour a
	// named set, hand it what is already stopped and let it act on the rest.
	already := h.alreadyStopped()
	if h.fencer == nil && h.ScopedFencer == nil {
		h.log.Warn("fence lease expired but no fencer wired; stacks left running",
			"lease", h.FenceLease(), "last_contact", last)
		return
	}
	var stopped []string
	var err error
	if h.ScopedFencer != nil {
		stopped, err = h.ScopedFencer(already)
	} else {
		stopped, err = h.fencer()
	}
	// Recorded even on a partial failure: whatever DID stop is real, and the
	// manager needs it to tell "this host stopped two of three" from "this host
	// did nothing at all". Accumulated across attempts rather than replaced —
	// a retry only ever sees what is still RUNNING, so the projects stopped on
	// the first attempt are absent from the second one's return value, and
	// overwriting would quietly shrink the list as the fence succeeded.
	h.recordStopped(stopped)
	if err != nil {
		// NOT latched. `fenced` stays false because it is not true — containers
		// may still be serving here, and a host that reports itself fenced
		// while still running stacks is the one lie this protocol cannot
		// tolerate. The manager re-places on grace expiry regardless, so
		// claiming success buys nothing and hides a split-brain.
		attempts := h.fenceAttempts.Add(1)
		backoff := fenceBackoff(attempts)
		h.nextFenceAttemptUnix.Store(now.Add(backoff).Unix())
		h.fenceErr.Store(err.Error())
		h.log.Error("fence FAILED — stacks may still be running on this host; "+
			"will retry",
			"error", err, "lease", h.FenceLease(),
			"attempt", attempts, "retry_in", backoff)
		return
	}

	h.fenced.Store(true)
	h.fenceErr.Store("")
	h.fenceAttempts.Store(0)
	h.nextFenceAttemptUnix.Store(0)
	h.log.Warn("fenced: stopped hosted stacks after losing contact with manager",
		"lease", h.FenceLease(), "last_contact", last, "stopped", stopped)
}

// recordStopped merges newly-stopped projects into this episode's list.
//
// Union rather than replace, because each attempt only reports what IT
// stopped. On a retry the projects already stopped are no longer running, so
// the fencer does not see them and cannot return them — replacing would make
// the reported list shrink as the fence got closer to complete, which is the
// opposite of what an operator would read it as.
func (h *Heartbeat) recordStopped(stopped []string) {
	if len(stopped) == 0 {
		return
	}
	seen := make(map[string]bool)
	var out []string
	if cur := h.fencedStacks.Load(); cur != nil {
		for _, p := range *cur {
			if !seen[p] {
				seen[p] = true
				out = append(out, p)
			}
		}
	}
	for _, p := range stopped {
		if !seen[p] {
			seen[p] = true
			out = append(out, p)
		}
	}
	h.fencedStacks.Store(&out)
}

// noteFenceRecovered clears fence state when contact returns. It returns true
// only when it actually cleared a `fenced=true` episode — i.e. this worker
// really did stop its stacks during a real partition — which is the one
// signal the caller needs to decide whether a resume is even possible.
//
// It still does NOT restart anything, and must not start to: the manager
// decides where those stacks belong now, and by the time contact is restored
// it may already have re-placed them. The caller (applyHeartbeatResult) only
// hands that decision to resumeFenceStacks, which restarts NOTHING unless the
// manager named the projects in the same response that restored contact.
func (h *Heartbeat) noteFenceRecovered() bool {
	// Cleared before the `fenced` check, because a worker mid-retry after a
	// FAILED fence has attempts pending while `fenced` is still false (§3.7).
	// Leaving them set would carry a stale backoff into the next genuine
	// partition and delay the fence that matters.
	h.fenceAttempts.Store(0)
	h.nextFenceAttemptUnix.Store(0)

	if !h.fenced.Load() {
		return false
	}
	h.fenced.Store(false)
	h.log.Warn("contact with manager restored after fencing; stacks are NOT restarted here — "+
		"only projects the manager names in this beat's stack_resume_projects are resumed, "+
		"and only in place (compose start, never up/down)")
	return true
}

// resumeFenceStacks is the 2400BUG-1028h J1 recontact fast path.
//
// A fenced worker used to be permanently one manual step from working: it
// stopped its own stacks at lease expiry, restored contact, cleared the fence
// state, and left the reconciler's 2-minute sweep to redeploy them — a full
// teardown/redeploy of a stack that never left this host. This restarts them
// IN PLACE instead, on the same tick contact came back.
//
// The authority rule is the whole point and is fail-closed: `projects` is
// whatever the manager named in the SAME heartbeat response that restored
// contact (`stack_resume_projects`, produced by the API's J2 fast path). The
// worker never decides placement itself and never widens the set — not from
// fencedStacks (this episode's STOPPED list is evidence of what it stopped,
// not authority to start it: the manager may have re-placed any of it
// elsewhere while we were partitioned), not from the host (no observeStacks
// here, which would also resurrect projects this worker was never given).
// nil/empty => do nothing.
func (h *Heartbeat) resumeFenceStacks(projects []string) {
	if h.resumer == nil {
		if len(projects) > 0 {
			h.log.Warn("manager confirmed placement on recontact but no resume callback is wired; "+
				"leaving them to the reconciler's sweep",
				"projects", projects)
		}
		return
	}
	if len(projects) == 0 {
		// Fail-closed: an older manager that does not know about the resume
		// fast path, or one that has not confirmed placement for this worker,
		// gets the pre-J1 behaviour unchanged.
		return
	}

	stopped := h.fencedStacks.Load()
	allowed := make(map[string]bool)
	if stopped != nil {
		for _, p := range *stopped {
			allowed[p] = true
		}
	}

	var named []string
	seen := make(map[string]bool)
	for _, p := range projects {
		p = strings.TrimSpace(p)
		if p == "" || seen[p] {
			continue
		}
		if !allowed[p] {
			// Split-brain guard: the manager naming a project this worker never
			// stopped this episode is not an instruction to start an arbitrary
			// compose project on this host.
			h.log.Warn("resume skipped: manager named a project this worker did not stop this episode",
				"project", p)
			continue
		}
		seen[p] = true
		named = append(named, p)
	}
	if len(named) == 0 {
		return
	}

	// `compose start` only, no retry loop: a failure here must NOT look like
	// success. The reconciler's 2-minute cadence is the fallback, and fenceBackoff
	// already paces any re-attempt of the fence itself.
	started, err := h.resumer(named)
	if err != nil {
		h.log.Error("resume after recontact FAILED — stacks left as stopped; "+
			"the reconciler's sweep remains the fallback (no tight retry loop)",
			"projects", named, "started", started, "error", err)
		return
	}
	h.log.Warn("resumed fenced stacks in place after confirmed recontact",
		"projects", named, "started", started)
}


// fenceReport is the fence state reported in each heartbeat, so an outage is
// attributable to fencing rather than to a container crash or an operator.
// Nil when this worker has never fenced.
func (h *Heartbeat) fenceReport() map[string]interface{} {
	at := h.fencedAtUnix.Load()
	if at == 0 {
		return nil
	}
	out := map[string]interface{}{
		"fenced":    h.fenced.Load(),
		"fenced_at": time.Unix(at, 0).UTC().Format(time.RFC3339),
		"lease_s":   int(h.FenceLease() / time.Second),
	}
	if v := h.fencedStacks.Load(); v != nil {
		out["stopped_projects"] = *v
	}
	if e, ok := h.fenceErr.Load().(string); ok && e != "" {
		// Surfaced so the manager can flag those stacks as possibly holding
		// resources on this host, for later reclamation.
		out["stop_failed"] = e
	}
	// §3.7 — a fence still being retried. `fenced` is false here and that is
	// the honest answer, but false alone reads identically to "never tried",
	// so the attempt count travels with it. A manager seeing
	// fenced=false + stop_failed + attempts=4 knows this host is trying and
	// failing to stop stacks it may still be serving.
	if n := h.fenceAttempts.Load(); n > 0 {
		out["fence_attempts"] = int(n)
		out["fence_retrying"] = true
	}
	return out
}

// fence-related state, embedded into Heartbeat.
type fenceState struct {
	lastContactUnix atomic.Int64
	fenceLeaseS     atomic.Int64
	fenced          atomic.Bool
	fencedAtUnix    atomic.Int64
	fencedStacks    atomic.Pointer[[]string]
	fenceErr        atomic.Value
	fencer          StackFencer
	// BUG-1028h J3 — the retry-aware variant, wired when the backend can act
	// on a named set. Preferred over `fencer` on a RETRY so projects already
	// stopped this episode are not re-issued a `compose stop`.
	ScopedFencer  StackFencerScoped
	fenceJitterPct  atomic.Int64
	resumer         StackResumer
	// 2400BUG-1028h J1 — the manager's placement confirmation for the
	// beat currently being applied, stashed by sendAPI and consumed by
	// applyHeartbeatResult on the same tick.
	resumeCandidates  atomic.Pointer[[]string]
	// §3.7 — consecutive outright failures, and when the next attempt may run.
	// A failed fence is retried rather than latched, so these are what stop the
	// retry becoming a hot loop against a broken Docker socket.
	fenceAttempts        atomic.Int64
	nextFenceAttemptUnix atomic.Int64
}
