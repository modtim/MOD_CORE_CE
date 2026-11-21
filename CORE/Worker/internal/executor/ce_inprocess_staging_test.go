package executor

// 1665v (CE Gap Z, phase 1) — the CE no-registry posture stages inputs/outputs
// in process (minio-go + an alpine tar bridge) instead of runtime-pulling the
// AGPL minio/mc client from Docker Hub. Pure tests only: the branch selection
// (helper image under the posture, Harbor ref unchanged with a registry) and
// the tar bridge helpers (round-trip; traversal/absolute/symlink rejection).
// The ce_registry_posture_test.go Hub-ref assertion is NOT inverted here — the
// sidecarImageRef fallback itself stays (phase 2, 1665w, retires it).

import (
	"archive/tar"
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func TestCeStagingHelper_NoRegistryUsesAlpineHelper(t *testing.T) {
	// (the CE posture must never name the minio/mc ref for the staging legs)
	got := ceStagingHelper(transport.HarborCredentials{}, "")
	if got != ceStageHelperImage {
		t.Errorf("CE staging legs must run on the alpine helper, got %q", got)
	}
	if ceStageHelperImage != "alpine:3.20" {
		t.Errorf("the CE helper must be the checkpoint_transfer helper image, got %q", ceStageHelperImage)
	}
}

func TestCeStagingHelper_RegistryKeepsMinioClientRef(t *testing.T) {
	// (byte-for-byte the paid path: the Harbor posture selects the very ref
	// sidecarImageRef renders, so every leg below sees the image it always did)
	got := ceStagingHelper(
		transport.HarborCredentials{Registry: "reg.example", Username: "u", Password: "p"},
		"reg.example",
	)
	if got != sidecarImageRef("reg.example") {
		t.Errorf("expected the Harbor-prefixed sidecar ref, got %q", got)
	}
}

// --- the tar bridge: naming parity + trust boundary ---------------------

func TestCePlainEntryName_RejectsTraversalAndNesting(t *testing.T) {
	// (busybox `tar cf - -C dir .` prefixes members with "./" — that prefix is
	// the container, not part of the name)
	if got := cePlainEntryName("./out.mp4"); got != "out.mp4" {
		t.Errorf("the ./ prefix must be stripped, got %q", got)
	}
	for _, bad := range []string{"../x", "..", "./../x", "/etc/passwd", "a/b", `a\b`, "", "./", "."} {
		if got := cePlainEntryName(bad); got != "" {
			t.Errorf("must reject %q, got %q", bad, got)
		}
	}
}

func TestCeTarBridge_RoundTrip(t *testing.T) {
	dir, err := os.MkdirTemp("", "ce-tar-src-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	// (two files, one nested dir the writer must not silently flatten)
	if mkerr := os.Mkdir(filepath.Join(dir, "sub"), 0o755); mkerr != nil {
		t.Fatal(mkerr)
	}
	payload := []byte("ce-bridge\n")
	if werr := os.WriteFile(filepath.Join(dir, "a.txt"), payload, 0o644); werr != nil {
		t.Fatal(werr)
	}
	if werr := os.WriteFile(filepath.Join(dir, "sub", "deep.bin"), []byte("d"), 0o644); werr != nil {
		t.Fatal(werr)
	}
	tarBytes, werr := ceWriteTarDir(dir)
	if werr != nil {
		t.Fatal(werr)
	}
	dst, derr := os.MkdirTemp("", "ce-dst-")
	if derr != nil {
		t.Fatal(derr)
	}
	defer os.RemoveAll(dst)
	names, rerr := ceReadTarIntoDir(tarBytes, dst)
	if rerr != nil {
		t.Fatal(rerr)
	}
	if len(names) != 1 || names[0] != "a.txt" {
		t.Fatalf("expected exactly the flat file a.txt, got %v", names)
	}
	back, rerr2 := os.ReadFile(filepath.Join(dst, "a.txt"))
	if rerr2 != nil {
		t.Fatal(rerr2)
	}
	if bytes.Compare(back, payload) != 0 {
		t.Errorf("round-trip bytes differ")
	}
}

// Naming parity: the in-process leg must land exactly the filenames the mc
// sidecar would have (buildDownloadCmd), so a node container that reads
// /data/input/<name> cannot tell the two paths apart. stagingFileRefs is the
// native path's single source of truth for dest names; asserting it against
// buildDownloadCmd's rendered destinations pins the contract without a MinIO,
// a daemon or a network.
func TestCeStagingNaming_ParityWithBuildDownloadCmd(t *testing.T) {
	multi := transport.TaskPayload{
		MinioBucket: "b",
		NodeKind:    "image_input",
		InputFiles: []transport.InputFile{
			transport.InputFile{CogID: 47, ContentHash: "h1", FileExtension: "jpg"},
			transport.InputFile{CogID: 8, ContentHash: "h2", FileExtension: "DNG"},
		},
	}
	cmd, err := buildDownloadCmd(multi)
	if err != nil {
		t.Fatal(err)
	}
	refs, rerr := stagingFileRefs(multi)
	if rerr != nil {
		t.Fatal(rerr)
	}
	if len(refs) != 2 {
		t.Fatalf("expected 2 staged refs, got %d", len(refs))
	}
	for _, r := range refs {
		want := "/data/input/" + r.dest
		if !strings.Contains(cmd, want) {
			t.Errorf("mc cmd must stage %s, got: %s", want, cmd)
		}
	}
	// (single-file shape: input_<nodeKind><ext>)
	single := transport.TaskPayload{
		MinioBucket: "b", NodeKind: "video_transcode", FileExtension: ".mp4",
		SourceFileLocation: "abc",
	}
	scmd, serr := buildDownloadCmd(single)
	if serr != nil {
		t.Fatal(serr)
	}
	srefs, sreferr := stagingFileRefs(single)
	if sreferr != nil || len(srefs) != 1 {
		t.Fatalf("single-file staging must yield exactly one ref, got %v err %v", srefs, sreferr)
	}
	wantSingle := "/data/input/" + srefs[0].dest
	if !strings.Contains(scmd, wantSingle) {
		t.Errorf("mc cmd must stage %s, got: %s", wantSingle, scmd)
	}
}

// The upload leg's trust boundary: a node container can write a symlink or a
// traversal into /data/output; the untar side must refuse it rather than
// stage bytes outside the temp dir (which would then be uploaded).
func TestCeReadTar_RejectsSymlinkAndTraversal(t *testing.T) {
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	// (a symlink pointing at a host path — the classic escape)
	if werr := tw.WriteHeader(&tar.Header{
		Name: "evil", Mode: 0o777, Size: 0, Typeflag: tar.TypeSymlink,
		Linkname: "/etc/passwd",
	}); werr != nil {
		t.Fatal(werr)
	}
	_ = tw.Close()
	dst, derr := os.MkdirTemp("", "ce-reject-")
	if derr != nil {
		t.Fatal(derr)
	}
	defer os.RemoveAll(dst)
	if _, rerr := ceReadTarIntoDir(buf.Bytes(), dst); rerr == nil {
		t.Fatal("a symlink entry must be refused")
	}
	// (a traversal member: "../x" resolves outside the staging dir)
	var buf2 bytes.Buffer
	tw2 := tar.NewWriter(&buf2)
	if werr := tw2.WriteHeader(&tar.Header{
		Name: "../x", Mode: 0o644, Size: 1, Typeflag: tar.TypeReg,
	}); werr != nil {
		t.Fatal(werr)
	}
	_, w2 := tw2.Write([]byte("z"))
	if w2 != nil {
		t.Fatal(w2)
	}
	_ = tw2.Close()
	if _, rerr2 := ceReadTarIntoDir(buf2.Bytes(), dst); rerr2 == nil {
		t.Fatal("a traversal member must be refused")
	}
	if st, err := os.Stat(filepath.Join(filepath.Dir(dst), "x")); err == nil && st.Mode().IsRegular() {
		t.Fatal("a traversal member escaped the staging dir")
	}
}

// --- 1665w (phase 2): the three legs that used to shell out to `mc` ---------

func TestCeScanStagingKey_ParityWithMcScript(t *testing.T) {
	// (the staging script wrote <prefix><sha256> in the task's own bucket,
	// prefix = stagingUploadParamsFromTask; the in-process pass must render the
	// identical key so task_output_scan.py:_validate_staging_key still accepts
	// it and the pre-store consult fetches the same bytes)
	task := transport.Task{
		ID: 4242,
		Payload: transport.TaskPayload{MinioBucket: "b", Purpose: "wizard_test_run",
			// (the durable path's OutputPrefix never enters staging: stagingUpload
			// ParamsFromTask ignores it, exactly as nativeScanStagingParams does)
			OutputPrefix: "_wizard_test_runs/s/1/", SandboxMaxOutputMB: 7},
	}
	docker := stagingUploadParamsFromTask(&task)
	native := nativeScanStagingParams(&task)
	if docker.Prefix != native.Prefix {
		t.Errorf("staging prefix must match the sidecar params, got %q vs %q", docker.Prefix, native.Prefix)
	}
	if docker.CapBytes != native.CapBytes || docker.Bucket != native.Bucket {
		t.Errorf("staging params must match the sidecar params (bucket/cap)")
	}
	// (the key the sidecar script echoed - `UPLOAD,<prefix><hash>,...` - vs the
	// key the in-process pass Puts)
	manifest := buildUploadScript(docker)
	// (the sidecar PUTs myminio/<bucket>/<prefix>$hash and echoes UPLOAD,<prefix>
	// <hash>,...; the in-process pass Puts native.Prefix+<sha256> - same key)
	if !strings.Contains(manifest, "myminio/"+docker.Bucket+"/"+docker.Prefix+"$hash") {
		t.Errorf("the sidecar script must stage at <prefix><hash>, got script %q", manifest)
	}
	if !strings.Contains(manifest, "UPLOAD,"+docker.Prefix) {
		t.Errorf("the manifest key column must carry the prefix, got script %q", manifest)
	}
	if docker.Prefix != native.Prefix {
		t.Errorf("expected the same staging key prefix, got %q vs %q", docker.Prefix, native.Prefix)
	}
	// (the fixed checkpoint-export key is unchanged by phase 2)
	if k := checkpointExportStagingKey(task.ID); k != "scan-staging/4242/checkpoint.tar" {
		t.Errorf("checkpoint staging key must stay fixed under the prefix, got %q", k)
	}
}

func TestCeCleanupRemovesExactlyStagedKeys(t *testing.T) {
	// (the sidecar cleanup was `rm -r <bucket>/scan-staging/<id>/`; the in-
	// process cleanup lists that exact prefix and removes every key under it,
	// never a neighbouring task's)
	task := transport.Task{ID: 77, Payload: transport.TaskPayload{MinioBucket: "b"}}
	other := transport.Task{ID: 78, Payload: transport.TaskPayload{MinioBucket: "b"}}
	staged := stagingUploadParamsFromTask(&task)
	removed := nativeScanStagingParams(&task)
	if removed.Prefix != "scan-staging/77/" {
		t.Errorf("cleanup prefix must be this task's own staging prefix, got %q", removed.Prefix)
	}
	if staged.Prefix != removed.Prefix {
		t.Errorf("cleanup must remove exactly what staging wrote")
	}
	if removed.Prefix == nativeScanStagingParams(&other).Prefix {
		t.Errorf("a neighbouring task's staging prefix must be distinct")
	}
	// (the checkpoint-export cleanup removes the same fixed key's prefix)
	if !strings.HasPrefix(checkpointExportStagingKey(task.ID), removed.Prefix) {
		t.Errorf("the checkpoint staging key must sit inside the cleaned prefix")
	}
}

func TestCeNoMinioMcReachableUnderPosture(t *testing.T) {
	// (the literal is assembled at runtime so this test file never contains
	// the string it greps for)
	needle := "minio" + "/mc"
	// (branch table first: under the posture the sidecar ref is empty and the
	// only volume shell is the alpine helper, so no leg can name the client)
	if sidecarImageRef("") != "" {
		t.Errorf("the posture must have no sidecar ref at all")
	}
	ce := ceStagingHelper(transport.HarborCredentials{}, "")
	if ce != ceStageHelperImage || strings.Contains(ce, needle) {
		t.Errorf("the posture's volume shell must be the alpine helper, got %q", ce)
	}
	harbor := ceStagingHelper(
		transport.HarborCredentials{Registry: "reg.example", Username: "u", Password: "p"},
		"reg.example")
	if ce == harbor {
		t.Errorf("the two postures must select different volume shells")
	}
	// (then the source scan: neither file the posture runs through may carry
	// the literal anywhere, comment or code)
	for _, name := range []string{"executor.go", "ce_inprocess_staging.go"} {
		var found bool
		for _, cand := range []string{name,
			"internal/executor/" + name,
			"../internal/executor/" + name,
			"../../internal/executor/" + name} {
			raw, err := os.ReadFile(cand)
			if err != nil {
				continue
			}
			found = true
			for _, line := range strings.Split(string(raw), "\n") {
				if strings.Contains(line, needle) {
					t.Errorf("%s must not reference the public mc client at all, got %q", name, line)
				}
			}
			break
		}
		if !found {
			t.Errorf("source scan skipped %s - run from the package or the Worker root", name)
		}
	}
}
