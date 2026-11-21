package executor

import (
	"context"
	"errors"
	"math"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

const k8sObsNS = "tenant-1"

func k8sObsDeployment(name, stack string, replicas int32, ready int32, extra map[string]string) *appsv1.Deployment {
	labels := map[string]string{K8sStackLabel: stack, "app.kubernetes.io/managed-by": "mod-k8s-stack-render"}
	for k, v := range extra {
		labels[k] = v
	}
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: k8sObsNS, Labels: labels},
		Spec:       appsv1.DeploymentSpec{Replicas: &replicas},
		Status:     appsv1.DeploymentStatus{ReadyReplicas: ready},
	}
}

func k8sObsPod(name, dep, stack, svc string, running bool, limits corev1.ResourceList, node string) *corev1.Pod {
	cs := corev1.ContainerStatus{Name: "main"}
	phase := corev1.PodRunning
	if running {
		cs.State.Running = &corev1.ContainerStateRunning{}
	} else {
		cs.State.Waiting = &corev1.ContainerStateWaiting{Reason: "CrashLoopBackOff"}
		cs.RestartCount = 7
		cs.LastTerminationState.Terminated = &corev1.ContainerStateTerminated{Reason: "OOMKilled"}
	}
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: k8sObsNS,
			Labels:          map[string]string{K8sStackLabel: stack, K8sServiceLabel: svc, "pod-template-hash": "abc12"},
			OwnerReferences: []metav1.OwnerReference{{Kind: "ReplicaSet", Name: dep + "-abc12"}},
		},
		Spec: corev1.PodSpec{
			NodeName:   node,
			Containers: []corev1.Container{{Name: "main", Resources: corev1.ResourceRequirements{Limits: limits}}},
		},
		Status: corev1.PodStatus{Phase: phase, ContainerStatuses: []corev1.ContainerStatus{cs}},
	}
}

func k8sObsGPUNode() *corev1.Node {
	return &corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: "gpu-node", Labels: map[string]string{
		gfdGPUMemoryLabel:   "16384",
		gfdGPUProductLabel:  "NVIDIA-RTX-A4000",
		gfdGPUReplicasLabel: "4",
	}}}
}

type k8sFakeMetrics struct {
	usage map[string]PodUsage
	err   error
}

func (f k8sFakeMetrics) PodUsage(context.Context, string, []string) (map[string]PodUsage, error) {
	return f.usage, f.err
}

type k8sFakeGPU struct{ vram map[string]float64 }

func (f k8sFakeGPU) PodVRAMMiB(context.Context, string, []string) (map[string]float64, error) {
	return f.vram, nil
}

func k8sObsByProject(t *testing.T, obs []K8sStackObservation) map[string]K8sStackObservation {
	t.Helper()
	m := map[string]K8sStackObservation{}
	for _, o := range obs {
		m[o.Project] = o
	}
	return m
}

func TestK8sStackObserve_StateMapping(t *testing.T) {
	gpu := corev1.ResourceList{k8sGPUResource: resource.MustParse("1")}
	client := fake.NewSimpleClientset(
		k8sObsGPUNode(),
		// healthy GPU stack
		k8sObsDeployment("modstack-vllm-api", "modstack-vllm", 1, 1, map[string]string{K8sHAClassLabel: "stateless"}),
		k8sObsPod("modstack-vllm-api-abc12-x1", "modstack-vllm-api", "modstack-vllm", "api", true, gpu, "gpu-node"),
		// crash-looping, OOMKilled stack
		k8sObsDeployment("modstack-db-pg", "modstack-db", 1, 0, nil),
		k8sObsPod("modstack-db-pg-abc12-y1", "modstack-db-pg", "modstack-db", "pg", false, nil, "cpu-node"),
		// sleeping stack (scaled to 0, no pods)
		k8sObsDeployment("modstack-nim-main", "modstack-nim", 0, 0, nil),
		// unrelated deployment without the stack label
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "other", Namespace: k8sObsNS}},
	)
	obs, err := NewK8sStackObserver(client, k8sObsNS).ObserveStacksDetailed(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	m := k8sObsByProject(t, obs)
	if len(m) != 3 {
		t.Fatalf("want 3 stacks, got %d: %+v", len(m), obs)
	}

	v := m["modstack-vllm"]
	if v.Running != 1 || v.Total != 1 || !v.Healthy() || v.HAClass != "stateless" {
		t.Errorf("vllm: %+v", v.StackObservation)
	}
	if len(v.Services) != 1 || v.Services[0] != "api" {
		t.Errorf("vllm services: %v", v.Services)
	}
	if v.DesiredReplicas != 1 || v.ReadyReplicas != 1 || v.GPUsAllocated != 1 {
		t.Errorf("vllm detail: %+v", v)
	}
	if v.VramGB != 0 || v.VRAMSource != K8sSourceAllocation {
		t.Errorf("vllm VRAM must be unmeasured w/o DCGM: vram=%v src=%s", v.VramGB, v.VRAMSource)
	}
	if math.Abs(v.AllocVRAMGB-4.0) > 1e-9 || len(v.GPUProducts) != 1 || v.GPUProducts[0] != "NVIDIA-RTX-A4000" {
		t.Errorf("vllm alloc: %v %v", v.AllocVRAMGB, v.GPUProducts)
	}
	if v.MetricsSource != K8sSourceUnavailable || v.CPUMilli != 0 {
		t.Errorf("vllm metrics must be unavailable: %+v", v)
	}

	d := m["modstack-db"]
	if d.Running != 0 || d.Total != 1 || d.Healthy() || !d.CrashLooping || !d.OOMKilled || d.Restarts != 7 {
		t.Errorf("db: %+v", d)
	}
	if !d.Fenceable() {
		t.Error("db without ha-class must be fenceable")
	}

	n := m["modstack-nim"]
	if n.Running != 0 || n.Total != 1 || n.DesiredReplicas != 0 {
		t.Errorf("sleeping nim must be present-but-down: %+v", n)
	}

	// Plain shape matches.
	plain, err := NewK8sStackObserver(client, k8sObsNS).ObserveStacks(context.Background())
	if err != nil || len(plain) != 3 {
		t.Fatalf("plain: %v %v", plain, err)
	}
}

