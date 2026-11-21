package heartbeat

import (
	"net"
	"net/url"
	"strings"
	"time"
)

// advertiseAddress reports the address a worker believes the MANAGER can reach
// it on, derived from the route to the manager itself.
//
// WHY THIS AND NOT A HOSTNAME. `worker_licenses.hostname` holds whatever the
// host calls itself — "example-host" — which resolves nowhere outside that machine.
// The manager needs an address, and only the worker can know one: it sits
// behind whatever NAT, VPN or LAN its operator chose. `executor.go:1337`
// already states the rule from the other direction.
//
// HOW. Opening a UDP socket sends no packets; it only makes the kernel pick a
// source address for that destination. So the answer is "the interface I would
// reach the manager from", which is self-configuring across every topology we
// care about: a LAN worker reports its LAN IP, and a worker whose route to the
// manager runs over a tailnet reports its tailnet IP without being told a mesh
// exists.
//
// It is a CANDIDATE, never a promise. Routing is not always symmetric and NAT
// can rewrite anything, so a worker on another site may report an address the
// manager cannot dial. The manager verifies before using it — this function's
// job is to offer the best guess the only party with the information can make.
func advertiseAddress(apiURL string) string {
	return advertiseAddressTo(managerHost(apiURL))
}

// advertiseAddressTo returns the source address the kernel would use to reach
// `host` — i.e. the interface/overlay IP this worker is reachable on FROM that
// host's side. dual-anchor-mesh-federation §2.3: called once per anchor (site
// control plane, cloud rendezvous) so the manager's heartbeat registry can
// drive site-first / cloud-fallback resolution. Empty host or no route ⇒ "".
func advertiseAddressTo(host string) string {
	if host == "" {
		return ""
	}
	// Port 80 is arbitrary: nothing is sent; an unconnectable port still yields
	// the source address the route would use.
	conn, err := net.DialTimeout("udp", net.JoinHostPort(host, "80"), 2*time.Second)
	if err != nil {
		return ""
	}
	defer conn.Close()

	addr, ok := conn.LocalAddr().(*net.UDPAddr)
	if !ok || addr.IP == nil || addr.IP.IsUnspecified() || addr.IP.IsLoopback() {
		return ""
	}
	return addr.IP.String()
}

// managerHost pulls the bare host out of the configured API URL.
func managerHost(apiURL string) string {
	raw := strings.TrimSpace(apiURL)
	if raw == "" {
		return ""
	}
	if !strings.Contains(raw, "://") {
		raw = "http://" + raw
	}
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return u.Hostname()
}
