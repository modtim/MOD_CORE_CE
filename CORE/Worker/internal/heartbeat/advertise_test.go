package heartbeat

import "testing"

func TestManagerHostParsesEveryFormWeConfigure(t *testing.T) {
	cases := map[string]string{
		"https://example.com":      "example.com",
		"http://198.51.100.105":                "198.51.100.105",
		"http://api:80/":                       "api",
		"example.com":              "example.com", // scheme-less is configured in the wild
		"https://example.com:8443": "example.com",
		"":                                     "",
		"   ":                                  "",
	}
	for in, want := range cases {
		if got := managerHost(in); got != want {
			t.Errorf("managerHost(%q) = %q, want %q", in, got, want)
		}
	}
}

// An unusable answer must be no answer. The manager treats an absent address as
// "unknown" and falls back; a loopback or 0.0.0.0 would be a confident lie that
// sends it dialling itself.
func TestNoAddressIsReportedWithoutAManager(t *testing.T) {
	if got := advertiseAddress(""); got != "" {
		t.Errorf("advertiseAddress(\"\") = %q, want empty", got)
	}
}

func TestAnUnresolvableManagerYieldsNoAddress(t *testing.T) {
	if got := advertiseAddress("http://no-such-host.invalid"); got != "" {
		t.Errorf("advertiseAddress(unresolvable) = %q, want empty", got)
	}
}
