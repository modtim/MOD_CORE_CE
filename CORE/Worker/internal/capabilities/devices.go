package capabilities

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// AcceleratorDevice describes a single physical accelerator (GPU, NPU, etc.)
// detected on this worker. The full list is sent in each heartbeat so the
// API can upsert AcceleratorProfile rows.
type AcceleratorDevice struct {
	Kind          string  `json:"kind"`  // cuda | rocm | intel | npu
	Index         int     `json:"index"` // ordinal within kind on this host
	Name          string  `json:"name"`  // marketing name, e.g. "NVIDIA A100"
	UUID          string  `json:"uuid,omitempty"`
	VendorID      string  `json:"vendor_id,omitempty"`
	DeviceID      string  `json:"device_id,omitempty"`
	PCIBusID      string  `json:"pci_bus_id,omitempty"`
	DriverVersion string  `json:"driver_version,omitempty"`
	VRAMGB        float64 `json:"vram_gb,omitempty"`
}

// EnumerateAccelerators discovers every individual GPU / NPU on this host.
// Unlike detectGPU (which finds the primary type and populates GPUType),
// this enumerates ALL devices with stable identifiers so the API can track
// each one independently for per-device benchmarking.
//
// Falls back to WORKER_HARDWARE_TAGS when tool-based enumeration finds
// nothing — if the operator says "I have ROCm + Intel", create placeholder
// device entries so the API can upsert AcceleratorProfile rows and dispatch
// benchmarks. The actual device details (VRAM, UUID, etc.) get populated
// once the benchmark container runs and queries the hardware directly.
func EnumerateAccelerators() []AcceleratorDevice {
	var devices []AcceleratorDevice
	devices = append(devices, enumerateNVIDIA()...)
	devices = append(devices, enumerateROCm()...)
	devices = append(devices, enumerateIntel()...)
	devices = append(devices, enumerateNPU()...)

	// Fallback 1: WORKER_ACCELERATOR_DEVICES — explicit operator-specified
	// device list when tool detection fails. Format:
	//   WORKER_ACCELERATOR_DEVICES=rocm:2,intel:1,npu:1
	// Creates N devices per kind with auto-indexed entries. Supports
	// multiple cards of the same type (e.g. 2x ROCm GPUs).
	if len(devices) == 0 {
		if accelEnv := os.Getenv("WORKER_ACCELERATOR_DEVICES"); accelEnv != "" {
			for _, entry := range strings.Split(accelEnv, ",") {
				entry = strings.TrimSpace(entry)
				parts := strings.SplitN(entry, ":", 2)
				kind := strings.ToLower(strings.TrimSpace(parts[0]))
				if kind == "" || kind == "cpu" || kind == "default" {
					continue
				}
				count := 1
				if len(parts) > 1 {
					if n, err := strconv.Atoi(strings.TrimSpace(parts[1])); err == nil && n > 0 {
						count = n
					}
				}
				for i := 0; i < count; i++ {
					devices = append(devices, AcceleratorDevice{
						Kind:  kind,
						Index: i,
						Name:  kind + " (operator-specified, details pending benchmark)",
					})
				}
			}
		}
	}

	// Fallback 2: WORKER_HARDWARE_TAGS — one device per unique kind tag.
	// Less precise (doesn't know about multiple cards of the same type)
	// but still creates rows so the API can dispatch benchmarks.
	if len(devices) == 0 {
		seenKinds := make(map[string]bool)
		for _, tag := range strings.Split(os.Getenv("WORKER_HARDWARE_TAGS"), ",") {
			kind := strings.ToLower(strings.TrimSpace(tag))
			if kind == "" || kind == "cpu" || kind == "default" || seenKinds[kind] {
				continue
			}
			seenKinds[kind] = true
			devices = append(devices, AcceleratorDevice{
				Kind:  kind,
				Index: 0,
				Name:  kind + " (from tag, details pending benchmark)",
			})
		}
	}

	return devices
}

// Injection point for enumerateNVIDIA so the unified-memory VRAM fallback can
// be unit tested with captured nvidia-smi output (no real GPU in CI).
var nvidiaSMIRunner = func() ([]byte, error) {
	return exec.Command("nvidia-smi",
		"--query-gpu=index,uuid,name,memory.total,pci.bus_id,driver_version",
		"--format=csv,noheader,nounits",
	).Output()
}

