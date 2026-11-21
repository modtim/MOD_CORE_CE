package monitor

import (
	"bufio"
	"context"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// SystemSnapshot captures system-level resource usage at a point in time.
type SystemSnapshot struct {
	Timestamp time.Time `json:"timestamp"`

	// CPU
	CPUPercent    float64 `json:"cpu_percent"`
	CPUTempC      float64 `json:"cpu_temperature_c"`
	ThermalThrotl bool    `json:"thermal_throttled"`

	// Memory
	RAMUsedMB      float64 `json:"ram_used_mb"`
	RAMTotalMB     float64 `json:"ram_total_mb"`
	RAMAvailableMB float64 `json:"ram_available_mb"` // MemAvailable from /proc/meminfo

	// Network
	NICRxMbps       float64 `json:"nic_rx_mbps"`
	NICTxMbps       float64 `json:"nic_tx_mbps"`
	NICCapacityMbps float64 `json:"nic_capacity_mbps"`

	// Disk
	DiskReadIOPS  float64 `json:"disk_read_iops"`
	DiskWriteIOPS float64 `json:"disk_write_iops"`
	DiskFreeGB    float64 `json:"disk_free_gb"`
	DiskTotalGB   float64 `json:"disk_total_gb"`

	// Swap
	SwapUsedMB float64 `json:"swap_used_mb"`
}

// cpuStat holds raw CPU jiffies from /proc/stat for delta calculation.
type cpuStat struct {
	user, nice, system, idle, iowait, irq, softirq, steal uint64
	ts                                                    time.Time
}

func (c cpuStat) total() uint64 {
	return c.user + c.nice + c.system + c.idle + c.iowait + c.irq + c.softirq + c.steal
}

func (c cpuStat) busy() uint64 {
	return c.total() - c.idle - c.iowait
}

// netStat holds raw byte counters from /proc/net/dev.
type netStat struct {
	rxBytes, txBytes uint64
	ts               time.Time
}

// diskStat holds raw I/O counters from /proc/diskstats.
type diskStat struct {
	reads, writes uint64
	ts            time.Time
}

// SystemCollector samples system-level resources by reading procfs.
// It keeps the previous sample to compute deltas (CPU%, network throughput, disk IOPS).
type SystemCollector struct {
	prevCPU  *cpuStat
	prevNet  *netStat
	prevDisk *diskStat
}

// NewSystemCollector creates a collector and takes an initial baseline reading.
func NewSystemCollector() *SystemCollector {
	sc := &SystemCollector{}
	// Take baseline readings so the first real sample can compute deltas
	sc.prevCPU = readCPUStat()
	sc.prevNet = readNetStat()
	sc.prevDisk = readDiskStat()
	return sc
}

// Sample reads the current system state and computes deltas from the previous sample.
func (sc *SystemCollector) Sample() SystemSnapshot {
	now := time.Now()
	snap := SystemSnapshot{Timestamp: now}

	// --- CPU ---
	cur := readCPUStat()
	if sc.prevCPU != nil && cur != nil {
		totalDelta := float64(cur.total() - sc.prevCPU.total())
		if totalDelta > 0 {
			busyDelta := float64(cur.busy() - sc.prevCPU.busy())
			snap.CPUPercent = math.Min(100.0, (busyDelta/totalDelta)*100.0)
		}
	}
	sc.prevCPU = cur

	// --- CPU Temperature ---
	snap.CPUTempC, snap.ThermalThrotl = readCPUThermal()

	// --- Memory ---
	snap.RAMTotalMB, snap.RAMAvailableMB, snap.RAMUsedMB, snap.SwapUsedMB = readMemInfo()

	// --- Network ---
	curNet := readNetStat()
	if sc.prevNet != nil && curNet != nil {
		dt := curNet.ts.Sub(sc.prevNet.ts).Seconds()
		if dt > 0 {
			snap.NICRxMbps = float64(curNet.rxBytes-sc.prevNet.rxBytes) * 8.0 / (dt * 1e6)
			snap.NICTxMbps = float64(curNet.txBytes-sc.prevNet.txBytes) * 8.0 / (dt * 1e6)
		}
	}
	sc.prevNet = curNet
	snap.NICCapacityMbps = readNICCapacity()

	// --- Disk ---
	curDisk := readDiskStat()
	if sc.prevDisk != nil && curDisk != nil {
		dt := curDisk.ts.Sub(sc.prevDisk.ts).Seconds()
		if dt > 0 {
			snap.DiskReadIOPS = float64(curDisk.reads-sc.prevDisk.reads) / dt
			snap.DiskWriteIOPS = float64(curDisk.writes-sc.prevDisk.writes) / dt
		}
	}
	sc.prevDisk = curDisk

	// --- Disk free ---
	snap.DiskFreeGB = readDiskFree()

	// --- Disk total ---
	snap.DiskTotalGB = readDiskTotal()

	return snap
}

// readCPUStat reads aggregate CPU jiffies from /proc/stat.
func readCPUStat() *cpuStat {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return nil
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "cpu ") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 9 {
			return nil
		}
		parse := func(s string) uint64 {
			v, _ := strconv.ParseUint(s, 10, 64)
			return v
		}
		return &cpuStat{
			user:    parse(fields[1]),
			nice:    parse(fields[2]),
			system:  parse(fields[3]),
			idle:    parse(fields[4]),
			iowait:  parse(fields[5]),
			irq:     parse(fields[6]),
			softirq: parse(fields[7]),
			steal:   parse(fields[8]),
			ts:      time.Now(),
		}
	}
	return nil
}

