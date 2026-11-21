// Tests for the exec half of host lockdown (C31). Nothing here touches /proc,
// PATH or a process: the fake Runner records every call and replays scripted
// stdout, and ReadFile/LookPath are stubbed, so the whole probe/apply/verify/
// remove state machine runs in CI on an unprivileged box with no nft.
package lockdown

import (
	"context"
	"errors"
	"strings"
	"testing"
)

// Call is one recorded Runner invocation.
type Call struct {
	Stdin string
	Name  string
	Args  []string
}

// Script is one scripted Runner result.
type Script struct {
	Out string
	Err error
}

// Fake is the test Runner: it records calls and replays scripted results per
// "name args" key. A key with several scripts yields them in order and then
// repeats the last one, so "apply lists X, verify lists X" needs one entry and
// drift needs two.
type Fake struct {
	Calls  []Call
	Script map[string][]Script
}

func (f *Fake) Run(ctx context.Context, stdin string, name string, args ...string) (string, error) {
	key := name
	if len(args) > 0 {
		key = key + " " + strings.Join(args, " ")
	}
	f.Calls = append(f.Calls, Call{Stdin: stdin, Name: name, Args: args})
	queue, ok := f.Script[key]
	if !ok || len(queue) == 0 {
		return "", nil
	}
	s := queue[0]
	if len(queue) > 1 {
		f.Script[key] = queue[1:]
	}
	return s.Out, s.Err
}

// script queues one result for a command key.
func (f *Fake) script(key string, out string, errText string) {
	s := Script{Out: out}
	if errText != "" {
		s.Err = errors.New(errText)
	}
	f.Script[key] = append(f.Script[key], s)
}

// fileReader serves a canned file map; anything else is "missing".
func fileReader(files map[string]string) func(string) ([]byte, error) {
	return func(path string) ([]byte, error) {
		content, ok := files[path]
		if !ok {
			return nil, errors.New("no such file: " + path)
		}
		return []byte(content), nil
	}
}

// pathResolver is the PATH seam: nft is either there or not.
func pathResolver(found bool) func(string) (string, error) {
	return func(name string) (string, error) {
		if !found {
			return "", errors.New("not found on PATH: " + name)
		}
		return "/usr/sbin/" + name, nil
	}
}

// RootStatus is a bare-metal root's CapEff: every capability granted, so bit 12
// (CAP_NET_ADMIN) is set.
const RootStatus = "CapEgg: 0000000000000000\nCapEff:\t000001ffffffffff\n"

// NoNetAdminStatus has bit 12 clear (a containerised worker).
const NoNetAdminStatus = "CapEgg: 000001ffffffffff\nCapEff:\t0000000000000fff\n"

// BareMetalOSRelease is a plain kernel.
const BareMetalOSRelease = "6.8.0-generic\n"

// WSLOSRelease is the WSL bridge marker.
const WSLOSRelease = "4.11.0-microsoft-standard-WSL2\n"

// newManager wires a Manager to the fake Runner plus canned /proc and PATH.
func newManager(f *Fake, files map[string]string, nftOnPath bool) *Manager {
	return &Manager{
		R:        f,
		ReadFile: fileReader(files),
		LookPath: pathResolver(nftOnPath),
	}
}

// probeFiles is the capable bare-metal box.
func bareFiles() map[string]string {
	return map[string]string{
		ProcStatusPath:    RootStatus,
		ProcOSReleasePath: BareMetalOSRelease,
		ProcVersionPath:   "Linux version 6.8.0\n",
	}
}

// wslFiles is the same box on WSL2.
func wslFiles() map[string]string {
	files := bareFiles()
	files[ProcOSReleasePath] = WSLOSRelease
	return files
}

// The exact command keys the Manager issues, spelled out so a change in argv is
// a test failure rather than a silent miss.
const (
	ProbeAddKey = "nft add table inet mod_lockdown_probe"
	ProbeDelKey = "nft delete table inet mod_lockdown_probe"
	ListKey     = "nft -j list table inet mod_lockdown"
	ApplyKey    = "nft -f -"
	DeleteKey   = "nft delete table inet mod_lockdown"
	DockerKey   = "docker info --format={{.OperatingSystem}}"
)

// --- probe matrix -----------------------------------------------------------