// readMemTotalGB returns the host's total system RAM in GB from a
// /proc/meminfo-shaped file, or 0 when it can't be read or parsed.
func readMemTotalGB(path string) float64 {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(line, "MemTotal:") {
			continue
		}
		fields := strings.Fields(line) // "MemTotal: 16384000 kB"
		if len(fields) >= 2 {
			if kb, err := strconv.ParseFloat(fields[1], 64); err == nil {
				return kb / 1024.0 / 1024.0
			}
		}
	}
	return 0
}

// nvidiaUnifiedMemoryGB returns the host's total system RAM in GB.
//
// On a unified-memory GPU (NVIDIA GX10 / DGX Spark, Grace-Blackwell, and the
// like) nvidia-smi reports memory.total as "[N/A]" or "Not Supported" — the GPU
// has no dedicated VRAM; it and the CPU share one memory pool. The per-GPU
// figure therefore yields nothing, and a card stored at vram_gb NULL is treated
// as a 0 GB card by the API's claim-time fit guard (worker_cards skips unsized
// cards), so the box is never claimed for any GPU work and its benchmark burns
// the whole profiling deadline. Sizing the card to the system total RAM is the
// safe, sane value: it is the memory a CUDA context on the unified pool can
// actually address, and it unblocks both profiling and placement without
// over-claiming.
//
// Injection point so tests pin a fixed number instead of reading /proc/meminfo.
var nvidiaUnifiedMemoryGB = func() float64 {
	return readMemTotalGB("/proc/meminfo")
}

func enumerateNVIDIA() []AcceleratorDevice {
	out, err := nvidiaSMIRunner()
	if err != nil {
		return nil
	}

	var devices []AcceleratorDevice
	unifiedGB := 0.0 // lazily resolved once, for cards with no per-GPU figure
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		parts := strings.SplitN(line, ", ", 6)
		if len(parts) < 3 {
			continue
		}
		idx, _ := strconv.Atoi(strings.TrimSpace(parts[0]))
		d := AcceleratorDevice{
			Kind:  "cuda",
			Index: idx,
			UUID:  strings.TrimSpace(parts[1]),
			Name:  strings.TrimSpace(parts[2]),
		}
		if len(parts) > 3 {
			if vram, err := strconv.ParseFloat(strings.TrimSpace(parts[3]), 64); err == nil {
				d.VRAMGB = vram / 1024.0
			}
		}
		// Unified-memory GPUs report memory.total as "[N/A]"/"Not Supported";
		// size the card from the shared system pool instead of leaving it unsized
		// (an unsized card is refused by the API's claim guard and never profiled).
		if d.VRAMGB <= 0 {
			if unifiedGB == 0 {
				unifiedGB = nvidiaUnifiedMemoryGB()
			}
			d.VRAMGB = unifiedGB
		}
		if len(parts) > 4 {
			d.PCIBusID = strings.TrimSpace(parts[4])
		}
		if len(parts) > 5 {
			d.DriverVersion = strings.TrimSpace(parts[5])
		}
		devices = append(devices, d)
	}
	return devices
}

// Injection points for enumerateROCm so the rocm-smi -> rocminfo -> DRI
// fallback chain can be unit tested without the tools or a WSL kernel.
var (
	rocmSMIRunner = func() ([]byte, error) {
		return exec.Command("rocm-smi", "--showproductname", "--showuniqueid", "--showmeminfo", "vram", "--csv").Output()
	}
	rocminfoRunner    = runROCmInfo
	rocmHostIsWSL     = rocmWSLHost
	rocmDRIEnumerator = enumerateROCmFromDRI
)

