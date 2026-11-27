package executor

// UNDER1S-OUTPUT-HARBOR-PAID: real-docker parity + benchmark for the registry
// posture archive read. Skipped without a Docker socket / alpine:3.20. The bench
// leg only runs with HP_BENCH=1 (test-only switch; no production behaviour).

import (
	"context"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"
)

func hpVolume(t *testing.T, name, script string) {
	t.Helper()
	if out, err := exec.Command("docker", "volume", "create", name).CombinedOutput(); err != nil {
		t.Skipf("docker volume create: %v %s", err, out)
	}
	t.Cleanup(func() { exec.Command("docker", "volume", "rm", "-f", name).Run() })
	if out, err := exec.Command("docker", "run", "--rm", "-v", name+":/data", "alpine:3.20",
		"sh", "-c", "mkdir -p /data/output && "+script).CombinedOutput(); err != nil {
		t.Skipf("seed volume (alpine:3.20 present?): %v %s", err, out)
	}
}

func hpLeaked(t *testing.T) int {
	out, _ := exec.Command("docker", "ps", "-aq", "--filter", "label="+sidecarWorkerLabel+"=hp-test").Output()
	return len(strings.Fields(string(out)))
}

// Same answers as the helper `cat` read for: all present, none present, one
// present, whitespace-padded, empty file. No helper container is left behind.
func TestHPArchiveReadParityWithHelperCat(t *testing.T) {
	e := u1sExec(t)
	e.workerName = "hp-test"
	ctx := context.Background()
	scripts := map[string]string{
		"all":   "echo '{\"outputs\":[]}' > /data/output/validation_result.json; echo '{\"o\":2}' > /data/output/export_result.json; echo '{\"usage\":{\"total_tokens\":9}}' > /data/output/result.json",
		"none":  "echo hi > /data/output/a.txt",
		"one":   "printf '  {\"usage\":1}  \\n\\n' > /data/output/result.json",
		"empty": ": > /data/output/result.json",
		"nofile": ":",
	}
	for tag, sc := range scripts {
		vol := "hp-par-" + tag + "-" + strconv.Itoa(os.Getpid())
		hpVolume(t, vol, sc)
		want := e.readResultJSONFiles(ctx, vol, "alpine:3.20", resultFileReadNames)
		got, err := e.readResultFilesViaArchive(ctx, vol, "alpine:3.20", resultFileReadNames)
		if err != nil {
			t.Fatalf("%s: archive read error: %v", tag, err)
		}
		for _, n := range resultFileReadNames {
			if got[n] != want[n] {
				t.Errorf("%s/%s: archive %q != helper %q", tag, n, got[n], want[n])
			}
		}
	}
	if n := hpLeaked(t); n != 0 {
		t.Errorf("%d helper containers leaked", n)
	}
}

// A symlinked / directory result.json must NOT be answered by the archive read
// (error -> the caller runs the helper read and keeps its exact answer).
func TestHPArchiveReadSymlinkAndDirFallBack(t *testing.T) {
	e := u1sExec(t)
	e.workerName = "hp-test"
	ctx := context.Background()
	for tag, sc := range map[string]string{
		"relsym": "echo '{\"usage\":1}' > /data/output/real.json; ln -s real.json /data/output/result.json",
		"abssym": "ln -s /etc/passwd /data/output/result.json",
		"dir":    "mkdir /data/output/result.json",
	} {
		vol := "hp-sym-" + tag + "-" + strconv.Itoa(os.Getpid())
		hpVolume(t, vol, sc)
		if got, err := e.readResultFilesViaArchive(ctx, vol, "alpine:3.20", resultFileReadNames); err == nil {
			t.Errorf("%s: must error so the helper read decides, got %v", tag, got)
		}
	}
	if n := hpLeaked(t); n != 0 {
		t.Errorf("%d helper containers leaked", n)
	}
}

// Errors: a missing helper image fails closed (error), no leak.
func TestHPArchiveReadBadImageFailsClosed(t *testing.T) {
	e := u1sExec(t)
	e.workerName = "hp-test"
	if _, err := e.readResultFilesViaArchive(context.Background(), "hp-none-vol", "no-such-image-hp:0", resultFileReadNames); err == nil {
		t.Fatalf("missing image must error")
	}
	if n := hpLeaked(t); n != 0 {
		t.Errorf("%d helper containers leaked", n)
	}
}

func hpMs(since time.Time) float64 { return float64(time.Since(since).Microseconds()) / 1000 }

func TestHPBenchHelperVsArchive(t *testing.T) {
	if os.Getenv("HP_BENCH") != "1" {
		t.Skip("set HP_BENCH=1")
	}
	e := u1sExec(t)
	ctx := context.Background()
	meta := "echo '{\"outputs\":[]}' > /data/output/export_result.json; echo '{\"usage\":{\"total_tokens\":3}}' > /data/output/result.json; "
	for _, sz := range []int{1024, 10 << 20, 100 << 20} {
		vol := "hp-bench-" + strconv.Itoa(sz)
		hpVolume(t, vol, meta+"head -c "+strconv.Itoa(sz)+" /dev/urandom > /data/output/big.bin")
		var oldMs, perMs, fullMs []float64
		for i := 0; i < 22; i++ {
			s := time.Now()
			_ = e.readResultJSONFiles(ctx, vol, "alpine:3.20", resultFileReadNames)
			o := hpMs(s)
			s = time.Now()
			if _, err := e.readResultFilesViaArchive(ctx, vol, "alpine:3.20", resultFileReadNames); err != nil {
				t.Fatalf("per-file: %v", err)
			}
			p := hpMs(s)
			s = time.Now()
			tb, err := e.ceReadOutputTarViaArchive(ctx, vol, "alpine:3.20")
			_ = ceFilesFromTar(tb, resultFileReadNames)
			if err != nil {
				t.Fatalf("full: %v", err)
			}
			f := hpMs(s)
			if i >= 2 { // 2 warm-up rounds dropped: n=20
				oldMs, perMs, fullMs = append(oldMs, o), append(perMs, p), append(fullMs, f)
			}
		}
		t.Logf("size=%d HELPER-RUN (today)      %s", sz, u1sStats(oldMs))
		t.Logf("size=%d ARCHIVE per-file (new)  %s", sz, u1sStats(perMs))
		t.Logf("size=%d ARCHIVE full-tree (CE)  %s", sz, u1sStats(fullMs))
	}
}
