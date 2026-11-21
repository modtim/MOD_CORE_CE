//go:build k8sintegration

// K8sBackend integration tests against a REAL cluster (k8s-reduction-engine
// T12) — normally Docker Desktop's built-in Kubernetes.
//
// Opt-in only: compiled solely with `-tags k8sintegration`, and every test
// SKIPS (never fails) when no kubeconfig / reachable cluster is found. Run
// via CORE/Worker/k8s/it/run-integration.sh, or directly:
//
//	go test -tags k8sintegration ./internal/executor/ -run Integration -count=1 -v
//
// Env:
//
//	KUBECONFIG             kubeconfig to use (default ~/.kube/config)
//	MOD_K8S_IT_NAMESPACE   use this (pre-created, e.g. NetworkPolicies already
//	                       applied by run-integration.sh) namespace instead of
//	                       creating + deleting a throwaway `mod-it-<rand>` one
//	MOD_K8S_IT_IMAGE       task image (default busybox:1.36)
package executor

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/k8sclient"
)

const itTaskTimeout = 4 * time.Minute // first run pulls the image

// itCluster returns a live clientset + a namespace to run in (and registers
// cleanup), or skips the test when no cluster is reachable.
func itCluster(t *testing.T) (kubernetes.Interface, string) {
	t.Helper()
	cfg, err := k8sclient.LoadConfig("")
	if err != nil {
		t.Skipf("k8s integration: no kubeconfig/in-cluster config (%v) — enable Docker Desktop Kubernetes or set KUBECONFIG", err)
	}
	probeCfg := *cfg
	probeCfg.Timeout = 8 * time.Second
	probe, err := kubernetes.NewForConfig(&probeCfg)
	if err != nil {
		t.Skipf("k8s integration: cannot build client (%v)", err)
	}
	if _, err := probe.Discovery().ServerVersion(); err != nil {
		t.Skipf("k8s integration: cluster at %s not reachable (%v)", cfg.Host, err)
	}
	cs, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		t.Skipf("k8s integration: cannot build client (%v)", err)
	}

	ctx := context.Background()
	if ns := strings.TrimSpace(os.Getenv("MOD_K8S_IT_NAMESPACE")); ns != "" {
		if _, err := cs.CoreV1().Namespaces().Get(ctx, ns, metav1.GetOptions{}); err != nil {
			t.Fatalf("MOD_K8S_IT_NAMESPACE=%s not usable: %v", ns, err)
		}
		return cs, ns // owned (and deleted) by the caller, e.g. run-integration.sh
	}

	buf := make([]byte, 3)
	_, _ = rand.Read(buf)
	ns := "mod-it-" + hex.EncodeToString(buf)
	_, err = cs.CoreV1().Namespaces().Create(ctx, &corev1.Namespace{
		ObjectMeta: metav1.ObjectMeta{Name: ns, Labels: map[string]string{LabelPartOf: LabelPartOfValue, "mod.tech/it": "true"}},
	}, metav1.CreateOptions{})
	if err != nil {
		t.Fatalf("create namespace %s: %v", ns, err)
	}
	t.Cleanup(func() {
		bg := metav1.DeletePropagationBackground
		if err := cs.CoreV1().Namespaces().Delete(context.Background(), ns, metav1.DeleteOptions{PropagationPolicy: &bg}); err != nil && !apierrors.IsNotFound(err) {
			t.Logf("cleanup: delete namespace %s: %v", ns, err)
		}
	})
	return cs, ns
}

func itImage() string {
	if v := strings.TrimSpace(os.Getenv("MOD_K8S_IT_IMAGE")); v != "" {
		return v
	}
	return "busybox:1.36"
}

