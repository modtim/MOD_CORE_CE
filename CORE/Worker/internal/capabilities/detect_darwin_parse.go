package capabilities

// MAC.1.s7 — Metal GPU detection for macOS (darwin) workers.
//
// A darwin worker used to report only hw.memsize: no accelerator, no
// hardware tag, so the API's variant gate (gpu_variants x hardware_tags)
// never matched a "metal" node to it and the claim guard saw a plain CPU
// box. The probe itself lives in detect.go and runs through darwinProbeRunner,
// the same injectable command-runner seam devices.go uses for nvidia-smi, so
// CI can feed captured output.
//
// This file is PURE parsing with NO build tag: the fixtures in the test file
// are captured `system_profiler SPDisplaysDataType -json` payloads and the
// tests run on Linux, where no Mac exists.
//
// Metal-capability rule: macOS 10.14+ only ships Metal-capable GPUs, so any
// recognizable graphics record is enough to tag "metal" (an explicit
// "_SupportsMetal": "No" opts out). Apple Silicon cards carry
// spdisplays_mtlgpufamilysupport / spdisplays_vram-shared and have no
// dedicated VRAM — they are unified-memory, so the caller reports hw.memsize
// in BOTH the RAM and VRAM fields and the API sizes the pool as
// max(ram, vram), never the sum. An Intel Mac's discrete or Integrated GPU is
// NOT unified and carries its own spdisplays_vram figure (MiB).

import (
	"encoding/json"
	"os/exec"
	"strconv"
	"strings"
)

// darwinProbeRunner is the mockable seam for the macOS display probe.
var darwinProbeRunner = func() ([]byte, error) {
	return exec.Command("system_profiler", "SPDisplaysDataType", "-json").Output()
}

type darwinCard struct {
	GPUName     string
	VRAMGB      float64
	ShaderUnits int
	Metal       bool
	Unified     bool
}

// darwinCards parses a system_profiler payload into one card per GPU record.
// Malformed or unrelated JSON returns an empty list, never a panic.
func darwinCards(jsonBytes []byte) []darwinCard {
	root := parseDarwinJSON(jsonBytes)
	var out []darwinCard
	visitDarwinValue(root, &out)
	return out
}

// parseDarwinDisplays is the pure probe parser: (metal, gpuName, cores).
// cores is the summed execution-unit count across Metal cards, 0 when
// system_profiler doesn't report one.
func parseDarwinDisplays(jsonBytes []byte) (bool, string, int) {
	return parseDarwinDisplaysFrom(darwinCards(jsonBytes))
}

func parseDarwinDisplaysFrom(cards []darwinCard) (bool, string, int) {
	metal := false
	name := ""
	cores := 0
	for _, c := range cards {
		if !c.Metal {
			continue
		}
		metal = true
		if name == "" {
			name = c.GPUName
		}
		cores += c.ShaderUnits
	}
	return metal, name, cores
}

// visitDarwinValue walks the whole JSON tree. system_profiler nests the card
// attributes two levels deep (DisplaysDataType -> [card] -> Graphics -> card
// attributes) and the depth differs across macOS releases, so every object is
// offered to cardFromDarwinNode rather than hard-coding a path.
func visitDarwinValue(v interface{}, out *[]darwinCard) {
	if list, okList := v.([]interface{}); okList {
		for _, item := range list {
			visitDarwinValue(item, out)
		}
		return
	}
	m, okMap := v.(map[string]interface{})
	if !okMap {
		return
	}
	if card := cardFromDarwinNode(m); card != nil {
		*out = append(*out, *card)
	}
	for _, child := range m {
		visitDarwinValue(child, out)
	}
}

