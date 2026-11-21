// Package egress holds the egress allowlist grammar shared by the Docker
// per-task egress proxy (GB.EGR.D.x) and the Kubernetes CiliumNetworkPolicy
// generator (GB.EGR.2), so both enforce exactly the same entries.
//
// Grammar (one entry per string, surrounding whitespace ignored):
//
//	example.com            exact host, default ports (80, 443)
//	example.com:8443       exact host, that port only
//	*.example.com          any subdomain of example.com (NOT the apex), default ports
//	*.example.com:8443     any subdomain, that port only
//	10.1.2.3 / 10.1.2.3:9000
//	2001:db8::1 / [2001:db8::1] / [2001:db8::1]:9000
//	10.0.0.0/8 / fd00::/8  CIDR, default ports
//
// Rejected: empty entries, "*" alone, "*" anywhere except a leading "*.",
// wildcard suffixes with a single label ("*.com"), ports outside 1-65535,
// IPv6 zones, and malformed hostnames.
//
// Private-address policy (owner answer E3): an exact hostname may resolve to
// a private/LAN address; a wildcard match never may. Match reports which
// kind of rule matched so the proxy can apply that rule. IP and CIDR
// literals are explicit operator statements and count as exact.
package egress

import (
	"fmt"
	"net/netip"
	"sort"
	"strconv"
	"strings"
)

// DefaultPorts are the ports allowed for an entry that names no port.
var DefaultPorts = []int{80, 443}

// Kind is the type of an allowlist rule.
type Kind int

const (
	KindHost     Kind = iota // exact hostname
	KindWildcard             // *.suffix
	KindPrefix               // IP literal (as /32 or /128) or CIDR
)

func (k Kind) String() string {
	switch k {
	case KindHost:
		return "host"
	case KindWildcard:
		return "wildcard"
	case KindPrefix:
		return "prefix"
	}
	return "unknown"
}

// Rule is one parsed allowlist entry.
type Rule struct {
	Kind Kind
	// Host is the lowercase hostname for KindHost, or the suffix without
	// the leading "*." for KindWildcard. Empty for KindPrefix.
	Host string
	// Prefix is set for KindPrefix. An IP literal is a single-address prefix.
	Prefix netip.Prefix
	// Port is the explicit port, or 0 meaning DefaultPorts.
	Port int
	// Raw is the original (trimmed) entry.
	Raw string
}

// Ports returns the ports this rule allows.
func (r Rule) Ports() []int {
	if r.Port != 0 {
		return []int{r.Port}
	}
	return append([]int(nil), DefaultPorts...)
}

func (r Rule) allowsPort(port int) bool {
	if r.Port != 0 {
		return port == r.Port
	}
	for _, p := range DefaultPorts {
		if p == port {
			return true
		}
	}
	return false
}

// Allowlist is an immutable, parsed set of rules. Safe for concurrent use.
type Allowlist struct {
	rules []Rule
}

// Rules returns a copy of the parsed rules in input order (for CNP generation).
func (a *Allowlist) Rules() []Rule {
	if a == nil {
		return nil
	}
	return append([]Rule(nil), a.rules...)
}

// Len returns the number of rules.
func (a *Allowlist) Len() int {
	if a == nil {
		return 0
	}
	return len(a.rules)
}

// Parse parses allowlist entries. Any invalid entry fails the whole list
// (fail closed). Empty entries are skipped. Duplicate entries are collapsed.
func Parse(entries []string) (*Allowlist, error) {
	a := &Allowlist{}
	seen := make(map[string]bool)
	for _, raw := range entries {
		e := strings.TrimSpace(raw)
		if e == "" {
			continue
		}
		r, err := parseEntry(e)
		if err != nil {
			return nil, fmt.Errorf("egress: entry %q: %w", e, err)
		}
		key := r.Kind.String() + "|" + r.Host + "|" + r.Prefix.String() + "|" + strconv.Itoa(r.Port)
		if seen[key] {
			continue
		}
		seen[key] = true
		a.rules = append(a.rules, r)
	}
	return a, nil
}

// ParseList splits a comma-separated list (e.g. MOD_EGRESS_ALLOW) and parses
// it. Empty items between commas are ignored; an all-empty list yields an
// empty allowlist that matches nothing.
func ParseList(s string) (*Allowlist, error) {
	var entries []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			entries = append(entries, p)
		}
	}
	return Parse(entries)
}

