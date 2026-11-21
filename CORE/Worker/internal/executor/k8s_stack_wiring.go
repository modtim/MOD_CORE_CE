package executor

// k8s-reduction-engine W1: stack-observation wiring for MOD_STACK_BACKEND=k8s.
//
// The worker process installs a K8sStackObserver once at start (only when the
// stack backend is k8s). Executor.ObserveStacks then reports K8s Deployments
// instead of docker-compose projects, and Executor.FenceStacks scales the
// fenceable stack Deployments to 0 (W5, k8s_stackfence.go). With no observer
// installed (the default / docker) both are unchanged.

import (
	"context"
	"os"
	"strings"
	"sync/atomic"
)

// k8sStackNamespaceEnvVar scopes stack observation; unset = namespace-per-tenant
// (every namespace labelled app.kubernetes.io/managed-by=mod).
const k8sStackNamespaceEnvVar = "MOD_K8S_STACK_NAMESPACE"

type stackObserver interface {
	ObserveStacks(ctx context.Context) ([]StackObservation, error)
}

type stackObserverBox struct{ o stackObserver }

var activeK8sStacks atomic.Pointer[stackObserverBox]

// K8sStackNamespace returns the namespace stacks are observed in ("" =
// per-tenant namespaces, see K8sTenantNamespaceSelector).
func K8sStackNamespace() string {
	return strings.TrimSpace(os.Getenv(k8sStackNamespaceEnvVar))
}

// UseK8sStackObserver routes Executor.ObserveStacks to o (nil restores the
// docker path). Called once at worker start when the stack backend is k8s.
func UseK8sStackObserver(o stackObserver) {
	if o == nil {
		activeK8sStacks.Store(nil)
		return
	}
	activeK8sStacks.Store(&stackObserverBox{o: o})
}

func k8sStackObserverActive() stackObserver {
	if b := activeK8sStacks.Load(); b != nil {
		return b.o
	}
	return nil
}
