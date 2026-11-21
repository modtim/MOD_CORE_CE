package executor

import (
	"context"
	"io"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

func newTestK8s(t *testing.T) (*K8sBackend, *fake.Clientset) {
	t.Helper()
	cs := fake.NewSimpleClientset()
	b := NewK8sBackend(cs, "tenant-7")
	return b, cs
}

func submitK8s(t *testing.T, b *K8sBackend, spec *TaskSpec) (*Handle, *batchv1.Job) {
	t.Helper()
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	ns, name, _ := parseHandle(h)
	job, err := b.Client.BatchV1().Jobs(ns).Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get job: %v", err)
	}
	return h, job
}

func TestK8sSubmitBuildsJob(t *testing.T) {
	b, _ := newTestK8s(t)
	h, job := submitK8s(t, b, &TaskSpec{
		Image:     "harbor/mod/node:1",
		Cmd:       []string{"python", "run.py"},
		Env:       map[string]string{"MOD_TASK_ID": "Task_42", "API_URL": "http://api"},
		Resources: ResourceLimits{CPUCores: 1.5, MemoryMB: 2048, ShmSizeMB: 512},
	})
	if h.Backend != "k8s" || !strings.HasPrefix(h.ID, "tenant-7/mod-task-task-42-") {
		t.Fatalf("bad handle %+v", h)
	}
	s := job.Spec
	if *s.BackoffLimit != 0 || *s.Parallelism != 1 || *s.Completions != 1 || s.TTLSecondsAfterFinished == nil {
		t.Fatalf("bad job spec: %+v", s)
	}
	ps := s.Template.Spec
	if ps.RestartPolicy != corev1.RestartPolicyNever {
		t.Fatalf("restartPolicy=%s", ps.RestartPolicy)
	}
	l := s.Template.Labels
	if l[LabelPartOf] != "mod-core" || l[LabelTask] != "task-42" || l[LabelEgressMode] != EgressModeDeny ||
		l[DefaultTaskPodSelectorKey] != DefaultTaskPodSelectorValue {
		t.Fatalf("bad labels %v", l)
	}
	c := ps.Containers[0]
	if c.Image != "harbor/mod/node:1" || len(c.Args) != 2 || len(c.Env) != 2 || c.Env[0].Name != "API_URL" {
		t.Fatalf("bad container %+v", c)
	}
	if c.Resources.Limits.Cpu().MilliValue() != 1500 || c.Resources.Limits.Memory().Value() != 2048<<20 {
		t.Fatalf("bad limits %v", c.Resources.Limits)
	}
	if len(ps.Volumes) != 1 || ps.Volumes[0].EmptyDir.Medium != corev1.StorageMediumMemory ||
		ps.Volumes[0].EmptyDir.SizeLimit.Value() != 512<<20 || c.VolumeMounts[0].MountPath != "/dev/shm" {
		t.Fatalf("bad shm volume %+v / %+v", ps.Volumes, c.VolumeMounts)
	}
	if ps.NodeSelector != nil {
		t.Fatalf("CPU task should have no nodeSelector: %v", ps.NodeSelector)
	}
}

func TestK8sEgressLabel(t *testing.T) {
	b, _ := newTestK8s(t)
	_, job := submitK8s(t, b, &TaskSpec{Image: "x", Egress: EgressUserEgress})
	if job.Spec.Template.Labels[LabelEgressMode] != EgressModeUser {
		t.Fatalf("want user-egress, got %v", job.Spec.Template.Labels)
	}
}

func TestK8sGPUContract(t *testing.T) {
	cases := []struct {
		name     string
		gpu      GPURequest
		single   string
		resName  corev1.ResourceName
		qty      int64
		selector string
	}{
		{"whole-1", GPURequest{Count: 1}, "", ResourceGPU, 1, ""},
		{"whole-1-pinned", GPURequest{Count: 1}, "time-sliced-4", ResourceGPU, 1, "time-sliced-4"},
		{"whole-2", GPURequest{Count: 2}, "", ResourceGPU, 2, DevicePluginExclusive},
		{"mig-0", GPURequest{MIGProfile: "1g.10gb"}, "", "nvidia.com/mig-1g.10gb", 1, ""},
		{"mig-2", GPURequest{Count: 2, MIGProfile: "3g.40gb"}, "", "nvidia.com/mig-3g.40gb", 2, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			b, _ := newTestK8s(t)
			b.SingleGPUNodeSelector = tc.single
			_, job := submitK8s(t, b, &TaskSpec{Image: "x", GPU: tc.gpu})
			ps := job.Spec.Template.Spec
			q, ok := ps.Containers[0].Resources.Limits[tc.resName]
			if !ok || q.Value() != tc.qty {
				t.Fatalf("limits %v: want %s=%d", ps.Containers[0].Resources.Limits, tc.resName, tc.qty)
			}
			if _, has := ps.Containers[0].Resources.Limits[ResourceGPU]; tc.gpu.MIGProfile != "" && has {
				t.Fatalf("MIG request must not also ask nvidia.com/gpu")
			}
			if got := ps.NodeSelector[LabelDevicePluginConfig]; got != tc.selector {
				t.Fatalf("nodeSelector=%q want %q", got, tc.selector)
			}
		})
	}
}

