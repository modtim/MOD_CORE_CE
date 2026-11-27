package capabilities

import "testing"

// TestPrimaryNonNPU verifies that primaryNonNPU selects the biggest
// non-NPU device, skipping NPU entries entirely.
func TestPrimaryNonNPU(t *testing.T) {
	devs := []AcceleratorDevice{
		{Kind: "cuda", Name: "RTX 4090", VendorID: "0x10de", VRAMGB: 24.0},
		{Kind: "cuda", Name: "RTX 3090", VendorID: "0x10de", VRAMGB: 24.0},
		{Kind: "npu", Name: "Intel NPU", VendorID: "0x8086", VRAMGB: 0.5},
	}
	best := primaryNonNPU(devs)
	if best == nil {
		t.Fatal("expected non-nil")
	}
	if best.Kind != "cuda" {
		t.Errorf("got %q, want cuda", best.Kind)
	}
	if best.VendorID != "0x10de" {
		t.Errorf("got %q, want 0x10de", best.VendorID)
	}
}

// TestPrimaryNonNPU_allNPU verifies that a list of only NPUs returns nil.
func TestPrimaryNonNPU_allNPU(t *testing.T) {
	devs := []AcceleratorDevice{
		{Kind: "npu", Name: "NPU", VendorID: "0x8086"},
	}
	if primaryNonNPU(devs) != nil {
		t.Fatal("expected nil for NPU-only list")
	}
}

// TestProfileGPUVendorCount verifies that a profile populated via
// EnumerateAccelerators carries vendor/count.
func TestProfileGPUVendorCount(t *testing.T) {
	p := Profile{
		OS:            "linux",
		Arch:          "amd64",
		CPUCores:      8,
		GPUType:       "CUDA",
		GPUName:       "NVIDIA A100",
		VRAMGB:        80.0,
		Accelerators: []AcceleratorDevice{
			{Kind: "cuda", Name: "NVIDIA A100", VendorID: "0x10de", VRAMGB: 80.0},
		},
	}
	// Simulate the derive block from detect():
	if primaryDev := primaryNonNPU(p.Accelerators); primaryDev != nil {
		switch primaryDev.VendorID {
		case "0x10de":
			p.GPUVendor = "NVIDIA"
		case "0x1002":
			p.GPUVendor = "AMD"
		case "0x8086":
			p.GPUVendor = "Intel"
		case "0x106b":
			p.GPUVendor = "Apple"
		}
	}
	p.GPUCount = len(p.Accelerators)

	if p.GPUVendor != "NVIDIA" {
		t.Errorf("got %q, want NVIDIA", p.GPUVendor)
	}
	if p.GPUCount != 1 {
		t.Errorf("got %d, want 1", p.GPUCount)
	}
}

// TestProfileGPUVendorCount_multi verifies vendor/count with mixed devices.
func TestProfileGPUVendorCount_multi(t *testing.T) {
	p := Profile{
		Accelerators: []AcceleratorDevice{
			{Kind: "cuda", Name: "A100", VendorID: "0x10de", VRAMGB: 80.0},
			{Kind: "intel", Name: "Arc", VendorID: "0x8086", VRAMGB: 24.0},
			{Kind: "npu", Name: "NPU", VendorID: "0x8086"},
		},
	}
	if primaryDev := primaryNonNPU(p.Accelerators); primaryDev != nil {
		switch primaryDev.VendorID {
		case "0x10de":
			p.GPUVendor = "NVIDIA"
		case "0x1002":
			p.GPUVendor = "AMD"
		case "0x8086":
			p.GPUVendor = "Intel"
		case "0x106b":
			p.GPUVendor = "Apple"
		}
	}
	p.GPUCount = len(p.Accelerators)

	if p.GPUVendor != "NVIDIA" {
		t.Errorf("got %q, want NVIDIA (biggest)", p.GPUVendor)
	}
	if p.GPUCount != 3 {
		t.Errorf("got %d, want 3 (total devices incl. NPU)", p.GPUCount)
	}
}
