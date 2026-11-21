package capabilities

// ROCm enumeration on WSL2. On the Strix Halo box (gfx1151 via /dev/dxg +
// librocdxg) rocm-smi yields no VRAM and /sys/class/drm has no AMD card, so the
// API stored "ROCM #0" at vram_gb NULL and the claim-time fit guard refused
// every GPU task as if the card had 0 GB. rocminfo (with
// HSA_ENABLE_DXG_DETECTION=1) is the source that actually sees the card.

import (
	"errors"
	"math"
	"testing"
)

// Trimmed from real `HSA_ENABLE_DXG_DETECTION=1 rocminfo` output on the
// Strix Halo host: CPU agent first, then the GPU agent with several pools and
// an ISA section whose "Name:" lines must not override the agent name.
const rocminfoStrixHalo = `WSL environment detected.
=====================
HSA System Attributes
=====================
Runtime Version:         1.18
Runtime Ext Version:     1.11
System Timestamp Freq.:  1000.000000MHz
Machine Model:           LARGE
System Endianness:       LITTLE
Mwaitx:                  DISABLED
XNACK enabled:           NO
DMAbuf Support:          YES

==========
HSA Agents
==========
*******
Agent 1
*******
  Name:                    AMD RYZEN AI MAX+ 395 w/ Radeon 8060S
  Uuid:                    CPU-XX
  Marketing Name:          AMD RYZEN AI MAX+ 395 w/ Radeon 8060S
  Vendor Name:             CPU
  Feature:                 None specified
  Profile:                 FULL_PROFILE
  Float Round Mode:        NEAR
  Max Queue Number:        0(0x0)
  Queue Min Size:          0(0x0)
  Queue Max Size:          0(0x0)
  Queue Type:              MULTI
  Node:                    0
  Device Type:             CPU
  Cache Info:
    L1:                      49152(0xc000) KB
  Chip ID:                 0(0x0)
  ASIC Revision:           0(0x0)
  Cacheline Size:          64(0x40)
  Max Clock Freq. (MHz):   5187
  BDFID:                   0
  Internal Node ID:        0
  Compute Unit:            32
  SIMDs per CU:            0
  Shader Engines:          0
  Shader Arrs. per Eng.:   0
  WatchPts on Addr. Ranges:1
  Features:                None
  Pool Info:
    Pool 1
      Segment:                 GLOBAL; FLAGS: FINE GRAINED
      Size:                    131072000(0x7d00000) KB
      Allocatable:             TRUE
      Alloc Granule:           4KB
      Alloc Recommended Granule:4KB
      Alloc Alignment:         4KB
      Accessible by all:       TRUE
    Pool 2
      Segment:                 GLOBAL; FLAGS: COARSE GRAINED
      Size:                    131072000(0x7d00000) KB
      Allocatable:             TRUE
      Alloc Granule:           4KB
      Alloc Recommended Granule:4KB
      Alloc Alignment:         4KB
      Accessible by all:       TRUE
  ISA Info:
*******
Agent 2
*******
  Name:                    gfx1151
  Uuid:                    GPU-XX
  Marketing Name:          AMD Radeon(TM) 8060S Graphics
  Vendor Name:             AMD
  Feature:                 KERNEL_DISPATCH
  Profile:                 BASE_PROFILE
  Float Round Mode:        NEAR
  Max Queue Number:        128(0x80)
  Queue Min Size:          64(0x40)
  Queue Max Size:          131072(0x20000)
  Queue Type:              MULTI
  Node:                    1
  Device Type:             GPU
  Cache Info:
    L1:                      32(0x20) KB
    L2:                      2048(0x800) KB
    L3:                      32768(0x8000) KB
  Chip ID:                 5510(0x1586)
  ASIC Revision:           0(0x0)
  Cacheline Size:          128(0x80)
  Max Clock Freq. (MHz):   2900
  BDFID:                   0
  Internal Node ID:        1
  Compute Unit:            40
  SIMDs per CU:            2
  Shader Engines:          2
  Shader Arrs. per Eng.:   2
  Workgroup Max Size:      1024(0x400)
  Pool Info:
    Pool 1
      Segment:                 GLOBAL; FLAGS: EXTENDED FINE GRAINED
      Size:                    65536000(0x3e80000) KB
      Allocatable:             TRUE
      Alloc Granule:           4KB
      Accessible by all:       FALSE
    Pool 2
      Segment:                 GLOBAL; FLAGS: COARSE GRAINED
      Size:                    116890624(0x6f79000) KB
      Allocatable:             TRUE
      Alloc Granule:           4KB
      Accessible by all:       FALSE
    Pool 3
      Segment:                 GLOBAL; FLAGS: COARSE GRAINED
      Size:                    1024(0x400) KB
      Allocatable:             TRUE
    Pool 4
      Segment:                 GROUP
      Size:                    64(0x40) KB
      Allocatable:             FALSE
  ISA Info:
    ISA 1
      Name:                    amdgcn-amd-amdhsa--gfx1151
      Machine Models:          HSA_MACHINE_MODEL_LARGE
    ISA 2
      Name:                    amdgcn-amd-amdhsa--gfx11-generic
      Machine Models:          HSA_MACHINE_MODEL_LARGE
*** Done ***
`

