package releases

import (
	"fmt"
	"os"
	"strings"
)

// PayloadVersion reads the version of the binaries being published, from the
// VERSION file baked beside them in the image.
//
// WHY NOT THE RUNNING BINARY'S VERSION. They are the same thing only until the
// embedded worker self-updates: after that, /usr/local/bin/mod-worker is newer
// than /releases/, and publishing the running version against the baked
// binaries produces a manifest that advertises one version and ships another.
// A remote worker installs it, reports the older number, sees the newer one
// still advertised, and updates again — a loop that re-downloads on every
// heartbeat cycle and never converges.
//
// The manifest's only job is that its version and its bytes agree. This reads
// the version from the same place the bytes come from, so they cannot drift.
func PayloadVersion(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read %s: %w", path, err)
	}
	v := strings.TrimSpace(string(raw))
	if v == "" {
		return "", fmt.Errorf("%s is empty", path)
	}
	// Same placeholders PublishIfNewer refuses, rejected here too so the caller
	// gets one answer rather than two different ones.
	switch v {
	case "dev", "docker", "unknown":
		return "", fmt.Errorf("%s holds a placeholder (%q)", path, v)
	}
	return v, nil
}
