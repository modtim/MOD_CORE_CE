package executor

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/fake"
	"k8s.io/client-go/rest"
	k8stesting "k8s.io/client-go/testing"
)

func w22NS(name string, managed bool) *corev1.Namespace {
	l := map[string]string{}
	if managed {
		l["app.kubernetes.io/managed-by"] = "mod"
		l["mod.techlabs/tenant-id"] = strings.TrimPrefix(name, "tenant-")
	}
	return &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name, Labels: l}}
}

func w22Clientset() *fake.Clientset {
	return fake.NewSimpleClientset(
		w22NS("tenant-1", true), w22NS("tenant-2", true), w22NS("rogue", false),
		fenceDep("tenant-1", "db-postgres", "db", "", 1),
		fenceDep("tenant-2", "vlm-server", "vlm", "", 2),
		fenceDep("rogue", "evil-web", "evil", "", 1),
	)
}

// assertNoClusterWideList: per-tenant mode must never list deployments/pods
// with NamespaceAll (the chart does not grant it).
func assertNoClusterWideList(t *testing.T, c *fake.Clientset) {
	t.Helper()
	for _, a := range c.Actions() {
		if a.GetVerb() == "list" && (a.GetResource().Resource == "deployments" || a.GetResource().Resource == "pods") && a.GetNamespace() == "" {
			t.Fatalf("cluster-wide list of %s", a.GetResource().Resource)
		}
	}
}