// TestProbeEnforcedHost: root with CAP_NET_ADMIN, nft on PATH, scratch table
// create+delete ok → enforced on the bare host.
func TestProbeEnforcedHost(t *testing.T) {
	f := newFake()
	m := newManager(f, bareFiles(), true)
	got := m.Probe(context.Background())
	if got.HostLockdown != Enforced || got.Scope != HostScope {
		t.Errorf("want enforced/host, got %s/%s (%s)", got.HostLockdown, got.Scope, got.Detail)
	}
	expectCalls(f, []string{DockerKey, ProbeAddKey, ProbeDelKey}, t)
}

// TestProbeWSLPartial: everything capable, but the kernel is a WSL bridge →
// partial, scope wsl-vm (OD-H1: not accepted for mediated by default).
func TestProbeWSLPartial(t *testing.T) {
	f := newFake()
	m := newManager(f, wslFiles(), true)
	got := m.Probe(context.Background())
	if got.HostLockdown != Partial || got.Scope != WSLScope {
		t.Errorf("want partial/wsl-vm, got %s/%s (%s)", got.HostLockdown, got.Scope, got.Detail)
	}
	expectCalls(f, []string{DockerKey, ProbeAddKey, ProbeDelKey}, t)
}

// TestProbeDockerDesktopUnsupported: the daemon says Docker Desktop, so the
// unfirewallable Windows/Mac host wins over every other signal.
func TestProbeDockerDesktopUnsupported(t *testing.T) {
	f := newFake()
	f.script(DockerKey, "Windows 10\r\nDocker Desktop (dockerinfo)\n", "")
	m := newManager(f, bareFiles(), true)
	got := m.Probe(context.Background())
	if got.HostLockdown != Unsupported {
		t.Errorf("want unsupported, got %s (%s)", got.HostLockdown, got.Detail)
	}
	// No probe work at all: the desktop check short-circuits.
	expectCalls(f, []string{DockerKey}, t)
}

// TestProbeNoNetAdminUnsupported: CapEff bit 12 clear → unsupported, with the
// reason in Detail.
func TestProbeNoNetAdminUnsupported(t *testing.T) {
	files := bareFiles()
	files[ProcStatusPath] = NoNetAdminStatus
	f := newFake()
	m := newManager(f, files, true)
	got := m.Probe(context.Background())
	if got.HostLockdown != Unsupported || !strings.Contains(got.Detail, "CAP_NET_ADMIN") {
		t.Errorf("want unsupported naming CAP_NET_ADMIN, got %s (%s)", got.HostLockdown, got.Detail)
	}
	expectCalls(f, []string{DockerKey}, t)
}

// TestProbeNoNftUnsupported: capable root but no nft binary on PATH.
func TestProbeNoNftUnsupported(t *testing.T) {
	f := newFake()
	m := newManager(f, bareFiles(), false)
	got := m.Probe(context.Background())
	if got.HostLockdown != Unsupported || !strings.Contains(got.Detail, "nft not on PATH") {
		t.Errorf("want unsupported naming nft, got %s (%s)", got.HostLockdown, got.Detail)
	}
	expectCalls(f, []string{DockerKey}, t)
}

// TestProbeCreateFailsError: capable + nft, but the scratch table create is
// refused (e.g. read-only kernel) → error, not unsupported.
func TestProbeCreateFailsError(t *testing.T) {
	f := newFake()
	f.script(ProbeAddKey, "", "permission denied (Operation not permitted)")
	m := newManager(f, bareFiles(), true)
	got := m.Probe(context.Background())
	if got.HostLockdown != Errored || !strings.Contains(got.Detail, "probe create failed") {
		t.Errorf("want error naming probe create failure, got %s (%s)", got.HostLockdown, got.Detail)
	}
	// The delete is never attempted once create fails.
	expectCalls(f, []string{DockerKey, ProbeAddKey}, t)
}

// --- helpers ---------------------------------------------------------------

// newFake is an empty recorder.
func newFake() *Fake { return &Fake{Script: map[string][]Script{}} }

// expectCalls asserts the exact sequence of commands the Manager issued, keyed
// "name args..." in order.
func expectCalls(f *Fake, want []string, t *testing.T) {
	if len(f.Calls) != len(want) {
		t.Errorf("call count %d != %d", len(f.Calls), len(want))
		return
	}
	for i := range len(want) {
		got := f.Calls[i].Name
		if len(f.Calls[i].Args) > 0 {
			got = got + " " + strings.Join(f.Calls[i].Args, " ")
		}
		if got != want[i] {
			t.Errorf("call %d: got %q want %q", i, got, want[i])
		}
	}
}

// stdinOf returns the stdin the Manager handed to the nth call.
func stdinOf(f *Fake, i int) string {
	if i >= len(f.Calls) {
		return ""
	}
	return f.Calls[i].Stdin
}

