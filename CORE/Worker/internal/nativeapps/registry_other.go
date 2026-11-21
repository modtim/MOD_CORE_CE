//go:build !windows

package nativeapps

// noopRegistryReader — the Windows "App Paths" registry doesn't exist off
// Windows, so every lookup reports not-found rather than failing the build.
type noopRegistryReader struct{}

func (noopRegistryReader) Lookup(exeName string) (string, bool) { return "", false }

var defaultRegistry registryReader = noopRegistryReader{}
