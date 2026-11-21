package executor

import (
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// --- shouldPruneContainer policy tests ---

func TestShouldPruneContainer(t *testing.T) {
	const fresh = 5 * time.Minute
	stale := sidecarStuckAfter() + time.Hour

	cases := []struct {
		name  string
		image string
		state string
		age   time.Duration
		want  bool
	}{
		// The sidecar registry is Vault-sourced per tenant, so a mod_core
		// project must match on ANY registry host — a deployment running its
		// own tenant Harbor is not a special case.
		{"exited sidecar, tenant harbor", "tenant-harbor.localhost/modtim/minio-client:latest", "exited", fresh, true},
		{"exited sidecar, stage harbor", "ghcr.io/modtim/minio-client:latest", "exited", fresh, true},
		{"exited sidecar, harbor with port", "harbor.local:5000/modtim/minio-client:latest", "exited", fresh, true},
		{"exited sidecar, no host", "modtim/minio-client:latest", "exited", fresh, true},
		// Non-running ghcr.io/modtim/* → prune.
		{"exited mod_core sidecar", "ghcr.io/modtim/minio-client:latest", "exited", fresh, true},
		{"created mod_core sidecar", "ghcr.io/modtim/build-task:latest", "created", fresh, true},
		{"dead mod_core sidecar", "ghcr.io/modtim/foo:latest", "dead", fresh, true},
		// Running mod_core within the backstop → could be a legitimate
		// multi-hour transfer, DO NOT touch.
		{"running mod_core sidecar, fresh", "ghcr.io/modtim/foo:latest", "running", fresh, false},
		{"running mod_core sidecar, at threshold", "ghcr.io/modtim/foo:latest", "running", sidecarStuckAfter(), false},
		// A 20-hour render's upload must survive: it is under the 24h floor.
		{"running mod_core sidecar, 20h transfer", "ghcr.io/modtim/minio-client:latest", "running", 20 * time.Hour, false},
		// Running mod_core past the backstop → unlabeled, unowned, leaked.
		{"running mod_core sidecar, stuck", "ghcr.io/modtim/minio-client:latest", "running", stale, true},
		{"running mod_core sidecar, just past threshold", "ghcr.io/modtim/foo:latest", "running", sidecarStuckAfter() + time.Second, true},
		// restarting/paused are never reclaimed regardless of age.
		{"restarting mod_core", "ghcr.io/modtim/foo:latest", "restarting", stale, false},
		{"paused mod_core", "ghcr.io/modtim/foo:latest", "paused", stale, false},
		// Other images → never touch even if exited or long-running (could
		// be user containers, platform services, etc.).
		{"exited platform service", "mod-api", "exited", fresh, false},
		{"exited public image", "redis:7-alpine", "exited", fresh, false},
		{"exited tenant harbor", "registry.example.com/tenant-1/foo:latest", "exited", fresh, false},
		{"exited customer registry", "registry:5000/user-image", "exited", fresh, false},
		// A different project on the SAME local harbor is not ours.
		{"tenant project on tenant harbor", "tenant-harbor.localhost/tenant-4/foo:latest", "exited", fresh, false},
		// "mod_core" must be the project segment, not a substring of one.
		{"lookalike project name", "registry.example.com/mod_core_backup/foo:latest", "exited", fresh, false},
		{"mod_core in image name only", "registry.example.com/other/mod_core-thing:latest", "exited", fresh, false},
		{"long-running platform service", "postgres:15-alpine", "running", stale, false},
		{"long-running tenant container", "registry.example.com/tenant-1/foo:latest", "running", stale, false},
		// Empty / missing.
		{"empty image", "", "exited", fresh, false},
		{"empty state", "ghcr.io/modtim/foo:latest", "", fresh, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := shouldPruneContainer(tc.image, tc.state, tc.age)
			if got != tc.want {
				t.Errorf("shouldPruneContainer(%q, %q, %v) = %v, want %v",
					tc.image, tc.state, tc.age, got, tc.want)
			}
		})
	}
}

