// k8s-reduction-engine T13: stack observation for stacks running as K8s
// Deployments.
//
// stackobserve.go reads the Docker daemon. On the K8s stack backend the same
// question — "is this stack actually serving right now?" — is answered by the
// API server instead: Deployment replicas, pod phase and container state. The
// observer below produces the SAME StackObservation shape the heartbeat
// already reports, so the manager side needs no change, plus an optional
// detailed view (K8sStackObservation) carrying pod/CPU/mem/GPU facts.
//
// Data sources and what is REAL vs UNAVAILABLE:
//   - state (replicas, phase, restarts, OOMKilled, crash-loop): real, core API.
//   - CPU / memory usage: needs metrics-server (`k8s.io/metrics` PodMetricses).
//     That module is not a dependency of the worker, so usage comes through
//     the PodMetricsSource interface; nil ⇒ reported unavailable. The worker
//     wires NewK8sStackPodMetricsSource (raw metrics.k8s.io GET), which
//     degrades to unavailable when metrics-server is absent (W22).
//   - GPU allocation: real — pod resource limits (`nvidia.com/gpu`,
//     `nvidia.com/mig-*`) + GPU Feature Discovery node labels.
//   - GPU VRAM *usage*: needs DCGM exporter (or Prometheus scraping it), via
//     GPUUsageSource; nil ⇒ VramGB stays 0, which by the StackObservation
//     contract means UNMEASURED, and only allocation is reported.

package executor

import (
	"context"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// Labels stamped by the compose→K8s renderer (API services/k8s_stack_render.py).
const (
	K8sStackLabel   = "mod.techlabs/stack"
	K8sServiceLabel = "mod.techlabs/service"
	// K8sHAClassLabel is the K8s spelling of haClassLabel. The renderer does
	// not stamp it yet; when absent the observation's HAClass is empty, which
	// Fenceable() treats as "fence" — the safe default.
	K8sHAClassLabel = "mod.techlabs/ha-class"

	k8sGPUResource = corev1.ResourceName("nvidia.com/gpu")
	k8sMIGPrefix   = "nvidia.com/mig-"

	// GPU Feature Discovery node labels.
	gfdGPUMemoryLabel   = "nvidia.com/gpu.memory" // MiB per physical GPU
	gfdGPUProductLabel  = "nvidia.com/gpu.product"
	gfdGPUReplicasLabel = "nvidia.com/gpu.replicas" // time-slicing replicas
)

// PodUsage is one pod's live resource usage.
type PodUsage struct {
	CPUMilli int64
	MemBytes int64
}

// PodMetricsSource supplies live pod CPU/memory (metrics-server PodMetricses).
// Returns usage keyed by pod name; pods missing from the map are unmeasured.
type PodMetricsSource interface {
	PodUsage(ctx context.Context, namespace string, podNames []string) (map[string]PodUsage, error)
}

// GPUUsageSource supplies live per-pod VRAM usage in MiB (DCGM exporter /
// Prometheus). Pods missing from the map are unmeasured.
type GPUUsageSource interface {
	PodVRAMMiB(ctx context.Context, namespace string, podNames []string) (map[string]float64, error)
}

// Values for K8sStackObservation.MetricsSource / VRAMSource.
const (
	K8sSourceUnavailable = "unavailable"
	K8sSourceLive        = "live"
	K8sSourceAllocation  = "allocation" // VRAM: allocation estimate only
)

// K8sPodObservation is one pod of a stack.
type K8sPodObservation struct {
	Name         string           `json:"name"`
	Service      string           `json:"service,omitempty"`
	Node         string           `json:"node,omitempty"`
	Phase        string           `json:"phase"`
	Serving      bool             `json:"serving"`
	Restarts     int32            `json:"restarts"`
	OOMKilled    bool             `json:"oom_killed,omitempty"`
	Waiting      string           `json:"waiting_reason,omitempty"`
	GPUs         int64            `json:"gpus,omitempty"`
	MIG          map[string]int64 `json:"mig,omitempty"`
	GPUProduct   string           `json:"gpu_product,omitempty"`
	CPUMilli     int64            `json:"cpu_milli,omitempty"`
	MemBytes     int64            `json:"mem_bytes,omitempty"`
	VRAMUsedMiB  float64          `json:"vram_used_mib,omitempty"`
	AllocVRAMMiB float64          `json:"alloc_vram_mib,omitempty"`
}

// K8sStackObservation is the StackObservation plus K8s-only detail. The
// embedded StackObservation is exactly what the heartbeat reports today.
type K8sStackObservation struct {
	StackObservation
	Namespace       string              `json:"namespace"`
	Deployments     []string            `json:"deployments,omitempty"`
	DesiredReplicas int32               `json:"desired_replicas"`
	ReadyReplicas   int32               `json:"ready_replicas"`
	Restarts        int32               `json:"restarts"`
	OOMKilled       bool                `json:"oom_killed,omitempty"`
	CrashLooping    bool                `json:"crash_looping,omitempty"`
	Pods            []K8sPodObservation `json:"pods,omitempty"`
	MetricsSource   string              `json:"metrics_source"`
	CPUMilli        int64               `json:"cpu_milli,omitempty"`
	MemBytes        int64               `json:"mem_bytes,omitempty"`
	GPUsAllocated   int64               `json:"gpus_allocated,omitempty"`
	MIGAllocated    map[string]int64    `json:"mig_allocated,omitempty"`
	GPUProducts     []string            `json:"gpu_products,omitempty"`
	AllocVRAMGB     float64             `json:"alloc_vram_gb,omitempty"`
	VRAMSource      string              `json:"vram_source"`
}

// K8sStackObserver observes MOD stacks deployed as K8s Deployments.
type K8sStackObserver struct {
	Client kubernetes.Interface
	// Namespace to observe; "" = namespace-per-tenant: every namespace
	// matching NamespaceSelector (never cluster-wide NamespaceAll).
	Namespace string
	// NamespaceSelector picks tenant namespaces when Namespace is "";
	// default K8sTenantNamespaceSelector (app.kubernetes.io/managed-by=mod).
	NamespaceSelector string
	// Selector is the label selector for MOD stacks; default is
	// "mod.techlabs/stack" (existence).
	Selector string
	Metrics  PodMetricsSource // nil ⇒ CPU/mem unavailable
	GPU      GPUUsageSource   // nil ⇒ VRAM allocation-only
}

// NewK8sStackObserver builds an observer with the default stack selector.
func NewK8sStackObserver(client kubernetes.Interface, namespace string) *K8sStackObserver {
	return &K8sStackObserver{Client: client, Namespace: namespace, Selector: K8sStackLabel}
}

// ObserveStacks returns the same shape as Executor.ObserveStacks.
func (o *K8sStackObserver) ObserveStacks(ctx context.Context) ([]StackObservation, error) {
	detailed, err := o.ObserveStacksDetailed(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]StackObservation, 0, len(detailed))
	for _, d := range detailed {
		out = append(out, d.StackObservation)
	}
	return out, nil
}