func parseEntry(e string) (Rule, error) {
	r := Rule{Raw: e}
	if e == "*" {
		return r, fmt.Errorf("bare \"*\" is not allowed")
	}

	// CIDR (no port syntax supported on CIDRs).
	if strings.Contains(e, "/") {
		if strings.Contains(e, "%") {
			return r, fmt.Errorf("IPv6 zones are not allowed")
		}
		p, err := netip.ParsePrefix(e)
		if err != nil {
			return r, fmt.Errorf("invalid CIDR: %v", err)
		}
		r.Kind = KindPrefix
		r.Prefix = unmapPrefix(p.Masked())
		return r, nil
	}

	// Bracketed IPv6, optional port.
	if strings.HasPrefix(e, "[") {
		end := strings.Index(e, "]")
		if end < 0 {
			return r, fmt.Errorf("unterminated '['")
		}
		addr, rest := e[1:end], e[end+1:]
		ip, err := parseIP(addr)
		if err != nil {
			return r, err
		}
		if !ip.Is6() {
			return r, fmt.Errorf("brackets are only valid around IPv6 addresses")
		}
		if rest != "" {
			if !strings.HasPrefix(rest, ":") {
				return r, fmt.Errorf("unexpected text after ']'")
			}
			if r.Port, err = parsePort(rest[1:]); err != nil {
				return r, err
			}
		}
		r.Kind = KindPrefix
		r.Prefix = netip.PrefixFrom(ip, ip.BitLen())
		return r, nil
	}

	// Unbracketed IPv6 literal (two or more colons): no port allowed.
	if strings.Count(e, ":") >= 2 {
		ip, err := parseIP(e)
		if err != nil {
			return r, err
		}
		r.Kind = KindPrefix
		r.Prefix = netip.PrefixFrom(ip, ip.BitLen())
		return r, nil
	}

	host := e
	if i := strings.LastIndex(e, ":"); i >= 0 {
		host = e[:i]
		p, err := parsePort(e[i+1:])
		if err != nil {
			return r, err
		}
		r.Port = p
	}

	// IPv4 literal.
	if ip, err := netip.ParseAddr(host); err == nil {
		if !ip.Is4() {
			return r, fmt.Errorf("invalid address")
		}
		r.Kind = KindPrefix
		r.Prefix = netip.PrefixFrom(ip, 32)
		return r, nil
	}

	host = strings.TrimSuffix(strings.ToLower(host), ".")
	if strings.HasPrefix(host, "*.") {
		suffix := host[2:]
		if err := validateHostname(suffix); err != nil {
			return r, fmt.Errorf("invalid wildcard suffix: %v", err)
		}
		if !strings.Contains(suffix, ".") {
			return r, fmt.Errorf("wildcard suffix must have at least two labels (\"*.%s\" is too broad)", suffix)
		}
		r.Kind = KindWildcard
		r.Host = suffix
		return r, nil
	}
	if strings.Contains(host, "*") {
		return r, fmt.Errorf("\"*\" is only allowed as a leading \"*.\"")
	}
	if err := validateHostname(host); err != nil {
		return r, err
	}
	r.Kind = KindHost
	r.Host = host
	return r, nil
}

func parseIP(s string) (netip.Addr, error) {
	if strings.Contains(s, "%") {
		return netip.Addr{}, fmt.Errorf("IPv6 zones are not allowed")
	}
	ip, err := netip.ParseAddr(s)
	if err != nil {
		return netip.Addr{}, fmt.Errorf("invalid IP address: %v", err)
	}
	return ip.Unmap(), nil
}

func parsePort(s string) (int, error) {
	if s == "" {
		return 0, fmt.Errorf("empty port")
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return 0, fmt.Errorf("invalid port %q", s)
		}
	}
	p, err := strconv.Atoi(s)
	if err != nil || p < 1 || p > 65535 {
		return 0, fmt.Errorf("port %q out of range 1-65535", s)
	}
	return p, nil
}