// --- apply / verify / remove -------------------------------------------------

// ListingA and ListingB are canned `nft -j list table inet mod_lockdown`
// payloads. A and B differ ONLY in handle bookkeeping (offsets/line numbers),
// which normaliseListing drops; C differs in a real rule (the allowed address).
const ListingA = `{"type":"list","images":[{"table":{"name":"mod_lockdown","handle":{"offset":0,"line":1,"column":1},"rules":[{"chain":{"name":"output","handle":{"offset":2,"line":2,"column":1},"rules":[{"match":{"type":"ip","field":"daddr"},"accept":{"handle":{"offset":4,"line":3,"column":1}}}]}}]}}]}`

const ListingB = `{"type":"list","images":[{"table":{"name":"mod_lockdown","handle":{"offset":99,"line":7,"column":3},"rules":[{"chain":{"name":"output","handle":{"offset":21,"line":9,"column":4},"rules":[{"match":{"type":"ip","field":"daddr"},"accept":{"handle":{"offset":41,"line":11,"column":5}}}]}}]}}]}`

const ListingC = `{"type":"list","images":[{"table":{"name":"mod_lockdown","handle":{"offset":0,"line":1,"column":1},"rules":[{"chain":{"name":"output","handle":{"offset":2,"line":2,"column":1},"rules":[{"match":{"type":"ip","field":"daddr"},"accept":{"handle":{"offset":4,"line":3,"column":1}}}]}},{"chain":{"name":"forward","handle":{"offset":8,"line":4,"column":1},"rules":[{"accept":{"handle":{"offset":9,"line":5,"column":1}}}]}}]}}]}`

// ListingMeta carries a metainfo block (creation bookkeeping) that normalise
// also drops.
const ListingMeta = `{"type":"list","metainfo":{"version":7,"tag":"mod"},"images":[{"table":{"name":"mod_lockdown","handle":{"offset":0,"line":1,"column":1},"rules":[{"chain":{"name":"output","handle":{"offset":2,"line":2,"column":1},"rules":[{"match":{"type":"ip","field":"daddr"},"accept":{"handle":{"offset":4,"line":3,"column":1}}}]}}]}}]}`

// TestApplyPassesRenderOnStdinOnce: the rendered script is handed to `nft -f -`
// as stdin, exactly once, and nothing else is exec'd besides the follow-on
// listing.
func TestApplyPassesRenderOnStdinOnce(t *testing.T) {
	p := EmptyPolicy()
	script := Render(p)
	f := newFake()
	m := newManager(f, bareFiles(), true)
	if err := m.Apply(context.Background(), p); err != nil {
		t.Errorf("apply failed: %s", err.Error())
		return
	}
	expectCalls(f, []string{ApplyKey, ListKey}, t)
	if stdinOf(f, 0) != script {
		t.Errorf("stdin mismatch:\nGOT\n%s\nWANT\n%s\n", stdinOf(f, 0), script)
	}
	if stdinOf(f, 1) != "" {
		t.Errorf("the listing call must not carry stdin, got %q", stdinOf(f, 1))
	}
}

// TestVerifyTrueAfterApply: Apply stores the hash of its own listing; Verify
// with an identical listing matches.
func TestVerifyTrueAfterApply(t *testing.T) {
	f := newFake()
	f.script(ListKey, ListingA, "")
	m := newManager(f, bareFiles(), true)
	if err := m.Apply(context.Background(), EmptyPolicy()); err != nil {
		t.Errorf("apply failed: %s", err.Error())
		return
	}
	ok, err := m.Verify(context.Background(), EmptyPolicy())
	if err != nil {
		t.Errorf("verify errored: %s", err.Error())
		return
	}
	if !ok {
		t.Errorf("verify must be true when the listing is unchanged")
	}
}

// TestVerifyHandlesOnlyDiffIsNotDrift: nft stamps per-object handles into every
// listing; a handles-only difference normalises equal, so it is NOT drift.
func TestVerifyHandlesOnlyDiffIsNotDrift(t *testing.T) {
	f := newFake()
	f.script(ListKey, ListingA, "")
	f.script(ListKey, ListingB, "")
	m := newManager(f, bareFiles(), true)
	if err := m.Apply(context.Background(), EmptyPolicy()); err != nil {
		t.Errorf("apply failed: %s", err.Error())
		return
	}
	ok, err := m.Verify(context.Background(), EmptyPolicy())
	if err != nil {
		t.Errorf("verify errored: %s", err.Error())
		return
	}
	if !ok {
		t.Errorf("handles-only difference must normalise equal (not drift)")
	}
}

