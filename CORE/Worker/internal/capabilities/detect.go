package capabilities

import (
	"bytes"
	"io"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/nativeapps"
)

// Profile describes the hardware and software capabilities of this worker.
// Sent during heartbeat so the manager only dispatches compatible tasks.
type Profile struct {
	OS            string   `json:"os"`       // linux, darwin, windows
	Arch          string   `json:"arch"`     // amd64, arm64
	GPUType       string   `json:"gpu_type"` // CUDA, ROCm, Intel, none — kept for backward compat
	GPUName       string   `json:"gpu_name"` // e.g. "NVIDIA RTX 4090"
	VRAMGB        float64  `json:"vram_gb"`
	RAMGB         float64  `json:"ram_gb"`
	CPUCores      int      `json:"cpu_cores"`
	Tools         []string `json:"tools"`          // native tools found in PATH
	HardwareTags  []string `json:"hardware_tags"`  // open-ended: ["cuda","tensorrt"], ["rocm"], ["cpu"]
	CloudProvider string   `json:"cloud_provider"` // aws, gcp, azure, or empty
	InstanceType  string   `json:"instance_type"`  // e.g. "g5.xlarge", "a2-highgpu-1g"

	// Per-device accelerator inventory — every GPU/NPU individually enumerated.
	// Sent in the heartbeat payload so the API can upsert AcceleratorProfile
	// rows and auto-dispatch per-device benchmarks on new worker join.
	Accelerators []AcceleratorDevice `json:"accelerators"`

	// native-workers GP.4 / D-P2 — customer-installed apps detected on this
	// worker (Windows registry App Paths / macOS mdfind+Info.plist / PATH),
	// probed per WORKER_NATIVE_APP_PROBES. Sent in the heartbeat so the API's
	// claim guard can satisfy a native task's requires_app. Empty (never
	// nil) when no probes are configured or none matched.
	NativeApps []nativeapps.DetectedApp `json:"native_apps"`

	// native-workers GP.17c — WSL container (wslc) availability on this host,
	// probed read-only (wsl --version + wslc.exe on PATH, never wsl --update).
	// Nil on non-windows so their heartbeats are byte-identical to before.
	Wslc *WslcCapability `json:"wslc,omitempty"`

	// Internal — all detected accelerator types (cuda, rocm, intel, npu, etc.)
	detectedAccelerators []string
}

// Detect probes the current system and returns its capability profile.
func Detect() Profile {
	p := Profile{
		OS:       runtime.GOOS,
		Arch:     runtime.GOARCH,
		CPUCores: runtime.NumCPU(),
		GPUType:  "none",
	}

	p.RAMGB = detectRAM()
	detectGPU(&p)
	detectCloud(&p)
	p.Tools = detectTools()
	// Platform capability tags are self-probed (see platform.go): whether this
	// host can actually RUN accelerated containers, not merely enumerate an
	// accelerator. The manager cannot determine this — runtime.GOOS returns
	// "linux" on WSL2, where enumeration works and kernel submit fails.
	autoTags := append(deriveAutoTags(&p), derivePlatformTags(&p)...)
	p.HardwareTags = mergeHardwareTags(autoTags, os.Getenv("WORKER_HARDWARE_TAGS"))
	p.Accelerators = EnumerateAccelerators()
	// F14b — the Intel branch of detectGPU (and any vendor path that only
	// sets GPUType) leaves the primary summary unsized, so the heartbeat
	// sent gpu_name="" and gpu_vram_mb=0, and the API merge gate
	// (`if request.has_gpu and request.gpu_vram_mb`) dropped gpu_name and
	// vram_gb entirely — which is why an Arc box showed has_gpu=true,
	// gpu_type=Intel but an EMPTY name and VRAM. Backfill the primary
	// summary from the per-device enumeration: the biggest non-NPU device
	// is the primary, exactly what the CUDA/ROCm branches already report.
	if p.GPUName == "" || p.VRAMGB <= 0 {
		primary := -1
		for i, d := range p.Accelerators {
			if d.Kind == "npu" {
				continue
			}
			if primary < 0 || p.Accelerators[i].VRAMGB > p.Accelerators[primary].VRAMGB {
				primary = i
			}
		}
		if primary >= 0 {
			if p.GPUName == "" {
				p.GPUName = p.Accelerators[primary].Name
			}
			if p.VRAMGB <= 0 {
				p.VRAMGB = p.Accelerators[primary].VRAMGB
			}
		}
	}
	p.NativeApps = nativeapps.Detect(nativeapps.SpecsFromEnv())
	if p.NativeApps == nil {
		p.NativeApps = []nativeapps.DetectedApp{}
	}

	// GP.17c — wslc probe runs only where wsl.exe exists (native Windows);
	// nil elsewhere so non-windows heartbeats are byte-identical.
	if runtime.GOOS == "windows" {
		p.Wslc = DetectWslc(runtime.GOOS, runCommandOutput, exec.LookPath)
	}

	return p
}

