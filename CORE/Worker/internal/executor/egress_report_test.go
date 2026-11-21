package executor

import (
	"fmt"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// logLine renders an egressproxy.LogLine as one JSON line, mirroring the proxy
// writer so the tests feed the same shape the aggregator consumes.
func logLine(t *testing.T, ev, host string, port int, reason string) string {
	t.Helper()
	var b strings.Builder
	b.WriteString(`{"ts":"2026-01-01T00:00:00Z","ev":`)
	b.WriteString(`"` + ev + `"`)
	if host != "" {
		b.WriteString(`,"host":`)
		b.WriteString(`"` + host + `"`)
	}
	if port != 0 {
		b.WriteString(`,"port":`)
		b.WriteString(itoa(port))
	}
	if reason != "" {
		b.WriteString(`,"reason":`)
		b.WriteString(`"` + reason + `"`)
	}
	b.WriteString("}")
	return b.String()
}

// itoa avoids importing strconv just for one test helper.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}

func TestAggregateProxyLogAllowAndDeny(t *testing.T) {
	log := strings.Join([]string{
		logLine(t, "egress_allow", "api.openai.com", 443, ""),
		logLine(t, "egress_deny", "evil.example.com", 8080, "not in allowlist"),
		logLine(t, "egress_deny", "evil.example.com", 8080, "not in allowlist"),
		logLine(t, "egress_deny", "suspicious.org", 53, "blocked"),
	}, "\n")

	report := aggregateProxyLog(strings.NewReader(log), []string{"api.openai.com"})

	if report.Mode != "proxy" {
		t.Fatalf("mode = %q, want proxy", report.Mode)
	}
	if len(report.AllowedHosts) != 1 || report.AllowedHosts[0] != "api.openai.com" {
		t.Fatalf("allowed hosts = %v, want [api.openai.com]", report.AllowedHosts)
	}
	if len(report.Denied) != 2 {
		t.Fatalf("denied tuples = %d, want 2", len(report.Denied))
	}
	// Sorted by count desc: the repeated evil.example.com first.
	if report.Denied[0].Host != "evil.example.com" || report.Denied[0].Count != 2 {
		t.Fatalf("top denied = %+v, want evil.example.com count 2", report.Denied[0])
	}
	if report.Denied[1].Host != "suspicious.org" || report.Denied[1].Reason != "blocked" {
		t.Fatalf("second denied = %+v, want suspicious.org blocked", report.Denied[1])
	}
}

func TestAggregateProxyLogDNSDenyCounted(t *testing.T) {
	log := strings.Join([]string{
		logLine(t, "dns_deny", "blocked.internal", 0, ""),
		logLine(t, "dns_deny", "blocked.internal", 0, ""),
		logLine(t, "dns_deny", "blocked.internal", 0, ""),
		logLine(t, "egress_allow", "allowed.com", 443, ""),
	}, "\n")

	report := aggregateProxyLog(strings.NewReader(log), []string{"allowed.com"})

	if len(report.Denied) != 1 {
		t.Fatalf("denied tuples = %d, want 1", len(report.Denied))
	}
	if report.Denied[0].Host != "blocked.internal" || report.Denied[0].Count != 3 {
		t.Fatalf("denied = %+v, want blocked.internal count 3", report.Denied[0])
	}
	if report.Denied[0].Reason != "dns_deny" {
		t.Fatalf("reason = %q, want dns_deny", report.Denied[0].Reason)
	}
	if report.DeniedTruncated {
		t.Fatal("DeniedTruncated should be false")
	}
}

func TestAggregateProxyLogCap50(t *testing.T) {
	var b strings.Builder
	// 60 DISTINCT tuples (distinct host) plus one more distinct tuple so the cap
	// boundary is unambiguous.
	for i := 0; i < 60; i++ {
		b.WriteString(logLine(t, "egress_deny", fmt.Sprintf("host%d", i), 443, "nope"))
		b.WriteByte('\n')
	}
	b.WriteString(logLine(t, "egress_deny", "other", 80, "nope"))

	report := aggregateProxyLog(strings.NewReader(b.String()), nil)

	if len(report.Denied) != 50 {
		t.Fatalf("denied tuples = %d, want capped at 50", len(report.Denied))
	}
	if !report.DeniedTruncated {
		t.Fatal("DeniedTruncated should be true past the cap")
	}
	// All counts are 1 and the slice is stable, so the first 50 are the first 50
	// distinct tuples emitted.
	for _, d := range report.Denied {
		if d.Count != 1 {
			t.Fatalf("count = %d, want 1", d.Count)
		}
	}
}

func TestAggregateProxyLogUndecodableSkipped(t *testing.T) {
	log := strings.Join([]string{
		"this is not json at all",
		`{"ev":`,                        // truncated line
		`{"ts":"2026-01-01T00:00:00Z"}`, // no ev field -> not a denial
		logLine(t, "egress_deny", "real.example", 443, "blocked"),
		"", // blank line
	}, "\n")

	report := aggregateProxyLog(strings.NewReader(log), nil)

	if len(report.Denied) != 1 {
		t.Fatalf("denied tuples = %d, want 1 (undecodable lines skipped)", len(report.Denied))
	}
	if report.Denied[0].Host != "real.example" {
		t.Fatalf("denied = %+v, want real.example", report.Denied[0])
	}
}

func TestAggregateProxyLogEmpty(t *testing.T) {
	report := aggregateProxyLog(strings.NewReader(""), []string{"a", "b"})
	if report.Mode != "proxy" {
		t.Fatalf("mode = %q, want proxy", report.Mode)
	}
	if len(report.AllowedHosts) != 2 {
		t.Fatalf("allowed hosts = %v, want 2 hosts", report.AllowedHosts)
	}
	if report.Denied == nil {
		t.Fatal("Denied should be non-nil (empty slice)")
	}
}

var _ = transport.EgressDenied{}