// shouldPruneLabeled decides on state + ownership, with age only as a
// last-resort backstop for containers whose owner is unknowable. The cases
// that matter most are the two that must NEVER fire: this worker's own
// long-running transfer, and another live worker's.
func TestShouldPruneLabeled(t *testing.T) {
	long := 20 * time.Hour
	past := sidecarStuckAfter() + time.Hour

	cases := []struct {
		name  string
		state string
		owner sidecarOwner
		age   time.Duration
		want  bool
	}{
		// Finished containers are always safe to remove.
		{"exited, self", "exited", ownerSelf, time.Minute, true},
		{"exited, orphan", "exited", ownerOrphan, time.Minute, true},
		{"exited, foreign", "exited", ownerForeign, time.Minute, true},
		{"created, orphan", "created", ownerOrphan, time.Minute, true},
		{"dead, self", "dead", ownerSelf, time.Minute, true},
		{"empty state, orphan", "", ownerOrphan, time.Minute, true},

		// THE case this whole design exists to protect: our own live
		// transfer, running for 20 hours, must never be reclaimed.
		{"running, self, fresh", "running", ownerSelf, time.Minute, false},
		{"running, self, 20h upload", "running", ownerSelf, long, false},
		{"running, self, past backstop", "running", ownerSelf, past, false},

		// An orphan is provably abandoned — reclaim at any age.
		{"running, orphan, fresh", "running", ownerOrphan, time.Second, true},
		{"running, orphan, old", "running", ownerOrphan, past, true},

		// Another live worker's sidecar: only the age backstop applies, and
		// a 20h transfer is under the 24h floor.
		{"running, foreign, 20h upload", "running", ownerForeign, long, false},
		{"running, foreign, past backstop", "running", ownerForeign, past, true},

		// Never touched regardless of owner or age.
		{"restarting, orphan", "restarting", ownerOrphan, past, false},
		{"paused, orphan", "paused", ownerOrphan, past, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := shouldPruneLabeled(tc.state, tc.owner, tc.age)
			if got != tc.want {
				t.Errorf("shouldPruneLabeled(%q, %v, %v) = %v, want %v",
					tc.state, tc.owner, tc.age, got, tc.want)
			}
		})
	}
}

// Managed stacks must be invisible to the sweep at every state — including
// exited, since the stack `stop` op leaves containers in place for a later
// `start`. A stack built on a mod_core image would otherwise be
// indistinguishable from a leaked sidecar to the image-prefix fallback.
func TestIsComposeManaged(t *testing.T) {
	cases := []struct {
		name   string
		labels map[string]string
		want   bool
	}{
		{"compose project set", map[string]string{composeProjectLabel: "llm-stack-7"}, true},
		{"compose plus our sidecar labels", map[string]string{
			composeProjectLabel: "llm-stack-7", sidecarLabel: "1"}, true},
		{"empty project value", map[string]string{composeProjectLabel: ""}, false},
		{"whitespace project value", map[string]string{composeProjectLabel: "   "}, false},
		{"no labels", map[string]string{}, false},
		{"nil labels", nil, false},
		{"our sidecar only", map[string]string{sidecarLabel: "1"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := isComposeManaged(tc.labels); got != tc.want {
				t.Errorf("isComposeManaged(%v) = %v, want %v", tc.labels, got, tc.want)
			}
		})
	}
}

// The volume reaper deletes data, and deletion is unrecoverable. Ownership is
// asserted by OUR prefix, so the guard rails are: real names on this host must
// be unmatchable, not merely deprioritised.
func TestIsTaskVolume(t *testing.T) {
	shouldMatch := []string{
		"modcore-task-trigger_e2e_probe-8ade32df55891276",
		"modcore-task-albedo-0123456789abcdef",
		"modcore-task--fedcba9876543210",
	}
	for _, n := range shouldMatch {
		if !isTaskVolume(n) {
			t.Errorf("isTaskVolume(%q) = false, want true", n)
		}
	}

	// Every one of these is a real volume observed on the dev box, or a near
	// miss. None may EVER match — deleting one destroys live or foreign data.
	mustNotMatch := []string{
		// This system's own live data.
		"mod_postgres_data", "mod_keycloak_db_data", "mod_worker_bin",
		"core_minio_data", "core_postgres_data", "core_keycloak_db_data",
		"startup_postgres_data", "startup_minio_data",
		// Model-weight caches: dangling by design while a stack is stopped.
		"mod_hf_cache", "managed-ai-generator_hf_cache", "llm-stack-7_vllm_cache",
		// OTHER SYSTEMS sharing this host. The whole point of the prefix.
		"comic-catalog_postgres_data", "comic-catalog_ollama_data",
		"flowise_data", "lister_postgres_data", "defense-logic-twin_lt-data",
		"deploy_lt-data",
		// The OLD naming scheme. Pre-prefix volumes are deliberately never
		// reaped — that is the trade for never guessing about existing data.
		"trigger_e2e_probe_8ade32df55891276",
		"llm_generate_strategy_ab79236f606ad2de",
		"accelerator_benchmark_0123456789abcdef",
		// Docker's anonymous 64-hex volumes (Harbor, postgres, etc.).
		"0f8d03977b2206d79a33bb3ba87925a5f0acc30633575c89f10e812bb8d8d297",
		// Prefix must be at the START, not anywhere in the name.
		"notours-modcore-task-abc", "",
	}
	for _, n := range mustNotMatch {
		if isTaskVolume(n) {
			t.Errorf("isTaskVolume(%q) = true, want false — this would delete real data", n)
		}
	}
}

