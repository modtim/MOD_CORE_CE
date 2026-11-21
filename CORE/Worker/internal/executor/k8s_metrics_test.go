package executor

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"k8s.io/client-go/kubernetes/fake"
)

type fakeMetrics struct {
	milli int64
	err   error
	calls atomic.Int32
}

func (f *fakeMetrics) PodUsage(_ context.Context, _ string, pods []string) (map[string]PodUsage, error) {
	f.calls.Add(1)
	if f.err != nil {
		return nil, f.err
	}
	return map[string]PodUsage{pods[0]: {CPUMilli: f.milli}}, nil
}

func fixedPod(context.Context) (string, string, error) { return "ns", "job-abc", nil }

func startedWatchdog(clk *fakeClock, active func(context.Context) bool) *IdleWatchdog {
	w := &IdleWatchdog{Window: time.Minute, Now: clk.Now, Tick: 15 * time.Second,
		Started: func(context.Context) bool { return true }, Active: active}
	w.check(context.Background(), clk.Now()) // start the clock
	return w
}

func TestMetricCPUAboveThresholdResetsIdle(t *testing.T) {
	clk := &fakeClock{t: time.Unix(1000, 0)}
	src := &fakeMetrics{milli: 500}
	w := startedWatchdog(clk, (&K8sCPUActivity{Source: src, Pod: fixedPod}).Active)
	for i := 0; i < 20; i++ { // 20 min of silent-but-busy compute
		clk.Add(time.Minute)
		if w.check(context.Background(), clk.Now()) {
			t.Fatalf("idle fired at minute %d despite CPU use", i+1)
		}
	}
	src.milli = 0
	clk.Add(30 * time.Second)
	if w.check(context.Background(), clk.Now()) {
		t.Fatal("fired before a full idle window")
	}
	clk.Add(45 * time.Second)
	if !w.check(context.Background(), clk.Now()) {
		t.Fatal("idle did not fire once CPU dropped")
	}
}

func TestMetricCPUBelowThresholdDoesNotReset(t *testing.T) {
	clk := &fakeClock{t: time.Unix(1000, 0)}
	src := &fakeMetrics{milli: 4} // 4m < 10m (docker's 1%)
	w := startedWatchdog(clk, (&K8sCPUActivity{Source: src, Pod: fixedPod}).Active)
	clk.Add(61 * time.Second)
	if !w.check(context.Background(), clk.Now()) {
		t.Fatal("near-zero CPU counted as activity")
	}
	if src.calls.Load() == 0 {
		t.Fatal("metrics source never consulted")
	}
}

func TestMetricNotConsultedWhileLogging(t *testing.T) {
	clk := &fakeClock{t: time.Unix(1000, 0)}
	src := &fakeMetrics{milli: 1000}
	w := startedWatchdog(clk, (&K8sCPUActivity{Source: src, Pod: fixedPod}).Active)
	for i := 0; i < 5; i++ {
		clk.Add(5 * time.Second)
		w.Touch()
		clk.Add(5 * time.Second)
		w.check(context.Background(), clk.Now())
	}
	if n := src.calls.Load(); n != 0 {
		t.Fatalf("metrics polled %d times while logs were flowing", n)
	}
}

func TestMetricUnavailableDegradesAndLogsOnce(t *testing.T) {
	k8sMetricsUnavailableWarn = sync.Once{}
	var buf bytes.Buffer
	log := slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelWarn}))
	src := &fakeMetrics{err: errors.Join(ErrMetricsUnavailable, errors.New("the server could not find the requested resource (404)"))}
	clk := &fakeClock{t: time.Unix(1000, 0)}
	a := &K8sCPUActivity{Source: src, Pod: fixedPod, Log: log}
	w := startedWatchdog(clk, a.Active)
	fired := false
	for i := 0; i < 8; i++ {
		clk.Add(15 * time.Second)
		if w.check(context.Background(), clk.Now()) {
			fired = true
		}
	}
	if !fired {
		t.Fatal("log-only fallback should still idle-kill a silent task")
	}
	if n := src.calls.Load(); n != 1 {
		t.Fatalf("metrics polled %d times after a permanent failure, want 1", n)
	}
	// A second task on the same worker must not repeat the warning.
	b := &K8sCPUActivity{Source: src, Pod: fixedPod, Log: log}
	b.Active(context.Background())
	if n := strings.Count(buf.String(), "CPU activity disabled"); n != 1 {
		t.Fatalf("unavailable warning logged %d times, want 1:\n%s", n, buf.String())
	}
}

func TestMetricTransientErrorNotActiveKeepsProbing(t *testing.T) {
	src := &fakeMetrics{err: errors.New("podmetrics \"job-abc\" not found")}
	a := &K8sCPUActivity{Source: src, Pod: fixedPod}
	for i := 0; i < 3; i++ {
		if a.Active(context.Background()) {
			t.Fatal("transient error counted as activity")
		}
	}
	src.err, src.milli = nil, 2000
	if !a.Active(context.Background()) {
		t.Fatal("probe did not recover after a transient error")
	}
}

func TestMetricParsePodMetrics(t *testing.T) {
	body := []byte(`{"kind":"PodMetrics","containers":[
		{"name":"stage-in","usage":{"cpu":"900m","memory":"1Mi"}},
		{"name":"task","usage":{"cpu":"250000000n","memory":"10Mi"}}]}`)
	u, err := parseTaskPodUsage(body)
	if err != nil || u.CPUMilli != 250 || u.MemBytes != 10<<20 {
		t.Fatalf("usage=%+v err=%v, want 250m/10Mi", u, err)
	}
	if _, err := parseTaskPodUsage([]byte(`{"containers":[{"name":"x","usage":{"cpu":"1"}}]}`)); err == nil {
		t.Fatal("missing task container should error")
	}
}

func TestMetricRESTSourceNoDiscoveryIsUnavailable(t *testing.T) {
	src := NewRESTPodMetricsSource(fake.NewSimpleClientset())
	_, err := src.PodUsage(context.Background(), "ns", []string{"p"})
	if !errors.Is(err, ErrMetricsUnavailable) {
		t.Fatalf("err=%v, want ErrMetricsUnavailable", err)
	}
}