// readMemInfo reports total, available, used memory and swap, in MB.
//
// sz-mac-worker.3b (launch-completion-megaplan): Linux reads /proc/meminfo
// directly (unchanged). A native Mac worker has no /proc, so Darwin shells
// out to `sysctl -n hw.memsize` (the one unified RAM/VRAM pool) and `vm_stat`
// (wired+active+dirty pages = used), reusing the same parsers the Metal GPU
// sampler already uses (powermetrics_parse.go / gpu_darwin.go) since it is
// literally the same pool. Any other GOOS returns zeros, same as a failed
// /proc/meminfo open always did.
func readMemInfo() (totalMB, availableMB, usedMB, swapUsedMB float64) {
	if runtime.GOOS == "darwin" {
		memRaw, _ := darwinMemProbe("sysctl", []string{"-n", "hw.memsize"})
		vmRaw, _ := darwinMemProbe("vm_stat", []string{})
		return computeDarwinMemInfo(memRaw, vmRaw)
	}
	return readMemInfoLinux()
}

// darwinMemProbe runs one short-lived command (sysctl/vm_stat) and returns
// its stdout. Bounded by a timeout so a hung probe never stalls the
// heartbeat loop.
func darwinMemProbe(cmd string, args []string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, cmd, args...).Output()
	if err != nil {
		return "", err
	}
	return string(out), nil
}

// computeDarwinMemInfo turns raw sysctl/vm_stat output into the same
// (total, available, used, swap) shape readMemInfoLinux returns. Pure and
// unit-testable independent of actually running on macOS; swap is always 0
// (vm_stat's "Swapins/Swapouts" counters are cumulative event counts, not a
// current swap-used size, so there is nothing honest to report here).
func computeDarwinMemInfo(memRaw, vmRaw string) (totalMB, availableMB, usedMB, swapUsedMB float64) {
	totalMB = parseMemsizeMB(memRaw)
	vm := parseVMStat(vmRaw)
	usedMB = vm.UsedMB
	if totalMB > 0 {
		availableMB = totalMB - usedMB
		if availableMB < 0 {
			availableMB = 0
		}
	}
	return
}

// readMemInfoLinux reads /proc/meminfo for total, available, used memory and
// swap.
func readMemInfoLinux() (totalMB, availableMB, usedMB, swapUsedMB float64) {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return
	}
	defer f.Close()

	var total, available, swapTotal, swapFree uint64
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case strings.HasPrefix(line, "MemTotal:"):
			total = parseMemInfoValue(line)
		case strings.HasPrefix(line, "MemAvailable:"):
			available = parseMemInfoValue(line)
		case strings.HasPrefix(line, "SwapTotal:"):
			swapTotal = parseMemInfoValue(line)
		case strings.HasPrefix(line, "SwapFree:"):
			swapFree = parseMemInfoValue(line)
		}
	}
	totalMB = float64(total) / 1024.0
	availableMB = float64(available) / 1024.0
	usedMB = totalMB - availableMB
	if swapTotal > 0 {
		swapUsedMB = float64(swapTotal-swapFree) / 1024.0
	}
	return
}

func parseMemInfoValue(line string) uint64 {
	fields := strings.Fields(line)
	if len(fields) < 2 {
		return 0
	}
	v, _ := strconv.ParseUint(fields[1], 10, 64)
	return v // in kB
}

// readCPUThermal reads CPU temperature and throttle state.
func readCPUThermal() (tempC float64, throttled bool) {
	// Try thermal zones
	zones, _ := filepath.Glob("/sys/class/thermal/thermal_zone*/temp")
	var maxTemp float64
	for _, z := range zones {
		data, err := os.ReadFile(z)
		if err != nil {
			continue
		}
		v, err := strconv.ParseFloat(strings.TrimSpace(string(data)), 64)
		if err != nil {
			continue
		}
		t := v / 1000.0 // millidegrees to degrees
		if t > maxTemp {
			maxTemp = t
		}
	}
	tempC = maxTemp

	// Thermal throttling means the CPU is clamping performance BECAUSE OF
	// HEAT. Low frequency on its own is NOT throttling — an idle CPU clocks
	// down for power saving (powersave/schedutil governors), which looks
	// identical to a thermal clamp. The old heuristic (curF < 0.9*maxF →
	// throttled) therefore fired on every idle box, and the cooler/more idle
	// the machine, the harder it tripped — flagging perfectly healthy workers
	// as throttled so the scheduler skipped them forever.
	//
	// Gate the frequency check on actual temperature: a CPU below the thermal
	// danger zone (Tjmax is ~95-100°C on Intel/AMD) cannot be thermally
	// throttled regardless of clock speed. Only when it's genuinely hot AND
	// the clock is suppressed do we call it thermal throttling. If the temp
	// can't be read (tempC == 0) we err toward "not throttled" rather than
	// blocking the worker.
	const thermalThrottleTempC = 90.0
	if tempC >= thermalThrottleTempC {
		curFreqs, _ := filepath.Glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")
		maxFreqs, _ := filepath.Glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq")
		if len(curFreqs) > 0 && len(maxFreqs) > 0 {
			curF := readFreqFile(curFreqs[0])
			maxF := readFreqFile(maxFreqs[0])
			if maxF > 0 && curF < maxF*0.9 {
				throttled = true
			}
		}
	}
	return
}

