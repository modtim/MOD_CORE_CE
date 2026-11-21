package availability

import "testing"

func TestParseLocalModeTable(t *testing.T) {
	cases := []struct {
		in     string
	 mode   LocalMode
	 accepted bool
	}{
		{"lend", LocalModeLend, true},
		{"pause", LocalModePause, true},
		{"none", LocalModeNone, true},
		{"", LocalModeNone, false},
		{"LEND", LocalModeNone, false},
		{"paused", LocalModeNone, false},
		{"resume", LocalModeNone, false},
		{"lend ", LocalModeNone, false},
		{"auto", LocalModeNone, false},
		{"pause; rm", LocalModeNone, false},
	}
	for _, c := range cases {
		got, err := ParseLocalMode(c.in)
		if got != c.mode {
			t.Errorf("ParseLocalMode(%q) = %q, want %q", c.in, got, c.mode)
		}
		if (err == nil) != c.accepted {
			t.Errorf("ParseLocalMode(%q) accepted=%v, want %v", c.in, err == nil, c.accepted)
		}
	}
}
