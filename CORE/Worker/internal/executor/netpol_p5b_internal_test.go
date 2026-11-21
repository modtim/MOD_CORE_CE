package executor

// FX.91 p5b — the internal-only posture network must actually be internal.
//
// p5 stamped a gov-profile node with no declared egress onto
// mod_stack-api-net, but ensureAttachableNetwork created that network WITHOUT
// Internal:true, so the "internal-only" claim was only asserted, not enforced.
// These tests pin:
//   1. a verdict that is internal-only creates the network with Internal:true;
//   2. an existing network is NEVER deleted/recreated, even when it lacks
//      Internal — the posture is recorded as an egress gap and WARNed once;
//   3. callers that need egress (the plain NIM stack-posture verdict) still get
//      the old Internal:false create, unchanged;
//   4. the posture is reported through NetpolStatus for the heartbeat.
//
// Uses the fake-Docker-client pattern already in this package (see
// fakeEgressDocker / fx91Probe).

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"

	"github.com/docker/docker/api/types/network"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// fakeNetpolDocker records what the posture-network path asked the daemon for.
// netpolMissing names a network the fake reports as absent (so the create path
// runs); otherwise inspect succeeds with the stored Internal flag.
type fakeNetpolDocker struct {
	calls       []string
	inspected   string
	internal    bool
	missing     bool
	createErr   error
	inspectErr  error
	createOpts  network.CreateOptions
	postureSeen NetpolPosture
}

func (f *fakeNetpolDocker) NetworkInspect(_ context.Context, name string, _ network.InspectOptions) (network.Inspect, error) {
	f.calls = append(f.calls, "NetworkInspect "+name)
	f.inspected = name
	if f.inspectErr != nil {
		return network.Inspect{}, f.inspectErr
	}
	if f.missing {
		return network.Inspect{}, errors.New("network not found")
	}
	return network.Inspect{ID: "net-" + name, Internal: f.internal}, nil
}

func (f *fakeNetpolDocker) NetworkCreate(_ context.Context, name string, o network.CreateOptions) (network.CreateResponse, error) {
	f.calls = append(f.calls, "NetworkCreate "+name)
	f.createOpts = o
	if f.createErr != nil {
		return network.CreateResponse{}, f.createErr
	}
	return network.CreateResponse{ID: "net-" + name}, nil
}

// netpolTestExecutor is an Executor with just the fields the posture path uses:
// a log sink we can read for the WARN, and the posture state.
func netpolTestExecutor(w *bytes.Buffer) *Executor {
	return &Executor{
		log: slog.New(slog.NewTextHandler(w, &slog.HandlerOptions{Level: slog.LevelDebug})),
	}
}

// ---------------------------------------------------------------------------
// 1. Internal-only verdict creates the network WITH Internal: true.
// ---------------------------------------------------------------------------

func TestNetpolP5b_InternalOnlyCreatesInternalNetwork(t *testing.T) {
	var buf bytes.Buffer
	e := netpolTestExecutor(&buf)
	f := &fakeNetpolDocker{missing: true}

	posture := e.ensurePostureNetwork(context.Background(), f, stackNet, true)

	if len(f.calls) != 2 || f.calls[1] != "NetworkCreate "+stackNet {
		t.Fatalf("missing network must be created; calls=%v", f.calls)
	}
	if !f.createOpts.Internal {
		t.Error("internal-only posture network must be created with Internal:true")
	}
	if f.createOpts.Labels["mod.purpose"] != "gov-undeclared-internal-only-posture" {
		t.Errorf("internal-only create should stamp the gov purpose label; got %v", f.createOpts.Labels)
	}
	if !posture.Internal || !posture.Created || posture.Conflict {
		t.Errorf("posture should read internal+created, no gap; got %+v", posture)
	}
	if strings.Contains(buf.String(), "WARN") {
		t.Errorf("a correctly internal network should not WARN; log=%q", buf.String())
	}
}

// ---------------------------------------------------------------------------
// 2. Existing network: never deleted/recreated; a non-internal one is a
//    reported gap, WARNed once per process.
// ---------------------------------------------------------------------------

func TestNetpolP5b_ExistingNonInternalNetworkIsReportedNotRecreated(t *testing.T) {
	var buf bytes.Buffer
	e := netpolTestExecutor(&buf)
	f := &fakeNetpolDocker{internal: false}

	posture := e.ensurePostureNetwork(context.Background(), f, stackNet, true)

	if len(f.calls) != 1 {
		t.Fatalf("an existing network must only be inspected, never created/removed; calls=%v", f.calls)
	}
	if posture.Internal || posture.Created || !posture.Conflict {
		t.Errorf("a non-internal existing net must report an egress gap; got %+v", posture)
	}
	if !strings.Contains(buf.String(), "WARN") || !strings.Contains(buf.String(), "Internal:true") {
		t.Errorf("expected the posture WARN naming the missing Internal flag; log=%q", buf.String())
	}

	// Second task on the same box: same posture recorded, WARN not repeated.
	buf.Reset()
	posture2 := e.ensurePostureNetwork(context.Background(), f, stackNet, true)
	if len(f.calls) != 2 {
		t.Fatalf("second check must still only inspect; calls=%v", f.calls)
	}
	if !posture2.Conflict {
		t.Errorf("posture must stay a gap on repeat checks; got %+v", posture2)
	}
	if strings.Contains(buf.String(), "WARN") {
		t.Errorf("WARN must fire once per process, not per task; log=%q", buf.String())
	}
	status := e.NetpolStatus()
	if status == nil || status["egress_gap"] != true || status["observations"] != 2 {
		t.Errorf("heartbeat posture should report the gap with 2 observations; got %v", status)
	}
	if status["warned"] != true {
		t.Errorf("status should record that the gap was warned; got %v", status)
	}
}

