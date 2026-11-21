package executor

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// Realistic dcgm-exporter 3.x (--kubernetes) output: whole GPU held by the
// task pod, two MIG instances on GPU 1 (one held by a stack pod, one idle),
// an unattributed GPU, legacy label names, and junk lines.
const dcgmFixture = `# HELP DCGM_FI_DEV_GPU_UTIL GPU utilization (in %).
# TYPE DCGM_FI_DEV_GPU_UTIL gauge
DCGM_FI_DEV_GPU_UTIL{gpu="0",UUID="GPU-aaaa",pci_bus_id="00000000:01:00.0",device="nvidia0",modelName="NVIDIA A100-SXM4-40GB",Hostname="node-1",DCGM_FI_DRIVER_VERSION="550.54.15",container="task",namespace="ns",pod="job-abc"} 87
DCGM_FI_DEV_GPU_UTIL{gpu="2",UUID="GPU-cccc",device="nvidia2",modelName="NVIDIA A100-SXM4-40GB",Hostname="node-1"} 99
# HELP DCGM_FI_DEV_FB_USED Framebuffer memory used (in MiB).
# TYPE DCGM_FI_DEV_FB_USED gauge
DCGM_FI_DEV_FB_USED{gpu="0",UUID="GPU-aaaa",device="nvidia0",modelName="NVIDIA A100-SXM4-40GB",Hostname="node-1",container="task",namespace="ns",pod="job-abc"} 12034
DCGM_FI_DEV_FB_USED{gpu="1",UUID="GPU-bbbb",device="nvidia1",modelName="NVIDIA A100-SXM4-40GB",GPU_I_PROFILE="3g.20gb",GPU_I_ID="1",Hostname="node-1",container="nim",namespace="tenant-a",pod="stack-vsr-7d9f-x1"} 9000
DCGM_FI_DEV_FB_USED{gpu="1",UUID="GPU-bbbb",device="nvidia1",modelName="NVIDIA A100-SXM4-40GB",GPU_I_PROFILE="3g.20gb",GPU_I_ID="2",Hostname="node-1",container="nim",namespace="tenant-a",pod="stack-vsr-7d9f-x1"} 1240.5
DCGM_FI_DEV_FB_USED{gpu="1",UUID="GPU-bbbb",GPU_I_PROFILE="1g.5gb",GPU_I_ID="5",Hostname="node-1"} 300
DCGM_FI_DEV_FB_USED{gpu="3",pod_name="legacy-pod",pod_namespace="tenant-a",container_name="c"} 512
# HELP DCGM_FI_PROF_GR_ENGINE_ACTIVE Ratio of time the graphics engine is active.
# TYPE DCGM_FI_PROF_GR_ENGINE_ACTIVE gauge
DCGM_FI_PROF_GR_ENGINE_ACTIVE{gpu="1",GPU_I_ID="1",container="nim",namespace="tenant-a",pod="stack-vsr-7d9f-x1"} 0.420000
DCGM_FI_DEV_SM_CLOCK{gpu="0",namespace="ns",pod="job-abc"} 1410
DCGM_FI_DEV_FB_USED{gpu="0",namespace="ns",pod="broken" 77
DCGM_FI_DEV_FB_USED{gpu="0",namespace="ns",pod="nan-pod"} NaN
DCGM_FI_DEV_FB_USED{gpu="0",namespace="ns",pod="notnum"} abc
garbage line without value
DCGM_FI_DEV_FB_USED{gpu="0",namespace="ns",pod="esc\"aped"} 5 1727000000000
`

func dcgmServer(t *testing.T, body string, status int) (*httptest.Server, *atomic.Int32) {
	t.Helper()
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		hits.Add(1)
		w.WriteHeader(status)
		fmt.Fprint(w, body)
	}))
	t.Cleanup(srv.Close)
	return srv, &hits
}

func TestParseDCGMMetricsSkipsUnattributedAndMalformed(t *testing.T) {
	s := parseDCGMMetrics([]byte(dcgmFixture))
	pods := map[string]int{}
	for _, x := range s {
		pods[x.Pod]++
	}
	for _, bad := range []string{"broken", "nan-pod", "notnum", ""} {
		if pods[bad] != 0 {
			t.Fatalf("pod %q should be skipped: %v", bad, pods)
		}
	}
	// job-abc: util + fb (SM_CLOCK ignored); stack pod: 2 fb + gr-engine.
	if pods["job-abc"] != 2 || pods["stack-vsr-7d9f-x1"] != 3 || pods["legacy-pod"] != 1 || pods[`esc"aped`] != 1 {
		t.Fatalf("unexpected samples: %v", pods)
	}
	for _, x := range s {
		if x.Pod == "stack-vsr-7d9f-x1" && x.Metric == dcgmMetricFBUsed && x.MIG == "" {
			t.Fatalf("MIG GPU_I_ID label lost: %+v", x)
		}
		if x.Pod == "legacy-pod" && x.Namespace != "tenant-a" {
			t.Fatalf("legacy pod_namespace not read: %+v", x)
		}
	}
}

func TestDCGMPodVRAMSumsMIGInstances(t *testing.T) {
	srv, _ := dcgmServer(t, dcgmFixture, 200)
	d := &DCGMSource{URL: srv.URL}
	got, err := d.PodVRAMMiB(context.Background(), "tenant-a", []string{"stack-vsr-7d9f-x1", "legacy-pod", "job-abc"})
	if err != nil {
		t.Fatal(err)
	}
	if got["stack-vsr-7d9f-x1"] != 10240.5 || got["legacy-pod"] != 512 {
		t.Fatalf("vram = %v", got)
	}
	if _, ok := got["job-abc"]; ok {
		t.Fatalf("job-abc is in namespace ns, not tenant-a: %v", got)
	}
}

