package executor

import (
	"os"
	"testing"
)

func TestDiskCleanupAction(t *testing.T) {
	const auto, deep = 85, 95
	cases := []struct {
		used int
		want pruneLevel
	}{
		{0, pruneNone},
		{84, pruneNone},
		{85, pruneLight}, // at the auto threshold → light
		{90, pruneLight},
		{94, pruneLight},
		{95, pruneDeep}, // at the deep threshold → deep
		{99, pruneDeep},
		{100, pruneDeep},
	}
	for _, c := range cases {
		if got := diskCleanupAction(c.used, auto, deep); got != c.want {
			t.Errorf("diskCleanupAction(%d, %d, %d) = %v, want %v",
				c.used, auto, deep, got, c.want)
		}
	}
}

func TestEnvPctClamped(t *testing.T) {
	const key = "MOD_TEST_PCT_CLAMP"
	defer os.Unsetenv(key)

	cases := []struct {
		set  bool
		val  string
		def  int
		want int
	}{
		{false, "", 85, 85},        // unset → default
		{true, "", 85, 85},         // empty → default
		{true, "70", 85, 70},       // valid
		{true, "notanint", 85, 85}, // garbage → default
		{true, "-5", 85, 0},        // clamp low
		{true, "150", 85, 100},     // clamp high
		{true, "0", 85, 0},         // explicit zero honored
	}
	for _, c := range cases {
		if c.set {
			os.Setenv(key, c.val)
		} else {
			os.Unsetenv(key)
		}
		if got := envPctClamped(key, c.def); got != c.want {
			t.Errorf("envPctClamped(set=%v val=%q def=%d) = %d, want %d",
				c.set, c.val, c.def, got, c.want)
		}
	}
}

func TestDiskCleanupDisabled(t *testing.T) {
	const key = "MOD_DISK_CLEANUP_DISABLED"
	orig, had := os.LookupEnv(key)
	defer func() {
		if had {
			os.Setenv(key, orig)
		} else {
			os.Unsetenv(key)
		}
	}()

	for _, v := range []string{"1", "true", "TRUE", "yes", "Yes"} {
		os.Setenv(key, v)
		if !diskCleanupDisabled() {
			t.Errorf("diskCleanupDisabled() = false for %q, want true", v)
		}
	}
	for _, v := range []string{"", "0", "false", "no"} {
		os.Setenv(key, v)
		if diskCleanupDisabled() {
			t.Errorf("diskCleanupDisabled() = true for %q, want false", v)
		}
	}
}

func TestPruneLevelString(t *testing.T) {
	if pruneNone.String() != "none" || pruneLight.String() != "light" || pruneDeep.String() != "deep" {
		t.Errorf("pruneLevel.String mismatch: %s/%s/%s",
			pruneNone, pruneLight, pruneDeep)
	}
}