// k8sGPUTest builds a transport.K8sGPURequest for the 14.13 tests.
func k8sGPUTest(t *testing.T, limits map[string]interface{}, labels, annotations,
	nodeSel map[string]string, sched string) *transport.K8sGPURequest {
	t.Helper()
	f := &transport.K8sGPURequest{
		Labels: labels, Annotations: annotations,
		NodeSelector: nodeSel, SchedulerName: sched,
	}
	f.Resources.Limits = limits
	return f
}

// TestK8sGPURequest (14.13 / D-GPU Layer 2): the API-computed k8s_gpu fragment
// is applied verbatim to the pod spec — merged over the structural labels /
// annotations and the CPU/mem limits (never dropping the existing ones) and
// driving nodeSelector + schedulerName. Absent block → today's behaviour.
func TestK8sGPURequest(t *testing.T) {
	cases := []struct {
		name      string
		gpu       GPURequest // worker-side GPU demand (the fallback path)
		k8sGPU    *transport.K8sGPURequest
		resName   corev1.ResourceName
		qty       float64 // expected limits[resName] (Value()); 0 = must be absent
		wantGPU   bool
		selector  map[string]string
		scheduler string
		label     string // extra label expected present
	}{
		{
			name:     "whole-gpu",
			gpu:      GPURequest{Count: 2},
			k8sGPU:   k8sGPUTest(t, map[string]interface{}{"nvidia.com/gpu": 2}, nil, nil, map[string]string{LabelDevicePluginConfig: DevicePluginExclusive}, ""),
			resName:  "nvidia.com/gpu",
			qty:      2,
			selector: map[string]string{LabelDevicePluginConfig: DevicePluginExclusive},
		},
		{
			name:    "mig",
			gpu:     GPURequest{Count: 1},
			k8sGPU:  k8sGPUTest(t, map[string]interface{}{"nvidia.com/mig-2g.20gb": 1}, nil, nil, map[string]string{LabelMIGConfigState: "success"}, ""),
			resName: "nvidia.com/mig-2g.20gb",
			qty:     1,
		},
		{
			name:      "kai-fraction-annotation",
			gpu:       GPURequest{Count: 1},
			k8sGPU:    k8sGPUTest(t, map[string]interface{}{"scheduling.k8s.io/gpu": "0.5"}, map[string]string{"nvidia.com/kai-queue": "mod-t1"}, map[string]string{"nvidia.com/gpu.memory": "24576"}, nil, "kai-scheduler"),
			resName:   "scheduling.k8s.io/gpu",
			qty:       0.5,
			scheduler: "kai-scheduler",
			label:     "nvidia.com/kai-queue",
		},
		{
			name:     "amd",
			gpu:      GPURequest{Count: 1},
			k8sGPU:   k8sGPUTest(t, map[string]interface{}{"amd.com/gpu": 1}, nil, nil, map[string]string{LabelDevicePluginConfig: "exclusive"}, ""),
			resName:  "amd.com/gpu",
			qty:      1,
			selector: map[string]string{LabelDevicePluginConfig: "exclusive"},
		},
		{
			name:    "absent-block-unchanged",
			gpu:     GPURequest{Count: 1},
			resName: ResourceGPU,
			qty:     1,
		},
		{
			name:    "absent-block-no-gpu",
			resName: ResourceGPU,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			b, _ := newTestK8s(t)
			_, job := submitK8s(t, b, &TaskSpec{
				Image:     "x",
				GPU:       tc.gpu,
				K8sGPU:    tc.k8sGPU,
				Resources: ResourceLimits{CPUCores: 1.5, MemoryMB: 2048},
			})
			ps := job.Spec.Template.Spec
			meta := job.Spec.Template.ObjectMeta
			limits := ps.Containers[0].Resources.Limits

			// Existing CPU/mem limits + structural labels survive.
			c0 := ps.Containers[0]
			if c0.Resources.Limits.Cpu().MilliValue() != 1500 || c0.Resources.Limits.Memory().Value() != 2048<<20 {
				t.Fatalf("cpu/mem limits dropped: %v", limits)
			}
			if meta.Labels[LabelPartOf] != LabelPartOfValue || meta.Labels[LabelEgressMode] != EgressModeDeny {
				t.Fatalf("structural labels dropped: %v", meta.Labels)
			}

			if tc.qty == 0 {
				if _, ok := limits[tc.resName]; ok {
					t.Fatalf("unexpected %s in limits %v", tc.resName, limits)
				}
			} else if q, ok := limits[tc.resName]; !ok ||
				math.Abs(q.AsApproximateFloat64()-tc.qty) > 1e-9 {
				t.Fatalf("limits %v: want %s=%v", limits, tc.resName, tc.qty)
			}
			_, has := limits[ResourceGPU]
			if tc.wantGPU != has && tc.resName != ResourceGPU {
				t.Fatalf("nvidia.com/gpu present=%v want %v (limits %v)", has, tc.wantGPU, limits)
			}
			if tc.selector != nil {
				for k, v := range tc.selector {
					if ps.NodeSelector[k] != v {
						t.Fatalf("nodeSelector=%v want %s=%s", ps.NodeSelector, k, v)
					}
				}
			} else if tc.k8sGPU == nil && tc.gpu.Count > 0 {
				// W21 whole-placement pins exclusive for Count>1; Count==1 has
				// no selector by default.
				if tc.gpu.Count > 1 && ps.NodeSelector[LabelDevicePluginConfig] != DevicePluginExclusive {
					t.Fatalf("absent block must keep today's nodeSelector, got %v", ps.NodeSelector)
				}
			}
			if ps.SchedulerName != tc.scheduler {
				t.Fatalf("schedulerName=%q want %q", ps.SchedulerName, tc.scheduler)
			}
			if tc.label != "" && meta.Labels[tc.label] == "" {
				t.Fatalf("label %s not merged: %v", tc.label, meta.Labels)
			}
		})
	}
}

