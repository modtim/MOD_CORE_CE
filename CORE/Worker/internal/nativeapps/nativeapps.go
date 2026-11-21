// Package nativeapps detects customer-installed native applications on a
// native (Windows/macOS) worker, for the launch-completion-megaplan GP.4 /
// D-P2 heartbeat field: the worker reports detected apps and versions, and
// the API's claim guard (worker_tasks._try_claim_task) requires the app
// named by a task's task_payload.requires_app before claiming.
//
// D-P2 is explicit: MOD never installs or bundles a customer app. This
// package only DETECTS what the customer already installed.
//
// The OS-specific probes (Windows registry, macOS mdfind) sit behind small
// interfaces so the orchestration and parsing logic here is fully
// unit-testable on Linux; only the real registry/mdfind implementations
// carry build tags (registry_windows.go / registry_other.go,
// mdfind_darwin.go / mdfind_other.go).
package nativeapps

import (
	"os/exec"
	"strings"
)

// DetectedApp is a single application discovered on this worker. Sent
// verbatim in the heartbeat's native_apps field
// (hardware_specs['native_apps'] on the API side).
type DetectedApp struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	Path    string `json:"path"`
}

// Spec is one app to probe for, mirroring a node recipe's
// requires_app.detect (CORE/API/schemas.py
// _validate_native_recipe_metadata): at most one strategy is set per Spec.
type Spec struct {
	// Name is what's reported back and matched against task_payload's
	// requires_app.name at claim time.
	Name string

	// WindowsRegistry is the exe name to resolve via the "App Paths"
	// registry key (e.g. "blender.exe"). Windows only; a no-op elsewhere.
	WindowsRegistry string

	// BundleID is the macOS CFBundleIdentifier to resolve via mdfind (e.g.
	// "org.blenderfoundation.blender"). macOS only; a no-op elsewhere.
	BundleID string

	// Path is an executable name to resolve via the PATH, on any OS. Used
	// either alone or as a fallback alongside WindowsRegistry/BundleID.
	Path string
}

// registryReader abstracts the Windows "App Paths" registry lookup so the
// orchestration logic below is testable without a real Windows registry.
type registryReader interface {
	// Lookup returns the resolved executable path for exeName (e.g.
	// "blender.exe") and whether it was found.
	Lookup(exeName string) (path string, ok bool)
}

// mdfindRunner abstracts the macOS `mdfind` Spotlight query so the
// orchestration logic below is testable without a real Spotlight index.
type mdfindRunner interface {
	// Find returns the app bundle path for bundleID and whether it was found.
	Find(bundleID string) (bundlePath string, ok bool)
}

// lookPathFunc matches exec.LookPath's signature; overridden in tests.
type lookPathFunc func(file string) (string, error)

var defaultLookPath lookPathFunc = exec.LookPath

// Detect probes every spec using the real, platform-appropriate backends.
func Detect(specs []Spec) []DetectedApp {
	return detectWith(specs, defaultRegistry, defaultMdfind, defaultLookPath)
}

// detectWith is Detect with its OS backends injected, so the resolution
// order and fallbacks are unit-testable on any platform.
func detectWith(specs []Spec, reg registryReader, md mdfindRunner, lookPath lookPathFunc) []DetectedApp {
	out := make([]DetectedApp, 0, len(specs))
	for _, s := range specs {
		if app, ok := detectOne(s, reg, md, lookPath); ok {
			out = append(out, app)
		}
	}
	return out
}

func detectOne(s Spec, reg registryReader, md mdfindRunner, lookPath lookPathFunc) (DetectedApp, bool) {
	if s.WindowsRegistry != "" {
		if p, ok := reg.Lookup(s.WindowsRegistry); ok && p != "" {
			return DetectedApp{Name: s.Name, Path: p}, true
		}
	}
	if s.BundleID != "" {
		if bp, ok := md.Find(s.BundleID); ok && bp != "" {
			ver, _ := ReadBundleShortVersion(bp)
			return DetectedApp{Name: s.Name, Path: bp, Version: ver}, true
		}
	}
	if s.Path != "" {
		if p, err := lookPath(s.Path); err == nil && p != "" {
			return DetectedApp{Name: s.Name, Path: p}, true
		}
	}
	return DetectedApp{}, false
}

// cleanAppPathValue normalises a raw "App Paths" default registry value.
// Some installers quote the path, and some append extra PATH directories
// after a semicolon (a second REG_SZ convention some installers repurpose
// the default value for) — only the executable path itself is wanted.
// Pure string logic, so it's testable without a real registry.
func cleanAppPathValue(v string) string {
	v = strings.TrimSpace(v)
	if strings.HasPrefix(v, `"`) {
		// Quoted path: the value is everything between the two quotes,
		// regardless of what an installer appended after the closing
		// quote (some tack on ";<extra dir>").
		if end := strings.Index(v[1:], `"`); end >= 0 {
			return v[1 : end+1]
		}
	}
	if i := strings.Index(v, ";"); i >= 0 {
		v = v[:i]
	}
	return strings.Trim(strings.TrimSpace(v), `"`)
}
