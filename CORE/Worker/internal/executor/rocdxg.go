package executor

import (
	"context"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/docker/docker/api/types"
	"gopkg.in/yaml.v3"
)

// rocdxgInContainer is where the host's librocdxg is bind-mounted. ROCm images
// resolve /opt/rocm/lib on LD_LIBRARY_PATH, and ROCr loads the bridge by
// soname when HSA_ENABLE_DXG_DETECTION=1, so both names are provided.
var rocdxgInContainer = []string{
	"/opt/rocm/lib/librocdxg.so",
	"/opt/rocm/lib/librocdxg.so.1",
	// Pre-7.x bridge builds were loaded in place of the thunk library.
	"/opt/rocm/lib/libhsakmt.so",
}

// rocdxgSearchDirs lists where a host may have installed librocdxg (the ROCm
// bridge WSL2 needs, since WSL has only /dev/dxg and no /dev/kfd). A versioned
// ROCm install (/opt/rocm-7.2.3) is common without the /opt/rocm symlink, and
// distro or hand builds land in the system lib dirs.
func rocdxgSearchDirs(ldLibraryPath string) []string {
	dirs := []string{"/opt/rocm/lib", "/opt/rocm/lib64"}
	versioned, _ := filepath.Glob("/opt/rocm-*/lib")
	// Newest ROCm first: /opt/rocm-7.2.3 before /opt/rocm-6.4.2.
	sort.Sort(sort.Reverse(sort.StringSlice(versioned)))
	dirs = append(dirs, versioned...)
	dirs = append(dirs, "/usr/local/lib", "/usr/local/lib64",
		"/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib")
	for _, d := range strings.Split(ldLibraryPath, ":") {
		if d = strings.TrimSpace(d); d != "" {
			dirs = append(dirs, d)
		}
	}
	return dirs
}

// findRocdxgLib returns the real (symlink-resolved) path of the host's
// librocdxg, or "" if none is installed. MOD_ROCDXG_LIB overrides the search
// for an install nowhere standard.
func findRocdxgLib() string {
	return findRocdxgLibIn(os.Getenv("MOD_ROCDXG_LIB"), rocdxgSearchDirs(os.Getenv("LD_LIBRARY_PATH")))
}

func findRocdxgLibIn(override string, dirs []string) string {
	if override != "" {
		if p := resolveRegularFile(override); p != "" {
			return p
		}
	}
	for _, d := range dirs {
		for _, name := range []string{"librocdxg.so", "librocdxg.so.1"} {
			if p := resolveRegularFile(filepath.Join(d, name)); p != "" {
				return p
			}
		}
		// Only a fully versioned file (librocdxg.so.1.2.2) left behind.
		if m, _ := filepath.Glob(filepath.Join(d, "librocdxg.so.*")); len(m) > 0 {
			sort.Sort(sort.Reverse(sort.StringSlice(m)))
			for _, c := range m {
				if p := resolveRegularFile(c); p != "" {
					return p
				}
			}
		}
	}
	return ""
}

func resolveRegularFile(p string) string {
	real, err := filepath.EvalSymlinks(p)
	if err != nil {
		return ""
	}
	if fi, err := os.Stat(real); err != nil || !fi.Mode().IsRegular() {
		return ""
	}
	return real
}

// rocdxgBinds mounts the host bridge under every name a ROCm image looks for.
func rocdxgBinds(lib string) []string {
	if lib == "" {
		return nil
	}
	out := make([]string, 0, len(rocdxgInContainer))
	for _, dst := range rocdxgInContainer {
		out = append(out, lib+":"+dst+":ro")
	}
	return out
}

// wslDxgLdLibraryDirs are the dynamic-linker search dirs a /dev/dxg container
// needs on LD_LIBRARY_PATH. The bind mounts alone (/usr/lib/wsl, librocdxg) put
// the WSL bridge libraries on disk inside the container, but nothing tells the
// loader to look there: the image's own ldconfig cache is built at image-build
// time and can't know about a runtime bind mount, so a dlopen("libdxcore.so"/
// "librocdxg.so") from inside ROCr fails silently and ROCm falls back to
// reporting no device. These dirs are PREPENDED to (never replace) the image's
// own LD_LIBRARY_PATH — its loader path is baked in (e.g. llama.cpp's /app) and
// dropping it breaks the binary. See mergeLdLibraryPath.
var wslDxgLdLibraryDirs = []string{
	"/host-gpu-libs",
	"/usr/lib/wsl/lib",
	"/usr/local/lib",
	"/opt/intel/lib",
	"/opt/rocm/lib",
}

