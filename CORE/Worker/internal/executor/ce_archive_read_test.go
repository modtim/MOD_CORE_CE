package executor

// UNDER1S-OUTPUT-SCAN-AND-UPLOAD unit tests (no docker): the pure tar parse,
// the prefetched-tar slot, and the end-to-end hand-off archive tar -> scan
// staging -> durable upload with NO docker helper run and matching hashes.

import (
	"archive/tar"
	"bytes"
	"context"
	"net/http/httptest"
	"os/exec"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func u1sTar(t *testing.T, ents []tar.Header, bodies []string) []byte {
	t.Helper()
	var b bytes.Buffer
	tw := tar.NewWriter(&b)
	for i := range ents {
		h := ents[i]
		h.Size = int64(len(bodies[i]))
		if h.Typeflag != tar.TypeReg {
			h.Size = 0
		}
		if err := tw.WriteHeader(&h); err != nil {
			t.Fatal(err)
		}
		if h.Typeflag == tar.TypeReg {
			tw.Write([]byte(bodies[i]))
		}
	}
	tw.Close()
	return b.Bytes()
}

func TestU1SFilesFromTar_TrimMissingNestedSymlinkTraversal(t *testing.T) {
	tb := u1sTar(t, []tar.Header{
		{Name: "./", Typeflag: tar.TypeDir},
		{Name: "./result.json", Typeflag: tar.TypeReg, Mode: 0o644},
		{Name: "./export_result.json", Typeflag: tar.TypeSymlink, Linkname: "/etc/passwd"},
		{Name: "./sub/validation_result.json", Typeflag: tar.TypeReg, Mode: 0o644},
		{Name: "../validation_result.json", Typeflag: tar.TypeReg, Mode: 0o644},
	}, []string{"", "  {\"a\":1}\n\n", "", "nested", "traversal"})
	got := ceFilesFromTar(tb, resultFileReadNames)
	if got["result.json"] != `{"a":1}` {
		t.Errorf("trimmed content expected, got %q", got["result.json"])
	}
	for _, n := range []string{"export_result.json", "validation_result.json"} {
		if v, ok := got[n]; !ok || v != "" {
			t.Errorf("%s must be present and empty (symlink/nested/traversal), got %q ok=%v", n, v, ok)
		}
	}
	if len(got) != len(resultFileReadNames) {
		t.Errorf("every requested name present, nothing else: %v", got)
	}
	if g := ceFilesFromTar([]byte("not a tar"), resultFileReadNames); g["result.json"] != "" || len(g) != 3 {
		t.Errorf("garbage tar must read as all-missing, got %v", g)
	}
}

func TestU1SPrefetchSlot_ScopedOneShotDrop(t *testing.T) {
	cePrefetchOutputTar("vA", 1, []byte("x"))
	if _, ok := ceTakePrefetchedTar("vA", 2); ok {
		t.Fatal("other task must miss")
	}
	if b, ok := ceTakePrefetchedTar("vA", 1); !ok || string(b) != "x" {
		t.Fatal("owner must get it")
	}
	if _, ok := ceTakePrefetchedTar("vA", 1); ok {
		t.Fatal("one-shot")
	}
	cePrefetchOutputTar("vA", 3, []byte("y"))
	ceDropPrefetchedTar("vA", 4)
	if _, ok := ceTakePrefetchedTar("vA", 3); !ok {
		t.Fatal("drop for another task must not evict")
	}
	cePrefetchOutputTar("vA", 5, []byte("z"))
	ceDropPrefetchedTar("vA", 5)
	if _, ok := ceTakePrefetchedTar("vA", 5); ok {
		t.Fatal("drop must release")
	}
}

func TestU1SArchiveTarFlowsStageToUploadWithoutDocker(t *testing.T) {
	orig := execCommandContext
	execCommandContext = func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		t.Fatalf("docker helper must NOT run when the archive tar is prefetched: %s %v", name, arg)
		return exec.Command("true")
	}
	defer func() { execCommandContext = orig }()
	body := "hello-output-bytes"
	tb := u1sTar(t, []tar.Header{{Name: "./", Typeflag: tar.TypeDir},
		{Name: "./out.bin", Typeflag: tar.TypeReg, Mode: 0o644}}, []string{"", body})
	fake := newScanFakeS3("tenant-7-files")
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := &transport.Credentials{MinIO: scanCreds(srv)}
	task := scanStagedTask(9301, 7)
	cePrefetchOutputTar("volU", task.ID, tb)
	e := &Executor{}
	staged, err := e.ceStageOutputsForScan(context.Background(), "alpine:3.20", "volU", creds, task)
	if err != nil || len(staged) != 1 {
		t.Fatalf("stage: %v %+v", err, staged)
	}
	outs, err := e.ceUploadInProcess(context.Background(), "alpine:3.20", "volU", creds, task)
	if err != nil || len(outs) != 1 {
		t.Fatalf("upload: %v %+v", err, outs)
	}
	want := scanSha256(body)
	if staged[0].ContentHash != want || outs[0].ContentHash != want {
		t.Fatalf("scanned hash %s and uploaded hash %s must both equal sha256(bytes) %s",
			staged[0].ContentHash, outs[0].ContentHash, want)
	}
	if !scanHas(fake, "scan-staging/9301/"+want) {
		t.Fatalf("staged object missing")
	}
	durable := 0
	for _, k := range fake.snapshotKeys() {
		if strings.HasSuffix(k, want) && !strings.HasPrefix(k, "scan-staging/") {
			durable++
			fake.mu.Lock()
			got := string(fake.objs[k])
			fake.mu.Unlock()
			if got != body {
				t.Fatalf("durable object bytes differ from the scanned bytes")
			}
		}
	}
	if durable != 1 {
		t.Fatalf("exactly one durable object expected, got %d (keys %v)", durable, fake.snapshotKeys())
	}
}
