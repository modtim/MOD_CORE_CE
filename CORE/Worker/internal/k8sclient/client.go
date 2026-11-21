// Package k8sclient loads a Kubernetes REST config / clientset for the
// k8s-reduction-engine backend (design.md "Config / tiering", task T7).
//
// This package is CONNECTION ONLY — it does not create/list/watch any K8s
// object. T2 (K8sBackend, Jobs) and T4 (K8sStackBackend, Deployments/
// Services/PVCs) call LoadClientset to get a ready *kubernetes.Clientset
// and then own all further API calls themselves.
//
// Resolution order (standard client-go convention):
//  1. In-cluster ServiceAccount config (rest.InClusterConfig) — used when the
//     worker itself runs as a pod (its ServiceAccount is granted the RBAC in
//     CORE/Worker/k8s/rbac.yaml).
//  2. An explicit kubeconfig file path (config.KubeconfigPath / $KUBECONFIG),
//     for a worker that runs OUTSIDE the cluster but submits Jobs/Deployments
//     into it (e.g. the on-prem manager driving a separate K8s cluster).
package k8sclient

import (
	"fmt"
	"os"
	"path/filepath"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

// LoadConfig resolves a *rest.Config using in-cluster config first, then
// falling back to a kubeconfig file. kubeconfigPath may be empty, in which
// case $KUBECONFIG (if set) or the default ~/.kube/config is used.
func LoadConfig(kubeconfigPath string) (*rest.Config, error) {
	if cfg, err := rest.InClusterConfig(); err == nil {
		return cfg, nil
	}

	path := resolveKubeconfigPath(kubeconfigPath)
	if path == "" {
		return nil, fmt.Errorf("k8sclient: not running in-cluster and no kubeconfig path resolved " +
			"(pass one explicitly, set $KUBECONFIG, or place a config at ~/.kube/config)")
	}

	cfg, err := clientcmd.BuildConfigFromFlags("", path)
	if err != nil {
		return nil, fmt.Errorf("k8sclient: loading kubeconfig %s: %w", path, err)
	}
	return cfg, nil
}

// LoadClientset resolves a *rest.Config (see LoadConfig) and builds a
// kubernetes.Clientset from it. This is the entry point T2/T4 use to get a
// live API client; it performs no cluster calls itself (NewForConfig is
// local construction only — the first real round-trip happens on T2/T4's
// first Submit/Deploy call).
func LoadClientset(kubeconfigPath string) (*kubernetes.Clientset, error) {
	cfg, err := LoadConfig(kubeconfigPath)
	if err != nil {
		return nil, err
	}

	clientset, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return nil, fmt.Errorf("k8sclient: building clientset: %w", err)
	}
	return clientset, nil
}

// resolveKubeconfigPath picks, in order: the explicit argument, $KUBECONFIG,
// then $HOME/.kube/config if it exists. Returns "" if none apply.
func resolveKubeconfigPath(explicit string) string {
	if explicit != "" {
		return explicit
	}
	if v := os.Getenv("KUBECONFIG"); v != "" {
		return v
	}
	if home, err := os.UserHomeDir(); err == nil {
		def := filepath.Join(home, ".kube", "config")
		if _, statErr := os.Stat(def); statErr == nil {
			return def
		}
	}
	return ""
}