func TestShouldPruneVolume(t *testing.T) {
	const self, inst = "mod-worker-embedded", "abc123"
	const ours = "modcore-task-albedo-0123456789abcdef"
	old := taskVolumeMinAge + time.Hour
	otherInst := map[string]string{sidecarWorkerLabel: self, sidecarInstanceLabel: "dead00"}

	cases := []struct {
		name   string
		vol    string
		labels map[string]string
		age    time.Duration
		want   bool
	}{
		{"orphan of a dead instance", ours, otherInst, old, true},
		{"unlabeled but ours by name, old", ours, nil, old, true},

		// Never.
		{"this process's own volume", ours,
			map[string]string{sidecarWorkerLabel: self, sidecarInstanceLabel: inst}, old, false},
		{"too young", ours, otherInst, time.Minute, false},
		{"keep label", ours,
			map[string]string{sidecarWorkerLabel: self, sidecarInstanceLabel: "dead00",
				keepVolumeLabel: "1"}, old, false},
		{"compose-owned", ours,
			map[string]string{composeProjectLabel: "some-stack"}, old, false},
		{"another system's volume", "comic-catalog_postgres_data", nil, old, false},
		{"our own service volume", "mod_postgres_data", nil, old, false},
		{"old naming scheme", "trigger_e2e_probe_8ade32df55891276", nil, old, false},
		{"missing timestamp reads as new", ours, otherInst, volumeAge(""), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := shouldPruneVolume(tc.vol, tc.labels, tc.age, self, inst)
			if got != tc.want {
				t.Errorf("shouldPruneVolume(%q, %v, %v) = %v, want %v",
					tc.vol, tc.labels, tc.age, got, tc.want)
			}
		})
	}
}

// createVolume must emit a name the reaper recognises. If these two ever drift
// apart the reaper silently stops collecting anything.
func TestCreateVolumeNameIsReapable(t *testing.T) {
	for _, kind := range []string{"albedo", "trigger_e2e_probe", "aec/weird kind", ""} {
		name := taskVolumePrefix + sanitizeProject(kind) + "-0123456789abcdef"
		if !isTaskVolume(name) {
			t.Errorf("createVolume-shaped name %q is not reapable", name)
		}
		if !shouldPruneVolume(name, map[string]string{
			sidecarWorkerLabel: "w", sidecarInstanceLabel: "dead"},
			taskVolumeMinAge+time.Hour, "w", "live") {
			t.Errorf("orphaned volume %q would not be reclaimed", name)
		}
	}
}

// A model-weight cache sits unused for days by design. The keep label is the
// only signal that survives every heuristic, so it must be honoured literally
// and must not be defeated by a falsey-looking value being present.
func TestIsVolumeKept(t *testing.T) {
	kept := []map[string]string{
		{keepVolumeLabel: "1"},
		{keepVolumeLabel: "true"},
		{keepVolumeLabel: "yes"},
		{keepVolumeLabel: "weights"},
		{keepVolumeLabel: "1", taskVolumeLabel: "1"},
	}
	for _, l := range kept {
		if !isVolumeKept(l) {
			t.Errorf("isVolumeKept(%v) = false, want true", l)
		}
	}

	notKept := []map[string]string{
		nil,
		{},
		{keepVolumeLabel: ""},
		{keepVolumeLabel: "   "},
		{keepVolumeLabel: "0"},
		{keepVolumeLabel: "false"},
		{keepVolumeLabel: "FALSE"},
		{taskVolumeLabel: "1"},
	}
	for _, l := range notKept {
		if isVolumeKept(l) {
			t.Errorf("isVolumeKept(%v) = true, want false", l)
		}
	}
}