// TestK8sGPURequestAnnotationMerge: a k8s_gpu fragment with annotations merges
// with (never replaces) the structural egress annotation.
func TestK8sGPURequestAnnotationMerge(t *testing.T) {
	b, _ := newTestK8s(t)
	_, job := submitK8s(t, b, &TaskSpec{
		Image:  "x",
		Egress: EgressAllowlist, // no Cilium → egress-allowlist annotation stamped
		K8sGPU: k8sGPUTest(t, map[string]interface{}{"nvidia.com/gpu": 1}, nil, map[string]string{"nvidia.com/gpu.memory": "24576"}, nil, ""),
	})
	meta := job.Spec.Template.ObjectMeta
	if meta.Annotations["nvidia.com/gpu.memory"] != "24576" {
		t.Fatalf("k8s_gpu annotation not applied: %v", meta.Annotations)
	}
	if meta.Annotations[AnnotationEgressAllowlist] != EgressAllowlistUnenforced {
		t.Fatalf("structural annotation dropped: %v", meta.Annotations)
	}
}

// hostnamePrefTerm is one of the API's merge_affinity preferred terms
// (k8s_node_routing.py build_preferred_terms shape).
func hostnamePrefTerm(weight int32, hostnames ...string) corev1.PreferredSchedulingTerm {
	return corev1.PreferredSchedulingTerm{
		Weight: weight,
		Preference: corev1.NodeSelectorTerm{
			MatchExpressions: []corev1.NodeSelectorRequirement{{
				Key:      "kubernetes.io/hostname",
				Operator: corev1.NodeSelectorOpIn,
				Values:   hostnames,
			}},
		},
	}
}

