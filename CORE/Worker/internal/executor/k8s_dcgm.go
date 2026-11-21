package executor

// k8s-reduction-engine W32: GPU activity + live VRAM from NVIDIA dcgm-exporter.
//
// metrics.k8s.io (W20/W22) has no GPU view: a GPU-bound task with a quiet CPU
// looked idle to the K8s idle watchdog, and stack VRAM was allocation-only.
// dcgm-exporter (GPU Operator ships it as nvidia-dcgm-exporter:9400) exposes
// per-GPU / per-MIG-instance gauges in the Prometheus text format and, when
// run with --kubernetes (the GPU Operator default), attributes each series to
// the pod that holds the device via the kubelet pod-resources API, adding
// `namespace`, `pod`, `container` labels (older releases: `pod_namespace`,
// `pod_name`, `container_name` — both are accepted).
//
// Metrics used (units per dcgm-exporter default-counters.csv):
//   - DCGM_FI_DEV_GPU_UTIL          gauge, percent 0..100 (whole GPUs; not
//     reported for MIG instances)
//   - DCGM_FI_PROF_GR_ENGINE_ACTIVE gauge, ratio 0..1 (profiling counter; the
//     MIG-capable utilisation signal, only if enabled in the counters CSV)
//   - DCGM_FI_DEV_FB_USED           gauge, MiB
//
// Config: MOD_K8S_DCGM_EXPORTER_URL (unset ⇒ disabled, behaviour unchanged),
// MOD_K8S_IDLE_GPU_UTIL_PCT (default 5). No new deps: the text format is
// hand-parsed; malformed lines and series without pod attribution are skipped.
// Any scrape failure ⇒ "no GPU data" ⇒ CPU/log-only behaviour as before.

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	envDCGMExporterURL = "MOD_K8S_DCGM_EXPORTER_URL"
	envIdleGPUUtilPct  = "MOD_K8S_IDLE_GPU_UTIL_PCT"
	defaultIdleGPUUtil = 5.0
	dcgmScrapeTimeout  = 3 * time.Second
	dcgmCacheTTL       = 5 * time.Second
	dcgmMaxBody        = 16 << 20
	dcgmMetricGPUUtil  = "DCGM_FI_DEV_GPU_UTIL"
	dcgmMetricGREngine = "DCGM_FI_PROF_GR_ENGINE_ACTIVE"
	dcgmMetricFBUsed   = "DCGM_FI_DEV_FB_USED"
)

// dcgmSample is one pod-attributed series from a scrape.
type dcgmSample struct {
	Metric    string
	Namespace string
	Pod       string
	Container string
	GPU       string // "gpu" label
	MIG       string // "GPU_I_ID" label ("" = whole GPU)
	Value     float64
}

// DCGMSource scrapes dcgm-exporter with a small TTL cache (shared by every
// task's idle probe and the stack observer). Implements GPUUsageSource.
type DCGMSource struct {
	URL    string
	Client *http.Client  // nil ⇒ client with dcgmScrapeTimeout
	TTL    time.Duration // 0 ⇒ dcgmCacheTTL
	Now    func() time.Time

	mu      sync.Mutex
	at      time.Time
	samples []dcgmSample
	err     error
}

var (
	dcgmFromEnvOnce sync.Once
	dcgmFromEnv     *DCGMSource
	dcgmWarnOnce    sync.Once
)

// DCGMSourceFromEnv returns the process-wide source, or nil when
// MOD_K8S_DCGM_EXPORTER_URL is unset (feature off).
func DCGMSourceFromEnv() *DCGMSource {
	dcgmFromEnvOnce.Do(func() {
		if u := strings.TrimSpace(os.Getenv(envDCGMExporterURL)); u != "" {
			dcgmFromEnv = &DCGMSource{URL: u}
		}
	})
	return dcgmFromEnv
}

// IdleGPUUtilThreshold reads MOD_K8S_IDLE_GPU_UTIL_PCT (default 5, percent).
func IdleGPUUtilThreshold() float64 {
	if v := strings.TrimSpace(os.Getenv(envIdleGPUUtilPct)); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil && f >= 0 && !math.IsNaN(f) {
			return f
		}
	}
	return defaultIdleGPUUtil
}

