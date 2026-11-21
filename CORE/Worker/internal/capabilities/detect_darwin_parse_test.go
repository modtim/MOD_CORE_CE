package capabilities

// MAC.1.s7 — captured `system_profiler SPDisplaysDataType -json` fixtures for
// the darwin Metal probe. Pure parsing, no build tag: these run on Linux CI
// where no Mac exists, and the probe seam (darwinProbeRunner) is stubbed so the
// darwin branch in detect.go can be exercised without a GPU.

import (
	"testing"
)

// mSeries is an Apple Silicon (M-series) card: no dedicated VRAM, the shared
// pool is reported instead, and the family-support key marks it Metal.
const mSeries = `{
  "DisplaysDataType": [
    {
      "_SecurityCode": "XXXXXXXX.AppleSystem",
      "Graphics": {
        "Apple_M5": {
          "spdisplays_gpu": "Apple M5",
          "spdisplays_execution-units": "160",
          "spdisplays_mtlgpufamilysupport": "spdisplays_mtl4family",
          "spdisplays_vram-shared": "32 GB",
          "_SupportsMetal": "Yes",
          "_SupportsMetalMip": "Yes"
        }
      },
      "_SupportsMetal": "Yes",
      "Displays": [
        {
          "spdisplays_display-name": "LCD",
          "x_resolution": "2880",
          "y_resolution": "1800"
        }
      ]
    }
  ]
}`

// intelMacWithAMD is an Intel Mac with a discrete Radeon Pro: it has its own
// VRAM figure and is NOT unified-memory.
const intelMacWithAMD = `{
  "DisplaysDataType": [
    {
      "Graphics": {
        "AMD_GPU": {
          "spdisplays_gpu": "AMD Radeon Pro XTX 8 GB",
          "spdisplays_vram": "8192 MB",
          "spdisplays_packed-shader-core-units": "256",
          "_SupportsMetal": "Yes",
          "_SupportsMetalMip": "Yes"
        }
      },
      "_SupportsMetal": "Yes"
    }
  ]
}`

// intelIOnly is an Intel Mac whose only GPU is the Integrated iGPU: Metal-capable
// on modern macOS, smaller dedicated VRAM.
const intelIOnly = `{
  "DisplaysDataType": [
    {
      "Graphics": {
        "INTEGRATED_GPU": {
          "spdisplays_gpu": "Intel(R) Graphics",
          "spdisplays_vram": "1536 MB",
          "spdisplays_execution-units": "64",
          "_SupportsMetal": "Yes"
        }
      }
    }
  ]
}`

// metalRefused is a legacy card that explicitly reports no Metal support: it
// must not produce the metal tag.
const metalRefused = `{
  "DisplaysDataType": [
    {
      "Graphics": {
        "LEGACY": {
          "spdisplays_gpu": "Legacy GL-only GPU",
          "spdisplays_vram": "1024 MB",
          "_SupportsMetal": "No",
          "_SupportsMetalMip": "No"
        }
      }
    }
  ]
}`

// garbage is truncated/garbage probe output: no tag, no panic.
const garbage = "{ not json at all"

func TestParseDarwinDisplays_AppleSiliconIsMetalUnified(t *testing.T) {
	metal, name, cores := parseDarwinDisplays([]byte(mSeries))
	if metal != true {
		t.Errorf("Apple Silicon must report metal")
	}
	if name != "Apple M5" {
		t.Errorf("gpu name: got %q, want %q", name, "Apple M5")
	}
	if cores != 160 {
		t.Errorf("execution units: got %d, want 160", cores)
	}
}

