package executor

// GPU-TENANT-SCRUB-IMAGE-NO-TORCH: the scrub helper must run on the default
// base-cuda image, which carries python3 but NO torch. These tests pin the
// script's dependency surface (CUDA driver API via ctypes, no torch import),
// the no-shell command shape, and that the helper container is created with
// exactly that command, so a revert to a torch script fails here.

import (
	"context"
	"os/exec"
	"strconv"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

func TestScrubScript_NoTorchUsesDriverAPI(t *testing.T) {
	s := tenantScrubScript
	if strings.Contains(s, "torch") {
		t.Fatalf("scrub script must not need torch (default image has none):\n%s", s)
	}
	for _, want := range []string{
		"libcuda.so.1", "cuInit", "cuDeviceGetCount", "cuCtxCreate_v2",
		"cuMemGetInfo_v2", "cuMemAlloc_v2", "cuMemsetD8_v2", "cuCtxSynchronize",
		"cuMemFree_v2", "cuCtxDestroy_v2",
	} {
		if !strings.Contains(s, want) {
			t.Fatalf("scrub script missing driver call %q", want)
		}
	}
	// Every visible card is scrubbed, not just device 0.
	if !strings.Contains(s, "range(cnt.value)") {
		t.Fatal("scrub script must loop over every visible CUDA device")
	}
	// Any driver failure must exit nonzero so the card is reported dirty.
	if !strings.Contains(s, "sys.exit(1)") {
		t.Fatal("scrub script must exit 1 on a driver error")
	}
	// GPU-TENANT-SCRUB-RESERVE: the reserve comes from argv[1] and the
	// remainder is zeroed in bounded chunks, freeing each chunk before the
	// next allocation.
	for _, want := range []string{
		"sys.argv[1]", "max(int(sys.argv[1]),0)", "while left>0",
		"min(left,C)", "cuMemFree_v2", "H-R",
	} {
		if !strings.Contains(s, want) {
			t.Fatalf("scrub script lacks bounded chunk/reserve behaviour %q", want)
		}
	}
}

func TestScrubCmd_PythonNoShell(t *testing.T) {
	c := tenantScrubCmd(tenantScrubDefaultReserveMiB)
	if len(c) != 4 || c[0] != "python3" || c[1] != "-c" || c[2] != tenantScrubScript || c[3] != "1024" {
		t.Fatalf("scrub cmd must be python3 -c <script> <reserve-mib> (no sh nesting), got %q", c[:2])
	}
	if c := tenantScrubCmd(0); c[3] != "0" {
		t.Fatalf("reserve 0 must be passed through, got %q", c)
	}
}

func TestScrubScript_CompilesUnderPython(t *testing.T) {
	py, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 not available")
	}
	cmd := exec.Command(py, "-c", "import sys;compile(sys.argv[1],\"scrub\",\"exec\")", tenantScrubScript)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("scrub script does not compile: %v: %s", err, out)
	}
}

func TestScrubHelper_CreateUsesDriverScriptCmd(t *testing.T) {
	store := tunables.New()
	store.Apply(map[string]interface{}{
		tenantScrubImageTunableKey:  "img:1",
		preferLocalImagesTunableKey: true,
	})
	f := &fakeEng{logBody: "777", waitCode: 0}
	e := hlServer(t, f, store)
	if _, _, err := e.launchScrubHelper(context.Background(), []string{"GPU-abc"}); err != nil {
		t.Fatalf("launch: %v", err)
	}
	cs, _ := f.snapshot()
	if len(cs) != 1 {
		t.Fatalf("expected one create, got %d", len(cs))
	}
	// The docker client flattens container.Config onto the top level of the
	// create body; fall back to a nested Config if a future client nests it.
	cfg := cs[0]
	if _, ok := cfg["Entrypoint"]; !ok {
		if nested := asMap(cs[0]["Config"]); nested != nil {
			cfg = nested
		}
	}
	if ep := asStrings(cfg["Entrypoint"]); len(ep) != 1 || ep[0] != "python3" {
		t.Fatalf("entrypoint must be python3, got %v", ep)
	}
	cmd := asStrings(cfg["Cmd"])
	if len(cmd) != 3 || cmd[0] != "-c" || strings.Contains(cmd[1], "torch") ||
		!strings.Contains(cmd[1], "libcuda.so.1") ||
		cmd[2] != strconv.Itoa(tenantScrubDefaultReserveMiB) {
		t.Fatalf("cmd must be -c <driver-api script> <reserve>, got %.80v", cmd)
	}
}