func (d *DCGMSource) now() time.Time {
	if d.Now != nil {
		return d.Now()
	}
	return time.Now()
}

// snapshot returns cached samples, re-scraping after TTL. Errors are cached
// for the TTL too so a dead exporter costs one timeout per TTL, not per task.
func (d *DCGMSource) snapshot(ctx context.Context) ([]dcgmSample, error) {
	ttl := d.TTL
	if ttl <= 0 {
		ttl = dcgmCacheTTL
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if !d.at.IsZero() && d.now().Sub(d.at) < ttl {
		return d.samples, d.err
	}
	d.samples, d.err = d.scrape(ctx)
	d.at = d.now()
	return d.samples, d.err
}

func (d *DCGMSource) scrape(ctx context.Context) ([]dcgmSample, error) {
	c := d.Client
	if c == nil {
		c = &http.Client{Timeout: dcgmScrapeTimeout}
	}
	ctx, cancel := context.WithTimeout(ctx, dcgmScrapeTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, d.URL, nil)
	if err != nil {
		return nil, fmt.Errorf("dcgm-exporter request: %w", err)
	}
	resp, err := c.Do(req)
	if err != nil {
		return nil, fmt.Errorf("dcgm-exporter scrape: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("dcgm-exporter scrape: HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, dcgmMaxBody))
	if err != nil {
		return nil, fmt.Errorf("dcgm-exporter read: %w", err)
	}
	return parseDCGMMetrics(body), nil
}

// parseDCGMMetrics hand-parses the Prometheus text exposition format,
// keeping only the metrics we use that carry pod attribution.
func parseDCGMMetrics(body []byte) []dcgmSample {
	var out []dcgmSample
	sc := bufio.NewScanner(bytes.NewReader(body))
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || line[0] == '#' {
			continue
		}
		name, labels, rest, ok := splitPromLine(line)
		if !ok {
			continue
		}
		switch name {
		case dcgmMetricGPUUtil, dcgmMetricGREngine, dcgmMetricFBUsed:
		default:
			continue
		}
		fields := strings.Fields(rest) // value [timestamp]
		if len(fields) == 0 {
			continue
		}
		v, err := strconv.ParseFloat(fields[0], 64)
		if err != nil || math.IsNaN(v) || math.IsInf(v, 0) {
			continue
		}
		pod := firstLabel(labels, "pod", "pod_name")
		if pod == "" {
			continue // no --kubernetes attribution (or device not held by a pod)
		}
		out = append(out, dcgmSample{
			Metric:    name,
			Namespace: firstLabel(labels, "namespace", "pod_namespace"),
			Pod:       pod,
			Container: firstLabel(labels, "container", "container_name"),
			GPU:       labels["gpu"],
			MIG:       labels["GPU_I_ID"],
			Value:     v,
		})
	}
	return out
}

func firstLabel(l map[string]string, keys ...string) string {
	for _, k := range keys {
		if v := l[k]; v != "" {
			return v
		}
	}
	return ""
}

// splitPromLine splits `name{k="v",...} rest` (labels optional).
func splitPromLine(line string) (name string, labels map[string]string, rest string, ok bool) {
	i := strings.IndexAny(line, "{ \t")
	if i <= 0 {
		return "", nil, "", false
	}
	name = line[:i]
	if line[i] != '{' {
		return name, map[string]string{}, line[i:], true
	}
	labels = map[string]string{}
	s := line[i+1:]
	for {
		s = strings.TrimLeft(s, " \t,")
		if s == "" {
			return "", nil, "", false
		}
		if s[0] == '}' {
			return name, labels, s[1:], true
		}
		eq := strings.IndexByte(s, '=')
		if eq <= 0 || eq+1 >= len(s) || s[eq+1] != '"' {
			return "", nil, "", false
		}
		key := strings.TrimSpace(s[:eq])
		s = s[eq+2:]
		var b strings.Builder
		closed := false
		for j := 0; j < len(s); j++ {
			c := s[j]
			if c == '\\' && j+1 < len(s) {
				j++
				switch s[j] {
				case 'n':
					b.WriteByte('\n')
				default:
					b.WriteByte(s[j])
				}
				continue
			}
			if c == '"' {
				s = s[j+1:]
				closed = true
				break
			}
			b.WriteByte(c)
		}
		if !closed {
			return "", nil, "", false
		}
		labels[key] = b.String()
	}
}

