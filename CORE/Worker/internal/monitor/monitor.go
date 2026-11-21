package monitor

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
)

// ResourceSample is a single time-series data point sent to the API.
type ResourceSample struct {
	Timestamp     time.Time `json:"timestamp"`
	Phase         string    `json:"phase"`
	CPUPercent    float64   `json:"cpu_percent"`
	RAMMB         float64   `json:"ram_mb"`
	VRAMMB        float64   `json:"vram_mb"`
	GPUUtilPct    float64   `json:"gpu_util_pct"`
	GPUTempC      float64   `json:"gpu_temp_c"`
	NetRxMbps     float64   `json:"net_rx_mbps"`
	NetTxMbps     float64   `json:"net_tx_mbps"`
	DiskReadIOPS  float64   `json:"disk_read_iops"`
	DiskWriteIOPS float64   `json:"disk_write_iops"`

	// Extended v2
	SingleThreadDetected *bool    `json:"single_thread_detected,omitempty"`
	ContainerDiskReadMB  *float64 `json:"container_disk_read_mb,omitempty"`
	ContainerDiskWriteMB *float64 `json:"container_disk_write_mb,omitempty"`
	VRAMAllocRateMBs     *float64 `json:"vram_alloc_rate_mb_s,omitempty"`
	ProcessCount         *int     `json:"process_count,omitempty"`
	GPUClockMHz          *float64 `json:"gpu_clock_mhz,omitempty"`
	SwapUsedMB           *float64 `json:"swap_used_mb,omitempty"`
}

// TaskSnapshot is per-task resource usage included in heartbeats.
type TaskSnapshot struct {
	RAMMB      float64 `json:"ram_mb"`
	VRAMMB     float64 `json:"vram_mb"`
	CPUPercent float64 `json:"cpu_pct"`
	Phase      string  `json:"phase"`
	ElapsedS   float64 `json:"elapsed_s"`
}

// Monitor collects resource samples for a running task.
// It runs a background goroutine that samples Docker container stats and
// system/GPU metrics at adaptive intervals.
type Monitor struct {
	taskID      int
	containerID string
	gpuType     string
	startedAt   time.Time

	docker  *client.Client
	sysColl *SystemCollector
	log     *slog.Logger

	mu              sync.Mutex
	phase           string
	samples         []ResourceSample
	lastSnap        TaskSnapshot
	stopped         bool
	loggedFirstStat bool // Log first non-zero sample for debugging

	cancel context.CancelFunc
	done   chan struct{}

	// Previous CPU counters, for delta math across successive samples.
	//
	// ContainerStatsOneShot returns a SINGLE reading with PreCPUStats left
	// zeroed — Docker only populates it on the streaming endpoint. The old
	// code differenced against that zero value, so cpuDelta became the
	// container's whole-lifetime CPU and systemDelta the host's CPU since
	// BOOT. The ratio is meaninglessly small: measured across 231,629 samples
	// on a live deployment, mean cpu_percent was 0.016% and the maximum ever
	// recorded was 2.09% — including samples taken while a stressor was
	// provably saturating 22 cores.
	//
	// Keeping the last reading and differencing successive samples ourselves
	// is what `docker stats` does, and it is the only way a one-shot poller
	// can produce a real percentage.
	prevCPUTotal  uint64
	prevCPUSystem uint64
	prevCPUValid  bool
	prevPercpu    []uint64
}

// New creates a monitor for a task's Docker container.
func New(taskID int, containerID, gpuType string, docker *client.Client, log *slog.Logger) *Monitor {
	return &Monitor{
		taskID:      taskID,
		containerID: containerID,
		gpuType:     gpuType,
		docker:      docker,
		sysColl:     NewSystemCollector(),
		log:         log,
		phase:       "processing",
		done:        make(chan struct{}),
	}
}

// Burst sampling window after a container appears. 500ms x 8s captures a
// 1-2s processing container that the regular ticker misses entirely.
const (
	burstInterval = 500 * time.Millisecond
	burstWindow   = 8 * time.Second
)