// TestVerifyMetainfoIgnored: the metainfo block is dropped by normalisation too.
func TestVerifyMetainfoIgnored(t *testing.T) {
	f := newFake()
	f.script(ListKey, ListingA, "")
	f.script(ListKey, ListingMeta, "")
	m := newManager(f, bareFiles(), true)
	if err := m.Apply(context.Background(), EmptyPolicy()); err != nil {
		t.Errorf("apply failed: %s", err.Error())
		return
	}
	ok, err := m.Verify(context.Background(), EmptyPolicy())
	if err != nil {
		t.Errorf("verify errored: %s", err.Error())
		return
	}
	if !ok {
		t.Errorf("metainfo difference must normalise equal (not drift)")
	}
}

// TestVerifyFalseOnDrift: a real rule difference (an extra chain) is drift.
func TestVerifyFalseOnDrift(t *testing.T) {
	f := newFake()
	f.script(ListKey, ListingA, "")
	f.script(ListKey, ListingC, "")
	m := newManager(f, bareFiles(), true)
	if err := m.Apply(context.Background(), EmptyPolicy()); err != nil {
		t.Errorf("apply failed: %s", err.Error())
		return
	}
	ok, err := m.Verify(context.Background(), EmptyPolicy())
	if err != nil {
		t.Errorf("verify errored: %s", err.Error())
		return
	}
	if ok {
		t.Errorf("a changed rule must report drift")
	}
}

// TestVerifyFalseWhenTableMissing: no table is "not enforced", not an error.
func TestVerifyFalseWhenTableMissing(t *testing.T) {
	f := newFake()
	f.script(ListKey, "", "No chain set to delete: No such file or directory")
	m := newManager(f, bareFiles(), true)
	ok, err := m.Verify(context.Background(), EmptyPolicy())
	if err != nil {
		t.Errorf("missing table must not be an error: %s", err.Error())
		return
	}
	if ok {
		t.Errorf("missing table must report false")
	}
}

// TestVerifyFalseBeforeAnyApply: nothing applied yet ⇒ nothing to match.
func TestVerifyFalseBeforeAnyApply(t *testing.T) {
	f := newFake()
	f.script(ListKey, ListingA, "")
	m := newManager(f, bareFiles(), true)
	ok, err := m.Verify(context.Background(), EmptyPolicy())
	if err != nil {
		t.Errorf("verify errored: %s", err.Error())
		return
	}
	if ok {
		t.Errorf("verify must be false before anything was applied")
	}
}

// TestRemoveIdempotent: deleting a table that is not there succeeds, and a real
// delete succeeds too.
func TestRemoveIdempotent(t *testing.T) {
	f := newFake()
	f.script(DeleteKey, "", "No chain set to delete: No such file or directory")
	m := newManager(f, bareFiles(), true)
	if err := m.Remove(context.Background()); err != nil {
		t.Errorf("missing-table remove must be a no-op, got %s", err.Error())
	}
	expectCalls(f, []string{DeleteKey}, t)
}

// TestRemoveRealTable: when the table is there, Remove deletes it and clears
// lastHash so a later Verify reports false.
func TestRemoveRealTable(t *testing.T) {
	f := newFake()
	f.script(ListKey, ListingA, "")
	f.script(DeleteKey, "", "")
	m := newManager(f, bareFiles(), true)
	if err := m.Apply(context.Background(), EmptyPolicy()); err != nil {
		t.Errorf("apply failed: %s", err.Error())
		return
	}
	if err := m.Remove(context.Background()); err != nil {
		t.Errorf("remove failed: %s", err.Error())
		return
	}
	ok, err := m.Verify(context.Background(), EmptyPolicy())
	if err != nil {
		t.Errorf("verify errored: %s", err.Error())
		return
	}
	if ok {
		t.Errorf("after Remove, verify must be false")
	}
}

// TestVerifyTrueForAppliedPolicyArg: the policy argument is honoured in the
// positive direction too - Apply(q) then Verify(q) with the live listing equal
// to what Apply read back is OK (FullPolicy, so a digest compare that ignored
// the arg or always failed would be caught here).
func TestVerifyTrueForAppliedPolicyArg(t *testing.T) {
	f := newFake()
	f.script(ListKey, ListingA, "")
	m := newManager(f, bareFiles(), true)
	if err := m.Apply(context.Background(), FullPolicy()); err != nil {
		t.Errorf("apply failed: %s", err.Error())
		return
	}
	ok, err := m.Verify(context.Background(), FullPolicy())
	if err != nil {
		t.Errorf("verify errored: %s", err.Error())
		return
	}
	if !ok {
		t.Errorf("Verify must be true for the policy that was applied")
	}
}