func TestDCGMGPUUtilWholeGPUAndMIG(t *testing.T) {
	srv, _ := dcgmServer(t, dcgmFixture, 200)
	d := &DCGMSource{URL: srv.URL}
	ctx := context.Background()
	if pct, ok, err := d.PodGPUUtilPct(ctx, "ns", "job-abc"); err != nil || !ok || pct != 87 {
		t.Fatalf("job-abc util = %v %v %v", pct, ok, err)
	}
	if pct, ok, _ := d.PodGPUUtilPct(ctx, "tenant-a", "stack-vsr-7d9f-x1"); !ok || pct < 41.9 || pct > 42.1 {
		t.Fatalf("MIG util = %v %v", pct, ok)
	}
	if _, ok, _ := d.PodGPUUtilPct(ctx, "ns", "legacy-pod"); ok {
		t.Fatal("pod with only FB_USED must report ok=false")
	}
}

func TestDCGMCacheTTL(t *testing.T) {
	srv, hits := dcgmServer(t, dcgmFixture, 200)
	clk := &fakeClock{t: time.Unix(1000, 0)}
	d := &DCGMSource{URL: srv.URL, Now: clk.Now, TTL: 5 * time.Second}
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		_, _, _ = d.PodGPUUtilPct(ctx, "ns", "job-abc")
	}
	if hits.Load() != 1 {
		t.Fatalf("hits = %d, want 1 (cached)", hits.Load())
	}
	clk.Add(6 * time.Second)
	_, _, _ = d.PodGPUUtilPct(ctx, "ns", "job-abc")
	if hits.Load() != 2 {
		t.Fatalf("hits = %d, want 2 after TTL", hits.Load())
	}
}

func TestIdleGPUActiveCPUIdleResetsIdle(t *testing.T) {
	srv, _ := dcgmServer(t, dcgmFixture, 200) // job-abc at 87%
	cpu := &K8sCPUActivity{Source: &fakeMetrics{milli: 1}, Pod: fixedPod}
	gpu := &K8sGPUActivity{Source: &DCGMSource{URL: srv.URL}, Pod: fixedPod, Threshold: 5}
	if cpu.Active(context.Background()) {
		t.Fatal("precondition: CPU idle")
	}
	clk := &fakeClock{t: time.Unix(1000, 0)}
	w := startedWatchdog(clk, anyActive(cpu.Active, gpu.Active))
	for i := 0; i < 10; i++ { // 10×15s = 150s > 60s window
		clk.Add(15 * time.Second)
		if w.check(context.Background(), clk.Now()) {
			t.Fatalf("GPU-busy task idle-killed at tick %d", i)
		}
	}
}

func TestIdleGPUBelowThresholdStillKills(t *testing.T) {
	srv, _ := dcgmServer(t, `DCGM_FI_DEV_GPU_UTIL{gpu="0",namespace="ns",pod="job-abc"} 3`, 200)
	gpu := &K8sGPUActivity{Source: &DCGMSource{URL: srv.URL}, Pod: fixedPod, Threshold: 5}
	clk := &fakeClock{t: time.Unix(1000, 0)}
	w := startedWatchdog(clk, anyActive((&K8sCPUActivity{Source: &fakeMetrics{milli: 1}, Pod: fixedPod}).Active, gpu.Active))
	killed := false
	for i := 0; i < 10 && !killed; i++ {
		clk.Add(15 * time.Second)
		killed = w.check(context.Background(), clk.Now())
	}
	if !killed {
		t.Fatal("3% GPU util under a 5% threshold must not keep the task alive")
	}
}

func TestIdleDCGMDownBehavesAsCPUOnly(t *testing.T) {
	srv, _ := dcgmServer(t, "oops", http.StatusServiceUnavailable)
	gpu := &K8sGPUActivity{Source: &DCGMSource{URL: srv.URL}, Pod: fixedPod, Threshold: 5}
	if gpu.Active(context.Background()) {
		t.Fatal("DCGM down must report not-active")
	}
	// CPU busy still counts.
	busy := anyActive((&K8sCPUActivity{Source: &fakeMetrics{milli: 500}, Pod: fixedPod}).Active, gpu.Active)
	if !busy(context.Background()) {
		t.Fatal("CPU activity must still count when DCGM is down")
	}
	// CPU idle + DCGM down ⇒ killed exactly as today.
	clk := &fakeClock{t: time.Unix(1000, 0)}
	w := startedWatchdog(clk, anyActive((&K8sCPUActivity{Source: &fakeMetrics{milli: 1}, Pod: fixedPod}).Active, gpu.Active))
	killed := false
	for i := 0; i < 10 && !killed; i++ {
		clk.Add(15 * time.Second)
		killed = w.check(context.Background(), clk.Now())
	}
	if !killed {
		t.Fatal("CPU idle + DCGM down must idle-kill")
	}
	// Unreachable URL (connection refused) is also just "not active".
	dead := &K8sGPUActivity{Source: &DCGMSource{URL: "http://127.0.0.1:1/metrics"}, Pod: fixedPod}
	if dead.Active(context.Background()) {
		t.Fatal("unreachable exporter must report not-active")
	}
}

func TestDCGMEnvConfig(t *testing.T) {
	t.Setenv(envIdleGPUUtilPct, "")
	if IdleGPUUtilThreshold() != 5 {
		t.Fatal("default threshold 5")
	}
	t.Setenv(envIdleGPUUtilPct, "12.5")
	if IdleGPUUtilThreshold() != 12.5 {
		t.Fatal("env threshold")
	}
	t.Setenv(envIdleGPUUtilPct, "-3")
	if IdleGPUUtilThreshold() != 5 {
		t.Fatal("negative ⇒ default")
	}
}