// TestK8sGPURequestAffinity (GI.6): the API's node-routing affinity lands in
// the built pod spec, appended to (never replacing) any affinity already set,
// with the nodeSelector untouched. Absent affinity → pod affinity unchanged.
func TestK8sGPURequestAffinity(t *testing.T) {
	// 1) Payload affinity lands in the pod spec.
	b, _ := newTestK8s(t)
	inAff := &corev1.Affinity{NodeAffinity: &corev1.NodeAffinity{
		PreferredDuringSchedulingIgnoredDuringExecution: []corev1.PreferredSchedulingTerm{
			hostnamePrefTerm(100, "node-a"),
			hostnamePrefTerm(80, "node-b"),
		},
	}}
	f := k8sGPUTest(t, map[string]interface{}{"nvidia.com/gpu": 1}, nil, nil, map[string]string{LabelDevicePluginConfig: DevicePluginExclusive}, "")
	f.Affinity = inAff
	_, job := submitK8s(t, b, &TaskSpec{Image: "x", K8sGPU: f})
	ps := job.Spec.Template.Spec

	terms := ps.Affinity.NodeAffinity.PreferredDuringSchedulingIgnoredDuringExecution
	if len(terms) != 2 {
		t.Fatalf("want 2 preferred terms, got %d: %+v", len(terms), terms)
	}
	if terms[0].Weight != 100 || terms[0].Preference.MatchExpressions[0].Values[0] != "node-a" {
		t.Fatalf("term 0 wrong: %+v", terms[0])
	}
	if terms[1].Weight != 80 || terms[1].Preference.MatchExpressions[0].Values[0] != "node-b" {
		t.Fatalf("term 1 wrong: %+v", terms[1])
	}
	// nodeSelector still present alongside the affinity.
	if ps.NodeSelector[LabelDevicePluginConfig] != DevicePluginExclusive {
		t.Fatalf("nodeSelector lost: %v", ps.NodeSelector)
	}

	// 2) Absent affinity → pod affinity unchanged (nil).
	f2 := k8sGPUTest(t, map[string]interface{}{"nvidia.com/gpu": 1}, nil, nil, map[string]string{LabelDevicePluginConfig: DevicePluginExclusive}, "")
	_, job2 := submitK8s(t, b, &TaskSpec{Image: "x", K8sGPU: f2})
	if job2.Spec.Template.Spec.Affinity != nil {
		t.Fatalf("absent affinity must leave pod affinity nil, got %+v", job2.Spec.Template.Spec.Affinity)
	}
	if job2.Spec.Template.Spec.NodeSelector[LabelDevicePluginConfig] != DevicePluginExclusive {
		t.Fatalf("absent-affinity nodeSelector lost: %v", job2.Spec.Template.Spec.NodeSelector)
	}

	// 3) Incoming affinity MERGES onto an existing base: preferred appended,
	// required preserved, hostname-deduped (existing wins).
	baseRequired := corev1.NodeSelectorTerm{
		MatchExpressions: []corev1.NodeSelectorRequirement{{
			Key:      "mod.tech/gpu",
			Operator: corev1.NodeSelectorOpIn,
			Values:   []string{"true"},
		}},
	}
	base := &corev1.Affinity{NodeAffinity: &corev1.NodeAffinity{
		RequiredDuringSchedulingIgnoredDuringExecution: &corev1.NodeSelector{
			NodeSelectorTerms: []corev1.NodeSelectorTerm{baseRequired},
		},
		PreferredDuringSchedulingIgnoredDuringExecution: []corev1.PreferredSchedulingTerm{hostnamePrefTerm(100, "node-a")},
	}}
	merged := mergeK8sAffinity(base, inAff)
	if merged == base {
		t.Fatalf("merge must not return base")
	}
	na := merged.NodeAffinity
	if na.RequiredDuringSchedulingIgnoredDuringExecution == nil || len(na.RequiredDuringSchedulingIgnoredDuringExecution.NodeSelectorTerms) != 1 ||
		na.RequiredDuringSchedulingIgnoredDuringExecution.NodeSelectorTerms[0].MatchExpressions[0].Key != "mod.tech/gpu" {
		t.Fatalf("required terms replaced: %+v", na.RequiredDuringSchedulingIgnoredDuringExecution)
	}
	if len(na.PreferredDuringSchedulingIgnoredDuringExecution) != 2 {
		t.Fatalf("want 2 preferred after merge (dedupe node-a), got %d: %+v", len(na.PreferredDuringSchedulingIgnoredDuringExecution), na.PreferredDuringSchedulingIgnoredDuringExecution)
	}
	if na.PreferredDuringSchedulingIgnoredDuringExecution[0].Preference.MatchExpressions[0].Values[0] != "node-a" ||
		na.PreferredDuringSchedulingIgnoredDuringExecution[1].Preference.MatchExpressions[0].Values[0] != "node-b" {
		t.Fatalf("merge order/contents wrong: %+v", na.PreferredDuringSchedulingIgnoredDuringExecution)
	}
	// base is not mutated.
	if len(base.NodeAffinity.PreferredDuringSchedulingIgnoredDuringExecution) != 1 {
		t.Fatalf("base preferred mutated: %+v", base.NodeAffinity.PreferredDuringSchedulingIgnoredDuringExecution)
	}
	// incoming is not mutated.
	if len(inAff.NodeAffinity.PreferredDuringSchedulingIgnoredDuringExecution) != 2 {
		t.Fatalf("incoming preferred mutated: %+v", inAff.NodeAffinity.PreferredDuringSchedulingIgnoredDuringExecution)
	}

	// 4) nil/empty incoming returns base unchanged.
	if got := mergeK8sAffinity(base, nil); got != base {
		t.Fatalf("nil incoming must return base, got %+v", got)
	}
	empty := &corev1.Affinity{NodeAffinity: &corev1.NodeAffinity{}}
	if got := mergeK8sAffinity(base, empty); got != base {
		t.Fatalf("empty incoming must return base, got %+v", got)
	}
	// nil base + incoming → the incoming terms (returned as a copy).
	got := mergeK8sAffinity(nil, inAff)
	if got == nil || got.NodeAffinity == nil || len(got.NodeAffinity.PreferredDuringSchedulingIgnoredDuringExecution) != 2 ||
		got.NodeAffinity.PreferredDuringSchedulingIgnoredDuringExecution[0].Weight != 100 || got.NodeAffinity.PreferredDuringSchedulingIgnoredDuringExecution[1].Weight != 80 {
		t.Fatalf("nil base must carry incoming terms, got %+v", got)
	}
}

