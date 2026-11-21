package executor

import "github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"

// K8sEgressReport builds the EgressReport for a K8s task (GB.EGR.10a).
//
// It returns nil unless spec.Egress == EgressAllowlist — default-deny and
// user-egress tasks carry no allowlist to report, matching Docker's path
// (which only reports the proxy log for allowlisted tasks).
//
// Mode is "cilium" when fqdnMode == FQDNModeCilium — the per-task
// CiliumNetworkPolicy is applied in K8sBackend.Submit (k8s_backend.go) and
// is the enforcement mechanism — and "unenforced" (the value the pod is
// annotated mod.tech/egress-allowlist: unenforced with in that case) for any
// other FQDN mode.
func K8sEgressReport(spec *TaskSpec, fqdnMode string) *transport.EgressReport {
	if spec == nil || spec.Egress != EgressAllowlist {
		return nil
	}
	mode := EgressAllowlistUnenforced
	if fqdnMode == FQDNModeCilium {
		mode = "cilium"
	}
	return &transport.EgressReport{
		Mode:         mode,
		AllowedHosts: spec.EgressHosts,
	}
}
