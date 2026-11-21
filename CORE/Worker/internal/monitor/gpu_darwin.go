//go:build darwin

package monitor

// MAC.1.s14 — Metal / unified-memory telemetry for darwin workers.
//
// A macOS worker has no nvidia-smi and no /sys: the GPU is Metal, reached
// through `powermetrics` (root-only), and its memory is ONE unified pool
// shared with the CPU (hw.memsize), not a dedicated VRAM bar. This file runs
// those probes; the parsing is platform-neutral (powermetrics_parse.go) so
// Linux CI can exercise it against the captured fixtures.
//
// Honesty rule that matters to the scheduler: a Metal worker's vram_total_mb
// IS its ram_total_mb (the API takes max(ram, vram), never the sum), and a
// probe that could not run must say so (`metal_probe_error`) rather than
// report a confident zero that would make the box look idle and let the
// packer oversubscribe a GPU that is actually busy.
import (
	"context"
	"os/exec"
	"strings"
	"time"
)

// Probe commands, overridable in tests (the live ones need root and a Mac).
var darwinPowermetricsCommand = "powermetrics"
var darwinSysctlCommand = "sysctl"
var darwinVMStatCommand = "vm_stat"

// darwinProbeTimeout bounds every probe: a hung powermetrics must never stall
// the heartbeat loop.
var darwinProbeTimeout = 3 * time.Second

// powermetricsArgs is the sampler set: gpu_power (utilisation + power draw)
// and tasks (per-PID GPU time). One sample, plist output.
var powermetricsArgs = []string{"--samplers", "gpu_power,tasks", "-i", "1000", "-n", "1", "-f", "plist"}

// runDarwinProbe runs one command under the probe timeout, returning stdout.
func runDarwinProbe(cmd string, args []string) (string, error) {
 ctx, cancel := context.WithTimeout(context.Background(), darwinProbeTimeout)
 defer cancel()
 out, err := exec.CommandContext(ctx, cmd, args...).Output()
 if err != nil {
  return "", err
 }
 return string(out), nil
}

// sampleDarwinMetal is the darwin GPU sampler behind SampleGPU's "Metal" case.
// Never fails the beat: whatever the probes could not answer stays absent,
// and the reason goes out as metal_probe_error.
func sampleDarwinMetal() GPUSnapshot {
 snap := GPUSnapshot{Type: "Metal", PowerState: "unknown"}

 memRaw, _ := runDarwinProbe(darwinSysctlCommand, []string{"-n", "hw.memsize"})
 poolMB := parseMemsizeMB(memRaw)
 if poolMB > 0 {
  // The ONE unified pool, reported in the existing vram slot: the API's
  // _is_unified_memory_worker takes max(ram, vram), so this is the same
  // figure the RAM block already carries, not an extra device.
  snap.VRAMTotalMB = poolMB
 }

 vmRaw, vmErr := runDarwinProbe(darwinVMStatCommand, []string{})
 vm := parseVMStat(vmRaw)
 if vm.UsedMB > 0 {
  snap.VRAMUsedMB = vm.UsedMB
  if poolMB > 0 {
   free := poolMB - vm.UsedMB
   if free < 0 {
    free = 0
   }
   snap.VRAMFreeMB = free
   snap.MemoryUtilPct = 100.0 * vm.UsedMB / poolMB
  }
 }

 pmRaw, pmErr := runDarwinProbe(darwinPowermetricsCommand, powermetricsArgs)
 gpu, answered := parsePowermetricsGPU(pmRaw)
 if answered {
  snap.UtilizationPct = gpu.ActivePct
  snap.PowerMW = gpu.PowerMW
  if gpu.ActivePct > 0 {
   snap.PowerState = "active"
  } else {
   snap.PowerState = "idle"
  }
  // An Intel Mac's GPU carries dedicated VRAM; Apple Silicon reports none
  // (the pool above is the whole story), so this stays the pool figure there.
  if gpu.TotalMB > 0 {
   snap.VRAMTotalMB = gpu.TotalMB
   snap.VRAMFreeMB = gpu.AvailableMB
  }
 }

 tasks, tasksAnswered := parsePowermetricsTasks(pmRaw)
 if tasksAnswered {
  snap.GPUTasks = tasks.Procs
 }

 if !answered {
  snap.MetalProbeError = probeErrorReason(pmErr, vmErr, pmRaw)
 }
 return snap
}

// probeErrorReason names the gpu_power failure for the heartbeat. powermetrics
// is root-only: on an unprivileged LaunchAgent it exits non-zero with a sudo
// complaint, and that is worth reporting honestly — a silent "none" would let
// the packer oversubscribe a GPU that is measurably busy. The memory figures
// above still stand; only the GPU half is missing.
func probeErrorReason(pmErr, vmErr error, raw string) string {
 if pmErr != nil {
  return "powermetrics failed: "+pmErr.Error()
 }
 if strings.Contains(raw, "<key>gpu_power</key>") {
  return "gpu_power sampler returned no gpu dict"
 }
 if strings.Contains(raw, "sudo") || strings.Contains(raw, "root") {
  return "powermetrics requires root (LaunchAgent runs unprivileged)"
 }
 _ = vmErr
 return "no gpu_power reading"
}