// runCommandOutput runs cmd and returns its combined stdout+stderr.
// Extracted for the wslc probe's injectable runner (tests pass fakes).
func runCommandOutput(cmd *exec.Cmd) ([]byte, error) {
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	err := cmd.Run()
	return buf.Bytes(), err
}

// metalQueueName is the ApiTaskQueue.queue_name a Metal-capable worker
// polls. It must match CORE/API/utils/queue_selector.py METAL_QUEUE
// ("Metal") exactly — the API enqueues a darwin native node under that
// spelling, so the worker's generic tag-uppercasing ("METAL") would never
// match a queue row.
const metalQueueName = "Metal"

// hasHardwareTag reports whether tags contains tag, case-insensitively
// (tags are lowercased by mergeHardwareTags, but a hand-set WORKER_HARDWARE_TAGS
// can arrive in any case).
func hasHardwareTag(tags []string, tag string) bool {
	for _, t := range tags {
		if strings.EqualFold(strings.TrimSpace(t), tag) {
			return true
		}
	}
	return false
}

// SupportedQueues returns the queue names this worker can handle, derived from hardware tags.
//
// Every worker polls CPU + default by default. Workers with ANY
// accelerator hardware (CUDA / ROCm / Intel / NPU / Metal / TensorRT /
// TPU / Vulkan) also poll the shared "ACCELERATOR" meta-queue. That
// meta-queue is what the wizard's multi-variant nodes route to: a
// node built for four variants is dispatched to "ACCELERATOR" and
// the scheduler's Phase 2 routing gate (gpu_variants × hardware_tags)
// then filters which specific worker gets it. Before this shared
// queue existed, a multi-variant node could only reach one backend's
// worker pool (the hero's) because process_type directly became
// queue_name — a 'base-cuda' hero would sit idle if only ROCm
// workers were online.
func (p *Profile) SupportedQueues() []string {
	// MAC.1.s8a — Metal lane. A native macOS worker (OS == darwin) whose
	// probe found Metal (hardware tag `metal`, MAC.1.s7) advertises the
	// API's METAL_QUEUE ("Metal", CORE/API/utils/queue_selector.py) and
	// opts out of the CPU + default pools: those two hold container work
	// the Mac host has no runtime for, and the queue selector routes a
	// darwin native node to "Metal" only, so subscribing to CPU/default
	// would only ever end in a refused claim. The opt-out is gated on
	// darwin — a Linux/WSL worker that carries a stray `metal` tag keeps
	// its queue list byte-identical.
	//
	// Darwin WITHOUT a metal tag (an Intel Mac whose card reports
	// _SupportsMetal=No, or Metal disabled by the driver): it has no
	// Metal queue to poll, so it keeps the historical CPU + default
	// subscription. It can still run native darwin CPU work, which
	// platform_guard matches by OS, and withholding every pool would
	// strand it idle with no way to report in.
	seen := map[string]bool{"CPU": true, "default": true}
	queues := []string{"CPU", "default"}

	metalNative := p.OS == "darwin" && hasHardwareTag(p.HardwareTags, "metal")

	hasAccelerator := false
	for _, tag := range p.HardwareTags {
		// MAC.1.s8a — on a darwin host the metal tag maps to the API's
		// Metal spelling; the generic uppercasing below would advertise
		// "METAL", which no ApiTaskQueue row is ever enqueued under.
		if metalNative && strings.EqualFold(tag, "metal") {
			hasAccelerator = true
			if !seen[metalQueueName] {
				seen[metalQueueName] = true
				queues = append(queues, metalQueueName)
			}
			continue
		}
		queueName := strings.ToUpper(tag)
		if queueName == "CPU" || queueName == "DEFAULT" {
			continue
		}
		hasAccelerator = true
		if !seen[queueName] {
			seen[queueName] = true
			queues = append(queues, queueName)
		}
	}
	if metalNative {
		// MAC.1.s8a — drop the container pools from a Metal-native worker.
		// ACCELERATOR stays: it is the wizard's multi-variant meta-queue,
		// gated at claim time by gpu_variants × hardware_tags, so a Mac
		// that gains a Metal variant is reachable through it.
		without := make([]string, 0, len(queues))
		for _, q := range queues {
			if q != "CPU" && q != "default" {
				without = append(without, q)
			}
		}
		queues = without
	}
	if hasAccelerator && !seen["ACCELERATOR"] {
		seen["ACCELERATOR"] = true
		queues = append(queues, "ACCELERATOR")
	}
	return queues
}

