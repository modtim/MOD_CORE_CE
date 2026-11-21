package monitor

// MAC.1.s14 — Metal / unified-memory telemetry for darwin workers.
//
// powermetrics emits plist XML (not the nvidia-smi csv gpu.go reads), so the
// parsing lives here, platform-neutrally and WITHOUT a build tag: the live
// probe runs only on macOS (gpu_darwin.go), but the parser is exercised on
// Linux CI against the captured fixtures in testdata/.
import (
	"regexp"
	"strconv"
	"strings"
)

// PowermetricsGPU is one `powermetrics --samplers gpu_power` reading. The
// plist carries power in MILLIWATTS (only the text renderer divides by 1000)
// and `active` as a residency percentage over the sampling window.
type PowermetricsGPU struct {
	ActivePct   float64
	AvgRatePct  float64
	MaxRatePct  float64
	PowerMW     float64
	AvgPowerMW  float64
	MaxPowerMW  float64
	TotalMB     float64 // dedicated VRAM — Intel Macs only, 0 on Apple Silicon
	AvailableMB float64
}

// PowermetricsTasks is the gpu_activity sampler's PID attribution: how much
// GPU time each process on the box spent, so a task's own demand separates
// from every other tenant's — the same reasoning as gpu.go's
// nvidia --query-compute-apps path.
type PowermetricsTasks struct {
	TotalGPUMS float64
	Procs      []GPUPID
}

type GPUPID struct {
	PID         int
	GPUTimeMS   float64
	ProcessName string
}

// VMStat is the unified-memory footprint from `vm_stat`, in MB. TotalMB stays
// 0 here — the pool size comes from hw.memsize (parseMemsizeMB).
type VMStat struct {
	TotalMB    float64
	UsedMB     float64
	SwapUsedMB float64
}

// vmStatPageSizeBytes is the fallback page size when vm_stat does not name one
// (its "page size of N bytes" trailer does on modern macOS). Apple Silicon is
// 4096; overridable in tests.
var vmStatPageSizeBytes = 4096.0

// PlistLeaf is one leaf <dict> in a powermetrics plist: its raw body (so the
// caller can still ask which keys exist) plus the flattened key->scalar map.
type PlistLeaf struct {
	Body string
	Vals map[string]string
}

// leafDicts returns every LEAF <dict> in a plist, plus whether the input
// looked like a plist at all. A dict holding a nested container is skipped:
// its keys belong to the parent, which is what keeps gpu_activity's `tasks`
// payload from being mistaken for a gpu_power reading. Malformed XML yields
// whatever parsed cleanly; nothing panics.
func leafDicts(raw string) ([]PlistLeaf, bool) {
	out := []PlistLeaf{}
	sawPlist := strings.Contains(raw, "<key>") || strings.Contains(raw, "<dict>")
	for i := 0; i+5 <= len(raw); i++ {
		if !strings.HasPrefix(raw[i:], "<dict>") {
			continue
		}
		 body, ok := dictBody(raw, i)
		if !ok {
			continue
		}
		if strings.Contains(body, "<dict") || strings.Contains(body, "<array") {
			continue
		}
		 vals := leafValues(body)
		if len(vals) > 0 {
			out = append(out, PlistLeaf{Body: body, Vals: vals})
		}
	}
	return out, sawPlist
}

// dictBody returns the text inside the <dict> opening at `at`, depth-matched
// so a nested </dict> cannot end the scan early.
func dictBody(raw string, at int) (string, bool) {
	depth := 1
	for i := at + 5; i < len(raw); {
		lt := strings.Index(raw[i:], "<")
		if lt < 0 {
			return "", false
		}
		 j := i + lt
		if strings.HasPrefix(raw[j:], "<dict>") {
			depth++
			i = j + 6
		} else if strings.HasPrefix(raw[j:], "</dict>") {
			depth--
			if depth == 0 {
				return raw[at+5 : j], true
			}
			i = j + 7
		} else {
			i = j + 1
		}
	}
	return "", false
}

// leafValues maps each <key>NAME</key> to the scalar text between its value
// tags. <true/> reads as "1"; container values contribute nothing.
func leafValues(body string) map[string]string {
	out := map[string]string{}
	for i := 0; i < len(body); {
		ks := strings.Index(body[i:], "<key>")
		if ks < 0 {
			break
		}
		ks += i
		ke := strings.Index(body[ks+5:], "</key>")
		if ke < 0 {
			break
		}
		ke += ks + 5
		 name := strings.TrimSpace(body[ks+5 : ke])
		val, ok := valueText(body[ke+6:])
		if ok && name != "" {
			out[name] = val
		}
		i = ke + 6
	}
	return out
}