// enumerateROCm lists AMD GPUs. Order of sources:
//
//  1. rocm-smi — the native-Linux path. Used as-is when it succeeds and every
//     device it reports carries a VRAM figure.
//  2. rocminfo with HSA_ENABLE_DXG_DETECTION=1 — WSL2 only. Under WSL the GPU
//     is reached through /dev/dxg + librocdxg; there is no amdgpu sysfs node
//     and rocm-smi either fails or reports no VRAM, which left the API with a
//     "ROCM #0" row at vram_gb NULL that the claim-time fit guard treats as a
//     0 GB card (so it refused every GPU task). rocminfo's HSA agent dump does
//     carry the marketing name and the GPU's GLOBAL coarse-grained pool size.
//  3. /sys/class/drm vendor scan — only when rocm-smi failed outright and
//     rocminfo did not produce anything better.
func enumerateROCm() []AcceleratorDevice {
	out, err := rocmSMIRunner()
	smiOK := err == nil
	var smi []AcceleratorDevice
	if smiOK {
		smi = parseROCmSMICSV(out)
		if !rocmDevicesMissingVRAM(smi) {
			return smi
		}
	}

	if rocmHostIsWSL() {
		if info := rocminfoDevices(); len(info) > 0 {
			// rocm-smi may still know UUIDs rocminfo does not ("GPU-XX" on
			// APUs); carry them over when both tools agree on the card count.
			if len(smi) == len(info) {
				for i := range info {
					if info[i].UUID == "" {
						info[i].UUID = smi[i].UUID
					}
				}
			}
			return info
		}
	}

	if !smiOK {
		return rocmDRIEnumerator()
	}
	return smi
}

// rocmDevicesMissingVRAM reports whether a rocm-smi result is unusable for
// placement: no devices at all, or any device without a VRAM figure.
func rocmDevicesMissingVRAM(devs []AcceleratorDevice) bool {
	if len(devs) == 0 {
		return true
	}
	for _, d := range devs {
		if d.VRAMGB <= 0 {
			return true
		}
	}
	return false
}

// parseROCmSMICSV parses `rocm-smi --showproductname --showuniqueid
// --showmeminfo vram --csv` output (header line skipped).
func parseROCmSMICSV(out []byte) []AcceleratorDevice {
	var devices []AcceleratorDevice
	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	idx := 0
	for _, line := range lines[1:] { // skip header
		parts := strings.SplitN(line, ",", 4)
		if len(parts) < 1 {
			continue
		}
		d := AcceleratorDevice{
			Kind:  "rocm",
			Index: idx,
			Name:  strings.TrimSpace(parts[0]),
		}
		if len(parts) > 1 {
			d.UUID = strings.TrimSpace(parts[1])
		}
		if len(parts) > 2 {
			if vram, err := strconv.ParseFloat(strings.TrimSpace(parts[2]), 64); err == nil {
				d.VRAMGB = vram / (1024.0 * 1024.0 * 1024.0) // bytes → GB
			}
		}
		devices = append(devices, d)
		idx++
	}
	return devices
}

// rocmWSLHost reports whether AMD GPUs on this host are reached through the
// WSL2 DXG bridge rather than amdgpu/kfd.
func rocmWSLHost() bool {
	if _, err := os.Stat("/dev/dxg"); err == nil {
		return true
	}
	return isWSL()
}

// rocminfoDevices runs rocminfo and parses it. rocminfo can exit non-zero
// after printing a usable agent list (e.g. a trailing agent it cannot open),
// so the output is parsed whenever there is any.
func rocminfoDevices() []AcceleratorDevice {
	out, _ := rocminfoRunner()
	if len(out) == 0 {
		return nil
	}
	return parseROCmInfo(string(out))
}

