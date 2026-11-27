package executor

// UNDER1S-OUTPUT-SCAN-AND-UPLOAD: real-docker parity + benchmark for the archive
// read. Skipped when no Docker socket / alpine:3.20 is present. The benchmark
// leg only runs with U1S_BENCH=1 (a test-only switch; no production behaviour).

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

func u1sMakeVolume(t *testing.T, name string, bigBytes int, hostile bool) {
	t.Helper()
	script := "mkdir -p /data/output/sub && echo '{\"outputs\":[]}' > /data/output/export_result.json" +
		" && echo '{\"usage\":{\"prompt_tokens\":3}}' > /data/output/result.json"
	if hostile {
		script += " && echo hi > /data/output/sub/nested.txt && ln -s /etc/passwd /data/output/link.txt"
	}
	if bigBytes > 0 {
		script += " && head -c " + strconv.Itoa(bigBytes) + " /dev/urandom > /data/output/big.bin"
	}
	if out, err := exec.Command("docker", "volume", "create", name).CombinedOutput(); err != nil {
		t.Skipf("docker volume create: %v %s", err, out)
	}
	t.Cleanup(func() { exec.Command("docker", "volume", "rm", "-f", name).Run() })
	if out, err := exec.Command("docker", "run", "--rm", "-v", name+":/data", "alpine:3.20",
		"sh", "-c", script).CombinedOutput(); err != nil {
		t.Skipf("seed volume (alpine:3.20 present?): %v %s", err, out)
	}
}

func u1sExec(t *testing.T) *Executor {
	cli := dockerOrSkip(t)
	return &Executor{docker: cli, workerName: "u1s-test", instanceID: "u1s"}
}

// Parity: the archive tar, fed through the SAME trust boundary, yields the same
// output tree as the helper `tar cf -`, and the metadata files read the same.
func TestU1SArchiveReadParityWithHelper(t *testing.T) {
	e := u1sExec(t)
	vol := "u1s-parity-" + strconv.Itoa(os.Getpid())
	u1sMakeVolume(t, vol, 4096, false)
	ctx := context.Background()
	arch, err := e.ceReadOutputTarViaArchive(ctx, vol, "alpine:3.20")
	if err != nil {
		t.Fatalf("archive read: %v", err)
	}
	var helper bytes.Buffer
	cmd := exec.Command("docker", "run", "--rm", "-v", vol+":/data:ro", "alpine:3.20",
		"tar", "cf", "-", "-C", "/data/output", ".")
	cmd.Stdout = &helper
	if err := cmd.Run(); err != nil {
		t.Fatalf("helper tar: %v", err)
	}
	da, db := t.TempDir(), t.TempDir()
	na, ea := ceReadTarIntoDir(arch, da)
	nb, eb := ceReadTarIntoDir(helper.Bytes(), db)
	if ea != nil || eb != nil {
		t.Fatalf("untar errs: %v / %v", ea, eb)
	}
	sort.Strings(na)
	sort.Strings(nb)
	if strings.Join(na, ",") != strings.Join(nb, ",") {
		t.Fatalf("file lists differ: archive=%v helper=%v", na, nb)
	}
	for _, n := range na {
		ba, _ := os.ReadFile(filepath.Join(da, n))
		bb, _ := os.ReadFile(filepath.Join(db, n))
		if !bytes.Equal(ba, bb) {
			t.Errorf("content differs for %s", n)
		}
	}
	got := ceFilesFromTar(arch, resultFileReadNames)
	want := e.readResultJSONFiles(ctx, vol, "alpine:3.20", resultFileReadNames)
	for _, n := range resultFileReadNames {
		if got[n] != want[n] {
			t.Errorf("metadata %s: archive=%q helper=%q", n, got[n], want[n])
		}
	}
	// no leaked helper container
	out, _ := exec.Command("docker", "ps", "-a", "-q", "--filter", "label="+sidecarLabel+"=1",
		"--filter", "label="+sidecarWorkerLabel+"=u1s-test").Output()
	if strings.TrimSpace(string(out)) != "" {
		t.Errorf("leaked helper container(s): %s", out)
	}
}

func u1sStats(ms []float64) string {
	sort.Float64s(ms)
	n := len(ms)
	return fmt.Sprintf("n=%d min=%.0f med=%.0f p90=%.0f max=%.0f ms", n, ms[0], ms[n/2], ms[(n*9)/10], ms[n-1])
}

func TestU1SBenchArchiveVsHelper(t *testing.T) {
	if os.Getenv("U1S_BENCH") != "1" {
		t.Skip("set U1S_BENCH=1")
	}
	e := u1sExec(t)
	ctx := context.Background()
	for _, sz := range []int{1024, 100 * 1024, 10 << 20, 100 << 20} {
		vol := "u1s-bench-" + strconv.Itoa(sz)
		u1sMakeVolume(t, vol, sz, false)
		var oldMs, newMs []float64
		for i := 0; i < 20; i++ {
			s := time.Now()
			_ = e.readResultJSONFiles(ctx, vol, "alpine:3.20", resultFileReadNames)
			var b bytes.Buffer
			c := exec.Command("docker", "run", "--rm", "-v", vol+":/data:ro", "alpine:3.20",
				"tar", "cf", "-", "-C", "/data/output", ".")
			c.Stdout = &b
			c.Run()
			oldMs = append(oldMs, float64(time.Since(s).Microseconds())/1000)
			s = time.Now()
			tb, err := e.ceReadOutputTarViaArchive(ctx, vol, "alpine:3.20")
			_ = ceFilesFromTar(tb, resultFileReadNames)
			if err != nil {
				t.Fatalf("archive: %v", err)
			}
			newMs = append(newMs, float64(time.Since(s).Microseconds())/1000)
		}
		t.Logf("size=%d OLD(read-helper+tar-helper) %s", sz, u1sStats(oldMs))
		t.Logf("size=%d NEW(archive read)            %s", sz, u1sStats(newMs))
	}
}

// Hostile tree (nested dir + symlink): BOTH tars must be refused by the same
// trust boundary, and the symlink must read as a missing metadata file.
func TestU1SArchiveReadHostileTreeRefusedLikeHelper(t *testing.T) {
	e := u1sExec(t)
	vol := "u1s-hostile-" + strconv.Itoa(os.Getpid())
	u1sMakeVolume(t, vol, 0, true)
	ctx := context.Background()
	arch, err := e.ceReadOutputTarViaArchive(ctx, vol, "alpine:3.20")
	if err != nil {
		t.Fatalf("archive read: %v", err)
	}
	if _, uerr := ceReadTarIntoDir(arch, t.TempDir()); uerr == nil {
		t.Fatalf("archive tar with symlink/nested entries must be refused by ceReadTarIntoDir")
	}
	_ = os.WriteFile(filepath.Join(t.TempDir(), "x"), nil, 0o600)
	got := ceFilesFromTar(arch, append([]string{"link.txt"}, resultFileReadNames...))
	if got["link.txt"] != "" {
		t.Errorf("symlink must read as missing, got %q", got["link.txt"])
	}
	if got["result.json"] == "" {
		t.Errorf("regular metadata file must still read")
	}
}