// ObserveStacksDetailed lists stacks with pod/CPU/mem/GPU detail.
//
// Semantics mirror the Docker observer:
//   - Running = pods actually serving (phase Running, every container in
//     state Running). A crash-looping pod is NOT running, like a Docker
//     "restarting" container.
//   - Total = pods, plus one per Deployment that currently has no pods at all
//     (scaled to 0 / sleeping, or failing to schedule) — so "this host has it
//     and it is down" (Total>0, Running=0) stays distinguishable from "never
//     had it" (absent).
//
// A list failure returns an error, never an empty slice: "could not look" and
// "looked and found nothing" are opposite claims (see main.go heartbeat).
// Metrics/GPU source failures degrade to "unavailable" and never fail the
// observation.
func (o *K8sStackObserver) ObserveStacksDetailed(ctx context.Context) ([]K8sStackObservation, error) {
	if o == nil || o.Client == nil {
		return nil, fmt.Errorf("k8s stack observer: no client")
	}
	selector := o.Selector
	if selector == "" {
		selector = K8sStackLabel
	}
	namespaces, err := o.stackNamespaces(ctx)
	if err != nil {
		return nil, err
	}
	depItems, err := o.listStackDeployments(ctx, namespaces, selector)
	if err != nil {
		return nil, fmt.Errorf("listing stack deployments: %w", err)
	}
	podItems, err := o.listStackPods(ctx, namespaces, selector)
	if err != nil {
		return nil, fmt.Errorf("listing stack pods: %w", err)
	}

	byKey := map[string]*K8sStackObservation{}
	get := func(namespace, stack string) *K8sStackObservation {
		key := namespace + "/" + stack
		s, ok := byKey[key]
		if !ok {
			s = &K8sStackObservation{
				StackObservation: StackObservation{Project: stack},
				Namespace:        namespace,
				MetricsSource:    K8sSourceUnavailable,
				VRAMSource:       K8sSourceUnavailable,
			}
			byKey[key] = s
		}
		return s
	}

	// Deployments: desired/ready, HA class, and which deployments own pods.
	depPods := map[string]int{} // ns/deployment → matched pods
	depStack := map[string]string{}
	for i := range depItems {
		d := &depItems[i]
		stack := strings.TrimSpace(d.Labels[K8sStackLabel])
		if stack == "" {
			continue
		}
		s := get(d.Namespace, stack)
		s.Deployments = append(s.Deployments, d.Name)
		s.DesiredReplicas += deploymentDesired(d)
		s.ReadyReplicas += d.Status.ReadyReplicas
		if v := strings.TrimSpace(d.Labels[K8sHAClassLabel]); v != "" {
			s.HAClass = v
		}
		key := d.Namespace + "/" + d.Name
		depPods[key] = 0
		depStack[key] = stack
	}

	nodeCache := map[string]map[string]string{}
	nodeLabels := func(ctx context.Context, name string) map[string]string {
		if name == "" {
			return nil
		}
		if l, ok := nodeCache[name]; ok {
			return l
		}
		var l map[string]string
		// Node read may be RBAC-denied in a tenant-scoped SA; degrade to no GFD.
		if n, err := o.Client.CoreV1().Nodes().Get(ctx, name, metav1.GetOptions{}); err == nil {
			l = n.Labels
		}
		nodeCache[name] = l
		return l
	}

	for i := range podItems {
		p := &podItems[i]
		stack := strings.TrimSpace(p.Labels[K8sStackLabel])
		if stack == "" || p.DeletionTimestamp != nil {
			continue
		}
		s := get(p.Namespace, stack)
		if v := strings.TrimSpace(p.Labels[K8sHAClassLabel]); v != "" && s.HAClass == "" {
			s.HAClass = v
		}
		if dep := owningDeployment(p); dep != "" {
			if _, ok := depPods[p.Namespace+"/"+dep]; ok {
				depPods[p.Namespace+"/"+dep]++
			}
		}
		po := observePod(p)
		if po.GPUs > 0 || len(po.MIG) > 0 {
			nl := nodeLabels(ctx, p.Spec.NodeName)
			po.GPUProduct = nl[gfdGPUProductLabel]
			po.AllocVRAMMiB = allocVRAMMiB(po.GPUs, po.MIG, nl)
		}
		s.Total++
		s.Restarts += po.Restarts
		s.OOMKilled = s.OOMKilled || po.OOMKilled
		if po.Waiting == "CrashLoopBackOff" {
			s.CrashLooping = true
		}
		if po.Serving {
			s.Running++
			if po.Service != "" {
				s.Services = append(s.Services, po.Service)
			}
		}
		s.Pods = append(s.Pods, po)
	}

	// A deployment with no pods still counts as present-but-down.
	for key, n := range depPods {
		if n == 0 {
			get(key[:strings.Index(key, "/")], depStack[key]).Total++
		}
	}

	out := make([]K8sStackObservation, 0, len(byKey))
	for _, s := range byKey {
		o.sampleUsage(ctx, s)
		out = append(out, *s)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Namespace != out[j].Namespace {
			return out[i].Namespace < out[j].Namespace
		}
		return out[i].Project < out[j].Project
	})
	return out, nil
}