// itWaitDone polls until the task leaves TaskRunning or the timeout hits.
func itWaitDone(t *testing.T, b *K8sBackend, h *Handle) *TaskStatus {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), itTaskTimeout)
	defer cancel()
	for {
		st, err := b.Poll(ctx, h)
		if err != nil {
			t.Fatalf("Poll(%s): %v", h.ID, err)
		}
		if st.Phase != TaskRunning {
			return st
		}
		select {
		case <-ctx.Done():
			t.Fatalf("task %s still running after %s", h.ID, itTaskTimeout)
		case <-time.After(2 * time.Second):
		}
	}
}

func itSpec(taskID string, cmd ...string) *TaskSpec {
	return &TaskSpec{
		Image:  itImage(),
		Env:    map[string]string{"MOD_TASK_ID": taskID},
		Cmd:    cmd,
		Egress: EgressDefaultDeny, // echo/exit/sleep need no network
	}
}

func TestK8sIntegration(t *testing.T) {
	cs, ns := itCluster(t)
	b := NewK8sBackend(cs, ns)
	suffix := func() string { x := make([]byte, 3); _, _ = rand.Read(x); return hex.EncodeToString(x) }

	t.Run("SucceedsWithLogs", func(t *testing.T) {
		h, err := b.Submit(context.Background(), itSpec("it-ok-"+suffix(), "sh", "-c", "echo hello"))
		if err != nil {
			t.Fatalf("Submit: %v", err)
		}
		t.Cleanup(func() { _ = b.Cancel(context.Background(), h) })
		st := itWaitDone(t, b, h)
		if st.Phase != TaskSucceeded || st.ExitCode != 0 {
			t.Fatalf("want succeeded/0, got %+v", st)
		}
		rc, err := b.Logs(context.Background(), h)
		if err != nil {
			t.Fatalf("Logs: %v", err)
		}
		defer rc.Close()
		out, _ := io.ReadAll(rc)
		if !strings.Contains(string(out), "hello") {
			t.Fatalf("logs %q do not contain hello", out)
		}
	})

	t.Run("FailsWithExitCode", func(t *testing.T) {
		h, err := b.Submit(context.Background(), itSpec("it-fail-"+suffix(), "sh", "-c", "exit 3"))
		if err != nil {
			t.Fatalf("Submit: %v", err)
		}
		t.Cleanup(func() { _ = b.Cancel(context.Background(), h) })
		st := itWaitDone(t, b, h)
		if st.Phase != TaskFailed || st.ExitCode != 3 {
			t.Fatalf("want failed/3, got %+v", st)
		}
	})

	t.Run("CancelLongRunning", func(t *testing.T) {
		h, err := b.Submit(context.Background(), itSpec("it-cancel-"+suffix(), "sleep", "300"))
		if err != nil {
			t.Fatalf("Submit: %v", err)
		}
		if st, err := b.Poll(context.Background(), h); err != nil || st.Phase != TaskRunning {
			t.Fatalf("want running right after submit, got %+v err=%v", st, err)
		}
		if err := b.Cancel(context.Background(), h); err != nil {
			t.Fatalf("Cancel: %v", err)
		}
		if err := b.Cancel(context.Background(), h); err != nil { // idempotent
			t.Fatalf("second Cancel: %v", err)
		}
		_, name, _ := strings.Cut(h.ID, "/")
		deadline := time.Now().Add(90 * time.Second)
		for {
			_, err := cs.BatchV1().Jobs(ns).Get(context.Background(), name, metav1.GetOptions{})
			if apierrors.IsNotFound(err) {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("job %s still present after Cancel (err=%v)", h.ID, err)
			}
			time.Sleep(2 * time.Second)
		}
		// Pods go via Background propagation; they must be gone (or terminating) soon.
		deadline = time.Now().Add(90 * time.Second)
		for {
			pods, err := cs.CoreV1().Pods(ns).List(context.Background(), metav1.ListOptions{LabelSelector: LabelJob + "=" + name})
			if err == nil && (len(pods.Items) == 0 || pods.Items[0].DeletionTimestamp != nil) {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("pod for %s not deleted after Cancel", h.ID)
			}
			time.Sleep(2 * time.Second)
		}
	})
}