// valueText reads the first plist value scalar in `rest`.
func valueText(rest string) (string, bool) {
	lt := strings.Index(rest, "<")
	if lt < 0 {
		return "", false
	}
	 gt := strings.Index(rest[lt:], ">")
	if gt < 0 {
		return "", false
	}
	 inner := strings.TrimSpace(rest[lt+1 : lt+gt])
	if strings.HasSuffix(inner, "/") {
		trimmed := strings.TrimRight(inner, "/")
		if trimmed == "true" {
			return "1", true
		}
		if trimmed == "false" {
			return "0", true
		}
		return "", false
	}
	 ce := strings.Index(rest[lt+gt:], "</"+inner+">")
	if ce < 0 {
		return "", false
	}
	ce += lt + gt
	return strings.TrimSpace(rest[lt+gt+1 : ce]), true
}

// f64 parses a plist scalar tolerantly: "" / garbage -> 0, never a panic.
func f64(s string) float64 {
	v, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil {
		return 0
	}
	return v
}

// hasKey reports whether a plist dict body carries `k` at all — the difference
// between "the sampler did not answer this" and "the answer is zero".
func hasKey(body string, k string) bool {
	return strings.Contains(body, "<key>"+k+"</key>")
}

// gpuFromDict maps one gpu_power leaf dict onto PowermetricsGPU.
func gpuFromDict(d map[string]string) PowermetricsGPU {
	return PowermetricsGPU{
		ActivePct: f64(d["active"]),
		AvgRatePct: f64(d["average-rate"]),
		MaxRatePct: f64(d["max-rate"]),
		PowerMW: f64(d["power"]),
		AvgPowerMW: f64(d["average-power"]),
		MaxPowerMW: f64(d["max-power"]),
		TotalMB: f64(d["total"]),
		AvailableMB: f64(d["available"]),
	}
}

// parsePowermetricsGPU maps a gpu_power reading. answered=false for input with
// no GPU-keyed dict (a box with no GPU, a truncated plist, or garbage) — the
// caller then reports nothing rather than a confident zero.
func parsePowermetricsGPU(raw string) (PowermetricsGPU, bool) {
	dicts, _ := leafDicts(raw)
	for _, d := range dicts {
		if !hasKey(d.Body, "active") && !hasKey(d.Body, "power") &&
			!hasKey(d.Body, "total") && !hasKey(d.Body, "average-rate") {
			continue
		}
		return gpuFromDict(d.Vals), true
	}
	return PowermetricsGPU{}, false
}

// gpuTimeRe reads the GPU-time number out of one gpu_activity task, whichever
// spelling the sampler used ("gpu-active-time" in the plist form,
// "gpu_active_time" in the JSON rendering). Values are milliseconds.
var gpuTimeRe = regexp.MustCompile(`(?i)gpu[-_ ]?active[-_ ]?time[^0-9]*([0-9]+(?:\.[0-9]+)?)`)

// pidRe reads a task's pid.
var pidRe = regexp.MustCompile(`(?i)"?pid"?[^0-9]*([0-9]+)`)

// procRe reads a task's process name.
var procRe = regexp.MustCompile(`(?i)"?process(?:-name)?"?\s*:\s*"?([^",}\n]+)`)

// taskFromDict maps one gpu_activity leaf dict (plist form) onto GPUPID.
func taskFromDict(d map[string]string) GPUPID {
	return GPUPID{
		PID: int(f64(d["pid"])),
		GPUTimeMS: f64(d["gpu-active-time"]),
		ProcessName: d["process"],
	}
}

// tasksFrom sums the per-PID GPU time into one snapshot.
func tasksFrom(procs []GPUPID) PowermetricsTasks {
	t := PowermetricsTasks{Procs: procs}
	for _, p := range procs {
		t.TotalGPUMS += p.GPUTimeMS
	}
	return t
}

