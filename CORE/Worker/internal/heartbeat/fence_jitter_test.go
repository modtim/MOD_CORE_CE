package heartbeat

// BUG-1028h J3 - the configurable fence lease + per-worker jitter.
//
// The whole-site case: the manager loses network, every worker's lease expires
// at the same second (it is ONE manager-derived value pushed to all of them),
// and the entire fleet fences in lockstep. Jitter spreads that. These tests
// pin the two properties that make spreading safe:
//
//   1. jitter only ever EXTENDS a lease - a worker may fence LATER than the
//      manager's base lease, never earlier; and
//   2. lease+jitter never passes the manager's headroom (grace - margin),
//      which is the chain lease_effective < grace < gap that the split-brain
//      argument rests on.
//
// Plus the retry guard: a fence that FAILED is retried (section 3.7), and the
// retry must not re-issue `compose stop` for projects this worker already
// stopped earlier in the same episode.

import (
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
)

func jitterLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func jitterHeartbeat(name string) *Heartbeat {
cfg := &config.Config{WorkerName: name}
return &Heartbeat{cfg: cfg, log: jitterLogger()}
}

func TestFenceJitter_LeaseNeverShortened(t *testing.T) {
h := jitterHeartbeat("mcw-jitter-a")
h.SetFenceLease(120)
h.SetFenceJitterPct(20)

base := 120 * time.Second
lease := h.FenceLease()
if lease < base {
t.Fatalf("jitter shortened the lease (%v < %v); a worker that fences "+
"EARLIER stops production stacks on ordinary heartbeat jitter", lease, base)
}
maxSpread := (120 * 20 / 100) * time.Second
if spread := lease - base; spread > maxSpread {
t.Fatalf("jitter spread %v exceeds lease*pct/100 = %v",
lease - base, maxSpread)
}
if lease > (120 * 125 / 100) * time.Second {
t.Fatalf("lease+jitter %v exceeds the 25 percent bound the worker clamps to",
lease)
}

now := time.Now()
// A worker that DID get jitter must still be running one second past the
// base lease - that is the whole point of the spread: this host fences later
// than its neighbours. (jitter 0 means this worker is a neighbour.)
if lease > base {
if shouldFence(now.Add(-base-time.Second), now, lease, false) {
t.Fatal("fenced at the base lease; the jittered worker must wait "+
"for its own extended lease")
}
}
if !shouldFence(now.Add(-lease-time.Second), now, lease, false) {
t.Fatal("did not fence past lease+jitter; the fleet would never fence at all")
}
}

func TestFenceJitter_DeterministicPerWorker(t *testing.T) {
first := jitterHeartbeat("mcw-deterministic")
first.SetFenceLease(120)
first.SetFenceJitterPct(25)
want := first.FenceLease()
for i := 0; i < 8; i++ {
again := jitterHeartbeat("mcw-deterministic")
again.SetFenceLease(120)
again.SetFenceJitterPct(25)
if again.FenceLease() != want {
t.Fatal("jitter is not deterministic per worker; a restarted worker "+
"re-jitters and the fleet's fence order becomes unobservable")
}
}

names := []string{"mcw-fleet-1", "mcw-fleet-2", "mcw-fleet-3"}
leases := []time.Duration{}
for _, n := range names {
h := jitterHeartbeat(n)
h.SetFenceLease(120)
h.SetFenceJitterPct(25)
leases = append(leases, h.FenceLease())
}
if leases[0] == leases[1] && leases[1] == leases[2] {
t.Fatal("three workers with different names all got the same jitter; "+
"the fleet still fences in lockstep")
}

h0 := jitterHeartbeat("mcw-default")
h0.SetFenceLease(120)
if h0.FenceLease() != 120*time.Second {
t.Fatal("default pct 0 changed the lease; behaviour must be unchanged "+
"until the owner opts in")
}

hneg := jitterHeartbeat("mcw-negative")
hneg.SetFenceLease(120)
hneg.SetFenceJitterPct(-50)
if hneg.FenceLease() != 120*time.Second {
t.Fatal("a negative pct changed the lease; a malformed push must not "+
"disable or extend fencing")
}
}

func TestFenceJitter_StoppedSetNotRestopped(t *testing.T) {
// A FAILED fence is retried (section 3.7). The retry must act only on what is
// still running: projects already stopped earlier in THIS episode must not be
// re-issued a `compose stop`.
running := []string{"alpha", "bravo", "charlie"}
var issued []string

h := jitterHeartbeat("mcw-retry")
attempt := 0
h.OnFenceStacksScoped(func(skip []string) ([]string, error) {
seen := map[string]bool{}
for _, s := range skip {
seen[s] = true
}
acted := []string{}
for _, p := range running {
if seen[p] {
continue // already stopped this episode - must not be re-stopped
}
acted = append(acted, p)
}
for _, p := range acted {
issued = append(issued, p)
}
attempt++
if attempt == 1 {
// alpha stopped, then the socket broke mid-fence.
running = []string{"bravo", "charlie"}
return []string{"alpha"}, errors.New("docker socket went away")
}
stoppedNow := running
running = []string{}
return stoppedNow, nil
})

// Contact lost 300s ago: the lease (120s + jitter) is long expired.
now := time.Now()
h.noteContact(now.Add(-300*time.Second))
h.evaluateFence(now)
if h.Fenced() {
t.Fatal("a failed fence must not latch `fenced`")
}
h.evaluateFence(now.Add(30*time.Second))

alphaStops := 0
for _, p := range issued {
if p == "alpha" {
alphaStops++
}
}
if alphaStops != 1 {
t.Fatalf("alpha was issued a compose stop %d times; the retry must skip "+
"what this worker already stopped this episode", alphaStops)
}
stopped := h.alreadyStopped()
if len(stopped) != 3 {
t.Fatalf("episode stopped set is %v; the manager needs all three to tell "+
"this host stopped everything", stopped)
}
for _, want := range []string{"bravo", "charlie"} {
found := false
for _, s := range stopped {
if s == want {
found = true
}
}
if !found {
t.Fatalf("retry did not stop %s; the manager would re-place a stack still "+
"running on this host", want)
}
}
}