// validateHostname enforces RFC 1123 labels (underscore tolerated for
// internal service names). An all-numeric final label is rejected so that
// partial IPv4 forms like "127.1" can never be treated as hostnames.
func validateHostname(h string) error {
	if h == "" {
		return fmt.Errorf("empty hostname")
	}
	if len(h) > 253 {
		return fmt.Errorf("hostname too long")
	}
	labels := strings.Split(h, ".")
	for _, l := range labels {
		if l == "" || len(l) > 63 {
			return fmt.Errorf("invalid label %q", l)
		}
		if l[0] == '-' || l[len(l)-1] == '-' {
			return fmt.Errorf("label %q starts or ends with '-'", l)
		}
		for _, c := range l {
			ok := (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_'
			if !ok {
				return fmt.Errorf("invalid character %q in hostname", c)
			}
		}
	}
	last := labels[len(labels)-1]
	if strings.Trim(last, "0123456789") == "" {
		return fmt.Errorf("numeric final label (not a valid hostname or IPv4 address)")
	}
	return nil
}

func unmapPrefix(p netip.Prefix) netip.Prefix {
	a := p.Addr()
	if a.Is4In6() && p.Bits() >= 96 {
		return netip.PrefixFrom(a.Unmap(), p.Bits()-96)
	}
	return p
}

// normalizeHost lowercases, strips a trailing dot and IPv6 brackets.
func normalizeHost(h string) string {
	h = strings.TrimSpace(h)
	if strings.HasPrefix(h, "[") && strings.HasSuffix(h, "]") {
		h = h[1 : len(h)-1]
	}
	return strings.TrimSuffix(strings.ToLower(h), ".")
}

// Match reports whether host:port is allowed. host may be a hostname or an
// IP literal (brackets optional for IPv6). exact is false only when the sole
// matching rule(s) are wildcards; an exact host, IP or CIDR hit wins over a
// wildcard hit. For an IP literal only IP/CIDR rules are consulted.
func (a *Allowlist) Match(host string, port int) (ok bool, exact bool) {
	if a == nil || port < 1 || port > 65535 {
		return false, false
	}
	h := normalizeHost(host)
	if h == "" {
		return false, false
	}
	if ip, err := netip.ParseAddr(h); err == nil {
		if ip.Zone() != "" {
			return false, false
		}
		m := a.MatchIP(ip, port)
		return m, m
	}
	wild := false
	for _, r := range a.rules {
		if !r.allowsPort(port) {
			continue
		}
		switch r.Kind {
		case KindHost:
			if h == r.Host {
				return true, true
			}
		case KindWildcard:
			if strings.HasSuffix(h, "."+r.Host) {
				wild = true
			}
		}
	}
	return wild, false
}

// MatchIP reports whether an IP literal target is covered by an IP/CIDR
// rule on that port. IPv4-mapped IPv6 addresses are unmapped first.
func (a *Allowlist) MatchIP(ip netip.Addr, port int) bool {
	if a == nil || !ip.IsValid() {
		return false
	}
	ip = ip.Unmap()
	for _, r := range a.rules {
		if r.Kind == KindPrefix && r.allowsPort(port) && r.Prefix.Contains(ip) {
			return true
		}
	}
	return false
}

// Hosts returns the sorted exact hostnames and wildcard patterns ("*.x"),
// e.g. for MOD_ALLOWED_EGRESS_HOSTS or CNP toFQDNs.
func (a *Allowlist) Hosts() []string {
	var out []string
	for _, r := range a.Rules() {
		switch r.Kind {
		case KindHost:
			out = append(out, r.Host)
		case KindWildcard:
			out = append(out, "*."+r.Host)
		}
	}
	sort.Strings(out)
	return out
}

var (
	privatePrefixes = mustPrefixes(
		"10.0.0.0/8",     // RFC1918
		"172.16.0.0/12",  // RFC1918
		"192.168.0.0/16", // RFC1918
		"127.0.0.0/8",    // loopback
		"100.64.0.0/10",  // CGNAT (also Tailscale/headscale)
		"169.254.0.0/16", // IPv4 link-local
		"0.0.0.0/8",      // "this network"
		"::1/128",        // IPv6 loopback
		"::/128",         // unspecified
		"fc00::/7",       // ULA
		"fe80::/10",      // IPv6 link-local
	)
	metadataPrefixes = mustPrefixes(
		"169.254.0.0/16",    // IPv4 link-local incl. 169.254.169.254 (AWS/GCP/Azure/OpenStack)
		"fe80::/10",         // IPv6 link-local
		"fd00:ec2::254/128", // AWS IMDS IPv6
	)
)

func mustPrefixes(ss ...string) []netip.Prefix {
	out := make([]netip.Prefix, 0, len(ss))
	for _, s := range ss {
		out = append(out, netip.MustParsePrefix(s))
	}
	return out
}

func inAny(ip netip.Addr, ps []netip.Prefix) bool {
	if !ip.IsValid() {
		return false
	}
	ip = ip.Unmap()
	for _, p := range ps {
		if p.Contains(ip) {
			return true
		}
	}
	return false
}

// IsPrivate reports whether ip is RFC1918, loopback, CGNAT (100.64/10),
// ULA (fc00::/7), link-local, or unspecified. IPv4-mapped IPv6 is unmapped.
// Invalid addresses are reported private (fail closed).
func IsPrivate(ip netip.Addr) bool {
	if !ip.IsValid() {
		return true
	}
	return inAny(ip, privatePrefixes)
}

// IsMetadata reports whether ip is a link-local / cloud metadata address
// (169.254.0.0/16, fe80::/10, fd00:ec2::254). These are always denied unless
// explicitly allowlisted as an IP/CIDR literal.
func IsMetadata(ip netip.Addr) bool {
	return inAny(ip, metadataPrefixes)
}