// parsePowermetricsTasks maps a gpu_activity reading. answered=false when no
// task list is present at all; an EMPTY list is a real "nothing used the GPU"
// measurement and answers true with zero processes.
func parsePowermetricsTasks(raw string) (PowermetricsTasks, bool) {
	procs := []GPUPID{}
	leaves, _ := leafDicts(raw)
	for _, d := range leaves {
		// gpu_activity's outer dict carries the task list as a nested array,
		// so leafDicts skipped it; a task row is a leaf dict with a pid.
		if !hasKey(d.Body, "pid") && !hasKey(d.Body, "gpu-active-time") {
			continue
		}
		procs = append(procs, taskFromDict(d.Vals))
	}
	if len(procs) > 0 {
		return tasksFrom(procs), true
	}
	// powermetrics also renders the task list as one JSON-ish blob inside a
	// <string> value, which leafDicts never sees; split it per task entry.
	// An explicit but EMPTY list is a real measurement ("nothing touched the
	// GPU"), which is why the key's presence answers true on its own.
	if !strings.Contains(raw, "<key>tasks</key>") && gpuTimeRe.FindString(raw) == "" {
		return PowermetricsTasks{}, false
	}
	for _, chunk := range strings.Split(raw, "},") {
		pm := pidRe.FindStringSubmatch(chunk)
		nt := gpuTimeRe.FindStringSubmatch(chunk)
		if pm == nil || nt == nil {
			continue
		}
		np := procRe.FindStringSubmatch(chunk)
		name := ""
		if np != nil {
			name = strings.TrimSpace(np[1])
		}
		procs = append(procs, GPUPID{
			PID: int(f64(pm[1])),
			GPUTimeMS: f64(nt[1]),
			ProcessName: name,
		})
	}
	if len(procs) == 0 && !strings.Contains(raw, "<key>tasks</key>") {
	 return PowermetricsTasks{}, false
	}
 return tasksFrom(procs), true
}

// memsizeRe reads the byte count out of a hw.memsize line — `sysctl -n` prints
// it bare, plain `sysctl` prefixes it with the key.
var memsizeRe = regexp.MustCompile(`hw\.memsize:\s*([0-9]+)|^([0-9]+)\s*$`)

// parseMemsizeMB reads `sysctl -n hw.memsize` (bytes) -> MB.
func parseMemsizeMB(raw string) float64 {
 m := memsizeRe.FindStringSubmatch(strings.TrimSpace(raw))
 if m == nil {
  return 0
 }
 digits := m[1]
 if digits == "" {
  digits = m[2]
 }
 b := f64(digits)
 if b <= 0 {
  return 0
 }
 return b / 1024.0 / 1024.0
}

// pageRe names the page size vm_stat counts in ("page size of 16384 bytes").
var pageRe = regexp.MustCompile(`page size[:\s]+([0-9]+)`)

// pagesRe reads the page count at the end of a vm_stat row ("Wired pages:
// 188204"). vm_stat puts the number AFTER the label, unlike most tools.
var pagesRe = regexp.MustCompile(`([0-9]+)\s*$`)

// parseVMStat maps `vm_stat` page counts onto MB. used = wired + active +
// dirty: the resident, non-freeable footprint of the ONE unified pool (the
// figure the API's shared-memory budget needs); free/cached/inactive pages
// excluded. Row labels are matched exactly so "Inactive pages" can never be
// read as "active" and a "purgeable dirty" row cannot overwrite the real one.
func parseVMStat(raw string) VMStat {
 pageSize := vmStatPageSizeBytes
 if m := pageRe.FindStringSubmatch(raw); m != nil {
  if v, err := strconv.ParseFloat(m[1], 64); err == nil && v > 0 {
   pageSize = v
  }
 }
 var wired, active, dirty, swapUsed float64
 for _, line := range strings.Split(raw, "\n") {
  colon := strings.Index(line, ":")
  if colon < 0 {
   continue
  }
  head := strings.ToLower(strings.TrimSpace(line[:colon]))
  m := pagesRe.FindStringSubmatch(strings.TrimSpace(line[colon+1:]))
  if m == nil {
   continue
  }
  pages, err := strconv.ParseFloat(m[1], 64)
  if err != nil {
   continue
  }
  switch head {
  case "wired pages":
   wired = pages
  case "active pages":
   active = pages
  case "dirty pages":
   dirty = pages
  case "swapped pages", "swap pages":
   swapUsed = pages
  }
 }
 mb := pageSize / 1024.0 / 1024.0
 return VMStat{
  UsedMB: (wired + active + dirty) * mb,
  SwapUsedMB: swapUsed * mb,
 }
}