// Start begins background sampling. Call Stop() to end.
func (m *Monitor) Start(estDurationSeconds int) {
	m.startedAt = time.Now()

	// Adaptive sample interval — fast enough to capture short tasks (3-10s)
	// while not flooding storage on multi-hour runs. A 5-second task gets
	// 1-2 samples; a 30-second task gets ~10; a 2-hour task gets ~240.
	interval := 3 * time.Second
	switch {
	case estDurationSeconds > 3600:
		interval = 30 * time.Second
	case estDurationSeconds > 300:
		interval = 15 * time.Second
	case estDurationSeconds > 60:
		interval = 5 * time.Second
	}

	m.log.Info("resource monitor started",
		"task_id", m.taskID,
		"interval", interval,
		"gpu_type", m.gpuType,
		"has_docker", m.docker != nil,
	)

	ctx, cancel := context.WithCancel(context.Background())
	m.cancel = cancel

	go m.run(ctx, interval)
}

// Stop ends the monitoring goroutine and waits for it to finish.
func (m *Monitor) Stop() {
	m.mu.Lock()
	if m.stopped {
		m.mu.Unlock()
		return
	}
	m.stopped = true
	m.mu.Unlock()

	if m.cancel != nil {
		m.cancel()
	}
	<-m.done
}

// SetPhase updates the current lifecycle phase label for samples.
func (m *Monitor) SetPhase(phase string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.phase = phase
}

// SetContainerID updates the Docker container ID once the container is running.
// Must be called after the executor creates the container so that per-container
// CPU/RAM stats can be collected. Safe to call while monitoring is active.
func (m *Monitor) SetContainerID(id string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.containerID = id
	m.log.Info("container ID set for resource monitoring", "container_id", id[:12])

	// Prime the CPU baseline, then burst-sample while the container is young.
	//
	// Two problems this solves, both measured live:
	//
	//   CPU percent is a delta between two readings, so the FIRST sample
	//   after a container appears can only ever report 0. Priming
	//   immediately means the first RECORDED sample already has a baseline.
	//
	//   The regular ticker is far coarser than a short container's life. A
	//   12s calibration task is mostly image pull, volume prep and sidecars;
	//   the processing container itself lives 1-2s. At a 5s interval every
	//   sample landed before it started or after it exited, returning empty
	//   stats — ram_mb 0 AND cpu 0 for the whole task, while a long-running
	//   stressor on the same host sampled fine (21 samples, 42MB, 206% CPU).
	//   Fast nodes are the common case in calibration, so they cannot be the
	//   case that records nothing.
	go m.burstSample()
}

// burstSample takes closely-spaced samples right after a container starts, so
// containers that live only a second or two are measured at all. It stops
// early once the monitor is stopped.
func (m *Monitor) burstSample() {
	ctx, cancel := context.WithTimeout(context.Background(), burstWindow+2*time.Second)
	defer cancel()

	// Prime the CPU baseline without recording it — a first reading has
	// nothing to difference against and would only store a false zero.
	m.sampleContainer(ctx)

	deadline := time.Now().Add(burstWindow)
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return
		case <-time.After(burstInterval):
		}
		m.mu.Lock()
		stopped := m.stopped
		m.mu.Unlock()
		if stopped {
			return
		}
		m.takeSample(ctx)
	}
}

// DrainSamples returns and clears accumulated samples (for batch sending).
func (m *Monitor) DrainSamples() []ResourceSample {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]ResourceSample, len(m.samples))
	copy(out, m.samples)
	m.samples = m.samples[:0]
	return out
}

// AllSamples returns all accumulated samples without clearing.
func (m *Monitor) AllSamples() []ResourceSample {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]ResourceSample, len(m.samples))
	copy(out, m.samples)
	return out
}

// LastTaskSnapshot returns the most recent per-task resource snapshot (for heartbeats).
func (m *Monitor) LastTaskSnapshot() TaskSnapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.lastSnap
}

