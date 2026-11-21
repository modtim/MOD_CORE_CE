package monitor

// MAC.1.s14 — Metal / unified-memory telemetry parsing, exercised on Linux
// (powermetrics_parse.go carries no build tag) against the captured darwin
// fixtures in testdata/. The live probe (gpu_darwin.go) is darwin-only and is
// the owner's Mac run; the parser is what has to be right everywhere.
import (
	"os"
	"strings"
	"testing"
)

func fixture(t *testing.T, path string) string {
 raw, err := os.ReadFile(path)
 if err != nil {
  t.Fatalf("fixture %s missing: %v", path, err)
 }
 return string(raw)
}

// gpuFixture is the captured `powermetrics --samplers gpu_power,tasks -i 1000
// -n 1 -f plist` output on an Apple Silicon box.
func TestParsePowermetricsGPU_HappyPath(t *testing.T) {
 raw := fixture(t, "testdata/powermetrics_gpu.plist")
 gpu, ok := parsePowermetricsGPU(raw)
 if !ok {
  t.Fatal("answered=false although the plist carries a gpu_power dict")
 }
 if gpu.ActivePct != 63.5 {
  t.Fatalf("active residency %.2f, want 63.5", gpu.ActivePct)
 }
 // powermetrics' plist unit is MILLIWATTS; the caller must not divide again.
 if gpu.PowerMW != 11840 {
  t.Fatalf("power %.0f mW, want 11840", gpu.PowerMW)
 }
 if gpu.AvgPowerMW != 10990 || gpu.MaxPowerMW != 13220 {
  t.Fatalf("power window %.0f/%.0f, want 10990/13220", gpu.AvgPowerMW, gpu.MaxPowerMW)
 }
 if gpu.AvgRatePct != 58.25 || gpu.MaxRatePct != 71 {
  t.Fatalf("rate %.2f/%.2f, want 58.25/71", gpu.AvgRatePct, gpu.MaxRatePct)
 }
 // Apple Silicon: no dedicated VRAM, so the sampler says 0 rather than
// inventing a second device (the pool comes from hw.memsize).
 if gpu.TotalMB != 0 {
  t.Fatalf("dedicated VRAM %.0f, want 0 on Apple Silicon", gpu.TotalMB)
 }
}

func TestParsePowermetricsGPU_MissingGPUDictIsUnknown(t *testing.T) {
 // A cpu_power-only reading: reporting a confident zero utilisation would make
 // the box look idle and let the packer oversubscribe a GPU that is busy.
 raw := fixture(t, "testdata/powermetrics_gpu.plist")
 trimmed := raw
 if i := strings.Index(trimmed, "<key>gpu_power</key>"); i >= 0 {
  j := strings.Index(trimmed[i:], "</dict>")
  trimmed = trimmed[:i] + trimmed[i+j+7:]
 }
 if _, ok := parsePowermetricsGPU(trimmed); ok {
  t.Fatal("answered=true with no gpu_power dict; the caller must report nothing")
 }
 if _, ok := parsePowermetricsGPU(""); ok {
  t.Fatal("answered=true for empty output")
 }
}

func TestParsePowermetricsGPU_GarbageInputNeverPanics(t *testing.T) {
 // Truncated / corrupt plist (powermetrics killed mid-write, or a sudo
 // complaint on stderr leaking into the capture): zero, answered=false.
 for _, raw := range []string{"not xml at all", "<dict><key>active</key>", "<dict></dict>", "<<<>>"} {
  if gpu, ok := parsePowermetricsGPU(raw); ok && gpu.ActivePct != 0 {
   t.Fatalf("garbage %q produced utilisation %.2f", raw, gpu.ActivePct)
  }
 }
 // A non-numeric scalar must not abort the whole reading either.
 gpu, ok := parsePowermetricsGPU("<dict><key>active</key><string>n/a</string><key>power</key><integer>900</integer></dict>")
 if !ok || gpu.ActivePct != 0 || gpu.PowerMW != 900 {
  t.Fatalf("n/a active: got %.0f/%.0f ok=%v, want 0/900/true", gpu.ActivePct, gpu.PowerMW, ok)
 }
}