// deriveAutoTags returns all detected accelerator types as hardware tags.
// A system can have multiple: ["cuda", "rocm", "intel", "npu"].
// "cpu" is always included — every worker can run CPU tasks.
func deriveAutoTags(p *Profile) []string {
	if len(p.detectedAccelerators) == 0 {
		return []string{"cpu"}
	}
	// Always include cpu — every accelerator system can also do CPU work
	tags := make([]string, 0, len(p.detectedAccelerators)+1)
	tags = append(tags, p.detectedAccelerators...)
	hasCPU := false
	for _, t := range tags {
		if t == "cpu" {
			hasCPU = true
			break
		}
	}
	if !hasCPU {
		tags = append(tags, "cpu")
	}
	return tags
}

// mergeHardwareTags combines auto-detected tags with user-specified WORKER_HARDWARE_TAGS.
// Auto-detected tags come first (higher priority for variant selection).
func mergeHardwareTags(auto []string, envVar string) []string {
	seen := make(map[string]bool, len(auto))
	result := make([]string, 0, len(auto)+4)

	for _, tag := range auto {
		t := strings.ToLower(strings.TrimSpace(tag))
		if t != "" && !seen[t] {
			seen[t] = true
			result = append(result, t)
		}
	}

	if envVar != "" {
		for _, tag := range strings.Split(envVar, ",") {
			t := strings.ToLower(strings.TrimSpace(tag))
			if t != "" && !seen[t] {
				seen[t] = true
				result = append(result, t)
			}
		}
	}

	return result
}

func contains(slice []string, item string) bool {
	for _, s := range slice {
		if s == item {
			return true
		}
	}
	return false
}

// ARC70 — the ROCm branch of detectGPU never sized p.VRAMGB, so a box whose
// primary card is AMD heartbeats gpu_vram_mb=0 (heartbeat.go sends
// int(h.profile.VRAMGB * 1024)). The API's 1141 telemetry-health check
// (worker_management.py) reads that zero against the benchmark-sized
// AcceleratorProfile row and quarantines the rocm kind, so the stack-36
// deploy fit gate 409s with "GPU telemetry reported 0 VRAM while the
// benchmark-sized card says 32 GB" even though rocm-smi and the monitor's
// own sysfs sampler read the card fine. Size the primary figure here,
// sysfs-first like the Intel sampler already is, with the rocm-smi CSV
// detectGPU already ran in hand as the fallback.
var amdSizeSysfsRoot = "/sys/class/drm"

func amdPrimaryVRAMGBFromSysfs() float64 {
	return largestAMDVRAMGB(amdSizeSysfsRoot)
}

// largestAMDVRAMGB returns the biggest amdgpu card's own VRAM (GB) under
// root, or 0 when nothing is measurable. Vendor-filtered (0x1002) exactly
// like enumerateROCmFromDRI so the Arrow Lake iGPU never masquerades as the
// AMD card, and a card without mem_info_vram_total yields nothing rather
// than a fake zero.
func largestAMDVRAMGB(root string) float64 {
	entries, err := os.ReadDir(root)
	if err != nil {
		return 0
	}
	var best float64
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasPrefix(name, "card") || strings.Contains(name, "-") {
			continue
		}
	 vendorPath := root + "/" + name + "/device/vendor"
	 vendorData, err := os.ReadFile(vendorPath)
	 if err != nil {
		 continue
	 }
	 if strings.TrimSpace(string(vendorData)) != "0x1002" {
		 continue
	 }
	 totalData, err := os.ReadFile(root + "/" + name + "/device/mem_info_vram_total")
	 if err != nil {
		 continue
	 }
	 if vram, err := strconv.ParseFloat(strings.TrimSpace(string(totalData)), 64); err == nil && vram > 0 {
		 if gb := vram / (1024.0 * 1024.0 * 1024.0); gb > best {
			 best = gb
		 }
	 }
	}
	// 0 when nothing is measurable — the caller then tries the rocm-smi CSV.
	 return best
}