func (m *Monitor) run(ctx context.Context, interval time.Duration) {
	defer close(m.done)

	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	// Take one sample immediately
	m.takeSample(ctx)

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			m.takeSample(ctx)
		}
	}
}

func (m *Monitor) takeSample(ctx context.Context) {
	m.mu.Lock()
	phase := m.phase
	m.mu.Unlock()

	// System-level metrics
	sys := m.sysColl.Sample()

	// GPU metrics. gpu.VRAMUsedMB is the WHOLE CARD — the right figure for
	// "how loaded was the box", the wrong one for "how much does this node
	// need". Prefer per-container attribution for the task's own vram_mb and
	// fall back to host-wide only when it cannot be determined, so a node's
	// recorded demand is its own rather than every tenant's.
	gpu := SampleGPU(m.gpuType)

	m.mu.Lock()
	cidForVRAM := m.containerID
	m.mu.Unlock()
	taskVRAM := gpu.VRAMUsedMB
	if mb, ok := ContainerVRAMUsedMB(m.gpuType, cidForVRAM); ok {
		taskVRAM = mb
	}

	// Container-specific metrics
	cs := m.sampleContainer(ctx)

	// VRAM allocation rate (delta from previous sample)
	var vramRate *float64
	m.mu.Lock()
	if len(m.samples) > 0 {
		prev := m.samples[len(m.samples)-1]
		dt := time.Since(prev.Timestamp).Seconds()
		if dt > 0 && gpu.VRAMUsedMB > 0 {
			rate := (gpu.VRAMUsedMB - prev.VRAMMB) / dt
			vramRate = &rate
		}
	}
	m.mu.Unlock()

	// Swap usage
	var swapMB *float64
	if sys.SwapUsedMB > 0 {
		swapMB = &sys.SwapUsedMB
	}

	// GPU clock speed
	var gpuClock *float64
	if gpu.ClockMHz > 0 {
		gpuClock = &gpu.ClockMHz
	}

	sample := ResourceSample{
		Timestamp:     time.Now(),
		Phase:         phase,
		CPUPercent:    cs.cpuPct,
		RAMMB:         cs.ramMB,
		VRAMMB:        taskVRAM,
		GPUUtilPct:    gpu.UtilizationPct,
		GPUTempC:      gpu.TemperatureC,
		NetRxMbps:     sys.NICRxMbps,
		NetTxMbps:     sys.NICTxMbps,
		DiskReadIOPS:  sys.DiskReadIOPS,
		DiskWriteIOPS: sys.DiskWriteIOPS,

		SingleThreadDetected: cs.singleThreaded,
		ContainerDiskReadMB:  cs.diskReadMB,
		ContainerDiskWriteMB: cs.diskWriteMB,
		VRAMAllocRateMBs:     vramRate,
		ProcessCount:         cs.processCount,
		GPUClockMHz:          gpuClock,
		SwapUsedMB:           swapMB,
	}

	snap := TaskSnapshot{
		RAMMB:      cs.ramMB,
		VRAMMB:     taskVRAM,
		CPUPercent: cs.cpuPct,
		Phase:      phase,
		ElapsedS:   time.Since(m.startedAt).Seconds(),
	}

	m.mu.Lock()
	m.samples = append(m.samples, sample)
	m.lastSnap = snap
	if !m.loggedFirstStat && (cs.cpuPct > 0 || cs.ramMB > 0) {
		m.loggedFirstStat = true
		m.log.Info("first non-zero resource sample",
			"cpu_percent", cs.cpuPct,
			"ram_mb", cs.ramMB,
			"vram_mb", taskVRAM,
		)
	}
	m.mu.Unlock()
}

type containerStats struct {
	cpuPct         float64
	ramMB          float64
	singleThreaded *bool
	diskReadMB     *float64
	diskWriteMB    *float64
	processCount   *int
}

