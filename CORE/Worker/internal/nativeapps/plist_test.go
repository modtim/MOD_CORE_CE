package nativeapps

import "testing"

const samplePlist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleIdentifier</key>
	<string>org.blenderfoundation.blender</string>
	<key>CFBundleShortVersionString</key>
	<string>4.1.2</string>
	<key>CFBundleVersion</key>
	<string>4.1.2.abc123</string>
</dict>
</plist>
`

func TestParseBundleShortVersion(t *testing.T) {
	got, err := ParseBundleShortVersion([]byte(samplePlist))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "4.1.2" {
		t.Errorf("got %q, want 4.1.2", got)
	}
}

func TestParseBundleShortVersion_Missing(t *testing.T) {
	plist := `<?xml version="1.0"?><plist version="1.0"><dict>
		<key>CFBundleIdentifier</key><string>org.foo</string>
	</dict></plist>`
	_, err := ParseBundleShortVersion([]byte(plist))
	if err == nil {
		t.Fatal("expected an error when CFBundleShortVersionString is absent")
	}
}

func TestParseBundleShortVersion_KeyOrderVariance(t *testing.T) {
	// The version key appears LAST, after other unrelated dict entries —
	// makes sure the state machine doesn't get stuck on an earlier "key".
	plist := `<?xml version="1.0"?><plist version="1.0"><dict>
		<key>CFBundleIdentifier</key><string>org.foo</string>
		<key>CFBundleName</key><string>Foo</string>
		<key>CFBundleShortVersionString</key><string>9.9.9</string>
	</dict></plist>`
	got, err := ParseBundleShortVersion([]byte(plist))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "9.9.9" {
		t.Errorf("got %q, want 9.9.9", got)
	}
}

func TestParseBundleShortVersion_Malformed(t *testing.T) {
	_, err := ParseBundleShortVersion([]byte("not xml at all"))
	if err == nil {
		t.Fatal("expected an error for malformed input")
	}
}

func TestReadBundleShortVersion_MissingFile(t *testing.T) {
	_, err := ReadBundleShortVersion("/nonexistent/path/Foo.app")
	if err == nil {
		t.Fatal("expected an error reading a nonexistent Info.plist")
	}
}
