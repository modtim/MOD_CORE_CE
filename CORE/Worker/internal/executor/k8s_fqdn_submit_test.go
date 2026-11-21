package executor

import (
	"context"
	"fmt"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	dynamicfake "k8s.io/client-go/dynamic/fake"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

// newTestK8sCilium builds a K8sBackend wired for FQDNMode == cilium, with a
// fake dynamic client that knows about the (unregistered, CRD-only)
// CiliumNetworkPolicy GVR — the same pattern the real backend_select.go
// (GB.EGR.4) uses via dynamic.NewForConfig, just faked out here.
func newTestK8sCilium(t *testing.T) (*K8sBackend, *fake.Clientset, *dynamicfake.FakeDynamicClient) {
	t.Helper()
	cs := fake.NewSimpleClientset()
	scheme := runtime.NewScheme()
	listKinds := map[schema.GroupVersionResource]string{
		CiliumNetworkPolicyGVR: "CiliumNetworkPolicyList",
	}
	dyn := dynamicfake.NewSimpleDynamicClientWithCustomListKinds(scheme, listKinds)
	b := NewK8sBackend(cs, "tenant-7")
	b.FQDNMode = FQDNModeCilium
	b.Dynamic = dyn
	return b, cs, dyn
}

func allowlistSpec(hosts ...string) *TaskSpec {
	return &TaskSpec{
		Image:       "harbor/mod/node:1",
		Env:         map[string]string{"MOD_TASK_ID": "Task_99"},
		Egress:      EgressAllowlist,
		EgressHosts: hosts,
	}
}

func TestK8sFQDNSubmitCreatesCNPWhenCiliumMode(t *testing.T) {
	b, _, dyn := newTestK8sCilium(t)
	h, job := submitK8s(t, b, allowlistSpec("api.example.com"))

	if job.Labels[LabelEgressMode] != EgressModeDeny {
		t.Fatalf("cilium-mode allowlist task's Job label = %q, want %q (narrowed by the CNP, not left open)",
			job.Labels[LabelEgressMode], EgressModeDeny)
	}
	if job.Spec.Template.Labels[LabelEgressMode] != EgressModeDeny {
		t.Fatalf("pod template egress-mode = %q, want %q", job.Spec.Template.Labels[LabelEgressMode], EgressModeDeny)
	}
	if _, unenforced := job.Annotations[AnnotationEgressAllowlist]; unenforced {
		t.Fatal("cilium mode must not carry the unenforced annotation — the CNP enforces it")
	}

	ns, name, err := parseHandle(h)
	if err != nil {
		t.Fatal(err)
	}
	cnp, err := dyn.Resource(CiliumNetworkPolicyGVR).Namespace(ns).Get(context.Background(), name+"-fqdn", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("CiliumNetworkPolicy was not created: %v", err)
	}
	refs := cnp.GetOwnerReferences()
	if len(refs) != 1 || string(refs[0].UID) != string(job.UID) || refs[0].Name != job.Name {
		t.Fatalf("CNP ownerReferences = %+v, want a single ref to job %s (uid %s)", refs, job.Name, job.UID)
	}
}

func TestK8sFQDNSubmitCNPFailureDeletesJob(t *testing.T) {
	b, cs, dyn := newTestK8sCilium(t)
	dyn.PrependReactor("create", CiliumNetworkPolicyGVR.Resource, func(action k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, fmt.Errorf("admission webhook rejected the CiliumNetworkPolicy")
	})

	_, err := b.Submit(context.Background(), allowlistSpec("api.example.com"))
	if err == nil {
		t.Fatal("want an error when the CNP create call fails")
	}

	jobs, listErr := cs.BatchV1().Jobs(b.Namespace).List(context.Background(), metav1.ListOptions{})
	if listErr != nil {
		t.Fatal(listErr)
	}
	if len(jobs.Items) != 0 {
		t.Fatalf("Job(s) left behind after CNP create failure: %+v", jobs.Items)
	}
}

func TestK8sFQDNSubmitModeOffRunsUnenforced(t *testing.T) {
	// GB.EGR.4's "auto, no Cilium" path: FQDNMode stays FQDNModeOff
	// (NewK8sBackend's default) and Dynamic is left nil. The task must still
	// run — no error — just unenforced (fail-open amendment, GB.EGR §6a).
	b, _ := newTestK8s(t)
	if b.FQDNMode != FQDNModeOff || b.Dynamic != nil {
		t.Fatalf("newTestK8s default should be off/nil-dynamic, got FQDNMode=%q Dynamic=%v", b.FQDNMode, b.Dynamic)
	}

	h, job := submitK8s(t, b, allowlistSpec("api.example.com"))
	if h == nil {
		t.Fatal("Submit returned nil handle")
	}
	if job.Labels[LabelEgressMode] != EgressModeUser {
		t.Fatalf("mode-off allowlist task's Job label = %q, want %q (open, unenforced)",
			job.Labels[LabelEgressMode], EgressModeUser)
	}
	if job.Annotations[AnnotationEgressAllowlist] != EgressAllowlistUnenforced {
		t.Fatalf("Job annotations = %v, want %s=%s", job.Annotations, AnnotationEgressAllowlist, EgressAllowlistUnenforced)
	}
	if job.Spec.Template.Annotations[AnnotationEgressAllowlist] != EgressAllowlistUnenforced {
		t.Fatalf("pod template annotations = %v, want %s=%s",
			job.Spec.Template.Annotations, AnnotationEgressAllowlist, EgressAllowlistUnenforced)
	}
}

func TestK8sFQDNSubmitModeOffNeverTouchesDynamicClient(t *testing.T) {
	// Even if a caller mistakenly leaves FQDNMode == off but still sets a
	// Dynamic client, Submit must not attempt a CNP create for it — off means
	// off. A nil-Dynamic panic here would also fail this test, which is the
	// point: off must be a true no-op on the CNP path.
	b, _ := newTestK8s(t)
	b.Dynamic = nil // explicit: off + nil dynamic is the real-world "auto, no cilium" shape
	if _, err := b.Submit(context.Background(), allowlistSpec("api.example.com")); err != nil {
		t.Fatalf("Submit with FQDNMode=off and nil Dynamic must succeed, got: %v", err)
	}
}

func TestK8sFQDNSubmitPlainUserEgressNeverGetsCNPEvenInCiliumMode(t *testing.T) {
	// A plain EgressUserEgress task (no declared allowlist) must not get a
	// CNP even when the cluster runs Cilium — CNPs are only for
	// EgressAllowlist tasks with declared hosts.
	b, _, dyn := newTestK8sCilium(t)
	h, job := submitK8s(t, b, &TaskSpec{Image: "x", Egress: EgressUserEgress})
	if job.Labels[LabelEgressMode] != EgressModeUser {
		t.Fatalf("egress label = %q, want user-egress", job.Labels[LabelEgressMode])
	}
	ns, name, _ := parseHandle(h)
	if _, err := dyn.Resource(CiliumNetworkPolicyGVR).Namespace(ns).Get(context.Background(), name+"-fqdn", metav1.GetOptions{}); err == nil {
		t.Fatal("a plain user-egress task must not get a CNP")
	}
}