// wslDxgLdLibraryPath is the fallback LD_LIBRARY_PATH when the image's own
// config can't be read (first deploy, before the image is local, or the image
// sets no LD_LIBRARY_PATH): just the WSL bridge dirs.
var wslDxgLdLibraryPath = strings.Join(wslDxgLdLibraryDirs, ":")

// mergeLdLibraryPath builds a /dev/dxg service's LD_LIBRARY_PATH: the WSL
// bridge dirs (wslDirs) PREPENDED to the image's own entries (imageDirs),
// de-duplicated (first occurrence wins, so a dir both list stays in the WSL
// position) with blank entries dropped.
func mergeLdLibraryPath(wslDirs, imageDirs []string) string {
	seen := make(map[string]bool, len(wslDirs)+len(imageDirs))
	out := make([]string, 0, len(wslDirs)+len(imageDirs))
	for _, d := range append(append([]string{}, wslDirs...), imageDirs...) {
		if d = strings.TrimSpace(d); d == "" || seen[d] {
			continue
		}
		seen[d] = true
		out = append(out, d)
	}
	return strings.Join(out, ":")
}

// imageLdLookup returns the LD_LIBRARY_PATH entries a stack image's own config
// sets (from its Docker image Config.Env), or nil when the image is not present
// locally / sets no LD_LIBRARY_PATH. Injected so the pure spec rewrite in
// wslStackGPUBinds stays testable without a Docker daemon; a nil lookup means
// "no image env" (the WSL dirs alone are used).
type imageLdLookup func(ctx context.Context, image string) []string

// wslDxgEnvVars mirrors the env a node task gets in executor.go (~L1738-1743)
// for a container that maps /dev/dxg. Applied with setEnvVarIfAbsent: a stack
// author's own value (any value, including a deliberately different one)
// always wins. (LD_LIBRARY_PATH is handled separately in wslStackGPUBinds — it
// must be merged with the image's own loader path, not injected as a fixed
// string that would drop dirs like llama.cpp's /app.)
var wslDxgEnvVars = []struct{ key, value string }{
	{"HSA_ENABLE_DXG_DETECTION", "1"},
	// libhsa-runtime64 unconditionally links librocprofiler-register (verified
	// via ldd on ghcr.io/ggml-org/llama.cpp:server-rocm's ROCm 7.2.1), and
	// that component aborts GPU apps under WSL — the same reason the
	// node-task executor disables it.
	{"ROCPROFILER_REGISTER_ENABLED", "0"},
}

