package executor

import (
	"bytes"
	"log/slog"
	"strings"
	"testing"

	"github.com/docker/docker/api/types/container"
)

const mib = int64(1024 * 1024)

func captureDefaultLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}

func sandboxTmpfs() map[string]string {
	return map[string]string{
		"/tmp":     "rw,size=256m,mode=1777",
		"/var/tmp": "rw,size=64m,mode=1777",
	}
}

func TestEphemeralZeroLeavesHostConfigUnchanged(t *testing.T) {
	t.Setenv("MOD_WORKER_MAX_SHM_MB", "")
	t.Setenv("MOD_WORKER_MAX_TMPFS_MB", "")
	buf := captureDefaultLog(t)

	hc := &container.HostConfig{ShmSize: 2048 * mib, Tmpfs: sandboxTmpfs()}
	if got := applyEphemeralResources(hc, 0, 0); got != (ephemeralApplied{}) {
		t.Errorf("applied = %+v, want zero value (untouched)", got)
	}

	if hc.ShmSize != 2048*mib {
		t.Errorf("ShmSize = %d, want unchanged %d", hc.ShmSize, 2048*mib)
	}
	if hc.Tmpfs["/tmp"] != "rw,size=256m,mode=1777" {
		t.Errorf("/tmp = %q, want unchanged", hc.Tmpfs["/tmp"])
	}

	if got := applyEphemeralResources(hc, -5, -5); got != (ephemeralApplied{}) {
		t.Errorf("applied = %+v, want zero value (untouched)", got)
	}
	if hc.ShmSize != 2048*mib || hc.Tmpfs["/tmp"] != "rw,size=256m,mode=1777" {
		t.Errorf("negative values must not change HostConfig: shm=%d tmp=%q", hc.ShmSize, hc.Tmpfs["/tmp"])
	}
	if buf.Len() != 0 {
		t.Errorf("unexpected log output: %s", buf.String())
	}
}

func TestEphemeralWithinCap(t *testing.T) {
	t.Setenv("MOD_WORKER_MAX_SHM_MB", "")
	t.Setenv("MOD_WORKER_MAX_TMPFS_MB", "")
	buf := captureDefaultLog(t)

	hc := &container.HostConfig{Tmpfs: sandboxTmpfs()}
	if got := applyEphemeralResources(hc, 4096, 1024); got != (ephemeralApplied{ShmMB: 4096, TmpfsMB: 1024}) {
		t.Errorf("applied = %+v, want {%+v}", got, ephemeralApplied{ShmMB: 4096, TmpfsMB: 1024})
	}

	if hc.ShmSize != 4096*mib {
		t.Errorf("ShmSize = %d, want %d", hc.ShmSize, 4096*mib)
	}
	if got, want := hc.Tmpfs["/tmp"], "rw,size=1024m,mode=1777"; got != want {
		t.Errorf("/tmp = %q, want %q", got, want)
	}
	if got, want := hc.Tmpfs["/var/tmp"], "rw,size=64m,mode=1777"; got != want {
		t.Errorf("/var/tmp = %q, want untouched %q", got, want)
	}
	if strings.Contains(buf.String(), "clamped") {
		t.Errorf("within-cap values must not log a clamp: %s", buf.String())
	}
}

func TestEphemeralOverDefaultCap(t *testing.T) {
	t.Setenv("MOD_WORKER_MAX_SHM_MB", "")
	t.Setenv("MOD_WORKER_MAX_TMPFS_MB", "")
	buf := captureDefaultLog(t)

	hc := &container.HostConfig{Tmpfs: sandboxTmpfs()}
	wantClamped := ephemeralApplied{ShmMB: defaultEphemeralCapMB, TmpfsMB: defaultEphemeralCapMB, Clamped: true}
	if got := applyEphemeralResources(hc, 100000, 100000); got != wantClamped {
		t.Errorf("applied = %+v, want %+v", got, wantClamped)
	}

	if hc.ShmSize != defaultEphemeralCapMB*mib {
		t.Errorf("ShmSize = %d, want default cap %d", hc.ShmSize, defaultEphemeralCapMB*mib)
	}
	if got, want := hc.Tmpfs["/tmp"], "rw,size=8192m,mode=1777"; got != want {
		t.Errorf("/tmp = %q, want %q", got, want)
	}
	out := buf.String()
	if strings.Count(out, "clamped") != 2 {
		t.Errorf("expected two clamp log lines, got: %s", out)
	}
	if !strings.Contains(out, "MOD_WORKER_MAX_SHM_MB") || !strings.Contains(out, "MOD_WORKER_MAX_TMPFS_MB") {
		t.Errorf("clamp log should name the cap env: %s", out)
	}
}