// runROCmInfo executes rocminfo (PATH first, then /opt/rocm/bin/rocminfo)
// with HSA_ENABLE_DXG_DETECTION=1 so the HSA runtime enumerates GPUs exposed
// through /dev/dxg (librocdxg). Harmless on native Linux.
func runROCmInfo() ([]byte, error) {
	bin, err := exec.LookPath("rocminfo")
	if err != nil {
		bin = "/opt/rocm/bin/rocminfo"
		if _, statErr := os.Stat(bin); statErr != nil {
			return nil, statErr
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin)
	cmd.Env = append(os.Environ(), "HSA_ENABLE_DXG_DETECTION=1")
	return cmd.Output()
}

// parseROCmInfo turns rocminfo's HSA agent dump into ROCm accelerator
// devices. Only agents with "Device Type: GPU" are returned, indexed 0..n in
// output order. Per agent:
//
//   - Name       ← "Marketing Name" (falls back to the agent "Name", e.g.
//     gfx1151, when the marketing name is blank)
//   - VRAMGB     ← Size of the agent's FIRST pool whose Segment is
//     "GLOBAL; FLAGS: COARSE GRAINED" (device-local memory; on an APU this
//     is the carve-out/GTT the driver grants the GPU). Converted to bytes and
//     divided by 1024^3, the same GB convention as the rocm-smi path.
//   - VendorID   ← "0x1002" when Vendor Name is AMD
//   - DeviceID   ← "Chip ID" hex (e.g. 0x1586), the same PCI device id the
//     /sys/class/drm fallback reports, so benchmark-strategy lookups by
//     vendor/device keep matching
//   - UUID       ← "Uuid" unless it is the "GPU-XX" placeholder
//
// The gfx target (agent "Name") has no field of its own in the heartbeat
// payload, so it is used only as a name fallback.
func parseROCmInfo(text string) []AcceleratorDevice {
	type agent struct {
		name, marketing, vendor, uuid, chipID, devType string
		vramGB                                         float64
	}
	var agents []agent
	var cur *agent
	inPools, inISA, poolCoarse := false, false, false

	for _, raw := range strings.Split(text, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "***") {
			continue
		}
		if isAgentHeader(line) {
			agents = append(agents, agent{})
			cur = &agents[len(agents)-1]
			inPools, inISA, poolCoarse = false, false, false
			continue
		}
		if cur == nil {
			continue // HSA system attributes preamble
		}
		switch {
		case line == "Pool Info:":
			inPools, inISA = true, false
			continue
		case line == "ISA Info:":
			inPools, inISA = false, true
			continue
		case line == "Cache Info:":
			continue
		case inPools && strings.HasPrefix(line, "Pool ") && !strings.Contains(line, ":"):
			poolCoarse = false
			continue
		}

		colon := strings.Index(line, ":")
		if colon < 0 {
			continue
		}
		key := strings.TrimSpace(line[:colon])
		val := strings.TrimSpace(line[colon+1:])

		if inPools {
			switch key {
			case "Segment":
				seg := strings.ToUpper(strings.Join(strings.Fields(val), " "))
				poolCoarse = strings.HasPrefix(seg, "GLOBAL") && strings.Contains(seg, "COARSE GRAINED")
			case "Size":
				if poolCoarse && cur.vramGB == 0 {
					cur.vramGB = parseROCmInfoSizeGB(val)
				}
			}
			continue
		}
		if inISA {
			continue
		}
		switch key {
		case "Name":
			if cur.name == "" {
				cur.name = val
			}
		case "Marketing Name":
			cur.marketing = val
		case "Vendor Name":
			cur.vendor = val
		case "Uuid":
			cur.uuid = val
		case "Chip ID":
			cur.chipID = val
		case "Device Type":
			cur.devType = strings.ToUpper(val)
		}
	}

	var devices []AcceleratorDevice
	for _, a := range agents {
		if a.devType != "GPU" {
			continue
		}
		d := AcceleratorDevice{
			Kind:   "rocm",
			Index:  len(devices),
			Name:   a.marketing,
			VRAMGB: a.vramGB,
		}
		if d.Name == "" {
			d.Name = a.name
		}
		if u := strings.ToUpper(a.uuid); u != "" && u != "GPU-XX" {
			d.UUID = a.uuid
		}
		if strings.EqualFold(a.vendor, "AMD") {
			d.VendorID = "0x1002"
		}
		d.DeviceID = rocminfoChipIDHex(a.chipID)
		devices = append(devices, d)
	}
	return devices
}

// isAgentHeader matches rocminfo's "Agent 2" section headers.
func isAgentHeader(line string) bool {
	rest, ok := strings.CutPrefix(line, "Agent ")
	if !ok {
		return false
	}
	_, err := strconv.Atoi(strings.TrimSpace(rest))
	return err == nil
}

// parseROCmInfoSizeGB parses a rocminfo pool size such as
// "116890624(0x6f79000) KB" into GB (bytes / 1024^3). Returns 0 when the
// value is unparseable.
func parseROCmInfoSizeGB(val string) float64 {
	end := 0
	for end < len(val) && val[end] >= '0' && val[end] <= '9' {
		end++
	}
	if end == 0 {
		return 0
	}
	n, err := strconv.ParseUint(val[:end], 10, 64)
	if err != nil {
		return 0
	}
	unit := "KB"
	if fields := strings.Fields(val[end:]); len(fields) > 0 {
		unit = strings.ToUpper(fields[len(fields)-1])
	}
	mult := 1024.0 // rocminfo reports pool sizes in KB
	switch unit {
	case "B":
		mult = 1
	case "MB":
		mult = 1024 * 1024
	case "GB":
		mult = 1024 * 1024 * 1024
	}
	return float64(n) * mult / (1024.0 * 1024.0 * 1024.0)
}

