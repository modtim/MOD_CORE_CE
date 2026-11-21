//go:build windows

package nativeapps

import "golang.org/x/sys/windows/registry"

// winRegistryReader reads the Windows "App Paths" registry convention:
// HKLM/HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\<exe>'s
// default value is the full path to that executable. Installers of nearly
// every major desktop app (Blender included) register this key, which is
// why it is the primary detection strategy for requires_app.detect.windows_registry.
type winRegistryReader struct{}

func (winRegistryReader) Lookup(exeName string) (string, bool) {
	const base = `SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\`
	for _, root := range []registry.Key{registry.LOCAL_MACHINE, registry.CURRENT_USER} {
		k, err := registry.OpenKey(root, base+exeName, registry.QUERY_VALUE)
		if err != nil {
			continue
		}
		v, _, err := k.GetStringValue("")
		k.Close()
		if err == nil && v != "" {
			return cleanAppPathValue(v), true
		}
	}
	return "", false
}

var defaultRegistry registryReader = winRegistryReader{}