// PodVRAMMiB implements GPUUsageSource: framebuffer used per pod, summed
// across every GPU / MIG instance attributed to it.
func (d *DCGMSource) PodVRAMMiB(ctx context.Context, namespace string, podNames []string) (map[string]float64, error) {
	samples, err := d.snapshot(ctx)
	if err != nil {
		return nil, err
	}
	want := make(map[string]bool, len(podNames))
	for _, p := range podNames {
		want[p] = true
	}
	out := map[string]float64{}
	for _, s := range samples {
		if s.Metric != dcgmMetricFBUsed || !want[s.Pod] || !nsMatch(namespace, s.Namespace) {
			continue
		}
		out[s.Pod] += s.Value
	}
	return out, nil
}

// PodGPUUtilPct is the pod's busiest device utilisation in percent
// (DCGM_FI_DEV_GPU_UTIL, or GR_ENGINE_ACTIVE×100 for MIG). ok=false when
// the pod has no utilisation series.
func (d *DCGMSource) PodGPUUtilPct(ctx context.Context, namespace, pod string) (pct float64, ok bool, err error) {
	samples, err := d.snapshot(ctx)
	if err != nil {
		return 0, false, err
	}
	for _, s := range samples {
		if s.Pod != pod || !nsMatch(namespace, s.Namespace) {
			continue
		}
		var v float64
		switch s.Metric {
		case dcgmMetricGPUUtil:
			v = s.Value
		case dcgmMetricGREngine:
			v = s.Value * 100
		default:
			continue
		}
		if !ok || v > pct {
			pct, ok = v, true
		}
	}
	return pct, ok, nil
}

// nsMatch: an empty namespace on either side does not disqualify (the
// observer may pass "" and old exporters may omit the label).
func nsMatch(want, got string) bool {
	return want == "" || got == "" || want == got
}

// K8sGPUActivity is an IdleWatchdog.Active probe: task pod GPU util above
// Threshold percent. DCGM unavailable ⇒ false (never keeps a task alive).
type K8sGPUActivity struct {
	Source    *DCGMSource
	Pod       func(ctx context.Context) (namespace, pod string, err error)
	Threshold float64
	Log       *slog.Logger
}

// Active reports whether the task pod's GPU is busier than the threshold.
func (a *K8sGPUActivity) Active(ctx context.Context) bool {
	if a == nil || a.Source == nil || a.Pod == nil {
		return false
	}
	ns, pod, err := a.Pod(ctx)
	if err != nil || pod == "" {
		return false
	}
	pct, ok, err := a.Source.PodGPUUtilPct(ctx, ns, pod)
	if err != nil {
		dcgmWarnOnce.Do(func() {
			l := a.Log
			if l == nil {
				l = slog.Default()
			}
			l.Warn("k8s idle watchdog: dcgm-exporter unreadable, GPU activity ignored", "error", err)
		})
		return false
	}
	return ok && pct > a.Threshold
}

// anyActive ORs activity probes (short-circuit; nil probes skipped).
func anyActive(probes ...func(context.Context) bool) func(context.Context) bool {
	return func(ctx context.Context) bool {
		for _, p := range probes {
			if p != nil && p(ctx) {
				return true
			}
		}
		return false
	}
}

// TaskActivity is the idle-watchdog Active probe for the Job behind h:
// task CPU (W20) OR, when MOD_K8S_DCGM_EXPORTER_URL is set, task-pod GPU
// utilisation above MOD_K8S_IDLE_GPU_UTIL_PCT. Without DCGM it is exactly
// TaskCPUActivity(h, log).Active.
func (b *K8sBackend) TaskActivity(h *Handle, log *slog.Logger) func(context.Context) bool {
	cpu := b.TaskCPUActivity(h, log)
	src := DCGMSourceFromEnv()
	if src == nil {
		return cpu.Active
	}
	gpu := &K8sGPUActivity{Source: src, Pod: cpu.Pod, Threshold: IdleGPUUtilThreshold(), Log: log}
	return anyActive(cpu.Active, gpu.Active)
}