func TestEphemeralOverEnvCap(t *testing.T) {
	t.Setenv("MOD_WORKER_MAX_SHM_MB", "512")
	t.Setenv("MOD_WORKER_MAX_TMPFS_MB", "300")
	buf := captureDefaultLog(t)

	hc := &container.HostConfig{Tmpfs: sandboxTmpfs()}
	if got := applyEphemeralResources(hc, 1024, 1024); got != (ephemeralApplied{ShmMB: 512, TmpfsMB: 300, Clamped: true}) {
		t.Errorf("applied = %+v, want %+v", got, ephemeralApplied{ShmMB: 512, TmpfsMB: 300, Clamped: true})
	}

	if hc.ShmSize != 512*mib {
		t.Errorf("ShmSize = %d, want env cap %d", hc.ShmSize, 512*mib)
	}
	if got, want := hc.Tmpfs["/tmp"], "rw,size=300m,mode=1777"; got != want {
		t.Errorf("/tmp = %q, want %q", got, want)
	}
	if strings.Count(buf.String(), "clamped") != 2 {
		t.Errorf("expected two clamp log lines, got: %s", buf.String())
	}
}

func TestEphemeralInvalidEnvCapFallsBackToDefault(t *testing.T) {
	for _, v := range []string{"abc", "0", "-10"} {
		t.Run(v, func(t *testing.T) {
			t.Setenv("MOD_WORKER_MAX_SHM_MB", v)
			captureDefaultLog(t)
			hc := &container.HostConfig{}
			if got := applyEphemeralResources(hc, 9000, 0); got != (ephemeralApplied{ShmMB: defaultEphemeralCapMB, Clamped: true}) {
				t.Errorf("applied = %+v, want %+v", got, ephemeralApplied{ShmMB: defaultEphemeralCapMB, Clamped: true})
			}
			if hc.ShmSize != defaultEphemeralCapMB*mib {
				t.Errorf("ShmSize = %d, want default cap %d", hc.ShmSize, defaultEphemeralCapMB*mib)
			}
		})
	}
}

func TestEphemeralNilTmpfsMap(t *testing.T) {
	t.Setenv("MOD_WORKER_MAX_SHM_MB", "")
	t.Setenv("MOD_WORKER_MAX_TMPFS_MB", "")
	captureDefaultLog(t)

	hc := &container.HostConfig{}
	// tmpfsMB was requested but there is no /tmp tmpfs to rewrite, so the
	// applied TmpfsMB must be 0 (untouched) and Clamped must stay false.
	if got := applyEphemeralResources(hc, 1024, 1024); got != (ephemeralApplied{ShmMB: 1024}) {
		t.Errorf("applied = %+v, want %+v", got, ephemeralApplied{ShmMB: 1024})
	}

	if hc.Tmpfs != nil {
		t.Errorf("Tmpfs must stay nil when no /tmp mount exists, got %v", hc.Tmpfs)
	}
	if hc.ShmSize != 1024*mib {
		t.Errorf("ShmSize = %d, want %d", hc.ShmSize, 1024*mib)
	}
}

func TestEphemeralTmpfsWithoutTmpEntry(t *testing.T) {
	captureDefaultLog(t)
	hc := &container.HostConfig{Tmpfs: map[string]string{"/run": "rw,size=16m,mode=0755"}}
	if got := applyEphemeralResources(hc, 0, 1024); got != (ephemeralApplied{}) {
		t.Errorf("applied = %+v, want zero value (no /tmp entry)", got)
	}

	if _, ok := hc.Tmpfs["/tmp"]; ok {
		t.Errorf("/tmp must not be created, got %v", hc.Tmpfs)
	}
	if hc.Tmpfs["/run"] != "rw,size=16m,mode=0755" {
		t.Errorf("/run = %q, want untouched", hc.Tmpfs["/run"])
	}
}

func TestEphemeralNilHostConfig(t *testing.T) {
	if got := applyEphemeralResources(nil, 1024, 1024); got != (ephemeralApplied{}) {
		t.Errorf("applied = %+v, want zero value", got)
	}
}

func TestEphemeralSetTmpfsSize(t *testing.T) {
	cases := []struct{ in, want string }{
		{"rw,size=256m,mode=1777", "rw,size=512m,mode=1777"},
		{"rw,mode=1777", "rw,mode=1777,size=512m"},
		{"", "size=512m"},
		{"size=1g", "size=512m"},
		{"rw,size=1m,size=2m,noexec", "rw,size=512m,noexec"},
	}
	for _, tc := range cases {
		if got := setTmpfsSize(tc.in, 512); got != tc.want {
			t.Errorf("setTmpfsSize(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}