// wslStackGPUBinds gives every compose service that maps /dev/dxg the same
// host GPU access a node task gets: the /usr/lib/wsl bind (D3D12/DXCore user
// mode drivers), the host's librocdxg, and the wslDxgEnvVars above. A stack
// image cannot carry the driver libraries (they must match the host's
// Windows driver), and the env cannot be baked in per-host either, so without
// this a ROCm stack on a WSL2 box sees /dev/dxg but reports "no ROCm-capable
// device".
//
// LD_LIBRARY_PATH is the one var resolved per service (not from the shared
// wslDxgEnvVars): the WSL bridge dirs are PREPENDED to the image's own
// LD_LIBRARY_PATH (looked up from the local Docker image via imageLdLookup) so
// the image's own loader dirs (e.g. llama.cpp's /app) survive. When imageLd is
// nil, or the image is absent locally / sets no LD_LIBRARY_PATH, the WSL dirs
// alone are used (a correct-but-minimal fallback).
//
// Services without /dev/dxg, and targets the spec already mounts, are left
// alone. Returns the rewritten spec and the services changed (binds or env
// added); on any parse problem the spec is returned untouched.
func wslStackGPUBinds(ctx context.Context, spec, lib string, imageLd imageLdLookup) (string, []string) {
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &doc); err != nil || len(doc.Content) == 0 {
		return spec, nil
	}
	services := mapValue(doc.Content[0], "services")
	if services == nil || services.Kind != yaml.MappingNode {
		return spec, nil
	}
	binds := []string{"/usr/lib/wsl:/usr/lib/wsl:ro"}
	binds = append(binds, rocdxgBinds(lib)...)
	var changed []string
	for i := 0; i+1 < len(services.Content); i += 2 {
		name, svc := services.Content[i].Value, services.Content[i+1]
		if svc.Kind != yaml.MappingNode || !mapsDxg(mapValue(svc, "devices")) {
			continue
		}
		image := composeServiceImage(svc)
		var imageLdDirs []string
		if imageLd != nil {
			imageLdDirs = imageLd(ctx, image)
		}
		ldValue := mergeLdLibraryPath(wslDxgLdLibraryDirs, imageLdDirs)
		vols := mapValue(svc, "volumes")
		if vols == nil {
			vols = &yaml.Node{Kind: yaml.SequenceNode, Tag: "!!seq"}
			svc.Content = append(svc.Content,
				&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: "volumes"}, vols)
		}
		if vols.Kind != yaml.SequenceNode {
			continue
		}
		have := map[string]bool{}
		for _, v := range vols.Content {
			have[volumeTarget(v)] = true
		}
		added := false
		for _, b := range binds {
			if t := strings.SplitN(b, ":", 3)[1]; !have[t] {
				vols.Content = append(vols.Content,
					&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: b})
				have[t], added = true, true
			}
		}
		// ROCm finds the GPU through /dev/dxg only with these vars (same as
		// the node-task executor); never override an explicit compose value.
		// LD_LIBRARY_PATH is resolved per service above (WSL dirs prepended to
		// the image's own) and injected first so it is set for the rest.
		envNode := mapValue(svc, "environment")
		if envNode == nil {
			envNode = &yaml.Node{Kind: yaml.MappingNode}
			svc.Content = append(svc.Content,
				&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: "environment"}, envNode)
		}
		touchedEnv := false
		if setEnvVarIfAbsent(envNode, "LD_LIBRARY_PATH", ldValue) {
			touchedEnv = true
		}
		for _, kv := range wslDxgEnvVars {
			if setEnvVarIfAbsent(envNode, kv.key, kv.value) {
				touchedEnv = true
			}
		}
		if added || touchedEnv {
			changed = append(changed, name)
		}
	}
	if len(changed) == 0 {
		return spec, nil
	}
	out, err := yaml.Marshal(&doc)
	if err != nil {
		return spec, nil
	}
	sort.Strings(changed)
	return string(out), changed
}

// imageLdLibraryPathFromDocker implements imageLdLookup against the local
// Docker daemon: it ImageInspects the stack image and returns the
// LD_LIBRARY_PATH entries from its baked-in Config.Env.
//
// wslStackGPUBinds runs BEFORE prePullStackImages, and that pre-pull only covers
// Harbor images — so on a first deploy the image is often not local yet (the
// live case, ghcr.io/ggml-org/llama.cpp:server-rocm, is not Harbor at all). To
// keep a FIRST deploy correct rather than degrading to the WSL-dirs-only
// fallback (which drops the image's own loader dirs like llama.cpp's /app), the
// lookup ensures the image is present: if ImageInspect misses, it does a
// best-effort public pull (PullImagePublic, shared layer cache so compose up
// won't re-download) and re-inspects. A pull here is a no-op cost on a re-deploy
// (image already local → the inspect fast-path) and fails fast on a private
// image (no creds) → nil → fallback, which is correct because prePullStackImages
// pulls that image with its task credentials before compose up anyway.
//
// On any failure (image absent and un-pullable, inspect error, no
// LD_LIBRARY_PATH) it returns nil so the caller falls back to the WSL bridge
// dirs alone.
func (e *Executor) imageLdLibraryPathFromDocker(ctx context.Context, image string) []string {
	if image == "" || e == nil || e.docker == nil {
		return nil
	}
	if insp, _, err := e.docker.ImageInspectWithRaw(ctx, image); err == nil {
		return imageLdDirsFromEnv(insp)
	}
	// First deploy: not local. Ensure it, then read its baked-in env.
	if err := e.PullImagePublic(ctx, image); err != nil {
		return nil
	}
	insp, _, err := e.docker.ImageInspectWithRaw(ctx, image)
	if err != nil {
		return nil
	}
	return imageLdDirsFromEnv(insp)
}

// imageLdDirsFromEnv returns the LD_LIBRARY_PATH entries from an image's
// Config.Env, or nil when the image sets none.
func imageLdDirsFromEnv(insp types.ImageInspect) []string {
	if insp.Config == nil {
		return nil
	}
	for _, kv := range insp.Config.Env {
		if k, v, ok := cutEnvKV(kv); ok && k == "LD_LIBRARY_PATH" && v != "" {
			return strings.Split(v, ":")
		}
	}
	return nil
}