// cardFromDarwinNode returns a GPU card when the object carries system_profiler
// GPU attributes, nil for everything else (display panels, monitor records).
func cardFromDarwinNode(m map[string]interface{}) *darwinCard {
	// A GPU record carries spdisplays_gpu and/or a VRAM/family key; the outer
	// card object and the display-panel records carry only _SupportsMetal and
	// spdisplays_display-name, which are not GPU-shaped.
	name := darwinText(m, "spdisplays_gpu")
	// A monitor record (spdisplays_display-name / panel) is not a GPU.
	if name == "" && darwinText(m, "spdisplays_mtlgpufamilysupport") == "" &&
		darwinText(m, "spdisplays_vram") == "" &&
		darwinText(m, "spdisplays_vram-shared") == "" {
		// Nothing GPU-shaped at all.
		return nil
	}

	shared := darwinText(m, "spdisplays_vram-shared") != "" ||
		darwinText(m, "spdisplays_mtlgpufamilysupport") != ""
	vramText := darwinText(m, "spdisplays_vram")
	vramGB := 0.0
	if shared || strings.Contains(strings.ToLower(vramText), "shared") {
		// Unified pool: sized by the caller from hw.memsize, not a card figure.
		shared = true
		vramGB = 0
	} else {
		vramGB = darwinVRAMGB(vramText)
	}

	// Metal support: explicit opt-out wins; otherwise any GPU record on a
	// modern macOS counts.
	metal := darwinText(m, "_SupportsMetal") != "No" &&
		darwinText(m, "_SupportsMetalMip") != "No"

	cores := darwinInt(m, "spdisplays_execution-units") +
		darwinInt(m, "spdisplays_packed-shader-core-units") +
		darwinInt(m, "spdisplays_shader-unit-count")

	return &darwinCard{
		GPUName: name, VRAMGB: vramGB, ShaderUnits: cores,
		Metal: metal, Unified: shared,
	}
}

// darwinText returns a string attribute, "" when absent or non-string.
func darwinText(m map[string]interface{}, key string) string {
	v, ok := m[key]
	if !ok {
		return ""
	}
	if s, isStr := v.(string); isStr {
		return strings.TrimSpace(s)
	}
	return ""
}

// darwinInt returns an integer attribute, 0 when absent/unparseable. system_profiler
// emits counts as strings ("128") or JSON numbers depending on release.
func darwinInt(m map[string]interface{}, key string) int {
	v, ok := m[key]
	if !ok {
		return 0
	}
	if f, isFloat := v.(float64); isFloat {
		return int(f)
	}
	if s, isStr := v.(string); isStr {
		n, err := strconv.ParseInt(strings.TrimSpace(s), 10, 32)
		if err != nil {
			return 0
		}
		return int(n)
	}
	return 0
}

// darwinVRAMGB turns system_profiler's VRAM text into GB. The text is
// "8192 MB", "3 GB", "16 GB (Shared)" or "[NOT SUPPORTED]"; anything
// unparseable is 0 (unknown), which the caller treats as "no figure".
func darwinVRAMGB(text string) float64 {
	t := strings.ToLower(strings.TrimSpace(text))
	if t == "" || strings.Contains(t, "not supported") || strings.Contains(t, "n/a") {
		return 0
	}
	// Take the leading number, then scale by the unit that follows it.
	digits := ""
	for i := 0; i < len(t); i = i + 1 {
		c := t[i]
		isDigit := (c >= '0' && c <= '9') || c == '.'
		if isDigit {
			digits = digits + t[i:i+1]
		} else if digits != "" {
			break
		}
	}
	if digits == "" {
		return 0
	}
	n, err := strconv.ParseFloat(digits, 64)
	if err != nil {
		return 0
	}
	if strings.Contains(t, "mb") {
		return n / 1024.0
	}
	return n
}

// parseDarwinJSON decodes the probe payload; garbage, empty or truncated
// output decodes to an empty map so the walk finds no cards and the worker
// reports no metal tag (never a panic).
func parseDarwinJSON(jsonBytes []byte) map[string]interface{} {
	if len(jsonBytes) == 0 {
		return map[string]interface{}{}
	}
	var v interface{}
	if err := json.Unmarshal(jsonBytes, &v); err != nil {
		return map[string]interface{}{}
	}
	if m, isMap := v.(map[string]interface{}); isMap {
		return m
	}
	if l, isList := v.([]interface{}); isList && len(l) > 0 {
		// Some macOS releases emit the card list at the top level.
		return map[string]interface{}{"_darwinRoot": l}
	}
	return map[string]interface{}{}
}