// TestVerifyFalseWhenPolicyChangedWithoutApply: Apply(q) then Verify(p) with
// p != q is drift even though the live listing still equals q - the policy
// argument is no longer discarded, so a caller cannot get "enforced" for a
// ruleset it never asked to install.
func TestVerifyFalseWhenPolicyChangedWithoutApply(t *testing.T) {
	f := newFake()
	f.script(ListKey, ListingA, "")
	m := newManager(f, bareFiles(), true)
	if err := m.Apply(context.Background(), EmptyPolicy()); err != nil {
		t.Errorf("apply failed: %s", err.Error())
		return
	}
	ok, err := m.Verify(context.Background(), FullPolicy())
	if err != nil {
		t.Errorf("verify errored: %s", err.Error())
		return
	}
	if ok {
		t.Errorf("Verify must be false when the policy argument differs from the applied one")
	}
}

// TestVerifyFalseAfterRestart: a fresh Manager (no process-local state) against
// a live table that was applied by an earlier process reports drift - never OK
// from memory. Applying first in the new process makes Verify true again.
func TestVerifyFalseAfterRestart(t *testing.T) {
	f := newFake()
	f.script(ListKey, ListingA, "")
	m := newManager(f, bareFiles(), true)
	ok, err := m.Verify(context.Background(), EmptyPolicy())
	if err != nil {
		t.Errorf("verify errored: %s", err.Error())
		return
	}
	if ok {
		t.Errorf("a fresh process must not report OK from memory")
	}
	if err := m.Apply(context.Background(), EmptyPolicy()); err != nil {
		t.Errorf("apply failed: %s", err.Error())
		return
	}
	ok, err = m.Verify(context.Background(), EmptyPolicy())
	if err != nil {
		t.Errorf("verify errored: %s", err.Error())
		return
	}
	if !ok {
		t.Errorf("after Apply in the fresh process, Verify must be true")
	}
}

// TestProbeNeverPanics: every seam failing (unreadable /proc, no PATH entry,
// every exec erroring) still yields a Status, not a crash.
// TestProbeNeverPanics: with a failing runner (docker absent, scratch-table add refused) every input
// class must come back as a documented state with the reason named: never a
// zero value and never "enforced". Each case pins the exact state, scope,
// reason substring and call sequence. A panic still fails the subtest.
func TestProbeNeverPanics(t *testing.T) {
	cases := []struct {
		name       string
		files      map[string]string
		nftOnPath  bool
		wantState  string
		wantScope  string
		wantDetail string
		wantCalls  []string
	}{
		{
			name:       "no /proc and no nft (original smoke input)",
			files:      map[string]string{},
			nftOnPath:  false,
			wantState:  Unsupported,
			wantScope:  HostScope,
			wantDetail: "no CAP_NET_ADMIN",
			wantCalls:  []string{DockerKey},
		},
		{
			name:       "capable box, nft absent",
			files:      bareFiles(),
			nftOnPath:  false,
			wantState:  Unsupported,
			wantScope:  HostScope,
			wantDetail: "nft not on PATH",
			wantCalls:  []string{DockerKey},
		},
		{
			name:       "capable box, nft present, scratch-table add refused",
			files:      bareFiles(),
			nftOnPath:  true,
			wantState:  Errored,
			wantScope:  HostScope,
			wantDetail: "probe create failed: boom",
			wantCalls:  []string{DockerKey, ProbeAddKey},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFake()
			f.script(DockerKey, "", "docker: command not found")
			f.script(ProbeAddKey, "", "boom")
			m := newManager(f, tc.files, tc.nftOnPath)
			got := m.Probe(context.Background())
			if got.HostLockdown != tc.wantState || got.Scope != tc.wantScope {
				t.Errorf("want %s/%s, got %q/%q (%s)", tc.wantState, tc.wantScope, got.HostLockdown, got.Scope, got.Detail)
			}
			if !strings.Contains(got.Detail, tc.wantDetail) {
				t.Errorf("want detail naming %q, got %q", tc.wantDetail, got.Detail)
			}
			expectCalls(f, tc.wantCalls, t)
		})
	}
}