// 116890624 KB == 114151 MiB, the figure llama.cpp reports for this GPU.
const strixHaloVRAMGB = 116890624.0 * 1024 / (1024 * 1024 * 1024)

func approxEqual(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

func TestParseROCmInfo_StrixHaloSingleGPU(t *testing.T) {
	devs := parseROCmInfo(rocminfoStrixHalo)
	if len(devs) != 1 {
		t.Fatalf("got %d devices, want exactly the GPU agent (CPU agent excluded): %+v", len(devs), devs)
	}
	d := devs[0]
	if d.Kind != "rocm" || d.Index != 0 {
		t.Errorf("kind/index = %q/%d, want rocm/0", d.Kind, d.Index)
	}
	if d.Name != "AMD Radeon(TM) 8060S Graphics" {
		t.Errorf("Name = %q, want the GPU marketing name", d.Name)
	}
	// First GLOBAL coarse-grained pool only: not the extended-fine pool before
	// it, not the second coarse pool after it, not the CPU agent's pools.
	if !approxEqual(d.VRAMGB, strixHaloVRAMGB) {
		t.Errorf("VRAMGB = %v, want %v (116890624 KB)", d.VRAMGB, strixHaloVRAMGB)
	}
	if d.VRAMGB < 111 || d.VRAMGB > 112 {
		t.Errorf("VRAMGB = %v, want ~111.5", d.VRAMGB)
	}
	if d.VendorID != "0x1002" || d.DeviceID != "0x1586" {
		t.Errorf("vendor/device = %q/%q, want 0x1002/0x1586", d.VendorID, d.DeviceID)
	}
	if d.UUID != "" {
		t.Errorf("UUID = %q, want empty for the GPU-XX placeholder", d.UUID)
	}
}

const rocminfoTwoGPUs = `*******
Agent 1
*******
  Name:                    AMD Ryzen 9 7950X 16-Core Processor
  Marketing Name:          AMD Ryzen 9 7950X 16-Core Processor
  Vendor Name:             CPU
  Device Type:             CPU
  Pool Info:
    Pool 1
      Segment:                 GLOBAL; FLAGS: COARSE GRAINED
      Size:                    65536000(0x3e80000) KB
  ISA Info:
*******
Agent 2
*******
  Name:                    gfx1201
  Uuid:                    GPU-5a7d1e2f3c4b5a69
  Marketing Name:          AMD Radeon AI PRO R9000
  Vendor Name:             AMD
  Device Type:             GPU
  Chip ID:                 30033(0x7551)
  Pool Info:
    Pool 1
      Segment:                 GLOBAL; FLAGS: COARSE GRAINED
      Size:                    33406976(0x1fdc000) KB
    Pool 2
      Segment:                 GLOBAL; FLAGS: EXTENDED FINE GRAINED
      Size:                    33406976(0x1fdc000) KB
  ISA Info:
    ISA 1
      Name:                    amdgcn-amd-amdhsa--gfx1201
*******
Agent 3
*******
  Name:                    gfx1100
  Uuid:                    GPU-0123456789abcdef
  Marketing Name:          Radeon RX 7900 XTX
  Vendor Name:             AMD
  Device Type:             GPU
  Chip ID:                 29772(0x744c)
  Pool Info:
    Pool 1
      Segment:                 GLOBAL; FLAGS: COARSE GRAINED
      Size:                    25149440(0x17fc000) KB
  ISA Info:
    ISA 1
      Name:                    amdgcn-amd-amdhsa--gfx1100
`

func TestParseROCmInfo_TwoGPUsIndexedInOrder(t *testing.T) {
	devs := parseROCmInfo(rocminfoTwoGPUs)
	if len(devs) != 2 {
		t.Fatalf("got %d devices, want 2: %+v", len(devs), devs)
	}
	want := []struct {
		name, uuid, dev string
		kb              float64
	}{
		{"AMD Radeon AI PRO R9000", "GPU-5a7d1e2f3c4b5a69", "0x7551", 33406976},
		{"Radeon RX 7900 XTX", "GPU-0123456789abcdef", "0x744c", 25149440},
	}
	for i, w := range want {
		d := devs[i]
		if d.Index != i {
			t.Errorf("dev %d: Index = %d", i, d.Index)
		}
		if d.Name != w.name || d.UUID != w.uuid || d.DeviceID != w.dev {
			t.Errorf("dev %d = %+v, want name %q uuid %q device %q", i, d, w.name, w.uuid, w.dev)
		}
		if gb := w.kb * 1024 / (1024 * 1024 * 1024); !approxEqual(d.VRAMGB, gb) {
			t.Errorf("dev %d VRAMGB = %v, want %v", i, d.VRAMGB, gb)
		}
	}
}

func TestParseROCmInfo_GarbageAndEmpty(t *testing.T) {
	for _, in := range []string{
		"",
		"\n\n",
		"rocminfo: command not found",
		"hsa api call failure at: rocminfo.cc:1306\nCall returned HSA_STATUS_ERROR_OUT_OF_RESOURCES",
		"Agent 1\n  Device Type: CPU\n",
		"Agent x\n  Device Type: GPU\n",                // malformed header: not an agent
		"  Device Type: GPU\n  Size: 1024(0x400) KB\n", // no agent header at all
	} {
		if devs := parseROCmInfo(in); len(devs) != 0 {
			t.Errorf("parseROCmInfo(%q) = %+v, want none", in, devs)
		}
	}
}

func TestParseROCmInfo_GPUWithoutCoarsePoolHasNoVRAMButFallsBackToGfxName(t *testing.T) {
	in := "Agent 1\n  Name: gfx1151\n  Device Type: GPU\n  Pool Info:\n    Pool 1\n      Segment: GROUP\n      Size: 64(0x40) KB\n"
	devs := parseROCmInfo(in)
	if len(devs) != 1 {
		t.Fatalf("got %+v", devs)
	}
	if devs[0].VRAMGB != 0 || devs[0].Name != "gfx1151" {
		t.Errorf("got %+v, want VRAM 0 and name gfx1151", devs[0])
	}
}

func TestParseROCmInfoSizeGB(t *testing.T) {
	cases := map[string]float64{
		"116890624(0x6f79000) KB":  strixHaloVRAMGB,
		"1048576 KB":               1,
		"1073741824(0x40000000) B": 1,
		"2048 MB":                  2,
		"garbage":                  0,
		"":                         0,
	}
	for in, want := range cases {
		if got := parseROCmInfoSizeGB(in); !approxEqual(got, want) {
			t.Errorf("parseROCmInfoSizeGB(%q) = %v, want %v", in, got, want)
		}
	}
}

// withROCmStubs swaps the enumerateROCm injection points for one test.
func withROCmStubs(t *testing.T, smiOut string, smiErr error, infoOut string, infoErr error, wsl bool, dri []AcceleratorDevice) (infoCalls *int) {
	t.Helper()
	oldSMI, oldInfo, oldWSL, oldDRI := rocmSMIRunner, rocminfoRunner, rocmHostIsWSL, rocmDRIEnumerator
	t.Cleanup(func() {
		rocmSMIRunner, rocminfoRunner, rocmHostIsWSL, rocmDRIEnumerator = oldSMI, oldInfo, oldWSL, oldDRI
	})
	calls := 0
	rocmSMIRunner = func() ([]byte, error) { return []byte(smiOut), smiErr }
	rocminfoRunner = func() ([]byte, error) { calls++; return []byte(infoOut), infoErr }
	rocmHostIsWSL = func() bool { return wsl }
	rocmDRIEnumerator = func() []AcceleratorDevice { return dri }
	return &calls
}

const smiHeader = "device,Unique ID,VRAM Total Memory (B)\n"

func TestEnumerateROCm_NativeRocmSMIWithVRAMSkipsRocminfo(t *testing.T) {
	calls := withROCmStubs(t, smiHeader+"AMD Radeon AI PRO R9000,0xabc,34208743424\n", nil, rocminfoStrixHalo, nil, true, nil)
	devs := enumerateROCm()
	if *calls != 0 {
		t.Errorf("rocminfo ran %d times; a complete rocm-smi result must be used as-is", *calls)
	}
	if len(devs) != 1 || devs[0].Name != "AMD Radeon AI PRO R9000" || devs[0].UUID != "0xabc" {
		t.Fatalf("got %+v", devs)
	}
	if !approxEqual(devs[0].VRAMGB, 34208743424.0/(1024*1024*1024)) {
		t.Errorf("VRAMGB = %v", devs[0].VRAMGB)
	}
}

func TestEnumerateROCm_RocmSMIWithoutVRAMOnWSLUsesRocminfo(t *testing.T) {
	calls := withROCmStubs(t, smiHeader+"ROCM #0,0xdeadbeef,N/A\n", nil, rocminfoStrixHalo, nil, true, nil)
	devs := enumerateROCm()
	if *calls != 1 {
		t.Fatalf("rocminfo ran %d times, want 1", *calls)
	}
	if len(devs) != 1 || devs[0].Name != "AMD Radeon(TM) 8060S Graphics" || !approxEqual(devs[0].VRAMGB, strixHaloVRAMGB) {
		t.Fatalf("got %+v", devs)
	}
	if devs[0].UUID != "0xdeadbeef" {
		t.Errorf("UUID = %q, want rocm-smi's UUID carried over when rocminfo has only GPU-XX", devs[0].UUID)
	}
}

func TestEnumerateROCm_RocmSMIFailsOnWSLUsesRocminfoNotDRI(t *testing.T) {
	dri := []AcceleratorDevice{{Kind: "rocm", VendorID: "0x1002"}}
	withROCmStubs(t, "", errors.New("exec: rocm-smi: not found"), rocminfoStrixHalo, nil, true, dri)
	devs := enumerateROCm()
	if len(devs) != 1 || devs[0].VRAMGB <= 0 || devs[0].Name == "" {
		t.Fatalf("got %+v, want the rocminfo device with name + VRAM", devs)
	}
}

func TestEnumerateROCm_RocminfoExitErrorWithOutputStillParsed(t *testing.T) {
	withROCmStubs(t, "", errors.New("no rocm-smi"), rocminfoStrixHalo, errors.New("exit status 1"), true, nil)
	if devs := enumerateROCm(); len(devs) != 1 || devs[0].VRAMGB <= 0 {
		t.Fatalf("got %+v", devs)
	}
}

func TestEnumerateROCm_RocminfoAlsoFailsFallsBackToDRI(t *testing.T) {
	dri := []AcceleratorDevice{{Kind: "rocm", VendorID: "0x1002", DeviceID: "0x1586"}}
	calls := withROCmStubs(t, "", errors.New("no rocm-smi"), "", errors.New("no rocminfo"), true, dri)
	devs := enumerateROCm()
	if *calls != 1 {
		t.Errorf("rocminfo ran %d times, want 1", *calls)
	}
	if len(devs) != 1 || devs[0].DeviceID != "0x1586" {
		t.Fatalf("got %+v, want the DRI fallback", devs)
	}
}

func TestEnumerateROCm_NotWSLKeepsExistingBehaviour(t *testing.T) {
	dri := []AcceleratorDevice{{Kind: "rocm", VendorID: "0x1002"}}

	// rocm-smi fails → DRI, rocminfo never consulted off-WSL.
	calls := withROCmStubs(t, "", errors.New("no rocm-smi"), rocminfoStrixHalo, nil, false, dri)
	if devs := enumerateROCm(); len(devs) != 1 || devs[0].VendorID != "0x1002" || devs[0].VRAMGB != 0 {
		t.Errorf("rocm-smi failure off-WSL: got %+v, want DRI result", devs)
	}
	if *calls != 0 {
		t.Errorf("rocminfo ran off-WSL")
	}

	// rocm-smi succeeds without VRAM → its devices are returned unchanged.
	withROCmStubs(t, smiHeader+"card0,0x1,N/A\n", nil, rocminfoStrixHalo, nil, false, dri)
	if devs := enumerateROCm(); len(devs) != 1 || devs[0].Name != "card0" {
		t.Errorf("rocm-smi without VRAM off-WSL: got %+v, want rocm-smi's device", devs)
	}
}

func TestEnumerateROCm_WSLRocminfoEmptyKeepsRocmSMIResult(t *testing.T) {
	withROCmStubs(t, smiHeader+"ROCM #0,0x1,N/A\n", nil, "garbage", nil, true, nil)
	if devs := enumerateROCm(); len(devs) != 1 || devs[0].Name != "ROCM #0" {
		t.Errorf("got %+v, want rocm-smi's device when rocminfo yields nothing", devs)
	}
}