// rocmCSVVRAMGB returns the biggest "VRAM Total Memory (B)" figure in
// `rocm-smi --showproductname --showmeminfo vram --csv` output (GB), or 0.
// Same header-aware reader as parseROCmSMICSV (devices.go), so the sysfs and
// tool paths agree on the unit and never read the "Used" column.
func rocmCSVVRAMGB(out string) float64 {
	var best float64
	for _, d := range parseROCmSMICSV([]byte(out)) {
		if d.VRAMGB > best {
			best = d.VRAMGB
		}
	}
	return best
}

func detectGPU(p *Profile) {
	// Detect ALL accelerators — a system can have NVIDIA + AMD + Intel + NPU simultaneously.
	// GPUType/GPUName/VRAMGB report the "primary" (first found), but HardwareTags captures all.
	var detectedTypes []string

	// NVIDIA (CUDA)
	if out, err := exec.Command("nvidia-smi",
		"--query-gpu=name,memory.total",
		"--format=csv,noheader,nounits",
	).Output(); err == nil {
		lines := strings.Split(strings.TrimSpace(string(out)), "\n")
		if len(lines) > 0 {
			parts := strings.SplitN(lines[0], ", ", 2)
			if p.GPUType == "none" {
				p.GPUType = "CUDA"
				p.GPUName = strings.TrimSpace(parts[0])
				if len(parts) > 1 {
					if vram, err := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64); err == nil {
						p.VRAMGB = vram / 1024.0
					}
				}
				// Unified-memory GPUs (GX10 / DGX Spark) report memory.total as
				// "[N/A]"/"Not Supported"; size the primary figure from the shared
				// system pool (already read into p.RAMGB) so the summary isn't left
				// at 0. Matches enumerateNVIDIA's fallback for the per-device rows.
				if p.VRAMGB <= 0 {
					p.VRAMGB = p.RAMGB
				}
			}
			detectedTypes = append(detectedTypes, "cuda")
		}
	}

	// AMD (ROCm) — tool check
	if out, err := exec.Command("rocm-smi", "--showproductname", "--showmeminfo", "vram", "--csv").Output(); err == nil {
		if p.GPUType == "none" {
			p.GPUType = "ROCm"
			p.GPUName = parseROCmName(string(out))
			// ARC70 — size the PRIMARY figure too. The CUDA branch above fills
			// VRAMGB from nvidia-smi; the ROCm branch never did, so a box whose
			// primary card is AMD heartbeats gpu_vram_mb=0 (heartbeat.go:
			// int(h.profile.VRAMGB * 1024)). The API's 1141 telemetry-health
			// check (worker_management.py) reads that zero against the
			// benchmark-sized AcceleratorProfile row and quarantines the rocm
			// kind, so hardware_gap returns "The AMD (ROCm) GPU on this system
			// is out of service (GPU telemetry reported 0 VRAM while the
			// benchmark-sized card says 32 GB)" and POST /v1/stacks/36/deploy
			// 409s — while rocm-smi and the monitor's own sysfs sampler both
			// read the card fine. sysfs-first, like the Intel sampler already
			// is; the rocm-smi CSV we already have in hand is the fallback.
			if vramGB := amdPrimaryVRAMGBFromSysfs(); vramGB > 0 {
				p.VRAMGB = vramGB
			} else if vramGB := rocmCSVVRAMGB(string(out)); vramGB > 0 {
				p.VRAMGB = vramGB
			}
		}
		detectedTypes = append(detectedTypes, "rocm")
	}

	// Intel GPU — tool check
	if _, err := exec.Command("xpu-smi", "discovery").Output(); err == nil {
		if p.GPUType == "none" {
			p.GPUType = "Intel"
		}
		detectedTypes = append(detectedTypes, "intel")
	}

	// DRI vendor ID fallback (works in WSL2 where lspci is virtualized)
	// Scan /sys/class/drm/card*/device/vendor for GPU vendor IDs
	intelFound := contains(detectedTypes, "intel")
	amdFound := contains(detectedTypes, "rocm")
	if entries, err := os.ReadDir("/sys/class/drm"); err == nil {
		for _, entry := range entries {
			if !strings.HasPrefix(entry.Name(), "card") || strings.Contains(entry.Name(), "-") {
				continue
			}
			vendorPath := "/sys/class/drm/" + entry.Name() + "/device/vendor"
			if data, err := os.ReadFile(vendorPath); err == nil {
				vendor := strings.TrimSpace(string(data))
				if vendor == "0x8086" && !intelFound {
					if p.GPUType == "none" {
						p.GPUType = "Intel"
					}
					detectedTypes = append(detectedTypes, "intel")
					intelFound = true
				} else if vendor == "0x1002" && !amdFound {
					if p.GPUType == "none" {
						p.GPUType = "ROCm"
					}
					detectedTypes = append(detectedTypes, "rocm")
					amdFound = true
				}
			}
		}
	}

	// NPU (Intel Neural Processing Unit, or any /dev/accel device)
	if _, err := os.Stat("/dev/accel/accel0"); err == nil {
		detectedTypes = append(detectedTypes, "npu")
	} else if out, _ := exec.Command("lspci").Output(); strings.Contains(strings.ToLower(string(out)), "neural") ||
		strings.Contains(strings.ToLower(string(out)), "npu") ||
		strings.Contains(strings.ToLower(string(out)), "ai accelerator") {
		detectedTypes = append(detectedTypes, "npu")
	}

	// Metal (macOS). nvidia-smi/rocm-smi/xpu-smi and /sys/class/drm are all
	// absent on a Mac, so without this branch a darwin worker reports no
	// accelerator at all and the API's variant gate never routes a "metal"
	// node to it. Parsing is pure + platform-neutral (detect_darwin_parse.go)
	// so the captured fixtures test on Linux CI.
	if runtime.GOOS == "darwin" {
		if out, err := darwinProbeRunner(); err == nil && len(out) > 0 {
			metal, gpuName, _ := parseDarwinDisplays(out)
			if metal {
				if !contains(detectedTypes, "metal") {
					detectedTypes = append(detectedTypes, "metal")
				}
				if p.GPUType == "none" {
					p.GPUType = "Metal"
					p.GPUName = gpuName
				}
				// Apple Silicon has no dedicated VRAM: the GPU and CPU share one
				// pool, so the card is sized from hw.memsize (already in p.RAMGB)
				// and reported in BOTH figures. The API's overlap scheduler
				// (_is_unified_memory_worker) takes the pool as max(ram, vram),
				// never the sum, so double-reporting is the correct encoding.
				// An Intel Mac's GPU carries its own spdisplays_vram instead.
				if p.VRAMGB <= 0 {
					if cards := darwinCards(out); len(cards) > 0 {
						unified := false
						vram := 0.0
						for _, c := range cards {
							if c.Unified {
								unified = true
							}
							if c.VRAMGB > vram {
								vram = c.VRAMGB
							}
						}
						if unified && p.RAMGB > 0 {
							p.VRAMGB = p.RAMGB
						} else {
							p.VRAMGB = vram
						}
					}
				}
			}
		}
	}

	// Fallback: HOST_GPU_TYPE env var (set by startup script in container mode).
	// Also: WORKER_HARDWARE_TAGS — if the operator explicitly listed
	// accelerator tags, trust them as a detection signal even if no tool
	// succeeded. This gets the UI to render "ROCm + Intel" instead of
	// "None" when rocm-smi / xpu-smi aren't installed but the operator
	// knows what's in the box.
	if len(detectedTypes) == 0 {
		if envGPU := os.Getenv("HOST_GPU_TYPE"); envGPU != "" && envGPU != "none" {
			p.GPUType = envGPU
			p.GPUName = os.Getenv("HOST_GPU_NAME")
			if vramStr := os.Getenv("HOST_VRAM_GB"); vramStr != "" {
				if vram, err := strconv.ParseFloat(vramStr, 64); err == nil {
					p.VRAMGB = vram
				}
			}
			detectedTypes = append(detectedTypes, strings.ToLower(envGPU))
		}
	}

	// WORKER_HARDWARE_TAGS tags-as-detection fallback. If the operator
	// listed accelerator tags explicitly and NOTHING else detected a GPU,
	// infer the primary GPUType from the first accelerator-like tag. This
	// keeps workers with ROCm/Intel hardware but no rocm-smi/xpu-smi from
	// reporting GPUType="none" just because the tooling is absent.
	if p.GPUType == "none" {
		for _, tag := range strings.Split(os.Getenv("WORKER_HARDWARE_TAGS"), ",") {
			t := strings.ToLower(strings.TrimSpace(tag))
			switch t {
			case "cuda":
				p.GPUType = "CUDA"
			case "rocm":
				p.GPUType = "ROCm"
			case "intel":
				p.GPUType = "Intel"
			case "npu":
				// NPU isn't a GPU per se; leave GPUType as-is.
				continue
			default:
				continue
			}
			if !contains(detectedTypes, t) {
				detectedTypes = append(detectedTypes, t)
			}
			break
		}
	}

	// Store all detected types so deriveAutoTags can use them
	p.detectedAccelerators = detectedTypes
}