func TestK8sStackObserve_LiveMetricsAndDCGM(t *testing.T) {
	mig := corev1.ResourceList{"nvidia.com/mig-1g.5gb": resource.MustParse("2")}
	client := fake.NewSimpleClientset(
		k8sObsDeployment("modstack-sd-web", "modstack-sd", 1, 1, nil),
		k8sObsPod("modstack-sd-web-abc12-z1", "modstack-sd-web", "modstack-sd", "web", true, mig, "gpu-node"),
	)
	o := NewK8sStackObserver(client, k8sObsNS)
	o.Metrics = k8sFakeMetrics{usage: map[string]PodUsage{"modstack-sd-web-abc12-z1": {CPUMilli: 250, MemBytes: 1 << 30}}}
	o.GPU = k8sFakeGPU{vram: map[string]float64{"modstack-sd-web-abc12-z1": 3072}}
	obs, err := o.ObserveStacksDetailed(context.Background())
	if err != nil || len(obs) != 1 {
		t.Fatalf("%v %v", obs, err)
	}
	s := obs[0]
	if s.MetricsSource != K8sSourceLive || s.CPUMilli != 250 || s.MemBytes != 1<<30 {
		t.Errorf("metrics: %+v", s)
	}
	if s.VRAMSource != K8sSourceLive || math.Abs(s.VramGB-3.0) > 1e-9 {
		t.Errorf("vram: %v %s", s.VramGB, s.VRAMSource)
	}
	if s.MIGAllocated["mig-1g.5gb"] != 2 || math.Abs(s.AllocVRAMGB-10.0) > 1e-9 {
		t.Errorf("mig alloc: %v %v", s.MIGAllocated, s.AllocVRAMGB)
	}
}

func TestK8sStackObserve_MetricsErrorDegrades(t *testing.T) {
	client := fake.NewSimpleClientset(
		k8sObsDeployment("modstack-a-x", "modstack-a", 1, 1, nil),
		k8sObsPod("modstack-a-x-abc12-q", "modstack-a-x", "modstack-a", "x", true, nil, ""),
	)
	o := NewK8sStackObserver(client, k8sObsNS)
	o.Metrics = k8sFakeMetrics{err: errors.New("metrics-server not installed")}
	obs, err := o.ObserveStacksDetailed(context.Background())
	if err != nil || len(obs) != 1 || obs[0].MetricsSource != K8sSourceUnavailable || obs[0].Running != 1 {
		t.Fatalf("metrics error must degrade, not fail: %+v %v", obs, err)
	}
}

func TestK8sStackObserve_ListErrorIsError(t *testing.T) {
	client := fake.NewSimpleClientset()
	client.PrependReactor("list", "pods", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("forbidden")
	})
	obs, err := NewK8sStackObserver(client, k8sObsNS).ObserveStacks(context.Background())
	if err == nil || obs != nil {
		t.Fatalf("list failure must be an error, not an empty observation: %v %v", obs, err)
	}
	if _, err := (&K8sStackObserver{}).ObserveStacks(context.Background()); err == nil {
		t.Fatal("nil client must error")
	}
}

func TestK8sStackObserve_AllocVRAM(t *testing.T) {
	nl := map[string]string{gfdGPUMemoryLabel: "24576"}
	if got := allocVRAMMiB(2, nil, nl); got != 49152 {
		t.Errorf("whole gpus: %v", got)
	}
	if got := allocVRAMMiB(1, nil, nil); got != 0 {
		t.Errorf("no GFD labels must be 0 (unknown): %v", got)
	}
	if got := allocVRAMMiB(0, map[string]int64{"mig-3g.20gb": 1}, nil); got != 20480 {
		t.Errorf("mig: %v", got)
	}
}
