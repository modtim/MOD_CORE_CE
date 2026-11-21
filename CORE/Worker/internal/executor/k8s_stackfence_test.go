package executor

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"reflect"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

func fenceDep(ns, name, stack, haClass string, replicas int32) *appsv1.Deployment {
	labels := map[string]string{K8sStackLabel: stack}
	if haClass != "" {
		labels[K8sHAClassLabel] = haClass
	}
	r := replicas
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns, Labels: labels},
		Spec:       appsv1.DeploymentSpec{Replicas: &r},
	}
}

func fenceReplicas(t *testing.T, c *fake.Clientset, ns, name string) (int32, string) {
	t.Helper()
	d, err := c.AppsV1().Deployments(ns).Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return *d.Spec.Replicas, d.Annotations[K8sFencedReplicasAnnotation]
}

func fenceLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestK8sFenceStacksScalesFenceableToZero(t *testing.T) {
	ns := "tenant-1"
	c := fake.NewSimpleClientset(
		fenceDep(ns, "db-postgres", "db", "snapshot_restore", 1),
		fenceDep(ns, "db-api", "db", "", 2), // HA class read from the stack's other deployment
		fenceDep(ns, "web-app", "web", "stateless", 3),
		fenceDep(ns, "cache-redis", "cache", "cache_backed", 1),
		fenceDep(ns, "ext-svc", "ext", "external", 1),
		fenceDep(ns, "sleep-svc", "sleeping", "", 0), // already not serving
		fenceDep(ns, "plain", "plain", "", 1),        // unclassified fences
		fenceDep("other-ns", "x", "x", "", 1),        // outside the namespace
	)
	o := NewK8sStackObserver(c, ns)
	got, err := o.FenceStacks(context.Background(), fenceLog())
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"db", "ext", "plain"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("fenced %v, want %v", got, want)
	}
	for name, prior := range map[string]string{"db-postgres": "1", "db-api": "2", "ext-svc": "1", "plain": "1"} {
		if r, ann := fenceReplicas(t, c, ns, name); r != 0 || ann != prior {
			t.Errorf("%s: replicas=%d annotation=%q, want 0/%q", name, r, ann, prior)
		}
	}
	for name, want := range map[string]int32{"web-app": 3, "cache-redis": 1, "sleep-svc": 0} {
		if r, ann := fenceReplicas(t, c, ns, name); r != want || ann != "" {
			t.Errorf("%s: replicas=%d annotation=%q, want untouched %d", name, r, ann, want)
		}
	}
	if r, _ := fenceReplicas(t, c, "other-ns", "x"); r != 1 {
		t.Errorf("deployment outside the stack namespace was fenced")
	}
}

func TestK8sFenceStacksCollectsFailures(t *testing.T) {
	ns := "tenant-1"
	c := fake.NewSimpleClientset(
		fenceDep(ns, "a", "a", "", 1),
		fenceDep(ns, "b", "b", "", 1),
	)
	c.PrependReactor("patch", "deployments", func(a k8stesting.Action) (bool, runtime.Object, error) {
		if a.(k8stesting.PatchAction).GetName() == "a" {
			return true, nil, errors.New("forbidden")
		}
		return false, nil, nil
	})
	got, err := NewK8sStackObserver(c, ns).FenceStacks(context.Background(), fenceLog())
	if err == nil {
		t.Fatal("expected fence incomplete error")
	}
	if !reflect.DeepEqual(got, []string{"b"}) {
		t.Fatalf("one failure must not stop the others: fenced %v", got)
	}
}

func TestK8sFenceStacksListFailure(t *testing.T) {
	c := fake.NewSimpleClientset()
	c.PrependReactor("list", "deployments", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("boom")
	})
	if _, err := NewK8sStackObserver(c, "tenant-1").FenceStacks(context.Background(), fenceLog()); err == nil {
		t.Fatal("list failure must surface as an error")
	}
}

func TestExecutorFenceStacksRoutesToK8s(t *testing.T) {
	c := fake.NewSimpleClientset(fenceDep("tenant-1", "db-postgres", "db", "", 1))
	UseK8sStackObserver(NewK8sStackObserver(c, "tenant-1"))
	defer UseK8sStackObserver(nil)
	e := &Executor{log: fenceLog()}
	got, err := e.FenceStacks(context.Background())
	if err != nil || !reflect.DeepEqual(got, []string{"db"}) {
		t.Fatalf("Executor.FenceStacks under k8s: got %v %v", got, err)
	}
	if r, _ := fenceReplicas(t, c, "tenant-1", "db-postgres"); r != 0 {
		t.Fatal("deployment not scaled to 0")
	}
}