func detectCloud(p *Profile) {
	client := &http.Client{Timeout: 500 * time.Millisecond}

	// AWS
	if resp, err := client.Get("http://169.254.169.254/latest/meta-data/instance-type"); err == nil {
		defer resp.Body.Close()
		if resp.StatusCode == 200 {
			if body, err := io.ReadAll(resp.Body); err == nil && len(body) > 0 {
				p.CloudProvider = "aws"
				p.InstanceType = strings.TrimSpace(string(body))
				return
			}
		}
	}

	// GCP
	req, _ := http.NewRequest("GET", "http://metadata.google.internal/computeMetadata/v1/instance/machine-type", nil)
	if req != nil {
		req.Header.Set("Metadata-Flavor", "Google")
		if resp, err := client.Do(req); err == nil {
			defer resp.Body.Close()
			if resp.StatusCode == 200 {
				if body, err := io.ReadAll(resp.Body); err == nil && len(body) > 0 {
					parts := strings.Split(strings.TrimSpace(string(body)), "/")
					p.CloudProvider = "gcp"
					p.InstanceType = parts[len(parts)-1]
					return
				}
			}
		}
	}

	// Azure
	req2, _ := http.NewRequest("GET", "http://169.254.169.254/metadata/instance/compute/vmSize?api-version=2021-02-01&format=text", nil)
	if req2 != nil {
		req2.Header.Set("Metadata", "true")
		if resp, err := client.Do(req2); err == nil {
			defer resp.Body.Close()
			if resp.StatusCode == 200 {
				if body, err := io.ReadAll(resp.Body); err == nil && len(body) > 0 {
					p.CloudProvider = "azure"
					p.InstanceType = strings.TrimSpace(string(body))
					return
				}
			}
		}
	}
}