// sampleContainer gets CPU%, RAM, disk I/O, process count, and single-thread
// detection for a specific Docker container.
func (m *Monitor) sampleContainer(ctx context.Context) containerStats {
	var result containerStats
	m.mu.Lock()
	cid := m.containerID
	m.mu.Unlock()

	if cid == "" || m.docker == nil {
		return result
	}

	statsCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	resp, err := m.docker.ContainerStatsOneShot(statsCtx, cid)
	if err != nil {
		short := cid
		if len(short) > 12 {
			short = short[:12]
		}
		m.log.Debug("container stats failed", "container", short, "error", err)
		return result
	}
	defer resp.Body.Close()

	var stats container.StatsResponse
	if err := json.NewDecoder(resp.Body).Decode(&stats); err != nil {
		return result
	}

	// RAM
	if stats.MemoryStats.Usage > 0 {
		result.ramMB = float64(stats.MemoryStats.Usage) / (1024.0 * 1024.0)
	}

	// CPU: delta between THIS one-shot reading and the previous one.
	// stats.PreCPUStats is always zero here (see prevCPU* on Monitor), so it
	// must not be used.
	curTotal := stats.CPUStats.CPUUsage.TotalUsage
	curSystem := stats.CPUStats.SystemUsage

	// Guarded: SetContainerID primes the baseline from its own goroutine, so
	// this can run concurrently with the sample ticker.
	m.mu.Lock()
	var cpuDelta, systemDelta float64
	if m.prevCPUValid && curTotal >= m.prevCPUTotal && curSystem >= m.prevCPUSystem {
		cpuDelta = float64(curTotal - m.prevCPUTotal)
		systemDelta = float64(curSystem - m.prevCPUSystem)
	}
	prevPercpu := m.prevPercpu
	m.prevCPUTotal = curTotal
	m.prevCPUSystem = curSystem
	m.prevPercpu = append([]uint64(nil), stats.CPUStats.CPUUsage.PercpuUsage...)
	m.prevCPUValid = true
	m.mu.Unlock()

	cpuCount := float64(stats.CPUStats.OnlineCPUs)
	if cpuCount == 0 {
		cpuCount = float64(len(stats.CPUStats.CPUUsage.PercpuUsage))
	}
	if cpuCount == 0 {
		cpuCount = 1
	}
	if systemDelta > 0 && cpuDelta > 0 {
		result.cpuPct = (cpuDelta / systemDelta) * cpuCount * 100.0
	}

	// Single-thread detection: one core doing most work while others idle
	if len(stats.CPUStats.CPUUsage.PercpuUsage) > 1 && len(prevPercpu) == len(stats.CPUStats.CPUUsage.PercpuUsage) {
		var maxCoreDelta, totalCoreDelta float64
		for i := range stats.CPUStats.CPUUsage.PercpuUsage {
			delta := float64(stats.CPUStats.CPUUsage.PercpuUsage[i] - prevPercpu[i])
			if delta > maxCoreDelta {
				maxCoreDelta = delta
			}
			totalCoreDelta += delta
		}
		if totalCoreDelta > 0 {
			ratio := maxCoreDelta / totalCoreDelta
			st := ratio > 0.8 && result.cpuPct > 50
			result.singleThreaded = &st
		}
	}

	// Container disk I/O from BlkioStats
	var readBytes, writeBytes uint64
	for _, entry := range stats.BlkioStats.IoServiceBytesRecursive {
		switch entry.Op {
		case "read", "Read":
			readBytes += entry.Value
		case "write", "Write":
			writeBytes += entry.Value
		}
	}
	if readBytes > 0 {
		v := float64(readBytes) / (1024.0 * 1024.0)
		result.diskReadMB = &v
	}
	if writeBytes > 0 {
		v := float64(writeBytes) / (1024.0 * 1024.0)
		result.diskWriteMB = &v
	}

	// Process count
	if stats.PidsStats.Current > 0 {
		pc := int(stats.PidsStats.Current)
		result.processCount = &pc
	}

	return result
}

// SystemSnapshotForHeartbeat returns a full system + GPU snapshot for the heartbeat.
func (m *Monitor) SystemSnapshotForHeartbeat() (SystemSnapshot, GPUSnapshot) {
	sys := m.sysColl.Sample()
	gpu := SampleGPU(m.gpuType)
	return sys, gpu
}