// A missing or malformed timestamp must read as "new", never as "ancient" —
// deleting on absent metadata is the failure mode to avoid.
func TestVolumeAge(t *testing.T) {
	if got := volumeAge(""); got != 0 {
		t.Errorf("volumeAge(\"\") = %v, want 0", got)
	}
	if got := volumeAge("not-a-timestamp"); got != 0 {
		t.Errorf("volumeAge(garbage) = %v, want 0", got)
	}
	stamp := time.Now().Add(-90 * time.Minute).Format(time.RFC3339)
	if got := volumeAge(stamp); got < 89*time.Minute || got > 91*time.Minute {
		t.Errorf("volumeAge(90m ago) = %v, want ~90m", got)
	}
	// A zero age must not clear the floor, even for a volume that is
	// otherwise a valid candidate.
	if shouldPruneVolume("modcore-task-albedo-0123456789abcdef", nil, volumeAge(""), "w", "live") {
		t.Error("a volume with an unparseable timestamp must not be reclaimed")
	}
}

func TestClassifyOwner(t *testing.T) {
	const self, inst = "mod-worker-embedded", "abc123"

	cases := []struct {
		name   string
		labels map[string]string
		want   sidecarOwner
	}{
		{"same worker, same instance", map[string]string{
			sidecarWorkerLabel: self, sidecarInstanceLabel: inst}, ownerSelf},
		{"same worker, older instance", map[string]string{
			sidecarWorkerLabel: self, sidecarInstanceLabel: "deadbeef"}, ownerOrphan},
		{"different worker", map[string]string{
			sidecarWorkerLabel: "mod-worker-remote-2", sidecarInstanceLabel: inst}, ownerForeign},
		// A pre-label container has neither, so it must not be mistaken for
		// ours and force-removed while running.
		{"no labels at all", map[string]string{}, ownerForeign},
		{"worker label only, matching", map[string]string{
			sidecarWorkerLabel: self}, ownerOrphan},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := classifyOwner(tc.labels, self, inst); got != tc.want {
				t.Errorf("classifyOwner(%v) = %v, want %v", tc.labels, got, tc.want)
			}
		})
	}
}

// The idle window is what bounds a stalled `mc`; a misparsed or absent
// override must never silently mean "no guard".
func TestSidecarIdleWindow(t *testing.T) {
	cases := []struct {
		name string
		env  string
		want time.Duration
	}{
		{"unset uses default", "", defaultSidecarIdleWindow},
		{"override honored", "30", 30 * time.Second},
		{"zero falls back", "0", defaultSidecarIdleWindow},
		{"negative falls back", "-1", defaultSidecarIdleWindow},
		{"garbage falls back", "abc", defaultSidecarIdleWindow},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.env == "" {
				os.Unsetenv("MOD_SIDECAR_IDLE_S")
			} else {
				t.Setenv("MOD_SIDECAR_IDLE_S", tc.env)
			}
			if got := _sidecarIdleWindow(); got != tc.want {
				t.Errorf("_sidecarIdleWindow() with env %q = %v, want %v", tc.env, got, tc.want)
			}
		})
	}
}

// The backstop must stay far above any plausible transfer, even if someone
// tunes the idle window down to something aggressive.
func TestSidecarStuckAfterFloor(t *testing.T) {
	t.Setenv("MOD_SIDECAR_IDLE_S", "10")
	if got := sidecarStuckAfter(); got < 24*time.Hour {
		t.Errorf("sidecarStuckAfter() = %v with a 10s idle window; must not drop below the 24h floor", got)
	}

	// A large idle window scales the backstop up rather than clamping it, so
	// the backstop can never fire on a transfer the guard is still watching.
	t.Setenv("MOD_SIDECAR_IDLE_S", "43200") // 12h
	if got := sidecarStuckAfter(); got <= 24*time.Hour {
		t.Errorf("sidecarStuckAfter() = %v with a 12h idle window; want > 24h", got)
	}

	// The default must leave clear air between guard and backstop.
	os.Unsetenv("MOD_SIDECAR_IDLE_S")
	if got, guard := sidecarStuckAfter(), _sidecarIdleWindow(); got <= guard {
		t.Errorf("backstop %v must exceed the guard window %v", got, guard)
	}
}

