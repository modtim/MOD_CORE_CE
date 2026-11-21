package executor

import (
	"bufio"
	"encoding/json"
	"io"
	"sort"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/egressproxy"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// egressReportDeniedCap is the maximum number of distinct denied
// (host, port, reason) tuples reported in an EgressReport. Tuples past this
// count are folded into DeniedTruncated rather than flooding the report.
const egressReportDeniedCap = 50

// aggregateProxyLog folds a line-oriented egress proxy log (JSON lines of
// egressproxy.LogLine, plus the DNS stub's dns_deny lines) into an
// *transport.EgressReport. Mode is always "proxy"; AllowedHosts is the caller's
// configured host list. Denials are aggregated by (host, port, reason) with a
// count, sorted by count desc and capped at egressReportDeniedCap. Lines that
// do not decode into a LogLine are skipped.
func aggregateProxyLog(r io.Reader, hosts []string) *transport.EgressReport {
	report := &transport.EgressReport{
		Mode:         "proxy",
		AllowedHosts: hosts,
	}

	type key struct {
		host   string
		port   int
		reason string
	}
	agg := make(map[key]int)
	var order []key

	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var ll egressproxy.LogLine
		if err := json.Unmarshal(line, &ll); err != nil {
			continue
		}
		if ll.Ev != "egress_deny" && ll.Ev != "dns_deny" {
			continue
		}

		// LogLine carries no reason for the DNS stub's dns_deny lines (those
		// write a separate DNSLogLine shape), so fall back to the event name as
		// the reason only when none was decoded.
		reason := ll.Reason
		if reason == "" {
			reason = ll.Ev
		}

		k := key{host: ll.Host, port: ll.Port, reason: reason}
		if _, ok := agg[k]; !ok {
			order = append(order, k)
		}
		agg[k]++
	}

	report.Denied = make([]transport.EgressDenied, 0, len(order))
	for _, k := range order {
		report.Denied = append(report.Denied, transport.EgressDenied{
			Host:   k.host,
			Port:   k.port,
			Reason: k.reason,
			Count:  agg[k],
		})
	}

	sort.SliceStable(report.Denied, func(i, j int) bool {
		return report.Denied[i].Count > report.Denied[j].Count
	})
	if len(report.Denied) > egressReportDeniedCap {
		report.Denied = report.Denied[:egressReportDeniedCap]
		report.DeniedTruncated = true
	}

	return report
}
