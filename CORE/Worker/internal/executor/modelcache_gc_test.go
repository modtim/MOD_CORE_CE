package executor

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// mkModelDir creates a fake "models--org--name" cache dir with a single
// blob file of size bytes, and sets its mtime (used as LastUsed fallback
// when no touch marker exists).
func mkModelDir(t *testing.T, root, name string, size int64, mtime time.Time) string {
	t.Helper()
	dir := filepath.Join(root, name)
	if err := os.MkdirAll(dir, 0755); err != nil {
		t.Fatal(err)
	}
	blob := filepath.Join(dir, "blob")
	if err := os.WriteFile(blob, make([]byte, size), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(dir, mtime, mtime); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestSelectModelVictims_LRUOrder(t *testing.T) {
	now := time.Now()
	old := modelDirInfo{Path: "/a", LastUsed: now.Add(-48 * time.Hour)}
	mid := modelDirInfo{Path: "/b", LastUsed: now.Add(-30 * time.Hour)}
	newest := modelDirInfo{Path: "/c", LastUsed: now.Add(-25 * time.Hour)}
	dirs := []modelDirInfo{newest, old, mid}

	got := selectModelVictims(dirs, nil, now)
	if len(got) != 3 || got[0].Path != "/a" || got[1].Path != "/b" || got[2].Path != "/c" {
		t.Fatalf("wrong LRU order: %+v", got)
	}
}

func TestSelectModelVictims_InUseNeverEvicted(t *testing.T) {
	now := time.Now()
	dirs := []modelDirInfo{
		{Path: "/pinned", LastUsed: now.Add(-72 * time.Hour)},
		{Path: "/free", LastUsed: now.Add(-72 * time.Hour)},
	}
	inUse := map[string]bool{"/pinned": true}

	got := selectModelVictims(dirs, inUse, now)
	if len(got) != 1 || got[0].Path != "/free" {
		t.Fatalf("in-use model was not excluded: %+v", got)
	}
}

func TestSelectModelVictims_RecentNeverEvicted(t *testing.T) {
	now := time.Now()
	dirs := []modelDirInfo{
		{Path: "/recent", LastUsed: now.Add(-30 * time.Minute)},
		{Path: "/old", LastUsed: now.Add(-2 * time.Hour)},
	}
	got := selectModelVictims(dirs, nil, now)
	if len(got) != 1 || got[0].Path != "/old" {
		t.Fatalf("recently-used model was not excluded: %+v", got)
	}
}

func TestSafeWithinRoot_RefusesSymlinkEscape(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	target := filepath.Join(outside, "secret")
	if err := os.MkdirAll(target, 0755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "models--evil--escape")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	if _, err := safeWithinRoot(root, link); err == nil {
		t.Fatal("expected symlink escape to be refused, got nil error")
	}

	// A legitimate directory inside root must still pass.
	legit := filepath.Join(root, "models--good--repo")
	if err := os.MkdirAll(legit, 0755); err != nil {
		t.Fatal(err)
	}
	if _, err := safeWithinRoot(root, legit); err != nil {
		t.Fatalf("legitimate in-root dir was refused: %v", err)
	}
}

func TestEvictModelCache_StopsAtThresholdAndReportsReclaimed(t *testing.T) {
	parent := t.TempDir()
	t.Setenv("MANAGED_AI_HF_CACHE_PATH", parent)
	realHub := filepath.Join(parent, "hub")
	if err := os.MkdirAll(realHub, 0755); err != nil {
		t.Fatal(err)
	}

	now := time.Now()
	mkModelDir(t, realHub, "models--a--oldest", 1000, now.Add(-72*time.Hour))
	mkModelDir(t, realHub, "models--b--middle", 2000, now.Add(-48*time.Hour))
	mkModelDir(t, realHub, "models--c--newest", 4000, now.Add(-25*time.Hour))

	// Free space is derived from how many of the three model dirs still
	// exist on disk — a deterministic stand-in for statfs that moves only
	// because evictModelCache's own os.RemoveAll calls (real filesystem
	// ops on the temp dir above) changed the count, not because the real
	// host disk was consulted. Starts at 18GB (< the 20GB floor) and gains
	// 5GB per removal; target is floor(20)+headroom(5)=25GB, which is
	// crossed after two removals — the third (newest) must survive.
	modelDirs := []string{"models--a--oldest", "models--b--middle", "models--c--newest"}
	orig := modelCacheFreeGB
	modelCacheFreeGB = func() (float64, bool) {
		remaining := 0
		for _, name := range modelDirs {
			if _, err := os.Stat(filepath.Join(realHub, name)); err == nil {
				remaining++
			}
		}
		removed := len(modelDirs) - remaining
		return 18.0 + 5.0*float64(removed), true
	}
	defer func() { modelCacheFreeGB = orig }()

	e := &Executor{log: slog.New(slog.NewTextHandler(os.Stderr, nil))}
	reclaimed := e.evictModelCache(context.Background(), 20)

	if want := uint64(1000 + 2000); reclaimed != want {
		t.Fatalf("reclaimed = %d, want %d (oldest+middle only)", reclaimed, want)
	}
	if _, err := os.Stat(filepath.Join(realHub, "models--a--oldest")); !os.IsNotExist(err) {
		t.Fatal("oldest model should have been evicted")
	}
	if _, err := os.Stat(filepath.Join(realHub, "models--b--middle")); !os.IsNotExist(err) {
		t.Fatal("middle model should have been evicted")
	}
	if _, err := os.Stat(filepath.Join(realHub, "models--c--newest")); err != nil {
		t.Fatalf("newest model should have survived once threshold was reached: %v", err)
	}
}

// GF.C0b.9 — mirror interlock: mirror-sourced objects must be evicted
// before WAN-sourced ones (a re-fetch of the former is a cheap LAN/mirror
// pull, the latter a cold WAN download), oldest-first within each class,
// and in-use protection must hold for BOTH classes.

func TestSelectModelVictims_MirrorSourcedBeforeWAN(t *testing.T) {
	now := time.Now()
	dirs := []modelDirInfo{
		// WAN, oldest overall — the pre-interlock LRU winner.
		{Path: "/wan-old", LastUsed: now.Add(-100 * time.Hour), Source: modelSourceWAN},
		// WAN, newest.
		{Path: "/wan-new", LastUsed: now.Add(-30 * time.Hour), Source: modelSourceWAN},
		// Mirror, oldest of the mirror class.
		{Path: "/mirror-old", LastUsed: now.Add(-90 * time.Hour), Source: modelSourceMirror},
		// Mirror, newest overall — must still come before BOTH WAN dirs.
		{Path: "/mirror-new", LastUsed: now.Add(-24 * time.Hour), Source: modelSourceMirror},
	}

	got := selectModelVictims(dirs, nil, now)
	want := []string{"/mirror-old", "/mirror-new", "/wan-old", "/wan-new"}
	if len(got) != len(want) {
		t.Fatalf("wrong count: %+v", got)
	}
	for i := range want {
		if got[i].Path != want[i] {
			t.Fatalf("order = %v, want %v (mirror first, LRU within class)",
				[]string{got[0].Path, got[1].Path, got[2].Path, got[3].Path}, want)
		}
	}
}

func TestSelectModelVictims_MirrorInUseStillProtected(t *testing.T) {
	now := time.Now()
	dirs := []modelDirInfo{
		{Path: "/mirror-pinned", LastUsed: now.Add(-72 * time.Hour), Source: modelSourceMirror},
		{Path: "/mirror-free", LastUsed: now.Add(-72 * time.Hour), Source: modelSourceMirror},
		{Path: "/wan-free", LastUsed: now.Add(-72 * time.Hour), Source: modelSourceWAN},
	}
	inUse := map[string]bool{"/mirror-pinned": true}

	got := selectModelVictims(dirs, inUse, now)
	if len(got) != 2 {
		t.Fatalf("in-use mirror model was not excluded: %+v", got)
	}
	for _, d := range got {
		if d.Path == "/mirror-pinned" {
			t.Fatal("in-use mirror model must NEVER be evicted, even though mirror-sourced")
		}
	}
}

func TestSelectModelVictims_EmptySourceIsWANClass(t *testing.T) {
	now := time.Now()
	// A pre-feature dir (no marker, zero-value Source) must sort with the
	// WAN class, not ahead of it.
	dirs := []modelDirInfo{
		{Path: "/unmarked", LastUsed: now.Add(-99 * time.Hour)},
		{Path: "/mirror", LastUsed: now.Add(-25 * time.Hour), Source: modelSourceMirror},
	}
	got := selectModelVictims(dirs, nil, now)
	if got[0].Path != "/mirror" || got[1].Path != "/unmarked" {
		t.Fatalf("unmarked dir must be WAN-class (last), got %+v", got)
	}
}

func TestMarkAndReadModelSource_RoundtripAndConservativeDefault(t *testing.T) {
	dir := t.TempDir()
	if got := modelSourceOf(dir); got != modelSourceWAN {
		t.Fatalf("absent marker must read as WAN, got %q", got)
	}
	markModelSource(dir, modelSourceMirror)
	if got := modelSourceOf(dir); got != modelSourceMirror {
		t.Fatalf("marker not round-tripped, got %q", got)
	}
	// A corrupted/unrecognised marker value degrades to WAN, never panics
	// and never misclassifies as cheap.
	if err := os.WriteFile(filepath.Join(dir, modelSourceMarker), []byte("bogus\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if got := modelSourceOf(dir); got != modelSourceWAN {
		t.Fatalf("unrecognised marker must read as WAN, got %q", got)
	}
}

// The endpoint the GC uses to decide "mirror in play": server-pushed
// tunable first, then the HF_ENDPOINT already present in the stack's
// compose env, then "" (no mirror — the box behaves exactly as before).

func TestModelMirrorEndpoint_TunableWinsOverComposeEnv(t *testing.T) {
	store := tunables.New()
	store.Apply(map[string]interface{}{
		modelMirrorEndpointTunableKey: "http://models.example.com",
	})
	e := &Executor{tunables: store}
	compose := "services:\n  a:\n    environment:\n      HF_ENDPOINT: https://other.example\n"
	if got := modelMirrorEndpoint(e, compose); got != "http://models.example.com" {
		t.Fatalf("tunable should win, got %q", got)
	}
}

func TestModelMirrorEndpoint_FallsBackToComposeHFEndpoint(t *testing.T) {
	e := &Executor{tunables: tunables.New()}
	mappingForm := "services:\n  a:\n    environment:\n      HF_ENDPOINT: https://models.example.com\n"
	if got := modelMirrorEndpoint(e, mappingForm); got != "https://models.example.com" {
		t.Fatalf("mapping env form not found, got %q", got)
	}
	listForm := "services:\n  a:\n    environment:\n      - HF_ENDPOINT=https://models.example.com\n"
	if got := modelMirrorEndpoint(e, listForm); got != "https://models.example.com" {
		t.Fatalf("list env form not found, got %q", got)
	}
}

func TestModelMirrorEndpoint_NeitherSetIsEmpty(t *testing.T) {
	if got := modelMirrorEndpoint(&Executor{tunables: tunables.New()},
		"services:\n  a:\n    image: x\n"); got != "" {
		t.Fatalf("no tunable, no HF_ENDPOINT -> empty, got %q", got)
	}
	// nil tunables (a hand-built executor, as in the older tests) must not
	// panic — same empty contract.
	if got := modelMirrorEndpoint(&Executor{}, "services:\n  a:\n    image: x\n"); got != "" {
		t.Fatalf("nil tunables store -> empty, got %q", got)
	}
}

// touchModelCacheForStack stamps the shared cache mirror-sourced exactly
// while the mirror is in play for that stack.

func TestTouchModelCacheForStack_StampsMirrorWhenMirrorInPlay(t *testing.T) {
	parent := t.TempDir()
	t.Setenv("MANAGED_AI_HF_CACHE_PATH", parent)
	hub := filepath.Join(parent, "hub")
	now := time.Now()
	mkModelDir(t, hub, "models--org--mirror-me", 10, now.Add(-48*time.Hour))
	mkModelDir(t, hub, "models--org--leave-wan", 10, now.Add(-48*time.Hour))

	// Mark one dir explicitly WAN to prove the stamp still reaches every
	// dir in the shared cache (coarse "touch all" stamping, see header).
	markModelSource(filepath.Join(hub, "models--org--leave-wan"), modelSourceWAN)

	compose := "services:\n  a:\n    volumes:\n      - " + parent + ":/root/.cache/huggingface\n"

	// Case 1: mirror in play via the tunable -> every dir stamped mirror.
	store := tunables.New()
	store.Apply(map[string]interface{}{
		modelMirrorEndpointTunableKey: "https://models.example.com",
	})
	e := &Executor{log: slog.New(slog.NewTextHandler(os.Stderr, nil)), tunables: store}
	e.touchModelCacheForStack(compose)
	for _, name := range []string{"models--org--mirror-me", "models--org--leave-wan"} {
		if got := modelSourceOf(filepath.Join(hub, name)); got != modelSourceMirror {
			t.Fatalf("%s not stamped mirror, got %q", name, got)
		}
	}

	// Case 2: mirror off (no tunable, no HF_ENDPOINT in the spec) -> no
	// stamping at all; the existing LRU behaviour is byte-for-byte intact.
	hub2parent := t.TempDir()
	t.Setenv("MANAGED_AI_HF_CACHE_PATH", hub2parent)
	hub2 := filepath.Join(hub2parent, "hub")
	mkModelDir(t, hub2, "models--org--plain", 10, now.Add(-48*time.Hour))
	compose2 := "services:\n  a:\n    volumes:\n      - " + hub2parent + ":/root/.cache/huggingface\n"
	e2 := &Executor{log: slog.New(slog.NewTextHandler(os.Stderr, nil)), tunables: tunables.New()}
	e2.touchModelCacheForStack(compose2)
	if got := modelSourceOf(filepath.Join(hub2, "models--org--plain")); got != modelSourceWAN {
		t.Fatalf("mirror off must not stamp, got %q", got)
	}
}

// The eviction driver itself: with the mirror in play, a mirror-sourced
// dir that is NEWER than every WAN dir must be the first removal — the
// exact scenario the pre-interlock raw LRU got wrong.
func TestEvictModelCache_MirrorSourcedEvictedFirst(t *testing.T) {
	parent := t.TempDir()
	t.Setenv("MANAGED_AI_HF_CACHE_PATH", parent)
	realHub := filepath.Join(parent, "hub")
	if err := os.MkdirAll(realHub, 0755); err != nil {
		t.Fatal(err)
	}

	now := time.Now()
	wanOld := filepath.Join(realHub, "models--a--wan-oldest")
	wanNew := filepath.Join(realHub, "models--b--wan-newer")
	mirror := filepath.Join(realHub, "models--c--mirror-newest")
	mkModelDir(t, realHub, "models--a--wan-oldest", 1000, now.Add(-100*time.Hour))
	mkModelDir(t, realHub, "models--b--wan-newer", 2000, now.Add(-60*time.Hour))
	mkModelDir(t, realHub, "models--c--mirror-newest", 3000, now.Add(-24*time.Hour))
	markModelSource(mirror, modelSourceMirror)
	// Writing the marker file bumped the dir mtime to "now" — restore the
	// aged mtime so the dir is not excluded by the recent-use guard.
	if err := os.Chtimes(mirror, now.Add(-24*time.Hour), now.Add(-24*time.Hour)); err != nil {
		t.Fatal(err)
	}

	// Free space starts below the floor and jumps clear of the target after
	// the FIRST removal — so exactly one dir is evicted, and the test
	// asserts WHICH one.
	orig := modelCacheFreeGB
	modelCacheFreeGB = func() (float64, bool) {
		if _, err := os.Stat(mirror); err == nil {
			return 18.0, true
		}
		return 100.0, true
	}
	defer func() { modelCacheFreeGB = orig }()

	e := &Executor{log: slog.New(slog.NewTextHandler(os.Stderr, nil))}
	reclaimed := e.evictModelCache(context.Background(), 20)
	// 3000 blob bytes + the 7-byte .mod-source marker itself.
	if reclaimed < 3000 || reclaimed > 4000 {
		t.Fatalf("reclaimed = %d, want ~3007 (the mirror-sourced dir only)", reclaimed)
	}
	if _, err := os.Stat(mirror); !os.IsNotExist(err) {
		t.Fatal("mirror-sourced model should have been evicted first")
	}
	for _, p := range []string{wanOld, wanNew} {
		if _, err := os.Stat(p); err != nil {
			t.Fatalf("WAN-sourced model %s should have survived: %v", p, err)
		}
	}
}

func TestEvictModelCache_AboveThresholdIsNoop(t *testing.T) {
	parent := t.TempDir()
	t.Setenv("MANAGED_AI_HF_CACHE_PATH", parent)
	realHub := filepath.Join(parent, "hub")
	if err := os.MkdirAll(realHub, 0755); err != nil {
		t.Fatal(err)
	}
	mkModelDir(t, realHub, "models--a--old", 1000, time.Now().Add(-72*time.Hour))

	orig := modelCacheFreeGB
	modelCacheFreeGB = func() (float64, bool) { return 100.0, true } // plenty free
	defer func() { modelCacheFreeGB = orig }()

	e := &Executor{log: slog.New(slog.NewTextHandler(os.Stderr, nil))}
	if got := e.evictModelCache(context.Background(), 20); got != 0 {
		t.Fatalf("expected no-op above threshold, reclaimed %d", got)
	}
	if _, err := os.Stat(filepath.Join(realHub, "models--a--old")); err != nil {
		t.Fatalf("model dir should have survived, stat err: %v", err)
	}
}