func readFreqFile(path string) float64 {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	v, _ := strconv.ParseFloat(strings.TrimSpace(string(data)), 64)
	return v
}

// readNetStat reads aggregate network byte counters from /proc/net/dev.
// Excludes loopback and docker/veth interfaces.
func readNetStat() *netStat {
	f, err := os.Open("/proc/net/dev")
	if err != nil {
		return nil
	}
	defer f.Close()

	var totalRx, totalTx uint64
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.Contains(line, ":") {
			continue
		}
		parts := strings.SplitN(line, ":", 2)
		iface := strings.TrimSpace(parts[0])

		// Skip virtual interfaces
		if iface == "lo" || strings.HasPrefix(iface, "docker") ||
			strings.HasPrefix(iface, "veth") || strings.HasPrefix(iface, "br-") {
			continue
		}

		fields := strings.Fields(parts[1])
		if len(fields) < 10 {
			continue
		}
		rx, _ := strconv.ParseUint(fields[0], 10, 64)
		tx, _ := strconv.ParseUint(fields[8], 10, 64)
		totalRx += rx
		totalTx += tx
	}
	return &netStat{rxBytes: totalRx, txBytes: totalTx, ts: time.Now()}
}

// readNICCapacity tries to find the link speed of the primary physical NIC.
func readNICCapacity() float64 {
	nics, _ := filepath.Glob("/sys/class/net/*/speed")
	for _, path := range nics {
		dir := filepath.Dir(path)
		iface := filepath.Base(dir)
		if iface == "lo" || strings.HasPrefix(iface, "docker") ||
			strings.HasPrefix(iface, "veth") || strings.HasPrefix(iface, "br-") {
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		speed, err := strconv.ParseFloat(strings.TrimSpace(string(data)), 64)
		if err != nil || speed <= 0 {
			continue
		}
		return speed // already in Mbps
	}
	return 1000.0 // default 1Gbps
}

// readDiskStat reads aggregate I/O operation counts from /proc/diskstats.
// Only counts whole-disk devices (sda, nvme0n1, vda), not partitions.
func readDiskStat() *diskStat {
	f, err := os.Open("/proc/diskstats")
	if err != nil {
		return nil
	}
	defer f.Close()

	var totalReads, totalWrites uint64
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 14 {
			continue
		}
		name := fields[2]
		// Only whole disks, not partitions (sda but not sda1, nvme0n1 but not nvme0n1p1)
		if strings.HasPrefix(name, "sd") && len(name) == 3 ||
			strings.HasPrefix(name, "nvme") && strings.HasSuffix(name, "n1") ||
			strings.HasPrefix(name, "vd") && len(name) == 3 {
			reads, _ := strconv.ParseUint(fields[3], 10, 64)
			writes, _ := strconv.ParseUint(fields[7], 10, 64)
			totalReads += reads
			totalWrites += writes
		}
	}
	return &diskStat{reads: totalReads, writes: totalWrites, ts: time.Now()}
}

// readDiskFree returns free disk space in GB for the root filesystem.
func readDiskFree() float64 {
	// Use syscall.Statfs on Linux
	var stat syscallStatfs
	if err := statfs("/", &stat); err != nil {
		return 0
	}
	return float64(stat.Bavail*uint64(stat.Bsize)) / (1024.0 * 1024.0 * 1024.0)
}

// readDiskTotal returns total disk space in GB for the root filesystem.
func readDiskTotal() float64 {
	// Use syscall.Statfs on Linux
	var stat syscallStatfs
	if err := statfs("/", &stat); err != nil {
		return 0
	}
	return float64(stat.Blocks*uint64(stat.Bsize)) / (1024.0 * 1024.0 * 1024.0)
}

// syscallStatfs and statfs are thin wrappers to avoid importing syscall directly.
// This keeps the file testable and cross-platform friendly at the type level.
type syscallStatfs struct {
	Bsize  int64
	Bavail uint64
	Blocks uint64
}

func statfs(path string, buf *syscallStatfs) error {
	return statfsImpl(path, buf)
}