// sampleUsage fills CPU/mem/VRAM from the optional sources and the GPU
// allocation roll-up. Never fails.
func (o *K8sStackObserver) sampleUsage(ctx context.Context, s *K8sStackObservation) {
	var names []string
	products := map[string]bool{}
	for i := range s.Pods {
		p := &s.Pods[i]
		if p.Serving {
			names = append(names, p.Name)
		}
		s.GPUsAllocated += p.GPUs
		for k, v := range p.MIG {
			if s.MIGAllocated == nil {
				s.MIGAllocated = map[string]int64{}
			}
			s.MIGAllocated[k] += v
		}
		if p.GPUProduct != "" {
			products[p.GPUProduct] = true
		}
		if p.Serving {
			s.AllocVRAMGB += p.AllocVRAMMiB / 1024.0
		}
	}
	for k := range products {
		s.GPUProducts = append(s.GPUProducts, k)
	}
	sort.Strings(s.GPUProducts)
	if s.GPUsAllocated > 0 || len(s.MIGAllocated) > 0 {
		s.VRAMSource = K8sSourceAllocation
	}
	if len(names) == 0 {
		return
	}

	if o.Metrics != nil {
		if usage, err := o.Metrics.PodUsage(ctx, s.Namespace, names); err == nil && len(usage) > 0 {
			s.MetricsSource = K8sSourceLive
			for i := range s.Pods {
				if u, ok := usage[s.Pods[i].Name]; ok {
					s.Pods[i].CPUMilli, s.Pods[i].MemBytes = u.CPUMilli, u.MemBytes
					s.CPUMilli += u.CPUMilli
					s.MemBytes += u.MemBytes
				}
			}
		}
	}
	if o.GPU != nil {
		if vram, err := o.GPU.PodVRAMMiB(ctx, s.Namespace, names); err == nil && len(vram) > 0 {
			var mib float64
			for i := range s.Pods {
				if v, ok := vram[s.Pods[i].Name]; ok && v > 0 {
					s.Pods[i].VRAMUsedMiB = v
					mib += v
				}
			}
			if mib > 0 {
				// Same unit/contract as the Docker sampler: measured GB, 0 = unmeasured.
				s.VramGB = mib / 1024.0
				s.VRAMSource = K8sSourceLive
			}
		}
	}
}

