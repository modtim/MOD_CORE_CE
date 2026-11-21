package capabilities

// Intel VRAM resolution and the JSON-ish line extractor.
//
// `knownIntelVRAMGB` is load-bearing for the multi-vendor managed-AI work: the
// PCI BAR reader it exists to pre-empt is power-of-two sized and reports a
// 24 GB card as 32 GB, and every placement floor downstream is compared against
// whatever number lands here. A card that claims 8 GB more than it has does not
// fail at placement — it fails when the model is already loading.

import "testing"

func TestKnownIntelVRAMGB_MatchesPublishedSpecs(t *testing.T) {
	cases := map[string]float64{
		"0xe211": 24.0,  // Arc Pro B60
		"0xe212": 16.0,  // Arc Pro B50
		"0xe20b": 12.0,  // Arc B580
		"0xe20c": 12.0,  // Arc B580, second device id
		"0xe20d": 10.0,  // Arc B570
		"0x56a0": 16.0,  // Arc A770 16GB
		"0x56a1": 8.0,   // Arc A750 / A770 8GB
		"0x56a5": 6.0,   // Arc A380
		"0x56a6": 4.0,   // Arc A310
		"0x0bd5": 48.0,  // Max 1100
		"0x0bd9": 128.0, // Max 1550
		"0x56c0": 16.0,  // Flex 170
		"0x56c1": 24.0,  // Flex 140, 2x12
	}
	for id, want := range cases {
		if got := knownIntelVRAMGB(id); got != want {
			t.Errorf("knownIntelVRAMGB(%s) = %v, want %v", id, got, want)
		}
	}
}

func TestKnownIntelVRAMGB_UnknownDeviceReturnsZeroRatherThanAGuess(t *testing.T) {
	// Zero is the signal to fall through to the BAR reader. Returning a
	// plausible default instead would be the worst option available: an
	// unrecognised card would silently advertise a number nobody measured, and
	// it would look exactly like a known one to everything downstream.
	for _, id := range []string{"0xffff", "0x0000", "", "not-a-device-id", "0x56a"} {
		if got := knownIntelVRAMGB(id); got != 0 {
			t.Errorf("knownIntelVRAMGB(%q) = %v, want 0 so the caller falls through", id, got)
		}
	}
}

func TestKnownIntelVRAMGB_NormalisesCaseAndWhitespace(t *testing.T) {
	// sysfs reads arrive with a trailing newline, and vendor tools disagree on
	// hex case. Both spellings have to resolve or a real card reads as unknown.
	for _, id := range []string{"0xE211", "0xe211\n", "  0xE211  ", "\t0xe211\t"} {
		if got := knownIntelVRAMGB(id); got != 24.0 {
			t.Errorf("knownIntelVRAMGB(%q) = %v, want 24 — a real card must not read as unknown", id, got)
		}
	}
}

func TestExtractJSONStringValue(t *testing.T) {
	cases := []struct {
		name string
		line string
		want string
	}{
		{"quoted value with trailing comma", `    "name": "Intel Arc A770",`, "Intel Arc A770"},
		{"quoted value, no comma", `"name": "Intel Arc A770"`, "Intel Arc A770"},
		{"value containing a colon", `"uuid": "GPU-abc:def"`, "GPU-abc:def"},
		{"inner comma survives", `"name": "Card, revision 2"`, "Card, revision 2"},
		{"unquoted numeric", `"vram": 16384,`, "16384"},
		{"empty value", `"name": ""`, ""},
		{"no colon at all", `not a json line`, ""},
	}
	for _, c := range cases {
		if got := extractJSONStringValue(c.line); got != c.want {
			t.Errorf("%s: extractJSONStringValue(%q) = %q, want %q", c.name, c.line, got, c.want)
		}
	}
}

func TestExtractJSONStringValue_TrimsOnlyAtTheEnds(t *testing.T) {
	// It strips `"` and `,` with strings.Trim, which operates on the ENDS only.
	// A value that genuinely ends in a comma loses it — documented here rather
	// than fixed, because the only caller parses device names from vendor tool
	// output and no device name ends in a comma.
	if got := extractJSONStringValue(`"name": "Card,"`); got != "Card" {
		t.Errorf("got %q, want %q — trailing comma inside the quotes is stripped", got, "Card")
	}
}
