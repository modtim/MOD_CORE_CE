package executor

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// dockerContainerIDPattern matches the 64-hex-digit container ID embedded in
// a /proc/<pid>/cgroup path, whether cgroup v1
// ("12:memory:/docker/<id>"), v2 ("0::/system.slice/docker-<id>.scope") or a
// nested "/docker/<id>/..." path.
var dockerContainerIDPattern = regexp.MustCompile(`[0-9a-f]{64}`)

const bytesPerMiB = int64(1024 * 1024)

// parseNvidiaComputeApps parses the output of
// nvidia-smi --query-compute-apps=pid,used_memory
// --format=csv,noheader,nounits (lines like "12345, 2048") into
// pid -> used VRAM MiB. Blank lines, stray spaces and [N/A] values are
// tolerated; a pid with an [N/A] memory is recorded as 0.
func parseNvidiaComputeApps(out []byte) map[int]int64 {
	result := make(map[int]int64)
	for _, chunk := range bytes.Split(out, []byte("\n")) {
		line := strings.TrimSpace(string(chunk))
		if line == "" {
			continue
		}
		fields := strings.Split(line, ",")
		if len(fields) < 2 {
			continue
		}
		pid, err := strconv.Atoi(strings.TrimSpace(fields[0]))
		if err != nil {
			continue
		}
		memory := strings.TrimSpace(fields[1])
		if memory == "" || memory == "[N/A]" {
			result[pid] = 0
			continue
		}
		mib, err := strconv.ParseInt(memory, 10, 64)
		if err != nil {
			continue
		}
		result[pid] = mib
	}
	return result
}

// parseRocmShowPids parses `rocm-smi --showpids` output. Data lines carry the
// columns PID, PROCESS NAME, GPU(s), VRAM USED, SDMA USED, CU OCCUPANCY, with
// VRAM USED in bytes; the result maps pid -> used VRAM MiB. Header rows,
// "=====" separator lines and "KFD process information:" banners are skipped,
// as are lines whose PID or VRAM field is not numeric (e.g. [N/A]).
func parseRocmShowPids(out []byte) map[int]int64 {
	result := make(map[int]int64)
	for _, chunk := range bytes.Split(out, []byte("\n")) {
		line := strings.TrimSpace(string(chunk))
		if line == "" || strings.HasPrefix(line, "=") || strings.HasPrefix(line, "KFD") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 4 {
			continue
		}
		pid, err := strconv.Atoi(fields[0])
		if err != nil {
			continue
		}
		vramBytes, err := strconv.ParseInt(fields[3], 10, 64)
		if err != nil {
			continue
		}
		result[pid] = vramBytes / bytesPerMiB
	}
	return result
}

// containerIDFromCgroup extracts the 64-hex-digit docker container ID from
// /proc/<pid>/cgroup content. Handles cgroup v1 ("12:memory:/docker/<id>"),
// cgroup v2 ("0::/system.slice/docker-<id>.scope") and nested paths
// (".../docker/<id>/..."); returns "" when no ID is present.
func containerIDFromCgroup(content []byte) string {
	return dockerContainerIDPattern.FindString(string(content))
}

// VRAMSampler periodically reads per-process GPU VRAM usage from the host
// (nvidia-smi, falling back to rocm-smi when nvidia-smi is unavailable or
// reports nothing) and attributes it to docker containers via the process
// cgroup. It is used to keep a per-container peak VRAM figure for active
// node runs. run and readCgroup are injectable for tests.
type VRAMSampler struct {
	mu         sync.Mutex
	run        func(ctx context.Context, name string, args ...string) ([]byte, error)
	readCgroup func(pid int) ([]byte, error)
	peaks      map[string]int64
}

// NewVRAMSampler returns a sampler that shells out to nvidia-smi/rocm-smi via
// exec.CommandContext and reads /proc/<pid>/cgroup with os.ReadFile.
func NewVRAMSampler() *VRAMSampler {
	return &VRAMSampler{
		run: func(ctx context.Context, name string, args ...string) ([]byte, error) {
			return exec.CommandContext(ctx, name, args...).Output()
		},
		readCgroup: func(pid int) ([]byte, error) {
			return os.ReadFile(fmt.Sprintf("/proc/%d/cgroup", pid))
		},
		peaks: make(map[string]int64),
	}
}

// SampleOnce takes a single snapshot and returns container ID -> total VRAM
// MiB across all of the container's pids. It tries nvidia-smi first and
// falls back to rocm-smi; a missing binary is not an error.
func (s *VRAMSampler) SampleOnce(ctx context.Context) map[string]int64 {
	perPID := map[int]int64{}
	nvidiaOut, err := s.run(ctx, "nvidia-smi",
		"--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits")
	if err == nil {
		perPID = parseNvidiaComputeApps(nvidiaOut)
	}
	if len(perPID) == 0 {
		if rocmOut, rErr := s.run(ctx, "rocm-smi", "--showpids"); rErr == nil {
			perPID = parseRocmShowPids(rocmOut)
		}
	}
	byContainer := map[string]int64{}
	for pid, mib := range perPID {
		cgroup, err := s.readCgroup(pid)
		if err != nil {
			continue
		}
		containerID := containerIDFromCgroup(cgroup)
		if containerID == "" {
			continue
		}
		byContainer[containerID] += mib
	}
	return byContainer
}

// Observe records a sample, keeping the per-container maximum.
func (s *VRAMSampler) Observe(samples map[string]int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for containerID, mib := range samples {
		if mib > s.peaks[containerID] {
			s.peaks[containerID] = mib
		}
	}
}

// Peak returns the highest VRAM MiB value observed for a container since its
// last Forget (0 when never observed).
func (s *VRAMSampler) Peak(containerID string) int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.peaks[containerID]
}

// Forget drops the tracked peak for a container, e.g. when its run ends.
func (s *VRAMSampler) Forget(containerID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.peaks, containerID)
}

// Run samples every interval (default 5s when interval <= 0) until ctx is
// done, but only takes a sample while active() is true.
func (s *VRAMSampler) Run(ctx context.Context, interval time.Duration, active func() bool) {
	if interval <= 0 {
		interval = 5 * time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if active == nil || active() {
				s.Observe(s.SampleOnce(ctx))
			}
		}
	}
}