func deploymentDesired(d *appsv1.Deployment) int32 {
	if d.Spec.Replicas == nil {
		return 1 // API default
	}
	return *d.Spec.Replicas
}

// owningDeployment infers the Deployment from the pod's ReplicaSet owner
// (<deployment>-<pod-template-hash>).
func owningDeployment(p *corev1.Pod) string {
	for _, ref := range p.OwnerReferences {
		if ref.Kind != "ReplicaSet" {
			continue
		}
		if h := p.Labels["pod-template-hash"]; h != "" && strings.HasSuffix(ref.Name, "-"+h) {
			return strings.TrimSuffix(ref.Name, "-"+h)
		}
		if i := strings.LastIndex(ref.Name, "-"); i > 0 {
			return ref.Name[:i]
		}
	}
	return ""
}

// observePod maps pod status + spec to a K8sPodObservation.
func observePod(p *corev1.Pod) K8sPodObservation {
	po := K8sPodObservation{
		Name:    p.Name,
		Service: strings.TrimSpace(p.Labels[K8sServiceLabel]),
		Node:    p.Spec.NodeName,
		Phase:   string(p.Status.Phase),
	}
	allRunning := len(p.Status.ContainerStatuses) > 0
	for _, cs := range p.Status.ContainerStatuses {
		po.Restarts += cs.RestartCount
		if cs.State.Running == nil {
			allRunning = false
		}
		if w := cs.State.Waiting; w != nil && po.Waiting == "" {
			po.Waiting = w.Reason
		}
		if t := cs.State.Terminated; t != nil && t.Reason == "OOMKilled" {
			po.OOMKilled = true
		}
		if t := cs.LastTerminationState.Terminated; t != nil && t.Reason == "OOMKilled" {
			po.OOMKilled = true
		}
	}
	po.Serving = p.Status.Phase == corev1.PodRunning && allRunning

	for _, c := range p.Spec.Containers {
		lim := c.Resources.Limits
		if q, ok := lim[k8sGPUResource]; ok {
			po.GPUs += q.Value()
		}
		for name, q := range lim {
			if strings.HasPrefix(string(name), k8sMIGPrefix) {
				if po.MIG == nil {
					po.MIG = map[string]int64{}
				}
				po.MIG[strings.TrimPrefix(string(name), "nvidia.com/")] += q.Value()
			}
		}
	}
	return po
}

var migMemRe = regexp.MustCompile(`(\d+(?:\.\d+)?)gb`)

// allocVRAMMiB estimates the VRAM a pod is ALLOCATED (not using):
//   - MIG: the profile's memory (mig-1g.5gb → 5 GB) × count.
//   - whole GPUs: GFD gpu.memory × count, divided by gpu.replicas when the
//     node is time-sliced (a fair-share estimate; time-slicing does not
//     partition memory).
//
// 0 when the node's GFD labels are unavailable.
func allocVRAMMiB(gpus int64, mig map[string]int64, nodeLabels map[string]string) float64 {
	var mib float64
	for profile, n := range mig {
		if m := migMemRe.FindStringSubmatch(profile); m != nil {
			if gb, err := strconv.ParseFloat(m[1], 64); err == nil {
				mib += gb * 1024 * float64(n)
			}
		}
	}
	if gpus > 0 {
		mem, err := strconv.ParseFloat(strings.TrimSpace(nodeLabels[gfdGPUMemoryLabel]), 64)
		if err == nil && mem > 0 {
			per := mem
			if r, err := strconv.ParseFloat(strings.TrimSpace(nodeLabels[gfdGPUReplicasLabel]), 64); err == nil && r > 1 {
				per = mem / r
			}
			mib += per * float64(gpus)
		}
	}
	return mib
}