func addPod(t *testing.T, cs *fake.Clientset, ns, job string, created time.Time, st corev1.PodStatus) {
	t.Helper()
	p := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: job + "-pod", Namespace: ns, Labels: map[string]string{LabelJob: job},
			CreationTimestamp: metav1.NewTime(created)},
		Status: st,
	}
	if _, err := cs.CoreV1().Pods(ns).Create(context.Background(), p, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
}

func setJobStatus(t *testing.T, cs *fake.Clientset, job *batchv1.Job, st batchv1.JobStatus) {
	t.Helper()
	job.Status = st
	if _, err := cs.BatchV1().Jobs(job.Namespace).UpdateStatus(context.Background(), job, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
}

func TestK8sPoll(t *testing.T) {
	now := time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)
	ctx := context.Background()

	t.Run("running", func(t *testing.T) {
		b, cs := newTestK8s(t)
		h, job := submitK8s(t, b, &TaskSpec{Image: "x"})
		addPod(t, cs, job.Namespace, job.Name, now, corev1.PodStatus{Phase: corev1.PodRunning})
		st, err := b.Poll(ctx, h)
		if err != nil || st.Phase != TaskRunning {
			t.Fatalf("got %+v %v", st, err)
		}
	})
	t.Run("succeeded", func(t *testing.T) {
		b, cs := newTestK8s(t)
		h, job := submitK8s(t, b, &TaskSpec{Image: "x"})
		setJobStatus(t, cs, job, batchv1.JobStatus{Conditions: []batchv1.JobCondition{{Type: batchv1.JobComplete, Status: corev1.ConditionTrue}}})
		if st, _ := b.Poll(ctx, h); st.Phase != TaskSucceeded {
			t.Fatalf("got %+v", st)
		}
	})
	t.Run("failed-exit-code", func(t *testing.T) {
		b, cs := newTestK8s(t)
		h, job := submitK8s(t, b, &TaskSpec{Image: "x"})
		addPod(t, cs, job.Namespace, job.Name, now, corev1.PodStatus{Phase: corev1.PodFailed, ContainerStatuses: []corev1.ContainerStatus{{
			Name: "task", State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{ExitCode: 137, Reason: "OOMKilled"}}}}})
		setJobStatus(t, cs, job, batchv1.JobStatus{Conditions: []batchv1.JobCondition{{Type: batchv1.JobFailed, Status: corev1.ConditionTrue, Reason: "BackoffLimitExceeded"}}})
		st, _ := b.Poll(ctx, h)
		if st.Phase != TaskFailed || st.ExitCode != 137 || !strings.Contains(st.Err, "OOMKilled") || !strings.Contains(st.Err, "BackoffLimitExceeded") {
			t.Fatalf("got %+v", st)
		}
	})
	t.Run("image-pull-backoff-grace", func(t *testing.T) {
		b, cs := newTestK8s(t)
		clock := now
		b.now = func() time.Time { return clock }
		h, job := submitK8s(t, b, &TaskSpec{Image: "x"})
		addPod(t, cs, job.Namespace, job.Name, now, corev1.PodStatus{Phase: corev1.PodPending, ContainerStatuses: []corev1.ContainerStatus{{
			Name: "task", State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "ImagePullBackOff", Message: "not found"}}}}})
		if st, _ := b.Poll(ctx, h); st.Phase != TaskRunning {
			t.Fatalf("within grace want running, got %+v", st)
		}
		clock = now.Add(DefaultPendingFailAfter + time.Second)
		if st, _ := b.Poll(ctx, h); st.Phase != TaskFailed || !strings.Contains(st.Err, "ImagePullBackOff") {
			t.Fatalf("after grace want failed, got %+v", st)
		}
	})
	t.Run("unschedulable-grace", func(t *testing.T) {
		b, cs := newTestK8s(t)
		b.now = func() time.Time { return now.Add(5 * time.Minute) }
		h, job := submitK8s(t, b, &TaskSpec{Image: "x", GPU: GPURequest{MIGProfile: "7g.80gb"}})
		addPod(t, cs, job.Namespace, job.Name, now, corev1.PodStatus{Phase: corev1.PodPending, Conditions: []corev1.PodCondition{{
			Type: corev1.PodScheduled, Status: corev1.ConditionFalse, Reason: corev1.PodReasonUnschedulable,
			Message: "0/3 nodes: Insufficient nvidia.com/mig-7g.80gb", LastTransitionTime: metav1.NewTime(now)}}})
		if st, _ := b.Poll(ctx, h); st.Phase != TaskFailed || !strings.Contains(st.Err, "Unschedulable") {
			t.Fatalf("got %+v", st)
		}
	})
	t.Run("not-found", func(t *testing.T) {
		b, _ := newTestK8s(t)
		st, err := b.Poll(ctx, &Handle{Backend: "k8s", ID: "tenant-7/nope"})
		if err != nil || st.Phase != TaskFailed {
			t.Fatalf("got %+v %v", st, err)
		}
	})
	t.Run("bad-handle", func(t *testing.T) {
		b, _ := newTestK8s(t)
		if _, err := b.Poll(ctx, &Handle{Backend: "docker", ID: "abc"}); err == nil {
			t.Fatal("want error for docker handle")
		}
	})
}

