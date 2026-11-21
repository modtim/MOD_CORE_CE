package nativeapps

import (
	"os"
	"strings"
)

// ParseSpecsFromEnv parses the WORKER_NATIVE_APP_PROBES env var into Specs.
// Format: comma-separated "Name:strategy:value" entries, e.g.
//
//	"Blender:registry:blender.exe,Blender:bundle:org.blenderfoundation.blender,FFmpeg:path:ffmpeg"
//
// strategy is one of "registry" (Windows App Paths), "bundle" (macOS
// CFBundleIdentifier), or "path" (PATH lookup, any OS) — mirroring the D-P1
// requires_app.detect keys (windows_registry/bundle_id/path) one-for-one.
// Malformed entries are skipped rather than failing the whole list, since
// this drives a best-effort heartbeat field, not a hard requirement.
func ParseSpecsFromEnv(raw string) []Spec {
	var specs []Spec
	for _, entry := range strings.Split(raw, ",") {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		parts := strings.SplitN(entry, ":", 3)
		if len(parts) != 3 {
			continue
		}
		name := strings.TrimSpace(parts[0])
		strategy := strings.ToLower(strings.TrimSpace(parts[1]))
		value := strings.TrimSpace(parts[2])
		if name == "" || value == "" {
			continue
		}
		s := Spec{Name: name}
		switch strategy {
		case "registry":
			s.WindowsRegistry = value
		case "bundle":
			s.BundleID = value
		case "path":
			s.Path = value
		default:
			continue
		}
		specs = append(specs, s)
	}
	return specs
}

// SpecsFromEnv reads WORKER_NATIVE_APP_PROBES from the environment.
func SpecsFromEnv() []Spec {
	return ParseSpecsFromEnv(os.Getenv("WORKER_NATIVE_APP_PROBES"))
}
