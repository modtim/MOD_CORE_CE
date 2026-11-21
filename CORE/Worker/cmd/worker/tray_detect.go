package main

import (
	"os"
	"strings"
)

// inContainerEnv reports whether the worker appears to be running inside a
// container, so the tray (which needs a desktop) stays off. It is
// deliberately a cheap, read-only heuristic and never blocks on the Docker
// daemon: it checks the well-known Docker env vars and the presence of
// /.dockerenv.
func inContainerEnv() bool {
	for _, key := range []string{"DOCKER_CONTAINER_ID", "MOD_IN_CONTAINER", "container"} {
		if v := strings.ToLower(strings.TrimSpace(os.Getenv(key))); v == "true" || v == "1" || v == "yes" {
			return true
		}
	}
	if _, err := os.Stat("/.dockerenv"); err == nil {
		return true
	}
	return false
}
