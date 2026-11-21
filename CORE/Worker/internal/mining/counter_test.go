package mining

import (
	"math"
	"sync/atomic"
	"testing"
	"time"
)

// fakeClock is a manually-advanced clock for test determinism.
type fakeClock struct {
	t atomic.Int64 // nanoseconds
}

func (c *fakeClock) Now() time.Time {
	return time.Unix(0, c.t.Load())
}
func (c *fakeClock) Set(t time.Time)   { c.t.Store(t.UnixNano()) }
func (c *fakeClock) Add(d time.Duration) { c.t.Add(d.Nanoseconds()) }

func newTestCounter() (*Counter, *fakeClock) {
	clk := &fakeClock{}
	clk.Set(time.Date(2026, 10, 9, 0, 0, 0, 0, time.UTC))
	cfg := DefaultConfig()
	cfg.SampleInterval = 30 * time.Second
	c := New(cfg)
	c.nowFn = clk.Now
	return c, clk
}

func TestCounter_Empty(t *testing.T) {
	c, _ := newTestCounter()
	s := c.Snapshot()
	if s.TotalSamples != 0 {
		t.Fatalf("expected 0 samples, got %d", s.TotalSamples)
	}
	if s.SustainedGPUSeconds != 0 {
		t.Fatalf("expected 0 sustained seconds, got %f", s.SustainedGPUSeconds)
	}
	if s.GPUUtilMaxPct != 0 {
		t.Fatalf("expected 0 max, got %f", s.GPUUtilMaxPct)
	}
	if s.GPUUtilMeanPct != 0 {
		t.Fatalf("expected 0 mean, got %f", s.GPUUtilMeanPct)
	}
}

func TestCounter_MaxMean(t *testing.T) {
	c, _ := newTestCounter()

	base := c.nowFn()
	samples := []Sample{
		{Timestamp: base, GPUUtil: 20.0, Phase: "idle"},
		{Timestamp: base.Add(30 * time.Second), GPUUtil: 85.0, Phase: "processing"},
		{Timestamp: base.Add(time.Minute), GPUUtil: 99.0, Phase: "processing"},
		{Timestamp: base.Add(90 * time.Second), GPUUtil: 50.0, Phase: "idle"},
	}
	for _, s := range samples {
		c.Sample(s)
	}

	r := c.Snapshot()
	if r.TotalSamples != 4 {
		t.Fatalf("expected 4 samples, got %d", r.TotalSamples)
	}
	if r.GPUUtilMaxPct != 99.0 {
		t.Fatalf("expected max 99, got %f", r.GPUUtilMaxPct)
	}
	expectedMean := (20.0 + 85.0 + 99.0 + 50.0) / 4.0
	if math.Abs(r.GPUUtilMeanPct-expectedMean) > 0.01 {
		t.Fatalf("expected mean %.2f, got %f", expectedMean, r.GPUUtilMeanPct)
	}
}

func TestCounter_SustainedHigh(t *testing.T) {
	c, clk := newTestCounter()

	base := c.nowFn()
	// 5 consecutive high samples at 30s intervals = 5 * 30s = 150s sustained
	for i := 0; i < 5; i++ {
		c.Sample(Sample{
			Timestamp: base.Add(time.Duration(i) * 30 * time.Second),
			GPUUtil:   97.0,
			Phase:     "idle",
		})
	}
	// One low sample (breaks the sustained chain)
	c.Sample(Sample{
		Timestamp: base.Add(5 * 30 * time.Second),
		GPUUtil:   10.0,
		Phase:     "idle",
	})
	// Another high sample after the gap
	c.Sample(Sample{
		Timestamp: base.Add(6 * 30 * time.Second),
		GPUUtil:   98.0,
		Phase:     "idle",
	})

	r := c.Snapshot()
	// 6 high samples total: 5 at the start + 1 at t=6. The last high sample
	// at t=6 gets its default 30s gap to next, so total sustained = 6 * 30 = 180s.
	if r.SustainedGPUSeconds != 180.0 {
		t.Fatalf("expected 180.0 sustained seconds, got %f", r.SustainedGPUSeconds)
	}
	if r.IdleHighUtilSamples != 6 {
		t.Fatalf("expected 6 idle high samples, got %d", r.IdleHighUtilSamples)
	}

	// Advance clock past the window so samples get pruned.
	clk.Add(2 * time.Hour)
	r2 := c.Snapshot()
	if r2.TotalSamples != 0 {
		t.Fatalf("expected pruned to 0, got %d", r2.TotalSamples)
	}
}