// rocminfoChipIDHex turns "5510(0x1586)" into "0x1586". Zero/blank → "".
func rocminfoChipIDHex(val string) string {
	if open := strings.Index(val, "(0x"); open >= 0 {
		if close := strings.Index(val[open:], ")"); close > 0 {
			hex := strings.ToLower(val[open+1 : open+close])
			if hex != "0x0" {
				return hex
			}
			return ""
		}
	}
	if n, err := strconv.ParseUint(strings.TrimSpace(val), 10, 64); err == nil && n > 0 {
		return "0x" + strconv.FormatUint(n, 16)
	}
	return ""
}

// enumerateROCmFromDRI falls back to /sys/class/drm for AMD cards when
// rocm-smi isn't installed. Less detail but still identifies each card.
func enumerateROCmFromDRI() []AcceleratorDevice {
	entries, err := os.ReadDir("/sys/class/drm")
	if err != nil {
		return nil
	}

	var devices []AcceleratorDevice
	idx := 0
	for _, entry := range entries {
		if !strings.HasPrefix(entry.Name(), "card") || strings.Contains(entry.Name(), "-") {
			continue
		}
		vendorPath := filepath.Join("/sys/class/drm", entry.Name(), "device", "vendor")
		data, err := os.ReadFile(vendorPath)
		if err != nil {
			continue
		}
		vendor := strings.TrimSpace(string(data))
		if vendor != "0x1002" { // AMD vendor ID
			continue
		}

		d := AcceleratorDevice{
			Kind:     "rocm",
			Index:    idx,
			VendorID: vendor,
		}
		// Try to read device_id
		if devData, err := os.ReadFile(filepath.Join("/sys/class/drm", entry.Name(), "device", "device")); err == nil {
			d.DeviceID = strings.TrimSpace(string(devData))
		}
		devices = append(devices, d)
		idx++
	}
	return devices
}

// Injection point for enumerateIntel so the xpu-smi name-enrichment merge can
// be unit tested with captured discovery output (no Intel GPU in CI).
var intelSMIRunner = func() ([]byte, error) {
	return exec.Command("xpu-smi", "discovery", "--json").Output()
}

// enumerateIntel lists Intel GPUs. /sys/class/drm is the SIZE source and is
// ALWAYS consulted; xpu-smi's discovery JSON carries marketing names but no
// per-device VRAM figure, so it is only ever a NAME source.
//
// That was the 1900ARC5 blocker: on a native-Linux box that has xpu-smi
// installed (example-host, the Arc B60 host), the old code returned the xpu-smi
// rows and never reached enumerateIntelFromDRI, so every "intel" row was
// upserted with vram_gb NULL. The API's claim guard (gpu_reservation.worker_
// cards) skips unsized cards — "an accelerator with unknown VRAM stays
// ASSIGNED forever" — so the B60 was never benchmarked and never claimed.
//
// The size must never be derived from host RAM here. example-host is multi-vendor
// (Arc B60 + Arrow Lake iGPU + an AMD R9000 exposed as an OpenCL platform),
// and the other platforms report the whole system pool (~235 GiB) rather than
// a card's own memory, so a pool-derived figure would over-size the B60 by
// ~10x and let a 24 GB card pass a 200 GB placement floor. Every source below
// is per-card: the Xe driver's own tile accounting, a published-spec table
// keyed by the card's own PCI device id, or that card's own prefetchable BAR.
func enumerateIntel() []AcceleratorDevice {
	dri := enumerateIntelFromDRI()
	out, err := intelSMIRunner()
	if err != nil {
		return dri
	}

	var names []string
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if strings.Contains(line, "\"device_name\"") {
			name := extractJSONStringValue(line)
			if name != "" {
				names = append(names, name)
			}
		}
	}

	if len(dri) == 0 {
		// No /sys/class/drm view (a container without /sys mounted, or a
		// Level-Zero-only userspace): keep the name-only rows so the API can
		// still upsert the device and dispatch a benchmark, which fills the
		// details from the device itself.
		var nameless []AcceleratorDevice
		for i, name := range names {
			nameless = append(nameless, AcceleratorDevice{
				Kind:  "intel",
				Index: i,
				Name:  name,
			})
		}
		return nameless
	}

	// Both sources agree on the card count → the vendor tool's marketing name
	// wins over our generic per-device-id label; the sysfs row keeps its VRAM,
	// uuid and pci_bus_id.
	if len(names) == len(dri) {
		for i := range dri {
			if names[i] != "" {
				dri[i].Name = names[i]
			}
		}
	}
	return dri
}