func TestNetpolP5b_ExistingInternalNetworkReportsNoGap(t *testing.T) {
	var buf bytes.Buffer
	e := netpolTestExecutor(&buf)
	f := &fakeNetpolDocker{internal: true}

	posture := e.ensurePostureNetwork(context.Background(), f, stackNet, true)
	if posture.Created {
		t.Error("an existing network is not created by this worker")
	}
	if !posture.Internal || posture.Conflict {
		t.Errorf("existing internal net should report enforced posture; got %+v", posture)
	}
	if strings.Contains(buf.String(), "WARN") {
		t.Errorf("enforced posture must not WARN; log=%q", buf.String())
	}
	status := e.NetpolStatus()
	if status == nil || status["egress_gap"] != false || status["internal"] != true {
		t.Errorf("status should report internal with no gap; got %v", status)
	}
}

// ---------------------------------------------------------------------------
// 3. Egress callers are unaffected: a non-internal-only verdict still creates
//    the plain bridge with the original purpose label.
// ---------------------------------------------------------------------------

func TestNetpolP5b_EgressCallerUnaffected(t *testing.T) {
	var buf bytes.Buffer
	e := netpolTestExecutor(&buf)
	f := &fakeNetpolDocker{missing: true}

	posture := e.ensurePostureNetwork(context.Background(), f, stackNet, false)

	if f.createOpts.Internal {
		t.Error("an egress caller must not get an Internal:true network")
	}
	if f.createOpts.Labels["mod.purpose"] != "nim-stack-posture-net" {
		t.Errorf("egress caller keeps the original purpose label; got %v", f.createOpts.Labels)
	}
	if posture.Conflict {
		t.Error("an egress verdict is not an internal-only posture, so it is never a gap")
	}
	if strings.Contains(buf.String(), "WARN") {
		t.Errorf("egress caller must not WARN about internal posture; log=%q", buf.String())
	}
}

func TestNetpolP5b_CreateFailureIsBestEffort(t *testing.T) {
	var buf bytes.Buffer
	e := netpolTestExecutor(&buf)
	f := &fakeNetpolDocker{missing: true, createErr: errors.New("500 daemon out of netns slots")}

	posture := e.ensurePostureNetwork(context.Background(), f, stackNet, true)
	if posture.Created || posture.Internal {
		t.Errorf("a failed create must report nothing enforced; got %+v", posture)
	}
	if !strings.Contains(buf.String(), "could not ensure posture network") {
		t.Errorf("create failure should WARN best-effort; log=%q", buf.String())
	}

	// "already exists" is success but we cannot know the flag, so it is not
	// claimed as enforced and the gap is reported.
	f2 := &fakeNetpolDocker{missing: true, createErr: errors.New("NetworkCreate: 409 Conflict: network already exists")}
	var buf2 bytes.Buffer
	e2 := netpolTestExecutor(&buf2)
	posture2 := e2.ensurePostureNetwork(context.Background(), f2, stackNet, true)
	if posture2.Created {
		t.Error("an already-exists create is not this worker's network")
	}
	if !posture2.Conflict {
		t.Errorf("unknown posture on an already-exists net must be reported as a gap; got %+v", posture2)
	}
}

// ---------------------------------------------------------------------------
// 4. The verdict that drives the flag: only internal-only verdicts ask for an
//    Internal:true network, and the stamped node still lands on the posture net.
// ---------------------------------------------------------------------------

func TestNetpolP5b_OnlyInternalOnlyVerdictsAskForInternal(t *testing.T) {
	stamped := transport.TaskPayload{
		ContainerEnv: map[string]string{"API_KEY": "x", govInternalOnly: "1"},
	}
	sel := SelectNetworkMode(stamped, mainNet, userNet, egressNet)
	if sel.DockerNetwork != stackNet || !sel.InternalOnly {
		t.Errorf("gov-stamped verdict must be internal-only on the posture net; got %+v", sel)
	}

	defaultDeny := SelectNetworkMode(transport.TaskPayload{NodeKind: "test"}, mainNet, userNet, egressNet)
	if !defaultDeny.InternalOnly {
		t.Error("default-deny verdict must be internal-only")
	}

	// The plain NIM client that names the posture net by hand is a platform
	// placement decision, not an internal-only one: it keeps its old verdict and
	// must not trigger the internal create.
	nimClient := transport.TaskPayload{
		ContainerEnv: map[string]string{"MOD_STACK_NETWORK": stackNet},
	}
	sel2 := SelectNetworkMode(nimClient, mainNet, userNet, egressNet)
	if sel2.DockerNetwork != stackNet || sel2.InternalOnly {
		t.Errorf("MOD_STACK_NETWORK verdict must keep its network and stay non-internal-only; got %+v", sel2)
	}

	// An allowlist verdict is egress by definition.
	allowlist := transport.TaskPayload{AllowedEgress: []string{"api.example.com"}}
	sel3 := SelectNetworkMode(allowlist, mainNet, userNet, egressNet)
	if sel3.InternalOnly {
		t.Error("an AllowedEgress verdict is never internal-only")
	}
}

func TestNetpolP5b_StatusOmittedBeforeFirstCheck(t *testing.T) {
	e := netpolTestExecutor(&bytes.Buffer{})
	if e.NetpolStatus() != nil {
		t.Error("a worker that has not checked the posture must report nothing (omit, not fake)")
	}
}