// wslRemapRocmDevices rewrites bare-metal ROCm device passthrough to the WSL2
// form. A compose written for native ROCm declares `devices: [/dev/kfd,
// /dev/dri]`, but a WSL2 host has NEITHER node — ROCm reaches the GPU through
// /dev/dxg alone (see rocdxgInContainer). Passing /dev/kfd to `compose up`
// there fails at the daemon with "error gathering device information ...
// /dev/kfd: no such file or directory", and the stack never starts. The
// node-task executor already remaps this at runtime; this brings stack deploys
// to parity so a ROCm compose written the normal way "just works" on WSL.
// Called only when /dev/dxg exists (WSL2). Returns the rewritten spec and the
// services whose devices changed. wslStackGPUBinds then adds the library binds
// (it keys on /dev/dxg, which this guarantees is present).
func wslRemapRocmDevices(spec string) (string, []string) {
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(spec), &doc); err != nil || len(doc.Content) == 0 {
		return spec, nil
	}
	services := mapValue(doc.Content[0], "services")
	if services == nil || services.Kind != yaml.MappingNode {
		return spec, nil
	}
	var changed []string
	for i := 0; i+1 < len(services.Content); i += 2 {
		name, svc := services.Content[i].Value, services.Content[i+1]
		if svc.Kind != yaml.MappingNode {
			continue
		}
		devices := mapValue(svc, "devices")
		if devices == nil || devices.Kind != yaml.SequenceNode {
			continue
		}
		var kept []*yaml.Node
		hadRocm, haveDxg := false, false
		for _, d := range devices.Content {
			base := strings.SplitN(deviceSource(d), ":", 2)[0]
			if base == "/dev/kfd" || base == "/dev/dri" || strings.HasPrefix(base, "/dev/dri/") {
				hadRocm = true
				continue // drop the bare-metal node — it does not exist on WSL2
			}
			if base == "/dev/dxg" {
				haveDxg = true
			}
			kept = append(kept, d)
		}
		if !hadRocm {
			continue
		}
		if !haveDxg {
			kept = append(kept, &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: "/dev/dxg"})
		}
		devices.Content = kept
		changed = append(changed, name)
	}
	if len(changed) == 0 {
		return spec, nil
	}
	out, err := yaml.Marshal(&doc)
	if err != nil {
		return spec, nil
	}
	sort.Strings(changed)
	return string(out), changed
}

// deviceSource is the host path of a compose device entry, in short
// ("src:dst[:perm]") or long ({source: src}) form.
func deviceSource(d *yaml.Node) string {
	if d.Kind == yaml.MappingNode {
		if s := mapValue(d, "source"); s != nil {
			return s.Value
		}
		return ""
	}
	return d.Value
}

// composeServiceImage returns a service's compose `image:` value, or "" when the
// service declares no image (built from a context). The value is a registry
// reference (host[:port]/path[:tag] or :digest); the caller matches it against
// the local Docker image config.
func composeServiceImage(svc *yaml.Node) string {
	if img := mapValue(svc, "image"); img != nil && img.Kind == yaml.ScalarNode {
		return img.Value
	}
	return ""
}

func mapValue(m *yaml.Node, key string) *yaml.Node {
	if m == nil || m.Kind != yaml.MappingNode {
		return nil
	}
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			return m.Content[i+1]
		}
	}
	return nil
}

func mapsDxg(devices *yaml.Node) bool {
	if devices == nil || devices.Kind != yaml.SequenceNode {
		return false
	}
	for _, d := range devices.Content {
		src := d.Value
		if d.Kind == yaml.MappingNode {
			if s := mapValue(d, "source"); s != nil {
				src = s.Value
			}
		}
		if strings.SplitN(src, ":", 2)[0] == "/dev/dxg" {
			return true
		}
	}
	return false
}

// volumeTarget is the container path of a short ("src:dst[:mode]") or long
// ({target: dst}) compose volume entry.
func volumeTarget(v *yaml.Node) string {
	if v.Kind == yaml.MappingNode {
		if t := mapValue(v, "target"); t != nil {
			return t.Value
		}
		return ""
	}
	parts := strings.Split(v.Value, ":")
	if len(parts) >= 2 {
		return parts[1]
	}
	return parts[0]
}
