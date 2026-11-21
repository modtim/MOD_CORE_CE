package executor

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	testContainerA = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	testContainerB = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
)

func TestGPUVRAMSamplerParseNvidia(t *testing.T) {
	out := "12345, 2048\n\n 4321 , 512 \n999, [N/A]\n\n"
	got := parseNvidiaComputeApps([]byte(out))
	want := map[int]int64{12345: 2048, 4321: 512, 999: 0}
	for pid, mib := range want {
		if got[pid] != mib {
			t.Fatalf("pid %d: got %d, want %d (map %v)", pid, got[pid], mib, got)
		}
	}
	if len(got) != len(want) {
		t.Fatalf("got %d entries, want %d: %v", len(got), len(want), got)
	}
	if len(parseNvidiaComputeApps([]byte("\n"))) != 0 {
		t.Fatal("blank output should parse to empty map")
	}
}

func TestGPUVRAMSamplerParseRocm(t *testing.T) {
	out := `=============================
KFD process information:
PID    PROCESS NAME               GPU(s)   VRAM USED    SDMA USED  CU OCCUPANCY
=============================
12345  python                     0        2147483648   0          0
4321   python                     1        536870912    0          0
=============================
`
	got := parseRocmShowPids([]byte(out))
	want := map[int]int64{12345: 2048, 4321: 512}
	for pid, mib := range want {
		if got[pid] != mib {
			t.Fatalf("pid %d: got %d, want %d (map %v)", pid, got[pid], mib, got)
		}
	}
	if len(got) != len(want) {
		t.Fatalf("got %d entries, want %d: %v", len(got), len(want), got)
	}
	if parseRocmShowPids([]byte("KFD process information:\n"))[12345] != 0 {
		t.Fatal("header-only output should parse to empty map")
	}
}

func TestGPUVRAMSamplerCgroup(t *testing.T) {
	cases := []struct {
		name    string
		content string
		want    string
	}{
		{"v1", "12:memory:/docker/" + testContainerA + "\n0::/init.scope\n", testContainerA},
		{"v2", "0::/system.slice/docker-" + testContainerB + ".scope\n", testContainerB},
		{"nested", "13:cpuset:/docker/" + testContainerA + "/criu-checkpoint", testContainerA},
		{"none", "12:memory:/system.slice/foo.service\n0::/\n", ""},
	}
	for _, tc := range cases {
		if got := containerIDFromCgroup([]byte(tc.content)); got != tc.want {
			t.Fatalf("%s: got %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestGPUVRAMSamplerPeak(t *testing.T) {
	s := NewVRAMSampler()
	s.Observe(map[string]int64{testContainerA: 1000})
	s.Observe(map[string]int64{testContainerA: 2000, testContainerB: 50})
	s.Observe(map[string]int64{testContainerA: 100})
	if s.Peak(testContainerA) != 2000 {
		t.Fatalf("peak A: got %d, want 2000", s.Peak(testContainerA))
	}
	if s.Peak(testContainerB) != 50 {
		t.Fatalf("peak B: got %d, want 50", s.Peak(testContainerB))
	}
	s.Forget(testContainerA)
	if s.Peak(testContainerA) != 0 {
		t.Fatalf("peak A after Forget: got %d, want 0", s.Peak(testContainerA))
	}
	if s.Peak(testContainerB) != 50 {
		t.Fatalf("peak B should be untouched by Forget A: got %d", s.Peak(testContainerB))
	}
}

func fakeRun(t *testing.T, binaries map[string]error, outputs map[string]string) func(context.Context, string, ...string) ([]byte, error) {
	t.Helper()
	return func(_ context.Context, name string, args ...string) ([]byte, error) {
		if err, missing := binaries[name]; missing {
			t.Logf("run: %s missing as expected", name)
			return nil, err
		}
		return []byte(outputs[name]), nil
	}
}

func TestGPUVRAMSamplerSampleOnceAggregates(t *testing.T) {
	s := NewVRAMSampler()
	s.run = fakeRun(t,
		map[string]error{},
		map[string]string{"nvidia-smi": "12345, 2048\n4321, 512\n777, 300\n"})
	cgroups := map[int]string{
		12345: "12:memory:/docker/" + testContainerA,
		4321:  "12:memory:/docker/" + testContainerA,
		777:   "12:memory:/system.slice/foo.service",
	}
	s.readCgroup = func(pid int) ([]byte, error) {
		cg, ok := cgroups[pid]
		if !ok {
			return nil, errors.New("no such process")
		}
		return []byte(cg), nil
	}
	got := s.SampleOnce(context.Background())
	if got[testContainerA] != 2560 {
		t.Fatalf("container A total: got %d, want 2560 (map %v)", got[testContainerA], got)
	}
	if _, present := got["12:memory:/system.slice/foo.service"]; present {
		t.Fatal("pid without a docker cgroup must not appear")
	}
	if len(got) != 1 {
		t.Fatalf("got %d containers, want 1: %v", len(got), got)
	}
}

func TestGPUVRAMSamplerNvidiaMissingFallsBackToRocm(t *testing.T) {
	s := NewVRAMSampler()
	s.run = fakeRun(t,
		map[string]error{"nvidia-smi": errors.New("executable file not found in $PATH")},
		map[string]string{"rocm-smi": "KFD process information:\n12345 python 0 2147483648 0 0\n"})
	s.readCgroup = func(pid int) ([]byte, error) {
		if pid == 12345 {
			return []byte("0::/system.slice/docker-" + testContainerA + ".scope"), nil
		}
		return nil, errors.New("no such process")
	}
	got := s.SampleOnce(context.Background())
	if got[testContainerA] != 2048 {
		t.Fatalf("rocm fallback total: got %d, want 2048 (map %v)", got[testContainerA], got)
	}
}

func TestGPUVRAMSamplerRunGatesOnActive(t *testing.T) {
	s := NewVRAMSampler()
	// calls is written from Run's own goroutine (both the s.run closure and
	// the active() closure below execute there) and polled from the test
	// goroutine below — atomic to avoid a race between the two.
	var calls atomic.Int32
	s.run = func(_ context.Context, name string, args ...string) ([]byte, error) {
		if strings.Contains(name, "nvidia") {
			calls.Add(1)
		}
		return []byte(""), nil
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		s.Run(ctx, 20*time.Millisecond, func() bool { return calls.Load() < 2 })
		close(done)
	}()
	deadline := time.Now().Add(2 * time.Second)
	for calls.Load() < 2 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not stop after ctx cancel")
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("sampled %d times, want exactly 2 (active gate stops sampling)", got)
	}
}