func detectRAM() float64 {
	switch runtime.GOOS {
	case "linux":
		out, err := exec.Command("grep", "MemTotal", "/proc/meminfo").Output()
		if err != nil {
			return 0
		}
		// "MemTotal:       16384000 kB"
		fields := strings.Fields(string(out))
		if len(fields) >= 2 {
			if kb, err := strconv.ParseFloat(fields[1], 64); err == nil {
				return kb / 1024.0 / 1024.0
			}
		}

	case "darwin":
		out, err := exec.Command("sysctl", "-n", "hw.memsize").Output()
		if err != nil {
			return 0
		}
		if b, err := strconv.ParseFloat(strings.TrimSpace(string(out)), 64); err == nil {
			return b / 1024.0 / 1024.0 / 1024.0
		}

	case "windows":
		out, err := exec.Command("wmic", "ComputerSystem", "get", "TotalPhysicalMemory", "/value").Output()
		if err != nil {
			return 0
		}
		for _, line := range strings.Split(string(out), "\n") {
			if strings.HasPrefix(strings.TrimSpace(line), "TotalPhysicalMemory=") {
				val := strings.TrimPrefix(strings.TrimSpace(line), "TotalPhysicalMemory=")
				if b, err := strconv.ParseFloat(val, 64); err == nil {
					return b / 1024.0 / 1024.0 / 1024.0
				}
			}
		}
	}

	return 0
}

// detectTools checks PATH for known tools used by processing containers.
func detectTools() []string {
	known := []string{"ffmpeg", "blender", "3dsmax", "magick", "inkscape"}
	var found []string
	for _, tool := range known {
		if _, err := exec.LookPath(tool); err == nil {
			found = append(found, tool)
		}
	}
	return found
}

// parseROCmName returns the first card's product name from rocm-smi CSV
// output, or "AMD GPU". It never returns the CSV header line (worker-r9700
// once reported its gpu_name as "device,VRAM Total Memory (B),...").
func parseROCmName(csv string) string {
	for _, d := range parseROCmSMICSV([]byte(csv)) {
		if d.Name != "" && !strings.Contains(d.Name, ",") {
			return d.Name
		}
	}
	return "AMD GPU"
}