func TestK8sStackObserve_PerTenantNamespacesOnly(t *testing.T) {
	c := w22Clientset()
	obs, err := NewK8sStackObserver(c, "").ObserveStacksDetailed(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, o := range obs {
		got = append(got, o.Namespace+"/"+o.Project)
	}
	if want := []string{"tenant-1/db", "tenant-2/vlm"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
	assertNoClusterWideList(t, c)
}

func TestK8sStackObserve_SingleNamespaceUnchanged(t *testing.T) {
	c := w22Clientset()
	obs, err := NewK8sStackObserver(c, "rogue").ObserveStacks(context.Background())
	if err != nil || len(obs) != 1 || obs[0].Project != "evil" {
		t.Fatalf("single-ns: %v %v", obs, err)
	}
	for _, a := range c.Actions() {
		if a.GetResource().Resource == "namespaces" {
			t.Fatal("single-ns mode must not list namespaces")
		}
	}
}

func TestK8sStackObserve_NoTenantNamespacesIsEmptyNotError(t *testing.T) {
	c := fake.NewSimpleClientset(w22NS("rogue", false), fenceDep("rogue", "evil-web", "evil", "", 1))
	obs, err := NewK8sStackObserver(c, "").ObserveStacks(context.Background())
	if err != nil || len(obs) != 0 {
		t.Fatalf("got %v %v", obs, err)
	}
}

func TestK8sStackNamespaceListErrorIsError(t *testing.T) {
	c := w22Clientset()
	c.PrependReactor("list", "namespaces", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("forbidden")
	})
	o := NewK8sStackObserver(c, "")
	if obs, err := o.ObserveStacks(context.Background()); err == nil || obs != nil {
		t.Fatalf("observe: %v %v", obs, err)
	}
	if _, err := o.FenceStacks(context.Background(), fenceLog()); err == nil {
		t.Fatal("fence must error when namespaces cannot be listed")
	}
}

func TestK8sFenceStacks_PerTenantNamespacesOnly(t *testing.T) {
	c := w22Clientset()
	got, err := NewK8sStackObserver(c, "").FenceStacks(context.Background(), fenceLog())
	if err != nil || !reflect.DeepEqual(got, []string{"db", "vlm"}) {
		t.Fatalf("fence: %v %v", got, err)
	}
	if r, _ := fenceReplicas(t, c, "tenant-2", "vlm-server"); r != 0 {
		t.Fatal("tenant-2 not fenced")
	}
	if r, ann := fenceReplicas(t, c, "rogue", "evil-web"); r != 1 || ann != "" {
		t.Fatal("unmanaged namespace must never be touched")
	}
	assertNoClusterWideList(t, c)
}

func TestK8sFenceStacks_OneNamespaceListFailureFencesTheRest(t *testing.T) {
	c := w22Clientset()
	c.PrependReactor("list", "deployments", func(a k8stesting.Action) (bool, runtime.Object, error) {
		if a.GetNamespace() == "tenant-1" {
			return true, nil, errors.New("boom")
		}
		return false, nil, nil
	})
	got, err := NewK8sStackObserver(c, "").FenceStacks(context.Background(), fenceLog())
	if err == nil || !strings.Contains(err.Error(), "tenant-1") {
		t.Fatalf("want error naming tenant-1, got %v", err)
	}
	if !reflect.DeepEqual(got, []string{"vlm"}) {
		t.Fatalf("fenced %v", got)
	}
	if r, _ := fenceReplicas(t, c, "tenant-1", "db-postgres"); r != 1 {
		t.Fatal("unlisted namespace touched")
	}
}

func TestParseStackPodUsageSumsContainers(t *testing.T) {
	u, err := parseStackPodUsage([]byte(`{"containers":[
		{"name":"web","usage":{"cpu":"250m","memory":"100Mi"}},
		{"name":"sidecar","usage":{"cpu":"1500000n","memory":"28Mi"}}]}`))
	if err != nil || u.CPUMilli != 252 || u.MemBytes != 128<<20 {
		t.Fatalf("%+v %v", u, err)
	}
	if _, err := parseStackPodUsage([]byte(`{"containers":[]}`)); err == nil {
		t.Fatal("empty sample must error")
	}
}

func TestK8sStackPodMetrics_AbsentDegrades(t *testing.T) {
	c := w22Clientset()
	src := NewK8sStackPodMetricsSource(c)
	if _, err := src.PodUsage(context.Background(), "tenant-1", []string{"p"}); !errors.Is(err, ErrMetricsUnavailable) {
		t.Fatalf("want ErrMetricsUnavailable, got %v", err)
	}
	c2 := fake.NewSimpleClientset(
		k8sObsDeployment("modstack-a-x", "modstack-a", 1, 1, nil),
		k8sObsPod("modstack-a-x-abc12-q", "modstack-a-x", "modstack-a", "x", true, nil, ""),
	)
	o := NewK8sStackObserver(c2, k8sObsNS)
	o.Metrics = NewK8sStackPodMetricsSource(c2)
	obs, err := o.ObserveStacksDetailed(context.Background())
	if err != nil || len(obs) != 1 || obs[0].MetricsSource != K8sSourceUnavailable || obs[0].Running != 1 {
		t.Fatalf("absent metrics-server must degrade: %+v %v", obs, err)
	}
}

func TestK8sStackPodMetrics_PresentIsLive(t *testing.T) {
	const pod = "modstack-a-x-abc12-q"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/apis/metrics.k8s.io/v1beta1":
			_, _ = w.Write([]byte(`{"kind":"APIResourceList","apiVersion":"v1","groupVersion":"metrics.k8s.io/v1beta1","resources":[{"name":"pods","namespaced":true,"kind":"PodMetrics","verbs":["get","list"]}]}`))
		case "/apis/metrics.k8s.io/v1beta1/namespaces/" + k8sObsNS + "/pods/" + pod:
			_, _ = w.Write([]byte(`{"containers":[{"name":"x","usage":{"cpu":"300m","memory":"1Gi"}},{"name":"y","usage":{"cpu":"200m","memory":"1Gi"}}]}`))
		default:
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"kind":"Status","apiVersion":"v1","status":"Failure","reason":"NotFound","code":404}`))
		}
	}))
	defer srv.Close()
	rc, err := kubernetes.NewForConfig(&rest.Config{Host: srv.URL})
	if err != nil {
		t.Fatal(err)
	}
	c := fake.NewSimpleClientset(
		k8sObsDeployment("modstack-a-x", "modstack-a", 1, 1, nil),
		k8sObsPod(pod, "modstack-a-x", "modstack-a", "x", true, nil, ""),
	)
	o := NewK8sStackObserver(c, k8sObsNS)
	o.Metrics = NewK8sStackPodMetricsSource(rc)
	obs, err := o.ObserveStacksDetailed(context.Background())
	if err != nil || len(obs) != 1 {
		t.Fatalf("%v %v", obs, err)
	}
	if s := obs[0]; s.MetricsSource != K8sSourceLive || s.CPUMilli != 500 || s.MemBytes != 2<<30 {
		t.Fatalf("metrics: %+v", s)
	}
}