func enumerateIntelFromDRI() []AcceleratorDevice {
	entries, err := os.ReadDir("/sys/class/drm")
	if err != nil {
		return nil
	}

	var devices []AcceleratorDevice
	idx := 0
	for _, entry := range entries {
		if !strings.HasPrefix(entry.Name(), "card") || strings.Contains(entry.Name(), "-") {
			continue
		}
		vendorPath := filepath.Join("/sys/class/drm", entry.Name(), "device", "vendor")
		data, err := os.ReadFile(vendorPath)
		if err != nil {
			continue
		}
		vendor := strings.TrimSpace(string(data))
		if vendor != "0x8086" { // Intel vendor ID
			continue
		}

		d := AcceleratorDevice{
			Kind:     "intel",
			Index:    idx,
			VendorID: vendor,
		}
		if devData, err := os.ReadFile(filepath.Join("/sys/class/drm", entry.Name(), "device", "device")); err == nil {
			d.DeviceID = strings.TrimSpace(string(devData))
		}
		// VRAM detection, in order of accuracy:
		//
		// 1. Intel Xe driver (kernel ≥ 6.8) — exposes the *exact* VRAM
		//    bytes at /sys/class/drm/cardN/device/tile0/vram0/total.
		//    This is what the driver itself reports.
		// 2. Known device-ID → VRAM table — for cards we've actually
		//    deployed against (Arc A/B-series, Data Center GPU), hard-
		//    code the spec'd VRAM. Authoritative for those models even
		//    on kernels that don't expose Xe sysfs yet.
		// 3. PCI prefetchable BAR — what the BIOS mapped. Always rounded
		//    up to the next power of two (a 24 GB card shows 32 GB).
		//    Used as a last resort when neither source above works.
		// 4. iGPUs — no prefetchable BAR, no Xe tile, not in the table
		//    → correctly 0 (iGPUs share system RAM, no dedicated VRAM).
		cardPath := filepath.Join("/sys/class/drm", entry.Name())
		if vramXe := readIntelVRAMFromXe(cardPath); vramXe > 0 {
			d.VRAMGB = vramXe
		} else if vramKnown := knownIntelVRAMGB(d.DeviceID); vramKnown > 0 {
			d.VRAMGB = vramKnown
		} else {
			d.VRAMGB = readVRAMFromPCIResource(cardPath)
		}
		d.Name = intelDeviceName(d.DeviceID)
		devices = append(devices, d)
		idx++
	}
	return devices
}

// readIntelVRAMFromXe reads the exact VRAM size the Intel Xe kernel
// driver reports for a DRM card. Xe (kernel ≥ 6.8) exposes per-tile
// memory info under /sys/class/drm/cardN/device/tile0/vram0/total —
// that's the authoritative byte count straight from the driver, not
// the rounded-up PCI BAR size. Falls back to zero when the file isn't
// present (i915 driver, older kernel, or non-Xe stack) so the caller
// can route to the BAR-based approximation.
//
// Multi-tile GPUs (Data Center GPU Max has up to 2 tiles) sum across
// every tileN/vram0/total file found.
func readIntelVRAMFromXe(cardDir string) float64 {
	var totalBytes uint64
	entries, err := os.ReadDir(filepath.Join(cardDir, "device"))
	if err != nil {
		return 0
	}
	for _, e := range entries {
		name := e.Name()
		if !strings.HasPrefix(name, "tile") {
			continue
		}
		// Each tile has vram0/total (bytes). Some driver builds
		// wrap it under a `memory/` directory; check both.
		candidates := []string{
			filepath.Join(cardDir, "device", name, "vram0", "total"),
			filepath.Join(cardDir, "device", name, "memory", "vram0", "total"),
		}
		for _, p := range candidates {
			data, err := os.ReadFile(p)
			if err != nil {
				continue
			}
			n, err := strconv.ParseUint(strings.TrimSpace(string(data)), 10, 64)
			if err == nil && n > 0 {
				totalBytes += n
				break
			}
		}
	}
	return float64(totalBytes) / (1024.0 * 1024.0 * 1024.0)
}