// Sidecars must be stamped with all three labels or the reaper cannot tell a
// live transfer from an orphan.
func TestSidecarLabels(t *testing.T) {
	e := &Executor{workerName: "mod-worker-embedded", instanceID: "abc123"}
	got := e.sidecarLabels()

	if got[sidecarLabel] != "1" {
		t.Errorf("sidecarLabel = %q, want \"1\"", got[sidecarLabel])
	}
	if got[sidecarWorkerLabel] != "mod-worker-embedded" {
		t.Errorf("worker label = %q", got[sidecarWorkerLabel])
	}
	if got[sidecarInstanceLabel] != "abc123" {
		t.Errorf("instance label = %q", got[sidecarInstanceLabel])
	}
	if classifyOwner(got, e.workerName, e.instanceID) != ownerSelf {
		t.Error("a sidecar this executor just stamped must classify as ownerSelf")
	}
}

// Two instances must not collide, or an orphan would be misread as ours and
// left running forever.
func TestNewInstanceIDUnique(t *testing.T) {
	seen := make(map[string]bool)
	for i := 0; i < 100; i++ {
		id := newInstanceID()
		if id == "" || id == "unknown" {
			t.Fatalf("newInstanceID() returned %q", id)
		}
		if seen[id] {
			t.Fatalf("newInstanceID() collided on %q", id)
		}
		seen[id] = true
	}
}

func TestContainerAge(t *testing.T) {
	if got := containerAge(0); got != 0 {
		t.Errorf("containerAge(0) = %v, want 0 (missing stamp must not read as ancient)", got)
	}
	if got := containerAge(-5); got != 0 {
		t.Errorf("containerAge(-5) = %v, want 0", got)
	}
	got := containerAge(time.Now().Add(-90 * time.Minute).Unix())
	if got < 89*time.Minute || got > 91*time.Minute {
		t.Errorf("containerAge(90m ago) = %v, want ~90m", got)
	}
}

// testExecutor creates an Executor with no Docker client (only for testing
// methods that don't need Docker, like resolveProcessingImage).
// The registry arg is retained for test readability — it is threaded through
// to resolveProcessingImage(payload, registry) by each caller, mirroring how
// Run() sources the value from creds.Harbor.Registry (Vault) at runtime.
func testExecutor(hardwareTags []string, _ string) *Executor {
	return &Executor{
		hardwareTags: hardwareTags,
		log:          slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError})),
	}
}

// --- resolveProcessingImage tests ---

func TestResolveProcessingImage_NoVariants_ReturnsDefault(t *testing.T) {
	e := testExecutor([]string{"cuda"}, "ghcr.io")
	payload := transport.TaskPayload{
		ProcessingImage: "albedo:latest",
	}
	got := e.resolveProcessingImage(payload, "ghcr.io")
	if got != "albedo:latest" {
		t.Errorf("expected albedo:latest, got %s", got)
	}
}

func TestResolveProcessingImage_EmptyImage_FallsBackToRegistry(t *testing.T) {
	e := testExecutor([]string{"cuda"}, "ghcr.io")
	payload := transport.TaskPayload{
		ProcessingImage: "",
		NodeKind:        "albedo",
	}
	got := e.resolveProcessingImage(payload, "ghcr.io")
	want := "ghcr.io/modtim/albedo"
	if got != want {
		t.Errorf("expected %s, got %s", want, got)
	}
}

func TestResolveProcessingImage_CUDAWorkerPicksCUDAVariant(t *testing.T) {
	e := testExecutor([]string{"cuda"}, "ghcr.io")
	payload := transport.TaskPayload{
		ProcessingImage: "albedo:latest",
		GPUVariants: map[string]string{
			"cuda": "albedo:v2-cuda",
			"rocm": "albedo:v2-rocm",
		},
	}
	got := e.resolveProcessingImage(payload, "ghcr.io")
	if got != "albedo:v2-cuda" {
		t.Errorf("expected albedo:v2-cuda, got %s", got)
	}
}

func TestResolveProcessingImage_ROCmWorkerPicksROCmVariant(t *testing.T) {
	e := testExecutor([]string{"rocm"}, "ghcr.io")
	payload := transport.TaskPayload{
		ProcessingImage: "albedo:latest",
		GPUVariants: map[string]string{
			"cuda": "albedo:v2-cuda",
			"rocm": "albedo:v2-rocm",
		},
	}
	got := e.resolveProcessingImage(payload, "ghcr.io")
	if got != "albedo:v2-rocm" {
		t.Errorf("expected albedo:v2-rocm, got %s", got)
	}
}

func TestResolveProcessingImage_IntelWorkerPicksIntelVariant(t *testing.T) {
	e := testExecutor([]string{"intel"}, "ghcr.io")
	payload := transport.TaskPayload{
		ProcessingImage: "albedo:latest",
		GPUVariants: map[string]string{
			"cuda":  "albedo:cuda",
			"intel": "albedo:intel",
		},
	}
	got := e.resolveProcessingImage(payload, "ghcr.io")
	if got != "albedo:intel" {
		t.Errorf("expected albedo:intel, got %s", got)
	}
}

