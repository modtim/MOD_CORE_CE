// Package mining provides GPU-utilisation mining-signature counters for the
// MOD Core worker.  It tracks a rolling window of per-task GPU-util samples so
// the heartbeat can report pre-computed fraud-signal fields to the API before
// the API's own sweep (which reads TaskResourceSample rows from the DB) would.
//
// The counter is concurrency-safe and uses only the stdlib (sync.Mutex).
package mining

import (
	"math"
	"sync"
	"time"
)

// Sample describes one GPU utilisation reading, matching the shape the API's
// mining_signature_detection module expects from telemetry (timestamp +
// gpu_util_pct + phase label).
type Sample struct {
	Timestamp time.Time `json:"timestamp"`
	GPUUtil   float64   `json:"gpu_util_pct"`
	Phase     string    `json:"phase"`
}

// Snapshot is the heartbeat field shape — matches the API's evaluate verdict
// keys so the API can read pre-computed values without re-scanning samples.
type Snapshot struct {
	SustainedGPUSeconds  float64 `json:"sustained_gpu_seconds"`
	GPUUtilMaxPct        float64 `json:"gpu_utilization_max_pct"`
	GPUUtilMeanPct       float64 `json:"gpu_utilization_mean_pct"`
	TotalSamples         int     `json:"total_samples"`
	IdleHighUtilSamples  int     `json:"idle_high_util_samples"`
	SustainedMinutes     float64 `json:"sustained_gpu_minutes"`
	SustainedHighPct     float64 `json:"sustained_high_pct"`
}

// Config controls the counter's thresholds and window.
type Config struct {
	// HighUtilThreshold is the utilisation percentage considered "high".
	// Mining detection uses 95% (GPU_UTIL_THRESHOLD_PCT in the API).
	HighUtilThreshold float64

	// Window is the sliding window over which samples are kept.  The API's
	// _sustained_seconds evaluates a 24h look-back; a shorter window here
	// still catches sustained high util (>= 95% held for the config
	// minutes).  We keep 1h which covers a ~2-min sustained burst with
	// many samples.
	Window time.Duration

	// SampleInterval is the expected time between consecutive samples from
	// the monitor (30s is the default in mining_signature_detection).
	SampleInterval time.Duration
}

// DefaultConfig returns a Config tuned to the API's mining detection defaults
// (95% threshold, 30s sample interval, 1h window).
func DefaultConfig() Config {
	return Config{
		HighUtilThreshold: 95.0,
		Window:            time.Hour,
		SampleInterval:    30 * time.Second,
	}
}

// Counter tracks a rolling window of GPU-util samples.  Thread-safe.
type Counter struct {
	cfg     Config
	nowFn   func() time.Time // nil => time.Now; tests substitute a fake clock
	mu      sync.Mutex
	samples []Sample
}

// New creates a counter with the given config.
func New(cfg Config) *Counter {
	if cfg.HighUtilThreshold == 0 {
		cfg = DefaultConfig()
	}
	if cfg.Window == 0 {
		cfg.Window = time.Hour
	}
	if cfg.SampleInterval == 0 {
		cfg.SampleInterval = 30 * time.Second
	}
	return &Counter{cfg: cfg}
}

// Sample adds one GPU utilisation reading.  The counter prunes the window
// on every call so it never grows unbounded.
func (c *Counter) Sample(s Sample) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.samples = append(c.samples, s)
}

// Snapshot returns a point-in-time summary of the current window.  The caller
// does not need to hold the mutex — the counter copies internally.
func (c *Counter) Snapshot() Snapshot {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.prune()

	s := c.samples
	if len(s) == 0 {
		return Snapshot{
			SustainedGPUSeconds:  0,
			GPUUtilMaxPct:        0,
			GPUUtilMeanPct:       0,
			TotalSamples:         0,
			IdleHighUtilSamples:  0,
		}
	}

	var totalUtil float64
	var maxUtil float64
	idleHighCount := 0
	sustainedSecs := float64(0)

	// Sort by timestamp (samples are usually in order, but be safe).
	ordered := make([]Sample, len(s))
	copy(ordered, s)
	c.sortSamples(ordered)

	for i, sm := range ordered {
		totalUtil += sm.GPUUtil
		if sm.GPUUtil > maxUtil {
			maxUtil = sm.GPUUtil
		}
		if sm.GPUUtil >= c.cfg.HighUtilThreshold {
			if sm.Phase == "idle" {
				idleHighCount++
			}
			// Count sustained seconds using gap-to-next (same logic as
			// the API's _sustained_seconds).
			var dt float64
			if i+1 < len(ordered) {
				dt = ordered[i+1].Timestamp.Sub(sm.Timestamp).Seconds()
			} else {
				dt = c.cfg.SampleInterval.Seconds()
			}
			if dt > 0 {
				sustainedSecs += dt
			}
		}
	}

	n := float64(len(ordered))
	return Snapshot{
		SustainedGPUSeconds:  math.Round(sustainedSecs*1000) / 1000,
		GPUUtilMaxPct:        math.Round(maxUtil*100) / 100,
		GPUUtilMeanPct:       math.Round((totalUtil/n)*100) / 100,
		TotalSamples:         len(ordered),
		IdleHighUtilSamples:  idleHighCount,
		SustainedMinutes:     math.Round(sustainedSecs/60*100) / 100,
		SustainedHighPct:     math.Round((sustainedSecs / (n * c.cfg.SampleInterval.Seconds())) * 10000) / 100,
	}
}

// prune removes samples older than the configured window.  Must hold c.mu.
func (c *Counter) prune() {
	now := time.Now()
	if c.nowFn != nil {
		now = c.nowFn()
	}
	cutoff := now.Add(-c.cfg.Window)
	i := 0
	for i < len(c.samples) && c.samples[i].Timestamp.Before(cutoff) {
		i++
	}
	if i > 0 {
		c.samples = append([]Sample(nil), c.samples[i:]...)
	}
}

// sortSamples sorts samples in-place by timestamp (insertion sort — fast
// because samples arrive nearly-ordered).
func (c *Counter) sortSamples(s []Sample) {
	for i := 1; i < len(s); i++ {
		key := s[i]
		j := i - 1
		for j >= 0 && s[j].Timestamp.After(key.Timestamp) {
			s[j+1] = s[j]
			j--
		}
		s[j+1] = key
	}
}

// Package-level default counter.  main() creates the counter, but the sample
// feed sites live elsewhere (the task loop's monitor drain in cmd/worker, the
// heartbeat's idle-host GPU read in internal/heartbeat), so wiring happens
// once via SetDefault and Feed reads it.  Unwired (nil) Feed is a no-op: a
// binary that never wires the counter reports nothing new.
var defMu sync.Mutex
var defCounter *Counter

func SetDefault(c *Counter) {
	defMu.Lock()
	defer defMu.Unlock()
	defCounter = c
}

// Default returns the wired counter or nil.
func Default() *Counter {
	defMu.Lock()
	defer defMu.Unlock()
	return defCounter
}

// Feed adds one sample to the default counter (no-op when unwired).
func Feed(s Sample) {
	if c := Default(); c != nil {
		c.Sample(s)
	}
}
