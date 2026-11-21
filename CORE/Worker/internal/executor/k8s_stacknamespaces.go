package executor

// k8s-reduction-engine W22: stack observation/fencing scope + stack CPU/mem.
//
// Namespace scope. Single-namespace mode (MOD_K8S_STACK_NAMESPACE / chart
// stacks.namespace) lists stacks in that one namespace, as before. The default
// namespace-per-tenant mode used to list with metav1.NamespaceAll, which needs
// cluster-wide deployments/pods list and would also see workloads MOD does
// not own. It now enumerates only namespaces labelled
// app.kubernetes.io/managed-by=mod (the API stamps that on every tenant-<id>
// namespace it creates) and lists stacks per namespace. Nothing here ever
// deletes anything.
//
// Stack CPU/mem. NewRESTPodMetricsSource (k8s_metrics.go) reports the `task`
// container of a node-task pod; stack pods have no such container, so the
// stack observer uses the same metrics.k8s.io GET but sums every container.

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"sync"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// K8sTenantNamespaceSelector selects the per-tenant namespaces the API
// creates (label stamped by the API's K8s stack backend).
const K8sTenantNamespaceSelector = "app.kubernetes.io/managed-by=mod"

// stackNamespaces returns the namespaces to observe/fence: the configured one
// in single-namespace mode, otherwise the MOD-managed tenant namespaces
// (sorted). A namespace list failure is an error ("could not look").
func (o *K8sStackObserver) stackNamespaces(ctx context.Context) ([]string, error) {
	if o.Namespace != "" {
		return []string{o.Namespace}, nil
	}
	sel := o.NamespaceSelector
	if sel == "" {
		sel = K8sTenantNamespaceSelector
	}
	nsl, err := o.Client.CoreV1().Namespaces().List(ctx, metav1.ListOptions{LabelSelector: sel})
	if err != nil {
		return nil, fmt.Errorf("listing MOD tenant namespaces (%s): %w", sel, err)
	}
	out := make([]string, 0, len(nsl.Items))
	for i := range nsl.Items {
		out = append(out, nsl.Items[i].Name)
	}
	sort.Strings(out)
	return out, nil
}

// listStackDeployments lists stack Deployments in every stack namespace. Any
// per-namespace failure fails the whole listing (a partial view would claim
// stacks are absent that were never looked at).
func (o *K8sStackObserver) listStackDeployments(ctx context.Context, namespaces []string, selector string) ([]appsv1.Deployment, error) {
	var out []appsv1.Deployment
	for _, ns := range namespaces {
		l, err := o.Client.AppsV1().Deployments(ns).List(ctx, metav1.ListOptions{LabelSelector: selector})
		if err != nil {
			return nil, fmt.Errorf("namespace %s: %w", ns, err)
		}
		out = append(out, l.Items...)
	}
	return out, nil
}

func (o *K8sStackObserver) listStackPods(ctx context.Context, namespaces []string, selector string) ([]corev1.Pod, error) {
	var out []corev1.Pod
	for _, ns := range namespaces {
		l, err := o.Client.CoreV1().Pods(ns).List(ctx, metav1.ListOptions{LabelSelector: selector})
		if err != nil {
			return nil, fmt.Errorf("namespace %s: %w", ns, err)
		}
		out = append(out, l.Items...)
	}
	return out, nil
}

// stackPodMetrics is a PodMetricsSource over metrics.k8s.io that sums all of
// a pod's containers (stack pods are multi-/arbitrarily-named containers).
type stackPodMetrics struct {
	client    kubernetes.Interface
	checkOnce sync.Once
	checkErr  error
}

// NewK8sStackPodMetricsSource returns the stack observer's CPU/mem source.
// Without metrics-server (or without RBAC to read it) every PodUsage call
// returns ErrMetricsUnavailable and the observer reports "unavailable".
func NewK8sStackPodMetricsSource(client kubernetes.Interface) PodMetricsSource {
	return &stackPodMetrics{client: client}
}

func (r *stackPodMetrics) groupInstalled() error {
	r.checkOnce.Do(func() {
		if r.client == nil {
			r.checkErr = fmt.Errorf("%w: no client", ErrMetricsUnavailable)
			return
		}
		d := r.client.Discovery()
		if d == nil || d.RESTClient() == nil {
			r.checkErr = fmt.Errorf("%w: no REST client", ErrMetricsUnavailable)
			return
		}
		if _, err := d.ServerResourcesForGroupVersion(k8sMetricsGroupVersion); err != nil {
			r.checkErr = fmt.Errorf("%w: %s not served: %v", ErrMetricsUnavailable, k8sMetricsGroupVersion, err)
		}
	})
	return r.checkErr
}

func (r *stackPodMetrics) PodUsage(ctx context.Context, namespace string, podNames []string) (map[string]PodUsage, error) {
	if err := r.groupInstalled(); err != nil {
		return nil, err
	}
	out := make(map[string]PodUsage, len(podNames))
	var lastErr error
	for _, pod := range podNames {
		body, err := r.client.Discovery().RESTClient().Get().
			AbsPath("/apis/metrics.k8s.io/v1beta1/namespaces", namespace, "pods", pod).
			DoRaw(ctx)
		if err != nil {
			if apierrors.IsForbidden(err) {
				return nil, fmt.Errorf("%w: %v", ErrMetricsUnavailable, err)
			}
			if !apierrors.IsNotFound(err) { // NotFound = no sample yet
				lastErr = err
			}
			continue
		}
		u, err := parseStackPodUsage(body)
		if err != nil {
			lastErr = err
			continue
		}
		out[pod] = u
	}
	if len(out) == 0 && lastErr != nil {
		return nil, lastErr
	}
	return out, nil
}

// parseStackPodUsage sums usage over every container of a PodMetrics body.
func parseStackPodUsage(body []byte) (PodUsage, error) {
	var pm podMetrics
	if err := json.Unmarshal(body, &pm); err != nil {
		return PodUsage{}, fmt.Errorf("decode pod metrics: %w", err)
	}
	if len(pm.Containers) == 0 {
		return PodUsage{}, fmt.Errorf("pod metrics: no container samples")
	}
	var u PodUsage
	for _, c := range pm.Containers {
		cpu, err := resource.ParseQuantity(c.Usage["cpu"])
		if err != nil {
			return PodUsage{}, fmt.Errorf("parse cpu quantity %q: %w", c.Usage["cpu"], err)
		}
		u.CPUMilli += cpu.MilliValue()
		if m, err := resource.ParseQuantity(c.Usage["memory"]); err == nil {
			u.MemBytes += m.Value()
		}
	}
	return u, nil
}
