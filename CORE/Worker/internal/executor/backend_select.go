package executor

import (
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"

	"k8s.io/client-go/discovery"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/k8sclient"
)

// k8sNamespaceEnvVar names the namespace K8sBackend submits task Jobs into.
// config.Config has no namespace field (design.md "Config / tiering"), so it
// is read from the environment here.
const k8sNamespaceEnvVar = "MOD_K8S_NAMESPACE"

// DefaultK8sTaskNamespace is used when MOD_K8S_NAMESPACE is unset.
const DefaultK8sTaskNamespace = "mod-tasks"

// k8sEgressFQDNEnvVar (GB.EGR.4) selects how K8sBackend.FQDNMode is chosen:
//
//   - "auto" (default): probe the cluster for the Cilium CRD
//     (ciliumnetworkpolicies.cilium.io/v2) via the discovery client. Present
//     -> behave as "cilium". Absent, or the probe errors (no Cilium, no
//     RBAC, unreachable API server, ...) -> "off" with one WARN log; this is
//     the fail-open path (GB.EGR §6a) — allowlisted tasks still run, just
//     unenforced (K8sBackend.BuildJob's mod.tech/egress-allowlist=unenforced
//     annotation).
//   - "cilium": require Cilium. If the CRD is missing (or the probe itself
//     errors), NewBackendFromConfig fails at startup instead of silently
//     degrading — an operator who explicitly asked for enforcement should
//     not get an unenforced allowlist without knowing it.
//   - "off": never build a Dynamic client / never probe for Cilium; today's
//     behavior (K8sBackend.FQDNMode stays its NewK8sBackend default, "off").
const k8sEgressFQDNEnvVar = "MOD_K8S_EGRESS_FQDN"

const (
	fqdnFlagAuto   = "auto"
	fqdnFlagCilium = "cilium"
	fqdnFlagOff    = "off"
)

// ciliumCRDDiscoveryTimeout bounds the "auto" mode's Cilium probe so a
// blackholed (as opposed to promptly-refused) API server can't hang worker
// startup — it only affects the discovery call, never the dynamic client
// used afterwards to actually create CiliumNetworkPolicies.
const ciliumCRDDiscoveryTimeout = 5 * time.Second

// loadK8sClient builds the client-go clientset (in-cluster first, then
// kubeconfig — see k8sclient.LoadConfig). A package var so tests can stub it.
var loadK8sClient = func(kubeconfigPath string) (kubernetes.Interface, error) {
	return k8sclient.LoadClientset(kubeconfigPath)
}

// loadK8sRestConfig resolves the same in-cluster/kubeconfig *rest.Config
// loadK8sClient's clientset is built from — GB.EGR.4 needs the *rest.Config
// itself to also build a discovery client (Cilium CRD probe) and a dynamic
// client (CiliumNetworkPolicy creation). A package var so tests can stub it.
var loadK8sRestConfig = func(kubeconfigPath string) (*rest.Config, error) {
	return k8sclient.LoadConfig(kubeconfigPath)
}

// newDynamicClientForConfig builds the dynamic.Interface used to create
// CiliumNetworkPolicies (K8sBackend.Dynamic). A package var so tests can
// stub it without a live cluster.
var newDynamicClientForConfig = func(cfg *rest.Config) (dynamic.Interface, error) {
	return dynamic.NewForConfig(cfg)
}

// ciliumCRDPresent reports whether the cluster exposes the
// CiliumNetworkPolicy CRD (cilium.io/v2), the signal GB.EGR.4's "auto" mode
// uses to decide whether allowlisted tasks can be enforced. It probes with a
// short-timeout copy of cfg (ciliumCRDDiscoveryTimeout) so a blackholed API
// server fails the probe instead of hanging worker startup; the caller's own
// *rest.Config (used for the dynamic client / clientset) is never mutated.
// A package var so tests can stub it without a live cluster.
var ciliumCRDPresent = func(cfg *rest.Config) (bool, error) {
	probeCfg := *cfg
	if probeCfg.Timeout <= 0 || probeCfg.Timeout > ciliumCRDDiscoveryTimeout {
		probeCfg.Timeout = ciliumCRDDiscoveryTimeout
	}
	disc, err := discovery.NewDiscoveryClientForConfig(&probeCfg)
	if err != nil {
		return false, err
	}
	resources, err := disc.ServerResourcesForGroupVersion(CiliumNetworkPolicyGVR.GroupVersion().String())
	if err != nil {
		return false, err
	}
	for _, r := range resources.APIResources {
		if r.Name == CiliumNetworkPolicyGVR.Resource {
			return true, nil
		}
	}
	return false, nil
}

// K8sTaskNamespace returns the namespace for task Jobs.
func K8sTaskNamespace() string {
	if v := strings.TrimSpace(os.Getenv(k8sNamespaceEnvVar)); v != "" {
		return v
	}
	return DefaultK8sTaskNamespace
}