// PID attribution: gpu_activity's task list is what lets a task's own GPU
// demand be separated from every other tenant's on the box.
func TestParsePowermetricsTasks_PIDAttribution(t *testing.T) {
 raw := fixture(t, "testdata/powermetrics_gpu.plist")
 tasks, ok := parsePowermetricsTasks(raw)
 if !ok || len(tasks.Procs) != 2 {
  t.Fatalf("got %d tasks ok=%v, want 2", len(tasks.Procs), ok)
 }
 if tasks.Procs[0].PID != 4127 || tasks.Procs[0].GPUTimeMS != 612 {
  t.Fatalf("first task pid/time %d/%.0f, want 4127/612", tasks.Procs[0].PID, tasks.Procs[0].GPUTimeMS)
 }
 if tasks.Procs[0].ProcessName != "blender" {
  t.Fatalf("first task process %q, want blender", tasks.Procs[0].ProcessName)
 }
 if tasks.TotalGPUMS != 700 {
  t.Fatalf("total GPU time %.0f ms, want 700 (612+88)", tasks.TotalGPUMS)
 }
 // The other tenant's 88 ms must not be folded into this task's demand —
  // that is the caller's job, exactly as gpu.go does for nvidia-smi rows.
}

func TestParsePowermetricsTasks_NoTaskListIsUnknown(t *testing.T) {
 if _, ok := parsePowermetricsTasks(fixture(t, "testdata/vm_stat.txt")); ok {
  t.Fatal("claimed a task list for input that has none")
 }
 // An EMPTY list is a real measurement ("nothing touched the GPU"), distinct
 // from "the sampler did not answer".
 empty := "<dict><key>gpu_activity</key><dict><key>tasks</key><array></array></dict></dict>"
 tasks, ok := parsePowermetricsTasks(empty)
 if !ok || len(tasks.Procs) != 0 {
  t.Fatalf("empty task list: got %d ok=%v, want 0/true", len(tasks.Procs), ok)
 }
}

func TestParseVMStat_UsedIsResidentPoolFootprint(t *testing.T) {
 raw := fixture(t, "testdata/vm_stat.txt")
 vm := parseVMStat(raw)
 // 16384-byte pages: wired 188204 + active 298412 + dirty 90311 pages.
 want := (188204.0 + 298412.0 + 90311.0) * 16384.0 / 1024.0 / 1024.0
 if vm.UsedMB != want {
  t.Fatalf("used %.0f MB, want %.0f (wired+active+dirty, page size from header)", vm.UsedMB, want)
 }
 if vm.SwapUsedMB != 12288.0*16384.0/1024.0/1024.0 {
  t.Fatalf("swap %.0f MB, want 192", vm.SwapUsedMB)
 }
 // "Inactive pages" must never be read as "active", and the purgeable-dirty
 // row must not overwrite the real dirty row.
 if vm.UsedMB != want {
  t.Fatal("label matching picked the wrong rows")
 }
}

func TestParseVMStat_MissingRowsAreZero(t *testing.T) {
 vm := parseVMStat("no rows at all\n")
 if vm.UsedMB != 0 || vm.SwapUsedMB != 0 {
  t.Fatalf("garbage vm_stat produced %.0f/%.0f, want 0/0", vm.UsedMB, vm.SwapUsedMB)
 }
 // No page-size header falls back to the Apple-Silicon default.
 vm2 := parseVMStat("Wired pages: 1024\n")
 if vm2.UsedMB != 4 {
  t.Fatalf("fallback page size gave %.0f MB, want 4", vm2.UsedMB)
 }
}

func TestParseMemsizeMB_Tolerant(t *testing.T) {
 if got := parseMemsizeMB("17179869184\n"); got != 16384 {
  t.Fatalf("hw.memsize 16 GiB -> %.0f MB, want 16384", got)
 }
 if got := parseMemsizeMB("hw.memsize: 34359738368"); got != 32768 {
  t.Fatalf("labelled sysctl form -> %.0f MB, want 32768", got)
 }
 if got := parseMemsizeMB(""); got != 0 {
  t.Fatalf("empty memsize -> %.0f, want 0", got)
 }
}
