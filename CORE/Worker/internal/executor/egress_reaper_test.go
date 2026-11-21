package executor

import (
	"testing"
	"time"
)

// TestEgressNetworkReaper drives the pure shouldPruneEgressNetwork policy across
// the table of cases the reaper depends on. It is pure by design — no docker
// daemon, no Executor, no network scan — so it can run in any environment,
// including the offline CI where the daemon is absent.
func TestEgressNetworkReaper(t *testing.T) {
	const (
		selfWorker   = "worker-A"
		selfInstance = "instance-1"
	)

	// Own labels: same worker name as the sweeping instance.
	ownLabels := map[string]string{
		sidecarLabel:         "1",
		sidecarWorkerLabel:   selfWorker,
		sidecarInstanceLabel: selfInstance,
	}
	// Orphan labels: same worker name, different (dead) instance.
	ownOrphanLabels := map[string]string{
		sidecarLabel:         "1",
		sidecarWorkerLabel:   selfWorker,
		sidecarInstanceLabel: "instance-9",
	}
	// Foreign labels: a different worker on a shared host.
	foreignLabels := map[string]string{
		sidecarLabel:         "1",
		sidecarWorkerLabel:   "worker-B",
		sidecarInstanceLabel: "instance-2",
	}
	// Not-sidecar: no sidecarLabel, so we do not own it.
	otherLabels := map[string]string{"mod.task": "some-other-task"}

	tests := []struct {
		name     string
		network  string
		labels   map[string]string
		worker   string
		instance string
		age      time.Duration
		attached int
		want     bool
	}{
		{
			name:    "mod-egr without sidecarLabel is never ours",
			network: "mod-egr-11111111",
			labels:  otherLabels,
			want:    false,
		},
		{
			name:    "non-egress name with sidecarLabel is not managed",
			network: "some-other-net",
			labels:  ownLabels,
			want:    false,
		},
		{
			name:     "our live network young with proxy attached is kept",
			network:  "mod-egr-11111111",
			labels:   ownLabels,
			worker:   selfWorker,
			instance: selfInstance,
			age:      1 * time.Minute,
			attached: 1, // only the proxy
			want:     false,
		},
		{
			name:     "our live network young no extra attachments is kept",
			network:  "mod-egr-22222222",
			labels:   ownLabels,
			worker:   selfWorker,
			instance: selfInstance,
			age:      5 * time.Minute,
			attached: 0,
			want:     false,
		},
		{
			name:     "our live network past age with extra attachment is kept",
			network:  "mod-egr-33333333",
			labels:   ownLabels,
			worker:   selfWorker,
			instance: selfInstance,
			age:      48 * time.Hour,
			attached: 1,
			want:     false,
		},
		{
			name:     "our live network past age no attachments is pruned (age backstop)",
			network:  "mod-egr-44444444",
			labels:   ownLabels,
			worker:   selfWorker,
			instance: selfInstance,
			age:      48 * time.Hour,
			attached: 0,
			want:     true,
		},
		{
			name:     "our own network even very young is kept",
			network:  "mod-egr-55555555",
			labels:   ownLabels,
			worker:   selfWorker,
			instance: selfInstance,
			age:      1 * time.Hour,
			attached: 0,
			want:     false,
		},
		{
			name:     "orphan (same worker, dead instance) is pruned",
			network:  "mod-egr-66666666",
			labels:   ownOrphanLabels,
			worker:   selfWorker,
			instance: selfInstance,
			age:      1 * time.Minute,
			attached: 1,
			want:     true,
		},
		{
			name:     "foreign worker network in use is kept",
			network:  "mod-egr-77777777",
			labels:   foreignLabels,
			worker:   selfWorker,
			instance: selfInstance,
			age:      1 * time.Minute,
			attached: 1,
			want:     false,
		},
		{
			name:     "foreign worker network past backstop with nothing attached is pruned",
			network:  "mod-egr-88888888",
			labels:   foreignLabels,
			worker:   selfWorker,
			instance: selfInstance,
			age:      sidecarStuckAfter() + time.Hour,
			attached: 0,
			want:     true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := shouldPruneEgressNetwork(tc.network, tc.labels, tc.worker, tc.instance, tc.age, tc.attached)
			if got != tc.want {
				t.Fatalf("shouldPruneEgressNetwork(%q, age=%v, attached=%d) = %v, want %v",
					tc.network, tc.age, tc.attached, got, tc.want)
			}
		})
	}
}

// TestEgressProxyName ensures the derived proxy name matches setupEgressProxy's
// naming scheme, so removeEgressProxy can find the proxy container from a
// network alone.
func TestEgressProxyName(t *testing.T) {
	const net = "mod-egr-deadbe"
	if got := egressProxyName(net); got != "mod-egr-proxy-deadbe" {
		t.Fatalf("egressProxyName(%q) = %q, want mod-egr-proxy-deadbe", net, got)
	}
}

// TestContainerMatchesExact checks both slash-prefixed and bare forms docker may
// surface.
func TestContainerMatchesExact(t *testing.T) {
	if !containerMatchesExact([]string{"/mod-egr-proxy-deadbe"}, "mod-egr-proxy-deadbe") {
		t.Fatalf("expected match for slash-prefixed name")
	}
	if !containerMatchesExact([]string{"mod-egr-proxy-deadbe"}, "mod-egr-proxy-deadbe") {
		t.Fatalf("expected match for bare name")
	}
	if containerMatchesExact([]string{"mod-egr-proxy-deadbe01", "other"}, "mod-egr-proxy-deadbe") {
		t.Fatalf("substring name must not match exactly")
	}
}