func TestParseDarwinDisplays_IntelMacWithDiscreteRadeon(t *testing.T) {
	metal, name, cores := parseDarwinDisplays([]byte(intelMacWithAMD))
	if metal != true {
		t.Errorf("Metal-capable Radeon must report metal")
	}
	if name != "AMD Radeon Pro XTX 8 GB" {
		t.Errorf("gpu name: got %q, want %q", name, "AMD Radeon Pro XTX 8 GB")
	}
	if cores != 256 {
		t.Errorf("shader units: got %d, want 256", cores)
	}
	// The card must NOT be unified: it carries its own VRAM figure.
	// darwinVRAMGB parses "8192 MB" -> 8 GB.
	// darwinVRAMGB is the pure text->GB helper the card builder uses.
	if got := darwinVRAMGB("8192 MB"); got != 8 {
		t.Errorf("8192 MB should be 8 GB, got %v", got)
	}
}

func TestParseDarwinDisplays_IntelIOnly(t *testing.T) {
	metal, name, cores := parseDarwinDisplays([]byte(intelIOnly))
	if metal != true {
		t.Errorf("Modern macOS iGPUs are Metal-capable")
	}
	if name != "Intel(R) Graphics" {
		t.Errorf("gpu name: got %q, want %q", name, "Intel(R) Graphics")
	}
	if cores != 64 {
		t.Errorf("execution units: got %d, want 64", cores)
	}
}

func TestParseDarwinDisplays_MetalRefusedCardIsNotTagged(t *testing.T) {
	metal, _, _ := parseDarwinDisplays([]byte(metalRefused))
	if metal {
		t.Errorf("a card that reports _SupportsMetal=No must not produce the metal tag")
	}
}

func TestParseDarwinDisplays_GarbageYieldsNoTagNoPanic(t *testing.T) {
	metal, name, cores := parseDarwinDisplays([]byte(garbage))
	if metal || name != "" || cores != 0 {
		t.Errorf("garbage JSON must yield no tag: got %v/%q/%d", metal, name, cores)
	}
}

func TestDarwinCards_UnifiedVsDedicatedVRAM(t *testing.T) {
	// Apple Silicon: unified pool, no per-card VRAM figure (the caller sizes it
	// from hw.memsize); Intel discrete: dedicated VRAM, not unified.
	uni := darwinCards([]byte(mSeries))
	if len(uni) != 1 {
		t.Errorf("expected 1 card, got %d", len(uni))
		return
	}
	if !uni[0].Unified || uni[0].VRAMGB != 0 {
		t.Errorf("Apple Silicon card must be unified with no dedicated VRAM: %+v", uni[0])
	}
	ded := darwinCards([]byte(intelMacWithAMD))
	if len(ded) != 1 {
		t.Errorf("expected 1 card, got %d", len(ded))
		return
	}
	if ded[0].Unified || ded[0].VRAMGB != 8 {
		t.Errorf("discrete Radeon card must be non-unified with 8 GB: %+v", ded[0])
	}
}

func TestDarwinCards_MonitorRecordIsNotAGPU(t *testing.T) {
	// A payload with only display-panel records (no Graphics card attributes)
	// must enumerate no GPU.
	panels := `{"DisplaysDataType":[{"Displays":[{"spdisplays_display-name":"LCD"}]}]}`
	if got := darwinCards([]byte(panels)); len(got) != 0 {
		t.Errorf("monitor-only payload must yield no GPU, got %d", len(got))
	}
}

func TestDarwinProbeRunner_DefaultsToSystemProfiler(t *testing.T) {
	// The seam must exist and be swappable: tests replace it with captured
	// output, exactly like devices.go's nvidiaSMIRunner.
	original := darwinProbeRunner
	defer func() { darwinProbeRunner = original }()
	darwinProbeRunner = func() ([]byte, error) { return []byte(mSeries), nil }
	out, err := darwinProbeRunner()
	if err != nil {
		t.Errorf("stubbed runner must not error: %v", err)
	}
	metal, name, _ := parseDarwinDisplays(out)
	if !metal || name != "Apple M5" {
		t.Errorf("stubbed probe must parse: got %v/%q", metal, name)
	}
}
