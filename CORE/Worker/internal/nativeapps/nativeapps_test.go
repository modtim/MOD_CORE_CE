package nativeapps

import (
	"errors"
	"testing"
)

type fakeRegistry struct {
	entries map[string]string
}

func (f fakeRegistry) Lookup(exeName string) (string, bool) {
	p, ok := f.entries[exeName]
	return p, ok
}

type fakeMdfind struct {
	entries map[string]string
}

func (f fakeMdfind) Find(bundleID string) (string, bool) {
	p, ok := f.entries[bundleID]
	return p, ok
}

func fakeLookPath(found map[string]string) lookPathFunc {
	return func(file string) (string, error) {
		if p, ok := found[file]; ok {
			return p, nil
		}
		return "", errors.New("not found")
	}
}

func TestDetectWith_WindowsRegistryHit(t *testing.T) {
	// The fake registryReader stands in for winRegistryReader — cleaning of
	// the raw registry value (quotes/semicolon suffix) is that real
	// implementation's job (see TestCleanAppPathValue), so this fixture
	// returns an already-clean path and only exercises the orchestration.
	reg := fakeRegistry{entries: map[string]string{"blender.exe": `C:\Program Files\Blender\blender.exe`}}
	specs := []Spec{{Name: "Blender", WindowsRegistry: "blender.exe"}}

	got := detectWith(specs, reg, fakeMdfind{}, fakeLookPath(nil))

	if len(got) != 1 {
		t.Fatalf("expected 1 detected app, got %d", len(got))
	}
	if got[0].Path != `C:\Program Files\Blender\blender.exe` {
		t.Errorf("expected registry path passed through, got %q", got[0].Path)
	}
	if got[0].Name != "Blender" {
		t.Errorf("expected name Blender, got %q", got[0].Name)
	}
}

func TestDetectWith_WindowsRegistryMiss_FallsThrough(t *testing.T) {
	specs := []Spec{{Name: "Blender", WindowsRegistry: "blender.exe", Path: "blender"}}

	got := detectWith(specs, fakeRegistry{}, fakeMdfind{}, fakeLookPath(map[string]string{"blender": "/usr/bin/blender"}))

	if len(got) != 1 || got[0].Path != "/usr/bin/blender" {
		t.Fatalf("expected PATH fallback to fire, got %+v", got)
	}
}

func TestDetectWith_BundleIDHit(t *testing.T) {
	md := fakeMdfind{entries: map[string]string{
		"org.blenderfoundation.blender": "/Applications/Blender.app",
	}}
	specs := []Spec{{Name: "Blender", BundleID: "org.blenderfoundation.blender"}}

	got := detectWith(specs, fakeRegistry{}, md, fakeLookPath(nil))

	if len(got) != 1 || got[0].Path != "/Applications/Blender.app" {
		t.Fatalf("expected bundle hit, got %+v", got)
	}
	// No Info.plist on disk in this test — version read fails silently and
	// the app is still reported (path detection succeeded).
	if got[0].Version != "" {
		t.Errorf("expected empty version without a real Info.plist, got %q", got[0].Version)
	}
}

func TestDetectWith_NoMatchAnywhere_Omitted(t *testing.T) {
	specs := []Spec{{Name: "Blender", WindowsRegistry: "blender.exe", BundleID: "org.blender", Path: "blender"}}

	got := detectWith(specs, fakeRegistry{}, fakeMdfind{}, fakeLookPath(nil))

	if len(got) != 0 {
		t.Fatalf("expected no detections, got %+v", got)
	}
}

func TestDetectWith_MultipleSpecsIndependent(t *testing.T) {
	specs := []Spec{
		{Name: "Blender", Path: "blender"},
		{Name: "Missing", Path: "doesnotexist"},
		{Name: "FFmpeg", Path: "ffmpeg"},
	}
	got := detectWith(specs, fakeRegistry{}, fakeMdfind{}, fakeLookPath(map[string]string{
		"blender": "/usr/bin/blender",
		"ffmpeg":  "/usr/bin/ffmpeg",
	}))

	if len(got) != 2 {
		t.Fatalf("expected 2 detections, got %d: %+v", len(got), got)
	}
	names := map[string]bool{got[0].Name: true, got[1].Name: true}
	if !names["Blender"] || !names["FFmpeg"] {
		t.Errorf("expected Blender+FFmpeg, got %+v", got)
	}
}

func TestCleanAppPathValue(t *testing.T) {
	cases := map[string]string{
		`"C:\Blender\blender.exe"`:             `C:\Blender\blender.exe`,
		`C:\Blender\blender.exe`:               `C:\Blender\blender.exe`,
		`"C:\Blender\blender.exe";C:\Blender\`: `C:\Blender\blender.exe`,
		`  C:\Blender\blender.exe  `:           `C:\Blender\blender.exe`,
	}
	for in, want := range cases {
		if got := cleanAppPathValue(in); got != want {
			t.Errorf("cleanAppPathValue(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestParseSpecsFromEnv(t *testing.T) {
	specs := ParseSpecsFromEnv("Blender:registry:blender.exe, FFmpeg:path:ffmpeg ,Bad:entry, Foo:bundle:com.foo.app,Skip:unknown:x,:path:noname")

	if len(specs) != 3 {
		t.Fatalf("expected 3 valid specs, got %d: %+v", len(specs), specs)
	}
	if specs[0].Name != "Blender" || specs[0].WindowsRegistry != "blender.exe" {
		t.Errorf("unexpected spec[0]: %+v", specs[0])
	}
	if specs[1].Name != "FFmpeg" || specs[1].Path != "ffmpeg" {
		t.Errorf("unexpected spec[1]: %+v", specs[1])
	}
	if specs[2].Name != "Foo" || specs[2].BundleID != "com.foo.app" {
		t.Errorf("unexpected spec[2]: %+v", specs[2])
	}
}

func TestParseSpecsFromEnv_Empty(t *testing.T) {
	if got := ParseSpecsFromEnv(""); len(got) != 0 {
		t.Errorf("expected no specs from empty string, got %+v", got)
	}
}
