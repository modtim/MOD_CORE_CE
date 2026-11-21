package executor

import (
	"testing"

	"github.com/docker/docker/api/types/container"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// TestExecutorEgress covers D.7b: the unenforced-egress report the executor
// attaches to a run when the network was an allowlist verdict but the proxy
// was not engaged. The report carries Mode "unenforced" and mirrors the
// declared allowlist in AllowedHosts.
func TestExecutorEgress(t *testing.T) {
	t.Run("unenforced allowlist yields report", func(t *testing.T) {
		rep := unenforcedEgressReport(NetworkSelection{
			FromAllowlist: true,
			EgressHosts:   []string{"api.example.com:443"},
		})
		if rep == nil {
			t.Fatal("expected a report, got nil")
		}
		if rep.Mode != "unenforced" {
			t.Errorf("Mode = %q, want %q", rep.Mode, "unenforced")
		}
		if len(rep.AllowedHosts) != 1 || rep.AllowedHosts[0] != "api.example.com:443" {
			t.Errorf("AllowedHosts = %v, want [api.example.com:443]", rep.AllowedHosts)
		}
	})

	t.Run("allowed hosts copied verbatim", func(t *testing.T) {
		rep := unenforcedEgressReport(NetworkSelection{
			FromAllowlist: true,
			EgressHosts:   []string{"api.example.com:443", "huggingface.co"},
		})
		if rep == nil || len(rep.AllowedHosts) != 2 || rep.AllowedHosts[1] != "huggingface.co" {
			t.Errorf("AllowedHosts = %v, want [api.example.com:443 huggingface.co]", rep)
		}
	})

	// The deferred copy in Run stamps Egress onto whatever *Result Run returns
	// (via named returns + one defer), and never dereferences a nil Result.
	t.Run("report is a valid transport.EgressReport", func(t *testing.T) {
		var rep *transport.EgressReport
		rep = unenforcedEgressReport(NetworkSelection{
			FromAllowlist: true,
			EgressHosts:   []string{"h"},
		})
		if rep == nil || rep.Mode != "unenforced" {
			t.Errorf("report did not carry the unenforced mode: %+v", rep)
		}
	})
}

// applyEgressHandle is the pure D.7c injector: it rewrites the container's
// network onto the bridge's private internal net, appends the proxy task env,
// and points DNS at the proxy's stub. The proxied path (runContainerWithCreds)
// calls it only when netSel.Proxied, so these tests pin that contract.
func TestExecutorEgressApplyHandle(t *testing.T) {
	h := &egressProxyHandle{
		Network: "mod-egr-task1234",
		Env:     []string{"HTTP_PROXY=http://mod-egress-proxy:3128", "MOD_EGRESS_ENFORCED=true"},
		DNS:     []string{"10.231.0.2"},
	}

	t.Run("rewrites network, appends env, sets DNS", func(t *testing.T) {
		network := "mod-user-egress"
		env := []string{"API_URL=http://x"}
		hc := &container.HostConfig{NetworkMode: container.NetworkMode("mod-user-egress")}

		applyEgressHandle(h, &network, &env, hc)

		if string(hc.NetworkMode) != "mod-egr-task1234" {
			t.Errorf("hostConfig.NetworkMode = %q, want %q (task would bypass the proxy)", hc.NetworkMode, "mod-egr-task1234")
		}
		if network != "mod-egr-task1234" {
			t.Errorf("network = %q, want %q", network, "mod-egr-task1234")
		}
		if env[0] != "API_URL=http://x" {
			t.Errorf("initial env not preserved: %v", env)
		}
		if len(env) != 3 || env[1] != "HTTP_PROXY=http://mod-egress-proxy:3128" || env[2] != "MOD_EGRESS_ENFORCED=true" {
			t.Errorf("env = %v", env)
		}
		if len(hc.DNS) != 1 || hc.DNS[0] != "10.231.0.2" {
			t.Errorf("DNS = %v, want [10.231.0.2]", hc.DNS)
		}
	})

	t.Run("empty starting values are initialized", func(t *testing.T) {
		h2 := &egressProxyHandle{
			Network: "mod-egr-abc",
			Env:     []string{"MOD_EGRESS_ENFORCED=true"},
			DNS:     []string{"10.0.0.9"},
		}
		var network string
		var env []string
		hc := &container.HostConfig{}

		applyEgressHandle(h2, &network, &env, hc)

		if network != "mod-egr-abc" || env[0] != "MOD_EGRESS_ENFORCED=true" || hc.DNS[0] != "10.0.0.9" {
			t.Errorf("empty starting values not initialized: net=%q env=%v dns=%v", network, env, hc.DNS)
		}
	})
}
