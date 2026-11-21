package executor

// k8s-reduction-engine W20: CPU activity for the K8s idle watchdog.
//
// The docker path counts a container as making progress when the resource
// monitor sees CPUPercent > 1.0 (cmd/worker/main.go cb.ResourceActive). On
// K8s the equivalent reading is the Metrics API PodMetrics for the task pod
// (metrics.k8s.io/v1beta1, served by metrics-server), `task` container
// usage.cpu. k8s.io/metrics is not a module dependency, so the object is
// fetched with a raw GET through the clientset's discovery REST client and
// decoded into a minimal struct.
//
// Degradation: if the metrics.k8s.io group is not installed or the worker is
// forbidden from reading it, the probe turns itself off for the task (the
// watchdog is log-only again) and warns once. Transient failures (the
// pod has no sample yet, metrics-server briefly 503) count as "not active"
// and are logged once per task at debug. The unavailable warning is logged
// once per worker process.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/client-go/kubernetes"
)

// k8sCPUActiveMilli mirrors docker's CPUPercent > 1.0 (1% of one core).
const k8sCPUActiveMilli = 10

const k8sMetricsGroupVersion = "metrics.k8s.io/v1beta1"

// ErrMetricsUnavailable marks a permanent Metrics API failure (group not
// installed, or RBAC forbids pods.metrics.k8s.io get).
var ErrMetricsUnavailable = errors.New("k8s metrics API unavailable")

type podMetrics struct {
	Containers []struct {
		Name  string            `json:"name"`
		Usage map[string]string `json:"usage"`
	} `json:"containers"`
}

// parseTaskPodUsage extracts the `task` container's usage from a PodMetrics
// JSON body (the stage-in/out sidecars are not the workload).
func parseTaskPodUsage(body []byte) (PodUsage, error) {
	var pm podMetrics
	if err := json.Unmarshal(body, &pm); err != nil {
		return PodUsage{}, fmt.Errorf("decode pod metrics: %w", err)
	}
	for _, c := range pm.Containers {
		if c.Name != k8sTaskContainerName {
			continue
		}
		var u PodUsage
		cpu, err := resource.ParseQuantity(c.Usage["cpu"])
		if err != nil {
			return PodUsage{}, fmt.Errorf("parse cpu quantity %q: %w", c.Usage["cpu"], err)
		}
		u.CPUMilli = cpu.MilliValue()
		if m, err := resource.ParseQuantity(c.Usage["memory"]); err == nil {
			u.MemBytes = m.Value()
		}
		return u, nil
	}
	return PodUsage{}, fmt.Errorf("pod metrics: no %q container sample", k8sTaskContainerName)
}

type restPodMetrics struct {
	client kubernetes.Interface

	checkOnce sync.Once
	checkErr  error
}

// NewRESTPodMetricsSource is a PodMetricsSource that GETs PodMetrics through
// the clientset's raw discovery REST client (only `get` RBAC is needed).
// Usage is the `task` container's. Pods without a sample yet are omitted.
func NewRESTPodMetricsSource(client kubernetes.Interface) PodMetricsSource {
	return &restPodMetrics{client: client}
}

func (r *restPodMetrics) groupInstalled() error {
	r.checkOnce.Do(func() {
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

func (r *restPodMetrics) PodUsage(ctx context.Context, namespace string, podNames []string) (map[string]PodUsage, error) {
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
		u, err := parseTaskPodUsage(body)
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

// K8sCPUActivity is an IdleWatchdog.Active probe for one task.
type K8sCPUActivity struct {
	Source    PodMetricsSource
	Pod       func(ctx context.Context) (namespace, pod string, err error)
	Threshold int64 // millicores; 0 = k8sCPUActiveMilli
	Log       *slog.Logger

	off       atomic.Bool
	transOnce sync.Once
}

// k8sMetricsUnavailableWarn makes the "degrading to log-only" warning
// once-per-process rather than once per task (or every 15s tick).
var k8sMetricsUnavailableWarn sync.Once

func (a *K8sCPUActivity) logger() *slog.Logger {
	if a.Log != nil {
		return a.Log
	}
	return slog.Default()
}

// Active reports whether the task container is using CPU above the threshold.
func (a *K8sCPUActivity) Active(ctx context.Context) bool {
	if a == nil || a.Source == nil || a.Pod == nil || a.off.Load() {
		return false
	}
	ns, pod, err := a.Pod(ctx)
	if err != nil || pod == "" {
		return false
	}
	usage, err := a.Source.PodUsage(ctx, ns, []string{pod})
	if err != nil {
		if errors.Is(err, ErrMetricsUnavailable) {
			a.off.Store(true)
			k8sMetricsUnavailableWarn.Do(func() {
				a.logger().Warn("k8s idle watchdog: metrics API unavailable, CPU activity disabled (log output only)",
					"error", err)
			})
		} else {
			a.transOnce.Do(func() {
				a.logger().Debug("k8s idle watchdog: pod metrics not readable yet", "error", err)
			})
		}
		return false
	}
	u, ok := usage[pod]
	if !ok {
		return false // no sample yet
	}
	th := a.Threshold
	if th <= 0 {
		th = k8sCPUActiveMilli
	}
	return u.CPUMilli > th
}

// TaskCPUActivity wires a CPU-activity probe for the Job behind h.
func (b *K8sBackend) TaskCPUActivity(h *Handle, log *slog.Logger) *K8sCPUActivity {
	return &K8sCPUActivity{
		Source: NewRESTPodMetricsSource(b.Client),
		Log:    log,
		Pod: func(ctx context.Context) (string, string, error) {
			ns, p, err := b.taskPod(ctx, h)
			if err != nil || p == nil {
				return ns, "", err
			}
			return ns, p.Name, nil
		},
	}
}