func TestK8sLogs(t *testing.T) {
	b, cs := newTestK8s(t)
	h, job := submitK8s(t, b, &TaskSpec{Image: "x"})
	if _, err := b.Logs(context.Background(), h); err == nil {
		t.Fatal("want error before pod exists")
	}
	addPod(t, cs, job.Namespace, job.Name, time.Now(), corev1.PodStatus{Phase: corev1.PodRunning})
	rc, err := b.Logs(context.Background(), h)
	if err != nil {
		t.Fatalf("Logs: %v", err)
	}
	defer rc.Close()
	data, _ := io.ReadAll(rc)
	if len(data) == 0 { // fake returns "fake logs"
		t.Fatal("empty logs")
	}
}

func TestK8sCancel(t *testing.T) {
	b, cs := newTestK8s(t)
	h, job := submitK8s(t, b, &TaskSpec{Image: "x"})
	var policy *metav1.DeletionPropagation
	cs.PrependReactor("delete", "jobs", func(a k8stesting.Action) (bool, runtime.Object, error) {
		policy = a.(k8stesting.DeleteActionImpl).DeleteOptions.PropagationPolicy
		return false, nil, nil
	})
	if err := b.Cancel(context.Background(), h); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	if policy == nil || *policy != metav1.DeletePropagationBackground {
		t.Fatalf("want Background propagation, got %v", policy)
	}
	if _, err := cs.BatchV1().Jobs(job.Namespace).Get(context.Background(), job.Name, metav1.GetOptions{}); err == nil {
		t.Fatal("job still exists")
	}
	if err := b.Cancel(context.Background(), h); err != nil {
		t.Fatalf("second Cancel should be idempotent: %v", err)
	}
}
