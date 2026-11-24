package capabilities

import "testing"

// Header worker-r9700 reported (ROCm 6.x): VRAM total is column 1, the used
// figure is column 2, the product name is "Card Series", and there is no
// "Unique ID" column. The old positional parser read the header line as the
// GPU name and the USED column as VRAM.
const r9700SMIHeader = "device,VRAM Total Memory (B),VRAM Total Used Memory (B),Card Series,Card Model,Card Vendor,Card SKU,Subsystem ID,Device Rev,Node ID,GUID,GFX Version\n"

const r9700SMIOut = "WARNING: AMD GPU device(s) is/are in a low-power state. Check power control/runtime_status\n\n" +
	r9700SMIHeader +
	"card0,34208743424,33554432000,AMD Radeon AI PRO R9000,0x7551,Advanced Micro Devices Inc. [AMD/ATI],R9000,0x0001,0xc0,1,12345,gfx1201\n" +
	"card1,34208743424,1048576,AMD Radeon AI PRO R9000,0x7551,Advanced Micro Devices Inc. [AMD/ATI],R9000,0x0001,0xc0,2,67890,gfx1201\n"

func TestParseROCmName_NeverReturnsHeader(t *testing.T) {
	if got := parseROCmName(r9700SMIOut); got != "AMD Radeon AI PRO R9000" {
		t.Fatalf("parseROCmName = %q", got)
	}
	if got := parseROCmName(r9700SMIHeader); got != "AMD GPU" {
		t.Fatalf("header-only output: parseROCmName = %q, want AMD GPU", got)
	}
	if got := parseROCmName(""); got != "AMD GPU" {
		t.Fatalf("empty output: parseROCmName = %q", got)
	}
}

func TestParseROCmSMICSV_HeaderAwareR9700(t *testing.T) {
	devs := parseROCmSMICSV([]byte(r9700SMIOut))
	if len(devs) != 2 {
		t.Fatalf("got %d devices: %+v", len(devs), devs)
	}
	want := 34208743424.0 / (1024 * 1024 * 1024)
	for i, d := range devs {
		if d.Kind != "rocm" || d.Index != i {
			t.Errorf("dev %d kind/index = %s/%d", i, d.Kind, d.Index)
		}
		if d.Name != "AMD Radeon AI PRO R9000" {
			t.Errorf("dev %d name = %q", i, d.Name)
		}
		if d.UUID != "" {
			t.Errorf("dev %d uuid = %q, want empty (no Unique ID column)", i, d.UUID)
		}
		if !approxEqual(d.VRAMGB, want) {
			t.Errorf("dev %d VRAMGB = %v, want total %v (not the used column)", i, d.VRAMGB, want)
		}
	}
}

func TestParseROCmSMICSV_NameFallsBackToCardModelThenDevice(t *testing.T) {
	out := "device,Card Series,Card Model,VRAM Total Memory (B)\n" +
		"card0,N/A,0x7551,1073741824\n" +
		"card1,,,2147483648\n"
	devs := parseROCmSMICSV([]byte(out))
	if len(devs) != 2 || devs[0].Name != "0x7551" || devs[1].Name != "card1" {
		t.Fatalf("got %+v", devs)
	}
	if !approxEqual(devs[1].VRAMGB, 2) {
		t.Errorf("VRAMGB = %v", devs[1].VRAMGB)
	}
}

func TestRocmCSVVRAMGB_ReadsTotalNotUsed(t *testing.T) {
	want := 34208743424.0 / (1024 * 1024 * 1024)
	if got := rocmCSVVRAMGB(r9700SMIOut); !approxEqual(got, want) {
		t.Fatalf("rocmCSVVRAMGB = %v, want %v", got, want)
	}
	if got := rocmCSVVRAMGB(""); got != 0 {
		t.Fatalf("empty = %v", got)
	}
}

func TestEnumerateROCm_R9700HeaderUsesRocmSMI(t *testing.T) {
	calls := withROCmStubs(t, r9700SMIOut, nil, rocminfoStrixHalo, nil, false, nil)
	devs := enumerateROCm()
	if *calls != 0 {
		t.Errorf("rocminfo ran %d times; rocm-smi had VRAM for every card", *calls)
	}
	if len(devs) != 2 || devs[1].Name != "AMD Radeon AI PRO R9000" {
		t.Fatalf("got %+v", devs)
	}
}