func TestCounter_ThresholdBoundary(t *testing.T) {
	c, _ := newTestCounter()

	base := c.nowFn()
	// Exactly at threshold — should count as high util
	c.Sample(Sample{Timestamp: base, GPUUtil: 95.0, Phase: "idle"})
	// One below — should NOT count
	c.Sample(Sample{Timestamp: base.Add(30 * time.Second), GPUUtil: 94.99, Phase: "idle"})
	// One above — should count
	c.Sample(Sample{Timestamp: base.Add(60 * time.Second), GPUUtil: 96.0, Phase: "idle"})

	r := c.Snapshot()
	// 2 high samples: first gets 30s gap, second gets default 30s = 60s
	if r.SustainedGPUSeconds != 60.0 {
		t.Fatalf("expected 60.0 sustained seconds, got %f", r.SustainedGPUSeconds)
	}
}

func TestCounter_IdleHighUtil(t *testing.T) {
	c, _ := newTestCounter()

	base := c.nowFn()
	// High util during "processing" phase — should NOT be idle_high_util
	c.Sample(Sample{Timestamp: base, GPUUtil: 97.0, Phase: "processing"})
	// High util during "idle" phase — SHOULD be idle_high_util
	c.Sample(Sample{Timestamp: base.Add(30 * time.Second), GPUUtil: 96.0, Phase: "idle"})
	// Normal util during "idle" — should not count
	c.Sample(Sample{Timestamp: base.Add(60 * time.Second), GPUUtil: 20.0, Phase: "idle"})

	r := c.Snapshot()
	if r.IdleHighUtilSamples != 1 {
		t.Fatalf("expected 1 idle high util sample, got %d", r.IdleHighUtilSamples)
	}
}

func TestConcurrentSamples(t *testing.T) {
	c, _ := newTestCounter()
	done := make(chan struct{})

	// 10 goroutines each adding 100 samples concurrently
	for g := 0; g < 10; g++ {
		go func() {
			for i := 0; i < 100; i++ {
				c.Sample(Sample{
					Timestamp: c.nowFn(),
					GPUUtil:   float64(50 + i%50),
					Phase:     "processing",
				})
			}
			done <- struct{}{}
		}()
	}

	for g := 0; g < 10; g++ {
		<-done
	}

	r := c.Snapshot()
	if r.TotalSamples != 1000 {
		t.Fatalf("expected 1000 samples, got %d", r.TotalSamples)
	}
}

func TestCounter_WindowRetention(t *testing.T) {
	c, clk := newTestCounter()
	base := c.nowFn()
	for i := 0; i < 6; i++ {
		c.Sample(Sample{
			Timestamp: base.Add(time.Duration(i) * 30 * time.Second),
			GPUUtil:   97.0,
			Phase:     "idle",
		})
	}

	clk.Add(30 * time.Minute)
	r := c.Snapshot()
	if r.TotalSamples != 6 {
		t.Fatalf("in-window: expected 6 samples retained, got %d", r.TotalSamples)
	}
	if r.SustainedGPUSeconds != 180.0 {
		t.Fatalf("in-window: expected 180s sustained, got %v", r.SustainedGPUSeconds)
	}
	if r.IdleHighUtilSamples != 6 {
		t.Fatalf("in-window: expected 6 idle high samples, got %d", r.IdleHighUtilSamples)
	}

	clk.Add(90 * time.Minute)
	if got := c.Snapshot().TotalSamples; got != 0 {
		t.Fatalf("past-window: expected 0 samples after 2h, got %d", got)
	}
}
