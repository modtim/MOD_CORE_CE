package executor

// k8s-reduction-engine W5: FenceStacks on the K8s stack backend.
//
// The Docker FenceStacks (stackobserve.go) `compose stop`s every serving,
// non-re-placeable stack when the worker loses its manager past the lease
// window (stack-ha-reconciliation §3.1). The K8s equivalent of `stop` (not
// `down`) is scaling the stack's Deployments to 0 replicas: pods go away, the
// Deployment object, its PVCs and its config stay, so reconciliation can scale
// it back up if the stack belongs here after all.
//
// Selection mirrors Docker:
//   - stacks are Deployments labelled mod.techlabs/stack=<name>, in
//     K8sStackNamespace() (the observer's namespace), or when unset in every
//     namespace labelled app.kubernetes.io/managed-by=mod (W22);
//   - a stack already not serving is skipped. On K8s "not serving" is
//     "desired replicas == 0" rather than "no ready pods": a Deployment with
//     replicas>0 and nothing ready yet (crash-looping, pulling) will come up by
//     itself, which is exactly the second live copy fencing exists to prevent;
//   - re-placeable HA classes (mod.techlabs/ha-class ∈ stateless/cache_backed,
//     StackObservation.Fenceable) are kept serving.
//
// The pre-fence replica count is stashed in an annotation as an audit record
// only. There is deliberately NO worker-side unfence (W12): on regaining the
// manager the Docker path restarts nothing (heartbeat/fence.go
// noteFenceRecovered) because the manager may already have re-placed the stack,
// and a worker restoring its old copy is the two-live-instances state fencing
// exists to prevent. Recovery is manager-driven; the API's K8sStackBackend
// clears this annotation on any scale/wake/sleep/re-deploy so a stale record
// cannot mislead an operator. Per-stack failures are
// collected, never aborting: one stack refusing to scale must not leave the
// others running.

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"strconv"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

// K8sFencedReplicasAnnotation records a Deployment's replica count at fence
// time.
const K8sFencedReplicasAnnotation = "mod.techlabs/fenced-replicas"

// stackFencer is implemented by stack observers that can also fence.
type stackFencer interface {
	FenceStacks(ctx context.Context, log *slog.Logger) ([]string, error)
}

// FenceStacks scales every serving, fenceable stack Deployment to 0 and
// returns the names of the stacks it fenced (same contract as the Docker
// Executor.FenceStacks).
func (o *K8sStackObserver) FenceStacks(ctx context.Context, log *slog.Logger) ([]string, error) {
	if o == nil || o.Client == nil {
		return nil, fmt.Errorf("k8s stack fence: no client")
	}
	if log == nil {
		log = slog.Default()
	}
	selector := o.Selector
	if selector == "" {
		selector = K8sStackLabel
	}
	namespaces, err := o.stackNamespaces(ctx)
	if err != nil {
		return nil, fmt.Errorf("cannot enumerate stacks to fence: %w", err)
	}
	// Per-namespace list failures do not stop fencing the namespaces that
	// could be listed; they are reported in the returned error.
	var deps []appsv1.Deployment
	var failures []string
	for _, ns := range namespaces {
		l, err := o.Client.AppsV1().Deployments(ns).List(ctx, metav1.ListOptions{LabelSelector: selector})
		if err != nil {
			failures = append(failures, fmt.Sprintf("list %s: %v", ns, err))
			continue
		}
		deps = append(deps, l.Items...)
	}
	if len(failures) > 0 && len(failures) == len(namespaces) {
		return nil, fmt.Errorf("cannot enumerate stacks to fence: %s", strings.Join(failures, "; "))
	}

	type stackDeps struct {
		namespace, stack, haClass string
		deployments               []string
		replicas                  map[string]int32
	}
	byKey := map[string]*stackDeps{}
	for i := range deps {
		d := &deps[i]
		stack := strings.TrimSpace(d.Labels[K8sStackLabel])
		if stack == "" {
			continue
		}
		key := d.Namespace + "/" + stack
		s := byKey[key]
		if s == nil {
			s = &stackDeps{namespace: d.Namespace, stack: stack, replicas: map[string]int32{}}
			byKey[key] = s
		}
		if v := strings.TrimSpace(d.Labels[K8sHAClassLabel]); v != "" && s.haClass == "" {
			s.haClass = v
		}
		if r := deploymentDesired(d); r > 0 {
			s.deployments = append(s.deployments, d.Name)
			s.replicas[d.Name] = r
		}
	}

	keys := make([]string, 0, len(byKey))
	for k := range byKey {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	var stopped []string
	for _, k := range keys {
		s := byKey[k]
		if len(s.deployments) == 0 {
			continue // already scaled to 0 — not serving
		}
		if !(StackObservation{HAClass: s.haClass}).Fenceable() {
			log.Info("fence: keeping stack, class is re-placeable",
				"project", s.stack, "namespace", s.namespace, "ha_class", s.haClass)
			continue
		}
		sort.Strings(s.deployments)
		var errs []string
		for _, name := range s.deployments {
			if err := o.scaleDeploymentToZero(ctx, s.namespace, name, s.replicas[name]); err != nil {
				errs = append(errs, fmt.Sprintf("%s: %v", name, err))
			}
		}
		if len(errs) > 0 {
			failures = append(failures, fmt.Sprintf("%s: %s", k, strings.Join(errs, ", ")))
			continue
		}
		stopped = append(stopped, s.stack)
	}
	if len(failures) > 0 {
		return stopped, fmt.Errorf("fence incomplete: %s", strings.Join(failures, "; "))
	}
	return stopped, nil
}

// scaleDeploymentToZero merge-patches spec.replicas=0 and records the prior
// count. A merge patch (RBAC: patch deployments) rather than the scale
// subresource keeps the annotation write in the same atomic request.
func (o *K8sStackObserver) scaleDeploymentToZero(ctx context.Context, ns, name string, prior int32) error {
	patch := map[string]any{
		"metadata": map[string]any{
			"annotations": map[string]string{
				K8sFencedReplicasAnnotation: strconv.Itoa(int(prior)),
			},
		},
		"spec": map[string]any{"replicas": 0},
	}
	body, err := json.Marshal(patch)
	if err != nil {
		return err
	}
	_, err = o.Client.AppsV1().Deployments(ns).Patch(ctx, name, types.MergePatchType, body, metav1.PatchOptions{})
	return err
}
