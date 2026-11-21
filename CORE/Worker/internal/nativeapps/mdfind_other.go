//go:build !darwin

package nativeapps

// noopMdfind — Spotlight/mdfind don't exist off macOS, so bundle_id lookups
// always report not-found rather than failing the build.
type noopMdfind struct{}

func (noopMdfind) Find(bundleID string) (string, bool) { return "", false }

var defaultMdfind mdfindRunner = noopMdfind{}