// FormatResourceSnapshot builds the resource_snapshot JSON for heartbeats.
func FormatResourceSnapshot(sys SystemSnapshot, gpu GPUSnapshot, taskSnaps map[int]TaskSnapshot, activeContainers, startingContainers int) map[string]interface{} {
	snap := map[string]interface{}{
		"cpu_percent":         sys.CPUPercent,
		"ram_used_mb":         sys.RAMUsedMB,
		"ram_total_mb":        sys.RAMTotalMB,
		"ram_available_mb":    sys.RAMAvailableMB,
		"nic_rx_mbps":         sys.NICRxMbps,
		"nic_tx_mbps":         sys.NICTxMbps,
		"nic_capacity_mbps":   sys.NICCapacityMbps,
		"disk_read_iops":      sys.DiskReadIOPS,
		"disk_write_iops":     sys.DiskWriteIOPS,
		"disk_free_gb":        sys.DiskFreeGB,
		"disk_total_gb":       sys.DiskTotalGB,
		"cpu_temperature_c":   sys.CPUTempC,
		"thermal_throttled":   sys.ThermalThrotl,
		"active_containers":   activeContainers,
		"containers_starting": startingContainers,
	}

	// Add disk usage fields if available
	if du, ok := LatestDiskUsage(); ok {
		snap["images_bytes"] = du.ImagesBytes
		snap["model_cache_bytes"] = du.ModelCacheBytes
		snap["workdirs_bytes"] = du.WorkdirsBytes
		snap["reclaimable_bytes"] = du.ReclaimableBytes
		// Cumulative since boot, independent of the DiskUsage snapshot above
		// (which is fully recomputed each tick) — see AddReclaimedBytes.
		snap["reclaimed_bytes"] = TotalReclaimedBytes()
	}

	if gpu.Type != "none" {
		snap["vram_used_mb"] = gpu.VRAMUsedMB
		snap["vram_total_mb"] = gpu.VRAMTotalMB
		snap["vram_fragmentation_pct"] = gpu.FragmentationPct
		snap["gpu_utilization_pct"] = gpu.UtilizationPct
		snap["gpu_temperature_c"] = gpu.TemperatureC
		snap["gpu_power_state"] = gpu.PowerState
		// Per-card values (ROCm sysfs path), keyed on the same card identity
		// (index / pci_bus_id) gpu_assignment.service_cards uses, so the API
		// and UI can attribute a pinned card's load. Absent for single-card
		// tool paths and for older payloads.
		if len(gpu.Cards) > 0 {
		 snap["gpu_cards"] = gpu.Cards
		}
		// MAC.1.s14 — Metal extras. gpu_power_mw is the GPU's power draw in
		// milliwatts (powermetrics' own unit; the CUDA/ROCm csv paths have no
		// comparable field and leave it 0). gpu_tasks is the PID-attributed
		// GPU time gpu_activity reports, the darwin analogue of the nvidia
		// --query-compute-apps rows gpu.go intersects with a container's
		// cgroup. metal_probe_error names why gpu_power could not answer
		// (powermetrics is root-only) — absent when the probe worked, so the
		// API can tell "not measured" from "measured idle".
		if gpu.PowerMW > 0 {
		 snap["gpu_power_mw"] = gpu.PowerMW
		}
	 if gpu.MetalProbeError != "" {
		 snap["metal_probe_error"] = gpu.MetalProbeError
	 }
	 if len(gpu.GPUTasks) > 0 {
		 snap["gpu_tasks"] = gpu.GPUTasks
	 }
	}

	// Per-task breakdown
	if len(taskSnaps) > 0 {
		tsMap := make(map[string]interface{})
		for tid, ts := range taskSnaps {
			tsMap[fmt.Sprintf("%d", tid)] = ts
		}
		snap["per_task_resources"] = tsMap
	}

	return snap
}