// NewBackendFromConfig picks the ephemeral-task Backend per
// cfg.ExecutorBackend (k8s-reduction-engine T10):
//
//   - default / "docker": DockerBackend wrapping exec (today's pipeline).
//   - "k8s": K8sBackend over a client-go clientset built from
//     cfg.KubeconfigPath (or in-cluster ServiceAccount config).
//
// Fail-safe: when k8s is requested but the client cannot be constructed, an
// error is returned — it does NOT silently fall back to Docker, because an
// operator who asked for K8s Jobs should not get tasks quietly running on the
// worker host instead.
func NewBackendFromConfig(cfg *config.Config, exec *Executor, log *slog.Logger) (Backend, error) {
	if cfg == nil || !cfg.IsK8sExecutorBackend() {
		return NewDockerBackend(exec), nil
	}
	ns := K8sTaskNamespace()
	client, err := loadK8sClient(cfg.KubeconfigPath)
	if err != nil {
		if log != nil {
			log.Error("executor_backend=k8s requested but the Kubernetes client could not be built; refusing to fall back to docker",
				"kubeconfig", cfg.KubeconfigPath, "namespace", ns, "error", err)
		}
		return nil, fmt.Errorf("executor backend k8s: %w", err)
	}
	if log != nil {
		log.Info("executor backend selected", "backend", "k8s", "namespace", ns, "kubeconfig", cfg.KubeconfigPath)
	}
	backend := NewK8sBackend(client, ns)
	if err := applyEgressFQDNMode(backend, cfg.KubeconfigPath, log); err != nil {
		return nil, fmt.Errorf("executor backend k8s: %w", err)
	}
	return backend, nil
}

// applyEgressFQDNMode resolves MOD_K8S_EGRESS_FQDN (GB.EGR.4) and sets
// backend.FQDNMode / backend.Dynamic accordingly. See k8sEgressFQDNEnvVar's
// doc comment for the auto/cilium/off contract.
func applyEgressFQDNMode(backend *K8sBackend, kubeconfigPath string, log *slog.Logger) error {
	mode := strings.ToLower(strings.TrimSpace(os.Getenv(k8sEgressFQDNEnvVar)))
	if mode == "" {
		mode = fqdnFlagAuto
	}

	switch mode {
	case fqdnFlagOff:
		backend.FQDNMode = FQDNModeOff
		return nil

	case fqdnFlagCilium:
		restCfg, err := loadK8sRestConfig(kubeconfigPath)
		if err != nil {
			return fmt.Errorf("%s=cilium: loading kubeconfig: %w", k8sEgressFQDNEnvVar, err)
		}
		present, err := ciliumCRDPresent(restCfg)
		if err != nil {
			return fmt.Errorf("%s=cilium: checking for the Cilium CRD: %w", k8sEgressFQDNEnvVar, err)
		}
		if !present {
			return fmt.Errorf("%s=cilium but the cluster has no %s CRD (%s)",
				k8sEgressFQDNEnvVar, CiliumNetworkPolicyGVR.Resource, CiliumNetworkPolicyGVR.GroupVersion().String())
		}
		dyn, err := newDynamicClientForConfig(restCfg)
		if err != nil {
			return fmt.Errorf("%s=cilium: building dynamic client: %w", k8sEgressFQDNEnvVar, err)
		}
		backend.FQDNMode = FQDNModeCilium
		backend.Dynamic = dyn
		return nil

	case fqdnFlagAuto:
		restCfg, err := loadK8sRestConfig(kubeconfigPath)
		if err != nil {
			warnCiliumNotDetected(log, err)
			backend.FQDNMode = FQDNModeOff
			return nil
		}
		present, err := ciliumCRDPresent(restCfg)
		if err != nil || !present {
			warnCiliumNotDetected(log, err)
			backend.FQDNMode = FQDNModeOff
			return nil
		}
		dyn, err := newDynamicClientForConfig(restCfg)
		if err != nil {
			warnCiliumNotDetected(log, err)
			backend.FQDNMode = FQDNModeOff
			return nil
		}
		backend.FQDNMode = FQDNModeCilium
		backend.Dynamic = dyn
		return nil

	default:
		return fmt.Errorf("%s=%q: unknown value (want %s, %s or %s)",
			k8sEgressFQDNEnvVar, mode, fqdnFlagAuto, fqdnFlagCilium, fqdnFlagOff)
	}
}

// warnCiliumNotDetected logs the single WARN GB.EGR.4 requires for the
// "auto, no Cilium" fallback (fail-open: allowlisted tasks still run, just
// unenforced). err is nil when the CRD lookup succeeded but simply found no
// Cilium; non-nil when the probe itself couldn't be completed.
func warnCiliumNotDetected(log *slog.Logger, err error) {
	if log == nil {
		return
	}
	if err != nil {
		log.Warn("Cilium not detected — allowlisted tasks run UNENFORCED", "error", err)
		return
	}
	log.Warn("Cilium not detected — allowlisted tasks run UNENFORCED")
}
