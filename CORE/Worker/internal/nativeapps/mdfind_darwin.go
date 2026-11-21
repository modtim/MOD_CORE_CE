//go:build darwin

package nativeapps

import (
	"fmt"
	"os/exec"
	"strings"
)

// mdfindExec resolves a macOS app bundle by CFBundleIdentifier via
// Spotlight's `mdfind` CLI — the requires_app.detect.bundle_id strategy.
type mdfindExec struct{}

func (mdfindExec) Find(bundleID string) (string, bool) {
	query := fmt.Sprintf("kMDItemCFBundleIdentifier == '%s'", bundleID)
	out, err := exec.Command("mdfind", query).Output()
	if err != nil {
		return "", false
	}
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if line != "" {
			return line, true
		}
	}
	return "", false
}

var defaultMdfind mdfindRunner = mdfindExec{}