func TestResolveProcessingImage_NoMatchFallsToCPU(t *testing.T) {
	e := testExecutor([]string{"rocm"}, "ghcr.io")
	payload := transport.TaskPayload{
		ProcessingImage: "albedo:latest",
		GPUVariants: map[string]string{
			"cuda": "albedo:cuda",
			"cpu":  "albedo:cpu",
		},
	}
	got := e.resolveProcessingImage(payload, "ghcr.io")
	if got != "albedo:cpu" {
		t.Errorf("expected albedo:cpu (fallback), got %s", got)
	}
}

func TestResolveProcessingImage_NoMatchNoCPU_ReturnsDefault(t *testing.T) {
	e := testExecutor([]string{"rocm"}, "ghcr.io")
	payload := transport.TaskPayload{
		ProcessingImage: "albedo:latest",
		GPUVariants: map[string]string{
			"cuda": "albedo:cuda",
		},
	}
	got := e.resolveProcessingImage(payload, "ghcr.io")
	if got != "albedo:latest" {
		t.Errorf("expected albedo:latest (default), got %s", got)
	}
}

func TestResolveProcessingImage_NoneGPUType_MapsToCPU(t *testing.T) {
	e := testExecutor([]string{"cpu"}, "ghcr.io")
	payload := transport.TaskPayload{
		ProcessingImage: "proc:latest",
		GPUVariants: map[string]string{
			"cuda": "proc:cuda",
			"cpu":  "proc:cpu",
		},
	}
	got := e.resolveProcessingImage(payload, "ghcr.io")
	if got != "proc:cpu" {
		t.Errorf("expected proc:cpu for cpu-only worker, got %s", got)
	}
}

func TestResolveProcessingImage_EmptyVariantValue_Skipped(t *testing.T) {
	e := testExecutor([]string{"cuda"}, "ghcr.io")
	payload := transport.TaskPayload{
		ProcessingImage: "fallback:latest",
		GPUVariants: map[string]string{
			"cuda": "",
		},
	}
	got := e.resolveProcessingImage(payload, "ghcr.io")
	if got != "fallback:latest" {
		t.Errorf("expected fallback:latest (empty variant skipped), got %s", got)
	}
}

func TestNodeShmSizeBytes(t *testing.T) {
	const mb = int64(1024 * 1024)
	cases := []struct {
		name  string
		set   bool
		value string
		want  int64
	}{
		{"unset uses 2048MB default", false, "", 2048 * mb},
		{"explicit 512", true, "512", 512 * mb},
		{"zero leaves docker default", true, "0", 0},
		{"negative leaves docker default", true, "-1", 0},
		{"garbage falls back to default", true, "lots", 2048 * mb},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.set {
				t.Setenv("MOD_WORKER_NODE_SHM_MB", tc.value)
			} else {
				t.Setenv("MOD_WORKER_NODE_SHM_MB", "")
				os.Unsetenv("MOD_WORKER_NODE_SHM_MB")
			}
			if got := nodeShmSizeBytes(); got != tc.want {
				t.Fatalf("nodeShmSizeBytes() = %d, want %d", got, tc.want)
			}
		})
	}
}

func TestDefaultIODirEnv(t *testing.T) {
	want := []string{
		"INPUT_DIR=/data/input",
		"INPUT_PATH=/data/input",
		"OUTPUT_DIR=/data/output",
		"OUTPUT_PATH=/data/output",
	}
	for _, in := range []map[string]string{nil, {}} {
		got := defaultIODirEnv(in)
		if len(got) != len(want) {
			t.Fatalf("defaultIODirEnv(%v) = %v, want %v", in, got, want)
		}
		for i := range want {
			if got[i] != want[i] {
				t.Errorf("defaultIODirEnv(%v)[%d] = %q, want %q", in, i, got[i], want[i])
			}
		}
	}

	got := defaultIODirEnv(map[string]string{"INPUT_DIR": "/custom"})
	if len(got) != 3 {
		t.Fatalf("defaultIODirEnv(INPUT_DIR set) = %v, want 3 entries", got)
	}
	for _, kv := range got {
		if strings.HasPrefix(kv, "INPUT_DIR=") {
			t.Errorf("server-set INPUT_DIR must not be overridden, got %q", kv)
		}
	}
}