// readVRAMFromPCIResource parses /sys/class/drm/cardN/device/resource and
// returns the size of the largest prefetchable memory region in GB.
// On a discrete Arc / Xe GPU with resizeable BAR, that's the full VRAM.
// On an iGPU (or any device whose BARs are all plain MMIO < 512 MB),
// this returns 0 — which is the right answer because iGPUs have no
// dedicated VRAM, they borrow system RAM on demand.
//
// PCI resource flag bits we care about:
//
//	0x100 = IORESOURCE_MEM
//	0x200 = IORESOURCE_PREFETCH (VRAM is prefetchable; plain MMIO isn't)
//
// Each line in the resource file has three hex fields: start, end, flags.
// Unused BARs have start = end = 0.
func readVRAMFromPCIResource(cardDir string) float64 {
	data, err := os.ReadFile(filepath.Join(cardDir, "device", "resource"))
	if err != nil {
		return 0
	}
	var largest uint64
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 3 {
			continue
		}
		start, e1 := strconv.ParseUint(strings.TrimPrefix(fields[0], "0x"), 16, 64)
		end, e2 := strconv.ParseUint(strings.TrimPrefix(fields[1], "0x"), 16, 64)
		flags, e3 := strconv.ParseUint(strings.TrimPrefix(fields[2], "0x"), 16, 64)
		if e1 != nil || e2 != nil || e3 != nil || start == 0 || end <= start {
			continue
		}
		if flags&0x200 == 0 { // not prefetchable → not VRAM
			continue
		}
		size := end - start + 1
		if size < 512*1024*1024 { // skip anything under 512 MB (MMIO, etc.)
			continue
		}
		if size > largest {
			largest = size
		}
	}
	return float64(largest) / (1024.0 * 1024.0 * 1024.0)
}

// knownIntelVRAMGB returns the exact VRAM for Intel GPU device IDs we've
// actually deployed against. Preferred over the PCI-BAR-based reader
// because PCI BARs are power-of-two sized and overstate a 24 GB card
// as 32 GB. Only used when the kernel doesn't expose Xe-driver sysfs
// paths (older kernels, i915 discrete).
//
// Numbers are per Intel's published specs. Verified against:
//   - Arc A-series:  https://www.intel.com/content/www/us/en/products/docs/discrete-gpus/arc/desktop/a-series/overview.html
//   - Arc B-series / Arc Pro B-series (Battlemage): product sheets, Dec 2024.
//   - Data Center GPU Max (Ponte Vecchio): product brief.
//
// An unknown device ID returns 0 so the caller can fall through to the
// BAR reader. That's safer than guessing a number.
func knownIntelVRAMGB(deviceID string) float64 {
	switch strings.ToLower(strings.TrimSpace(deviceID)) {
	// Arc Pro B-series (Battlemage, workstation) — launched late 2024.
	case "0xe211": // Arc Pro B60 — 24 GB GDDR6
		return 24.0
	case "0xe212": // Arc Pro B50 — 16 GB GDDR6
		return 16.0
	// Arc B-series (Battlemage, consumer) — launched Dec 2024.
	case "0xe20b", "0xe20c": // Arc B580 — 12 GB GDDR6
		return 12.0
	case "0xe20d": // Arc B570 — 10 GB GDDR6
		return 10.0
	// Arc A-series (Alchemist, consumer).
	case "0x56a0": // Arc A770 16GB
		return 16.0
	case "0x56a1": // Arc A750 / A770 8GB (shared device ID, common SKU is 8 GB)
		return 8.0
	case "0x56a5": // Arc A380 — 6 GB GDDR6
		return 6.0
	case "0x56a6": // Arc A310 — 4 GB GDDR6
		return 4.0
	// Data Center GPU Max (Ponte Vecchio, 1-tile and 2-tile variants).
	case "0x0bd5", "0x0bd6", "0x0bd7": // Max 1100 — 48 GB HBM2e
		return 48.0
	case "0x0bd9", "0x0bda", "0x0bdb": // Max 1550 — 128 GB HBM2e
		return 128.0
	// Data Center GPU Flex (Arctic Sound M).
	case "0x56c0": // Flex 170 — 16 GB
		return 16.0
	case "0x56c1": // Flex 140 — 2×12 GB = 24 GB
		return 24.0
	}
	return 0
}

