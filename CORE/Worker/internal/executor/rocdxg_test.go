package executor

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

func writeLib(t *testing.T, p string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("elf"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestFindRocdxgFollowsSymlinkToVersionedFile(t *testing.T) {
	root := t.TempDir()
	real := filepath.Join(root, "rocm-7.2.3", "lib", "librocdxg.so.1.2.2")
	writeLib(t, real)
	lib := filepath.Join(root, "rocm", "lib")
	os.MkdirAll(lib, 0o755)
	if err := os.Symlink(real, filepath.Join(lib, "librocdxg.so")); err != nil {
		t.Fatal(err)
	}
	if got := findRocdxgLibIn("", []string{lib}); got != real {
		t.Fatalf("got %q want %q", got, real)
	}
}

func TestFindRocdxgInVersionedDirOnly(t *testing.T) {
	root := t.TempDir()
	real := filepath.Join(root, "rocm-7.2.3", "lib", "librocdxg.so.1.2.2")
	writeLib(t, real)
	dirs := []string{filepath.Join(root, "rocm", "lib"), filepath.Join(root, "rocm-7.2.3", "lib")}
	if got := findRocdxgLibIn("", dirs); got != real {
		t.Fatalf("got %q want %q", got, real)
	}
}

func TestFindRocdxgOverrideWins(t *testing.T) {
	root := t.TempDir()
	a := filepath.Join(root, "a", "librocdxg.so")
	b := filepath.Join(root, "custom", "bridge.so")
	writeLib(t, a)
	writeLib(t, b)
	if got := findRocdxgLibIn(b, []string{filepath.Dir(a)}); got != b {
		t.Fatalf("got %q want %q", got, b)
	}
}

func TestFindRocdxgNoneAndDanglingLink(t *testing.T) {
	root := t.TempDir()
	os.Symlink(filepath.Join(root, "gone"), filepath.Join(root, "librocdxg.so"))
	if got := findRocdxgLibIn("", []string{root, filepath.Join(root, "missing")}); got != "" {
		t.Fatalf("got %q want empty", got)
	}
}

func TestRocdxgSearchDirsIncludesLdLibraryPath(t *testing.T) {
	dirs := rocdxgSearchDirs("/x/lib: /y/lib")
	seen := map[string]bool{}
	for _, d := range dirs {
		seen[d] = true
	}
	for _, want := range []string{"/opt/rocm/lib", "/usr/local/lib", "/usr/lib/x86_64-linux-gnu", "/x/lib", "/y/lib"} {
		if !seen[want] {
			t.Errorf("missing %s in %v", want, dirs)
		}
	}
}

func TestRocdxgBinds(t *testing.T) {
	if rocdxgBinds("") != nil {
		t.Fatal("no lib must mean no binds")
	}
	b := rocdxgBinds("/opt/rocm-7.2.3/lib/librocdxg.so.1.2.2")
	if len(b) != 3 || b[0] != "/opt/rocm-7.2.3/lib/librocdxg.so.1.2.2:/opt/rocm/lib/librocdxg.so:ro" {
		t.Fatalf("binds %v", b)
	}
}

func TestWSLStackGPUBinds_AddsToDxgServicesOnly(t *testing.T) {
	spec := `services:
  llm:
    image: harbor.example/llama:1
    devices:
      - /dev/dxg
    volumes:
      - /usr/lib/wsl:/usr/lib/wsl:ro
      - models:/models
  web:
    image: nginx
volumes:
  models: {}
`
	out, changed := wslStackGPUBinds(context.Background(), spec, "/opt/rocm-7.2.3/lib/librocdxg.so.1.2.2", nil)
	if len(changed) != 1 || changed[0] != "llm" {
		t.Fatalf("changed = %v, want [llm]", changed)
	}
	if strings.Count(out, "/usr/lib/wsl:/usr/lib/wsl:ro") != 1 {
		t.Fatalf("existing /usr/lib/wsl mount duplicated:\n%s", out)
	}
	for _, dst := range rocdxgInContainer {
		if !strings.Contains(out, "/opt/rocm-7.2.3/lib/librocdxg.so.1.2.2:"+dst+":ro") {
			t.Fatalf("missing bind for %s:\n%s", dst, out)
		}
	}
	if strings.Contains(strings.SplitN(out, "web:", 2)[1], "librocdxg") {
		t.Fatalf("service without /dev/dxg was changed:\n%s", out)
	}
	// Idempotent: a second pass finds everything already mounted.
	if _, again := wslStackGPUBinds(context.Background(), out, "/opt/rocm-7.2.3/lib/librocdxg.so.1.2.2", nil); len(again) != 0 {
		t.Fatalf("second pass changed %v", again)
	}
}

func TestWSLStackGPUBinds_NoLibStillMountsWSLDrivers(t *testing.T) {
	spec := "services:\n  a:\n    image: x\n    devices: [\"/dev/dxg:/dev/dxg\"]\n"
	out, changed := wslStackGPUBinds(context.Background(), spec, "", nil)
	if len(changed) != 1 || !strings.Contains(out, "/usr/lib/wsl:/usr/lib/wsl:ro") || strings.Contains(out, "librocdxg") {
		t.Fatalf("changed=%v out=\n%s", changed, out)
	}
}

func TestWSLStackGPUBinds_UntouchedWithoutDxgOrOnBadYAML(t *testing.T) {
	spec := "services:\n  a:\n    image: x\n"
	if out, ch := wslStackGPUBinds(context.Background(), spec, "/x/librocdxg.so", nil); out != spec || ch != nil {
		t.Fatalf("spec without /dev/dxg changed")
	}
	if out, ch := wslStackGPUBinds(context.Background(), "::not yaml", "/x/librocdxg.so", nil); out != "::not yaml" || ch != nil {
		t.Fatalf("bad yaml changed")
	}
}

func TestWSLStackGPUBinds_SetsHsaDxgDetection(t *testing.T) {
	// No environment: one is created with the flag.
	spec := "services:\n  a:\n    image: x\n    devices: [\"/dev/dxg\"]\n"
	out, changed := wslStackGPUBinds(context.Background(), spec, "", nil)
	if len(changed) != 1 || changed[0] != "a" {
		t.Fatalf("changed = %v, want [a]", changed)
	}
	if !strings.Contains(out, "HSA_ENABLE_DXG_DETECTION: 1") {
		t.Fatalf("flag not added to absent env:\n%s", out)
	}

	// List-form environment: appended, not overridden.
	spec = "services:\n  a:\n    image: x\n    devices: [\"/dev/dxg\"]\n    environment:\n      - OTHER=2\n"
	out, changed = wslStackGPUBinds(context.Background(), spec, "", nil)
	if len(changed) != 1 || changed[0] != "a" {
		t.Fatalf("changed = %v, want [a]", changed)
	}
	if !strings.Contains(out, "HSA_ENABLE_DXG_DETECTION=1") || !strings.Contains(out, "OTHER=2") {
		t.Fatalf("flag not appended to list env:\n%s", out)
	}

	// Explicit value is kept, even if it disagrees.
	spec = "services:\n  a:\n    image: x\n    devices: [\"/dev/dxg\"]\n    environment:\n      HSA_ENABLE_DXG_DETECTION: \"0\"\n"
	out, changed = wslStackGPUBinds(context.Background(), spec, "", nil)
	if len(changed) != 1 || changed[0] != "a" {
		t.Fatalf("changed = %v, want [a] (binds still added)", changed)
	}
	if strings.Count(out, "HSA_ENABLE_DXG_DETECTION") != 1 || !strings.Contains(out, "HSA_ENABLE_DXG_DETECTION: \"0\"") {
		t.Fatalf("explicit flag value was overridden:\n%s", out)
	}

	// Non-dxg service: untouched.
	spec = "services:\n  a:\n    image: x\n"
	if out, ch := wslStackGPUBinds(context.Background(), spec, "/x/librocdxg.so", nil); out != spec || ch != nil || strings.Contains(out, "HSA_ENABLE_DXG_DETECTION") {
		t.Fatalf("non-dxg service touched:\n%s", out)
	}
}

func TestWSLStackGPUBinds_SetsLdLibraryPathAndRocprofilerDisable(t *testing.T) {
	// No environment: both are created alongside the existing HSA flag.
	spec := "services:\n  a:\n    image: x\n    devices: [\"/dev/dxg\"]\n"
	out, changed := wslStackGPUBinds(context.Background(), spec, "", nil)
	if len(changed) != 1 || changed[0] != "a" {
		t.Fatalf("changed = %v, want [a]", changed)
	}
	if !strings.Contains(out, "LD_LIBRARY_PATH: "+wslDxgLdLibraryPath) {
		t.Fatalf("LD_LIBRARY_PATH not added to absent env:\n%s", out)
	}
	if !strings.Contains(out, "ROCPROFILER_REGISTER_ENABLED: \"0\"") && !strings.Contains(out, "ROCPROFILER_REGISTER_ENABLED: 0") {
		t.Fatalf("ROCPROFILER_REGISTER_ENABLED not added to absent env:\n%s", out)
	}

	// List-form environment: appended, not overridden.
	spec = "services:\n  a:\n    image: x\n    devices: [\"/dev/dxg\"]\n    environment:\n      - OTHER=2\n"
	out, changed = wslStackGPUBinds(context.Background(), spec, "", nil)
	if len(changed) != 1 || changed[0] != "a" {
		t.Fatalf("changed = %v, want [a]", changed)
	}
	if !strings.Contains(out, "LD_LIBRARY_PATH="+wslDxgLdLibraryPath) ||
		!strings.Contains(out, "ROCPROFILER_REGISTER_ENABLED=0") ||
		!strings.Contains(out, "OTHER=2") {
		t.Fatalf("vars not appended to list env:\n%s", out)
	}

	// Explicit values are kept, even if they disagree. Assert on the PARSED
	// env (not the re-serialized string) so the check is independent of
	// yaml.v3's quoting of the round-tripped value.
	spec = "services:\n  a:\n    image: x\n    devices: [\"/dev/dxg\"]\n    environment:\n      LD_LIBRARY_PATH: \"/custom\"\n      ROCPROFILER_REGISTER_ENABLED: \"1\"\n"
	out, changed = wslStackGPUBinds(context.Background(), spec, "", nil)
	if len(changed) != 1 || changed[0] != "a" {
		t.Fatalf("changed = %v, want [a] (HSA flag still added)", changed)
	}
	if got := envValue(t, out, "a", "LD_LIBRARY_PATH"); got != "/custom" {
		t.Fatalf("explicit LD_LIBRARY_PATH was overridden: got %q, want \"/custom\"\n%s", got, out)
	}
	if got := envValue(t, out, "a", "ROCPROFILER_REGISTER_ENABLED"); got != "1" {
		t.Fatalf("explicit ROCPROFILER_REGISTER_ENABLED was overridden: got %q, want \"1\"\n%s", got, out)
	}

	// Non-dxg service: untouched.
	spec = "services:\n  a:\n    image: x\n"
	if out, ch := wslStackGPUBinds(context.Background(), spec, "/x/librocdxg.so", nil); out != spec || ch != nil || strings.Contains(out, "LD_LIBRARY_PATH") {
		t.Fatalf("non-dxg service touched:\n%s", out)
	}
}

// envValue parses a compose spec and returns service svc's value for env key
// (mapping or list form). t.Fatal on a missing key — assertions here want a
// definitive value, not a substring.
func envValue(t *testing.T, spec, svc, key string) string {
	t.Helper()
	var doc struct {
		Services map[string]struct {
			Environment interface{} `yaml:"environment"`
		} `yaml:"services"`
	}
	if err := yaml.Unmarshal([]byte(spec), &doc); err != nil {
		t.Fatalf("parse %q: %v", spec, err)
	}
	env, ok := doc.Services[svc]
	if !ok {
		t.Fatalf("service %q not in spec:\n%s", svc, spec)
	}
	switch e := env.Environment.(type) {
	case map[string]string:
		return e[key]
	case map[string]interface{}:
		return fmt.Sprint(e[key])
	case map[interface{}]interface{}:
		return fmt.Sprint(e[key])
	case []string:
		for _, kv := range e {
			if k, _, ok := strings.Cut(kv, "="); ok && k == key {
				return kv[len(k)+1:]
			}
		}
		return ""
	}
	t.Fatalf("service %q has no env for %q:\n%s", svc, key, spec)
	return ""
}

func TestMergeLdLibraryPath(t *testing.T) {
	// WSL dirs prepended to the image's own; image dir kept.
	got := mergeLdLibraryPath(wslDxgLdLibraryDirs, []string{"/app"})
	want := wslDxgLdLibraryPath + ":/app"
	if got != want {
		t.Fatalf("merge = %q, want %q", got, want)
	}
	// De-duplicated: a dir both list stays in the WSL (prepended) position.
	got = mergeLdLibraryPath(wslDxgLdLibraryDirs, []string{"/usr/lib/wsl/lib", "/app"})
	if got != want {
		t.Fatalf("dedup merge = %q, want %q", got, want)
	}
	// Blank entries dropped.
	got = mergeLdLibraryPath(wslDxgLdLibraryDirs, []string{"", "  ", "/app"})
	if got != want {
		t.Fatalf("blank merge = %q, want %q", got, want)
	}
	// No image env → just the WSL dirs (the fallback).
	if got := mergeLdLibraryPath(wslDxgLdLibraryDirs, nil); got != wslDxgLdLibraryPath {
		t.Fatalf("no-image merge = %q, want %q", got, wslDxgLdLibraryPath)
	}
}

func TestWSLStackGPUBinds_MergesImageLdLibraryPath(t *testing.T) {
	// imageLd returns the image's baked-in LD_LIBRARY_PATH dirs; they must be
	// PREPENDED-to, not replaced.
	spec := "services:\n  a:\n    image: ghcr.io/ggml-org/llama.cpp:server-rocm\n    devices: [\"/dev/dxg\"]\n"
	lookup := func(_ context.Context, image string) []string {
		if image != "ghcr.io/ggml-org/llama.cpp:server-rocm" {
			return nil // other images in this test set (x) have no baked-in env
		}
		return []string{"/usr/lib/wsl/lib", "/opt/rocm/lib", "/app"}
	}
	out, changed := wslStackGPUBinds(context.Background(), spec, "", lookup)
	if len(changed) != 1 || changed[0] != "a" {
		t.Fatalf("changed = %v, want [a]", changed)
	}
	got := envValue(t, out, "a", "LD_LIBRARY_PATH")
	want := "/host-gpu-libs:/usr/lib/wsl/lib:/usr/local/lib:/opt/intel/lib:/opt/rocm/lib:/app"
	if got != want {
		t.Fatalf("merged LD_LIBRARY_PATH = %q, want %q\n%s", got, want, out)
	}
	// The image's /usr/lib/wsl/lib and /opt/rocm/lib must NOT be duplicated.
	if strings.Count(out, "/usr/lib/wsl/lib") != 1 || strings.Count(out, "/opt/rocm/lib") != 1 {
		t.Fatalf("image dirs duplicated:\n%s", out)
	}

	// An explicit compose value still wins, untouched, even with a lookup.
	spec = "services:\n  a:\n    image: x\n    devices: [\"/dev/dxg\"]\n    environment:\n      LD_LIBRARY_PATH: \"/myapp\"\n"
	out, _ = wslStackGPUBinds(context.Background(), spec, "", lookup)
	if got := envValue(t, out, "a", "LD_LIBRARY_PATH"); got != "/myapp" {
		t.Fatalf("explicit compose LD_LIBRARY_PATH overridden: got %q", got)
	}
}

func TestWSLStackGPUBinds_NilLookupUsesFallback(t *testing.T) {
	// No image lookup (or image with no LD_LIBRARY_PATH) → WSL dirs alone.
	spec := "services:\n  a:\n    image: x\n    devices: [\"/dev/dxg\"]\n"
	out, _ := wslStackGPUBinds(context.Background(), spec, "", nil)
	if got := envValue(t, out, "a", "LD_LIBRARY_PATH"); got != wslDxgLdLibraryPath {
		t.Fatalf("fallback LD_LIBRARY_PATH = %q, want %q", got, wslDxgLdLibraryPath)
	}
	// A lookup that returns nil behaves the same.
	out, _ = wslStackGPUBinds(context.Background(), spec, "", func(context.Context, string) []string { return nil })
	if got := envValue(t, out, "a", "LD_LIBRARY_PATH"); got != wslDxgLdLibraryPath {
		t.Fatalf("nil-return lookup LD_LIBRARY_PATH = %q, want %q", got, wslDxgLdLibraryPath)
	}
}

func TestWslRemapRocmDevicesKfdDriToDxg(t *testing.T) {
	spec := "services:\n  llama:\n    image: x\n    devices:\n      - /dev/kfd\n      - /dev/dri\n"
	out, changed := wslRemapRocmDevices(spec)
	if len(changed) != 1 || changed[0] != "llama" {
		t.Fatalf("changed = %v, want [llama]", changed)
	}
	if strings.Contains(out, "/dev/kfd") || strings.Contains(out, "/dev/dri") {
		t.Fatalf("kfd/dri not dropped:\n%s", out)
	}
	if !strings.Contains(out, "/dev/dxg") {
		t.Fatalf("/dev/dxg not added:\n%s", out)
	}
}

func TestWslRemapRocmDevicesAlreadyDxgUnchanged(t *testing.T) {
	spec := "services:\n  llama:\n    image: x\n    devices:\n      - /dev/dxg\n"
	_, changed := wslRemapRocmDevices(spec)
	if changed != nil {
		t.Fatalf("changed = %v, want nil (already dxg)", changed)
	}
}

func TestWslRemapRocmDevicesNoGpuUnchanged(t *testing.T) {
	spec := "services:\n  web:\n    image: x\n    ports:\n      - 80:80\n"
	_, changed := wslRemapRocmDevices(spec)
	if changed != nil {
		t.Fatalf("changed = %v, want nil (no rocm devices)", changed)
	}
}

func TestWslRemapRocmDevicesDropsDriSubnodesAddsSingleDxg(t *testing.T) {
	spec := "services:\n  llama:\n    image: x\n    devices:\n      - /dev/kfd\n      - /dev/dri/renderD128\n      - /dev/dri/card0\n"
	out, changed := wslRemapRocmDevices(spec)
	if len(changed) != 1 {
		t.Fatalf("changed = %v, want 1 service", changed)
	}
	if strings.Contains(out, "/dev/dri") || strings.Contains(out, "/dev/kfd") {
		t.Fatalf("dri subnodes/kfd not dropped:\n%s", out)
	}
	if strings.Count(out, "/dev/dxg") != 1 {
		t.Fatalf("want exactly one /dev/dxg:\n%s", out)
	}
}