// GPU-TENANT-SCRUB-RESERVE: the reserve knob reaches the launcher from the
// heartbeat tunables map; default 1024 and an explicit 0 both pass through.
func TestScrubReserve_ReachesLauncher(t *testing.T) {
	if got := (&Executor{}).scrubReserveMiB(); got != tenantScrubDefaultReserveMiB {
		t.Fatalf("no tunables: want default %d MiB, got %d", tenantScrubDefaultReserveMiB, got)
	}
	store := tunables.New()
	store.Apply(map[string]interface{}{
		tenantScrubReserveTunableKey: 2048,
		tenantScrubImageTunableKey:  "img:1",
		preferLocalImagesTunableKey: true,
	})
	f := &fakeEng{logBody: "0", waitCode: 0}
	e := hlServer(t, f, store)
	if _, _, err := e.launchScrubHelper(context.Background(), []string{"0"}); err != nil {
		t.Fatalf("launch: %v", err)
	}
	cs, _ := f.snapshot()
	if len(cs) != 1 {
		t.Fatalf("expected one create, got %d", len(cs))
	}
	cfg := cs[0]
	if _, ok := cfg["Entrypoint"]; !ok {
		if nested := asMap(cs[0]["Config"]); nested != nil {
			cfg = nested
		}
	}
	if got := asStrings(cfg["Cmd"]); len(got) != 3 || got[2] != "2048" {
		t.Fatalf("reserve tunable must reach the helper argv, got %v", got)
	}
	if got := (&Executor{}).scrubReserveMiB(); got != tenantScrubDefaultReserveMiB {
		t.Fatalf("no tunables: want default %d, got %d", tenantScrubDefaultReserveMiB, got)
	}
	neg := tunables.New()
	neg.Apply(map[string]interface{}{tenantScrubReserveTunableKey: -7})
	if got := (&Executor{tunables: neg}).scrubReserveMiB(); got != 0 {
		t.Fatalf("negative reserve must clamp to 0, got %d", got)
	}
}

// GPU-TENANT-SCRUB-RESERVE: zero-byte success is only honest when the card's
// free memory was smaller than the reserve; the log line must say so.
func TestScrubZeroNote_ReserveExplanation(t *testing.T) {
	ok := &ScrubResult{OK: true, BytesZeroed: 0}
	if got := scrubZeroNote(ok, 1024); got == "" ||
		!strings.Contains(got, "1024 MiB") {
		t.Fatalf("zero-byte success must explain the reserve, got %q", got)
	}
	if got := scrubZeroNote(&ScrubResult{OK: false, BytesZeroed: 0}, 1024); got != "" {
		t.Fatalf("failed scrub must not claim a reserve note, got %q", got)
	}
	if got := scrubZeroNote(&ScrubResult{OK: true, BytesZeroed: 42}, 1024); got != "" {
		t.Fatalf("nonzero scrub must not claim a reserve note, got %q", got)
	}
}

func scrubStartStore() *tunables.Store {
	store := tunables.New()
	store.Apply(map[string]interface{}{
		tenantScrubImageTunableKey:  "img:1",
		preferLocalImagesTunableKey: true,
	})
	return store
}

func callIndex(calls []string, suffix, method string) int {
	for i, c := range calls {
		if strings.HasPrefix(c, method+" ") && strings.HasSuffix(c, suffix) {
			return i
		}
	}
	return -1
}

// GPU-SCRUB-REAL-TEST: the helper container must be STARTED (it used to be
// created and waited on, which returns at once for a Created container, so the
// scrub "succeeded" in milliseconds without touching the card) and removed.
func TestScrubHelper_StartsAndRemovesContainer(t *testing.T) {
	f := &fakeEng{logBody: "4294967296", waitCode: 0}
	e := hlServer(t, f, scrubStartStore())
	n, rc, err := e.launchScrubHelper(context.Background(), []string{"0"})
	if err != nil || rc != 0 || n != 4294967296 {
		t.Fatalf("launch: n=%d rc=%d err=%v", n, rc, err)
	}
	_, calls := f.snapshot()
	create := callIndex(calls, "/containers/create", "POST")
	start := callIndex(calls, "/start", "POST")
	wait := callIndex(calls, "/wait", "POST")
	del := callIndex(calls, "/containers/hlctr", "DELETE")
	if create < 0 || start < 0 || wait < 0 || del < 0 {
		t.Fatalf("want create,start,wait,remove; calls=%v", calls)
	}
	if !(create < start && start < wait && wait < del) {
		t.Fatalf("order must be create<start<wait<remove, calls=%v", calls)
	}
}

// A start refusal is a failed scrub (fail-closed): error out, no wait, and the
// never-started container is removed rather than left Created on the host.
func TestScrubHelper_StartFailureIsFailClosedAndRemoves(t *testing.T) {
	f := &fakeEng{startFail: true}
	e := hlServer(t, f, scrubStartStore())
	if _, _, err := e.launchScrubHelper(context.Background(), []string{"0"}); err == nil {
		t.Fatalf("a helper that cannot start must fail the scrub")
	}
	_, calls := f.snapshot()
	if callIndex(calls, "/wait", "POST") >= 0 {
		t.Fatalf("must not wait on a helper that never started: %v", calls)
	}
	if callIndex(calls, "/containers/hlctr", "DELETE") < 0 {
		t.Fatalf("never-started helper must be removed: %v", calls)
	}
}

// A nonzero helper exit (broken script, no libcuda) through the production
// launcher + Scrub() refuses the switch.
func TestScrubHelper_NonzeroExitRefusesViaScrub(t *testing.T) {
	f := &fakeEng{waitCode: 3}
	e := hlServer(t, f, scrubStartStore())
	s := newTenantScrubber(e)
	if _, err := s.Scrub(context.Background(), "0"); err == nil {
		t.Fatalf("exit 3 must be a failed scrub")
	}
}