// Minimal Intel PCI device-ID → marketing-name lookup so the Dashboard
// shows "Intel Arc Pro B60" instead of a blank name. xpu-smi would
// normally fill this in; this fallback kicks in when xpu-smi isn't
// installed (most remote workers). Only covers the handful of device
// IDs we've actually deployed against — unknown IDs fall through to
// "Intel GPU (<id>)" so operators can file an issue with the ID.
func intelDeviceName(deviceID string) string {
	id := strings.ToLower(strings.TrimSpace(deviceID))
	switch id {
	case "0xe20b", "0xe20c", "0xe20d", "0xe210", "0xe211", "0xe212", "0xe215", "0xe216":
		return "Intel Arc B-series (Battlemage)"
	case "0x56a0", "0x56a1", "0x56a2", "0x56a5", "0x56a6", "0x5690", "0x5691", "0x5692":
		return "Intel Arc A-series (Alchemist)"
	case "0x0bd5", "0x0bd6", "0x0bd7", "0x0bd9", "0x0bda", "0x0bdb":
		return "Intel Data Center GPU Max"
	case "0x56c0", "0x56c1":
		return "Intel Data Center GPU Flex"
	case "0x7d67", "0x7d55":
		return "Intel Graphics (Meteor Lake / Arrow Lake iGPU)"
	case "0x7d40", "0x7d45":
		return "Intel Graphics (Raptor Lake iGPU)"
	case "":
		return ""
	default:
		return "Intel GPU (" + deviceID + ")"
	}
}

func enumerateNPU() []AcceleratorDevice {
	var devices []AcceleratorDevice
	idx := 0

	// /dev/accel/accel* — neural processing units
	matches, _ := filepath.Glob("/dev/accel/accel*")
	for _, path := range matches {
		name := filepath.Base(path)
		d := AcceleratorDevice{
			Kind:  "npu",
			Index: idx,
			Name:  name,
		}
		// Try to read device name from sysfs
		sysPath := filepath.Join("/sys/class/accel", name, "device", "device")
		if data, err := os.ReadFile(sysPath); err == nil {
			d.DeviceID = strings.TrimSpace(string(data))
		}
		devices = append(devices, d)
		idx++
	}
	return devices
}

// NVIDIA vendor ID for DRI fallback (used by the per-device enumeration
// as a last resort when nvidia-smi is missing but the card is visible in sysfs).
func enumerateNVIDIAFromDRI() []AcceleratorDevice {
	entries, err := os.ReadDir("/sys/class/drm")
	if err != nil {
		return nil
	}

	var devices []AcceleratorDevice
	idx := 0
	for _, entry := range entries {
		if !strings.HasPrefix(entry.Name(), "card") || strings.Contains(entry.Name(), "-") {
			continue
		}
		vendorPath := filepath.Join("/sys/class/drm", entry.Name(), "device", "vendor")
		data, err := os.ReadFile(vendorPath)
		if err != nil {
			continue
		}
		vendor := strings.TrimSpace(string(data))
		if vendor != "0x10de" { // NVIDIA vendor ID
			continue
		}

		d := AcceleratorDevice{
			Kind:     "cuda",
			Index:    idx,
			VendorID: vendor,
		}
		if devData, err := os.ReadFile(filepath.Join("/sys/class/drm", entry.Name(), "device", "device")); err == nil {
			d.DeviceID = strings.TrimSpace(string(devData))
		}
		devices = append(devices, d)
		idx++
	}
	return devices
}

func extractJSONStringValue(line string) string {
	// Crude extractor for `"key": "value"` — avoids importing encoding/json
	// in the capabilities package which should stay dependency-light.
	idx := strings.Index(line, ":")
	if idx < 0 {
		return ""
	}
	val := strings.TrimSpace(line[idx+1:])
	val = strings.Trim(val, `",`)
	return val
}
