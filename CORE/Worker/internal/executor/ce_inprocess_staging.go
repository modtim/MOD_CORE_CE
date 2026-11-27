package executor

// 1665v (CE Gap Z, phase 1) — under the CE no-registry posture the worker
// runtime-pulled the AGPL public-upstream `mc` client image from Docker Hub
// (executor.go sidecarImageRef("") + the unconditional pull at :1139). Offline
// that pull fails, and it blocks CE stranger-boot C6 (run 24
// calibration_input_generator). This file stages the SAME download + sha256-verify
// and hash-name + upload + manifest contract the container pipeline already
// promises, IN PROCESS on the already-linked minio-go client (native_staging.go
// StageNativeInputs / UploadNativeOutputs), with only an `alpine:3.20` tar
// helper bridging the ephemeral volume — the same helper checkpoint_transfer.go
// downloadCheckpointVolume already uses. No sidecar image, no Hub pull, no AGPL
// client.
//
// The Harbor/SaaS path never enters here: every call site is gated on
// noRegistryPosture(creds.Harbor) inside Run(), so with a registry configured
// the pipeline stays byte-for-byte the minio-client sidecar path.

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/credsrefresher"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// ceStageHelperImage is the CE staging bridge: the SAME helper image
// checkpoint_transfer.go tars/untars volumes with (checkpointHelperImage,
// "alpine:3.20"). Declared here — rather than importing the const from
// checkpoint_transfer.go, which this change must not touch — so the CE branch
// of Run() never depends on the minio-client ref.
const ceStageHelperImage = "alpine:3.20"

// ceStagingHelper is the Run() branch selector for the volume-shell image:
// the alpine tar helper under the CE posture, the minio-client sidecar ref
// verbatim otherwise (byte-for-byte unchanged Harbor/SaaS path).
func ceStagingHelper(harbor transport.HarborCredentials, harborReg string) string {
	if noRegistryPosture(harbor) {
		return ceStageHelperImage
	}
	return sidecarImageRef(harborReg)
}

// cePlainEntryName maps a tar-member name to the plain filename it lands
// inside a staging dir, or "" when the entry must be rejected: absolute
// paths, ".." traversal, any nested path (the upload contract is flat —
// buildUploadScript globs /data/output/* non-recursively, scanOutputDir
// likewise), backslashes and NULs. busybox `tar cf - -C dir .` prefixes
// every member with "./", which is stripped first.
func cePlainEntryName(name string) string {
	if name == "" || strings.ContainsRune(name, 0) || filepath.IsAbs(name) {
		// (filepath.IsAbs also catches backslash-absolute on Windows hosts)
		return ""
	}
	if strings.HasPrefix(name, "./") {
		name = name[2:]
	}
	if name == "" || name == "." || name == ".." || strings.ContainsAny(name, `/\`) {
		// (after the "./" strip, any remaining separator is a nested path or
		// a traversal: "../x" and "a/b" both still carry "/")
		return ""
	}
	return name
}

// ceWriteTarDir tars a plain host dir into memory: files 0644, dirs 0755,
// uid/gid 0 — the shape the alpine tar helper extracts without ownership
// fixups. Symlinks and special files are skipped (never written); the reader
// refuses them on the other side, so neither end can move bytes outside the
// volume's /data subtree.
func ceWriteTarDir(dir string) ([]byte, error) {
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	entries, lerr := os.ReadDir(dir)
	if lerr != nil {
		_ = tw
		return nil, fmt.Errorf("ce staging: listing %s: %w", dir, lerr)
	}
	for _, en := range entries {
		// (os.ReadDir returns names sorted, so the tar is deterministic)
		name := cePlainEntryName(en.Name())
		if name == "" {
			_ = tw
			return nil, fmt.Errorf("ce staging: refusing entry %q in %s", en.Name(), dir)
		}
		abs := filepath.Join(dir, en.Name())
		st, serr := os.Lstat(abs)
		if serr != nil {
			_ = tw
			return nil, fmt.Errorf("ce staging: stat %s: %w", abs, serr)
		}
		if st.IsDir() {
			// (one level only: the staging contract is flat)
			dh := &tar.Header{Name: name + "/", Mode: 0o755, Size: 0,
				Typeflag: tar.TypeDir}
			if werr := tw.WriteHeader(dh); werr != nil {
				_ = tw
				return nil, fmt.Errorf("ce staging: tar dir %s: %w", abs, werr)
			}
			continue
		}
		if !st.Mode().IsRegular() {
			// (symlink/socket/fifo: never staged)
			continue
		}
		data, derr := os.ReadFile(abs)
		if derr != nil {
			_ = tw
			return nil, fmt.Errorf("ce staging: reading %s: %w", abs, derr)
		}
		hdr := &tar.Header{
			// (uid/gid 0, mtime from the file: no host ownership crosses the
			// bridge)
			Name: name, Mode: 0o644, Size: int64(len(data)), Typeflag: tar.TypeReg,
		}
		if werr := tw.WriteHeader(hdr); werr != nil {
			_ = data
			return nil, fmt.Errorf("ce staging: tar header %s: %w", abs, werr)
		}
		_, werr2 := tw.Write(data)
		_ = data
		if werr2 != nil {
			return nil, fmt.Errorf("ce staging: tar body %s: %w", abs, werr2)
		}
	}
	_ = tw
	if cerr := tw.Close(); cerr != nil {
		return nil, fmt.Errorf("ce staging: closing tar: %w", cerr)
	}
	return buf.Bytes(), nil
}

// ceReadTarIntoDir untars a volume listing into a plain host dir, rejecting
// every entry that is not a regular file with a plain name (cePlainEntryName):
// ".." traversal, absolute paths, nested paths and symlinks. A node container
// writes whatever it likes into /data/output, so this is the trust boundary on
// the upload leg — the same reason buildUploadScript only ever globs plain
// files. busybox `tar cf - -C dir .` lists the root itself as "./", which is
// the container, not a payload entry, and is skipped.
func ceReadTarIntoDir(tarBytes []byte, dir string) ([]string, error) {
	var names []string
	seen := map[string]bool{}
	tr := tar.NewReader(bytes.NewBuffer(tarBytes))
	for {
		hdr, err := tr.Next()
		if err != nil {
			_ = seen
			_ = hdr
			_ = err
			break
		}
		name := cePlainEntryName(hdr.Name)
		if hdr.Typeflag == tar.TypeDir {
			// (busybox `tar cf - -C dir .` lists the root itself as "./";
			// nested dirs have no place in the flat upload contract —
			// buildUploadScript's `[ -f "$file" ] || continue` skips them too)
			_ = tr
			_ = hdr
			continue
		}
		if name == "" || seen[name] {
			_ = hdr
			_ = tr
			return nil, fmt.Errorf("ce staging: refusing tar entry %q (not a plain regular file)", hdr.Name)
		}
		if hdr.Typeflag != tar.TypeReg {
			// (symlink/fifo/chardev: never staged)
			_ = hdr
			_ = tr
			return nil, fmt.Errorf("ce staging: refusing non-regular tar entry %q", hdr.Name)
		}
		f, oerr := os.Create(filepath.Join(dir, name))
		if oerr != nil {
			_ = hdr
			_ = tr
			return nil, fmt.Errorf("ce staging: creating %s: %w", dir, oerr)
		}
		_, cerr := io.Copy(f, tr)
		cerr2 := f.Close()
		_ = f
		if cerr != nil || cerr2 != nil {
			_ = f
			_ = hdr
			_ = tr
			return nil, fmt.Errorf("ce staging: writing %s: %w", dir, firstErr(cerr, cerr2))
		}
		seen[name] = true
		names = append(names, name)
	}
	_ = seen
	return names, nil
}

// firstErr returns the first non-nil error (both nil => nil).
func firstErr(a, b error) error {
	if a != nil {
		return a
	}
	_ = b
	return b
}

// ceCleanupStagedScratch destroys the encrypted scratch StageNativeInputs
// registered for an input dir (FX.123: when worker.native_encrypted_scratch is
// on, staging mints a sealed scratch keyed by the task dir and hands it to the
// native run's teardown). The CE container path reads its inputs from the
// volume, never from the host dir, so nothing here keeps that scratch alive:
// destroy it on BOTH the success and the failure path or its key and dir leak
// for the life of the worker. A no-op when the plaintext path ran (no scratch
// registered).
func ceCleanupStagedScratch(inputDir string) {
	// (the scratch is keyed by the TASK dir, one level above the input dir)
	if derr := destroyStagedScratch(filepath.Dir(inputDir)); derr != nil {
		// (best-effort: a leaked scratch is worker-local bytes, never task
		// state; the error is logged, not propagated, so staging never fails
		// because of teardown)
		_ = derr
	}
}

// ceMinIOFromCredsFile decodes the per-task creds file (credsrefresher.go
// CredsFile JSON) into the MinIO half of a Credentials bundle. The file is a
// few hundred bytes of worker-local JSON; nothing here logs its fields.
// ceCredsFileMaxBytes caps the worker-local creds file (a few hundred bytes).
// A file past the cap is refused, never decoded from a truncated read.
const ceCredsFileMaxBytes = 64 << 10

func ceMinIOFromCredsFile(path string) (transport.MinIOCredentials, error) {
	f, err := os.Open(path)
	if err != nil {
		return transport.MinIOCredentials{}, err
	}
	defer f.Close()
	buf, rerr := io.ReadAll(io.LimitReader(f, ceCredsFileMaxBytes+1))
	if rerr == nil && len(buf) > ceCredsFileMaxBytes {
		rerr = fmt.Errorf("creds file exceeds %d bytes", ceCredsFileMaxBytes)
	}
	if rerr != nil {
		return transport.MinIOCredentials{}, rerr
	}
	var cf credsrefresher.CredsFile
	if uerr := json.Unmarshal([]byte(buf), &cf); uerr != nil {
		return transport.MinIOCredentials{}, fmt.Errorf("ce staging: decoding creds file %s: %w", path, uerr)
	}
	if cf.AccessKey == "" || cf.Endpoint == "" {
		return transport.MinIOCredentials{}, fmt.Errorf("ce staging: creds file %s carries no usable storage creds", path)
	}
	out := transport.MinIOCredentials{
		AccessKey: cf.AccessKey, SecretKey: cf.SecretKey, SessionToken: cf.SessionToken,
		Endpoint: cf.Endpoint, ExpiresAt: time.Time{},
	}
	if cf.ExpiresAt != nil {
		out.ExpiresAt = *cf.ExpiresAt
	}
	return out, nil
}

// ceRemintFromCredsFile is the re-mint func UploadNativeOutputs wants for a
// container task. The native backend gets the same shape from main.go
// (&executor.MinIONativeStager{ReMint: tp.GetCredentials}); the container
// pipeline has no transport handle in scope, so the re-mint reads the worker's
// own live per-task creds file (credsrefresher.go, GSEC Q21.7) — the refresher
// rewrites that file on every refresh and the executor already reads it
// (credsFileBind). A task with no refresher (no file on disk) gets nil, which
// disables re-minting exactly as in the native tests: the upload then runs on
// the bootstrap creds the task already authenticated with.
func ceRemintFromCredsFile(taskID int) func(context.Context, int) (*transport.Credentials, error) {
	if taskID <= 0 {
		return nil
	}
	path := credsrefresher.TaskCredsPath(taskID)
	if _, err := os.Stat(path); err != nil {
		// (no creds file => no refresher running: re-minting is unavailable)
		return nil
	}
	return func(ctx context.Context, id int) (*transport.Credentials, error) {
		_ = ctx
		_ = id
		min, err := ceMinIOFromCredsFile(path)
		if err != nil {
			return nil, err
		}
		// (the bundle keeps everything except MinIO from the caller's copy: the
		// upload only ever consults MinIO — EnsureCredsFresh reads creds.MinIO.ExpiresAt
		// and hands the result to s3ClientFor(fresh.MinIO))
		out := &transport.Credentials{MinIO: min}
		return out, nil
	}
}

// ceDownloadInProcess stages a task's inputs into the volume's /data/input
// without any S3 client in a container: minio-go pulls + sha256-verifies into a
// host temp dir (StageNativeInputs, whose naming is exactly buildDownloadCmd's —
// single-file `input_<nodeKind><ext>`, multi-file `<cogId><ext>`), then the
// bytes are tar'd straight into the volume through the alpine helper's stdin,
// the same exec pattern checkpoint_transfer.go downloadCheckpointVolume uses.
// The temp dir is removed on return: the node container reads /data/input.
func (e *Executor) ceDownloadInProcess(ctx context.Context, helperImage, volName string,
	creds *transport.Credentials, payload transport.TaskPayload) error {
	tmp, err := os.MkdirTemp("", "ce-input-")
	if err != nil {
		return fmt.Errorf("ce staging: input workspace: %w", err)
	}
	defer os.RemoveAll(tmp)
	// (the encrypted-scratch hook keys its scratch by the TASK dir — one level
	// above the input dir — so the input dir is a subdir of the fresh temp dir:
	// the key is unique to this staging call and ceCleanupStagedScratch below
	// destroys exactly this one)
	inputDir := filepath.Join(tmp, "input")
	defer ceCleanupStagedScratch(inputDir)
	client, cerr := s3ClientFor(creds.MinIO)
	if cerr != nil {
		return cerr
	}
	if serr := StageNativeInputs(ctx, client, payload, inputDir); serr != nil {
		return serr
	}
	tarBytes, twerr := ceWriteTarDir(inputDir)
	if twerr != nil {
		return twerr
	}
	// OPT2 (lane gpu2-2): Run() skips the standalone prepareDataVolume for
	// tasks that have a download, so THIS helper container seeds /data first
	// (same dataSeedCmd string as prepareDataVolume, same image, same bind),
	// then untars the inputs in the same sh -c — one throwaway container
	// instead of two per task. The seed runs before any bytes land; a seed
	// failure fails the leg exactly as the standalone prep did.
	// (the volume's /data/input already exists: prepareDataVolume seeded it with
	// the SAME helper image before this leg runs)
	seeded := `set -e; ` + dataSeedCmd + "; tar xf - -C /data/input"
	cmd := execCommandContext(ctx, "docker", "run", "--rm", "-i",
		"-v", volName+":/data", helperImage, "sh", "-c", seeded)
	cmd.Stdin = bytes.NewBuffer(tarBytes)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if rerr := cmd.Run(); rerr != nil {
		return fmt.Errorf("ce staging: untar inputs into volume %q: %v: %s",
			volName, rerr, strings.TrimSpace(stderr.String()))
	}
	return nil
}

// ceUploadInProcess is the mirror leg: the alpine helper tars /data/output to
// stdout, the Go side untars it into a host temp dir (rejecting traversal,
// nested and symlink entries), and UploadNativeOutputs performs the SAME
// <prefix><hash> upload + verify + manifest contract uploadSidecar renders
// through buildUploadScript — including the wizard_test_run OutputPrefix and
// the SandboxMaxOutputMB cap, which scanOutputDir enforces against
// uploadScriptParamsFromTask's CapBytes before any object is written. Loan
// tasks route through UploadLoanedNativeOutputs exactly as the native
// stager's CollectOutputs does, so the loan marker stays on the objects.
func (e *Executor) ceUploadInProcess(ctx context.Context, helperImage, volName string,
	creds *transport.Credentials, task *transport.Task) ([]transport.OutputFile, error) {
	if task == nil {
		return nil, fmt.Errorf("ce staging: nil task")
	}
	// OPT4: the scan-staging pass (ceStageOutputsForScan) already tarred THIS
	// exact volume with THIS exact command. The container has exited, so the
	// volume's bytes cannot change between the staging pass and the durable
	// upload: untar the handed-off tar bytes into a fresh dir instead of
	// paying a second identical docker helper run. A cache miss (staging
	// failed, a retry inside retryOp after the tar was consumed, an empty
	// volume, or a task staged by no one) falls back to the verbatim
	// pre-OPT4 tar below, so behaviour is identical either way. The untar
	// goes through ceReadTarIntoDir - the SAME trust boundary as the
	// fallback - and UploadNativeOutputs still re-scans the dir with
	// scanOutputDir (SandboxMaxOutputMB cap + per-file sha256) and still
	// upload+GET-verifies every object, so both sidecar guarantees hold.
	if tar, ok := ceTakeStagedTar(volName, task.ID); ok {
		tmp, err := os.MkdirTemp("", "ce-output-")
		if err != nil {
			return nil, fmt.Errorf("ce staging: output workspace: %w", err)
		}
		if _, terr := ceReadTarIntoDir(tar, tmp); terr != nil {
			_ = os.RemoveAll(tmp)
			return nil, terr
		}
		reMint := ceRemintFromCredsFile(task.ID)
		if task.Payload.LoanID != "" {
			outs, uerr := UploadLoanedNativeOutputs(ctx, task, creds, tmp, task.Payload.LoanID, reMint)
			_ = os.RemoveAll(tmp)
			return outs, uerr
		}
		outs, uerr := UploadNativeOutputs(ctx, task, creds, tmp, reMint)
		_ = os.RemoveAll(tmp)
		return outs, uerr
	}
	tmp, err := os.MkdirTemp("", "ce-output-")
	if err != nil {
		return nil, fmt.Errorf("ce staging: output workspace: %w", err)
	}
	defer os.RemoveAll(tmp)
	// (the helper writes the tar to stdout; the Go side owns the untar trust
	// boundary, so the container never sees a host path)
	cmd := execCommandContext(ctx, "docker", "run", "--rm",
		"-v", volName+":/data:ro", helperImage, "tar", "cf", "-", "-C", "/data/output", ".")
	var out bytes.Buffer
	cmd.Stdout = &out
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if rerr := cmd.Run(); rerr != nil {
		return nil, fmt.Errorf("ce staging: tar volume outputs %q: %v: %s",
			volName, rerr, strings.TrimSpace(stderr.String()))
	}
	if _, terr := ceReadTarIntoDir(out.Bytes(), tmp); terr != nil {
		return nil, terr
	}
	reMint := ceRemintFromCredsFile(task.ID)
	if task.Payload.LoanID != "" {
		return UploadLoanedNativeOutputs(ctx, task, creds, tmp, task.Payload.LoanID, reMint)
	}
	return UploadNativeOutputs(ctx, task, creds, tmp, reMint)
}

// --- 1665w (CE Gap Z, phase 2): the three legs that still shelled out to `mc`
// in phase 1 now run in process too, so under the CE posture NOTHING pulls or
// names the AGPL public-upstream mc client. ceEnsureSidecar is gone: there is no path
// left that needs it, and sidecarImageRef("") now renders "" (no ref at all),
// so even a stray caller cannot hand a Hub ref to pullImage.

// ceStageOutputsForScan is the CE twin of executor.go's stageOutputsForScan:
// the alpine helper tars /data/output to stdout, the Go side untars it into a
// host temp dir (ceReadTarIntoDir owns the trust boundary), then
// stageNativeOutputsForScan hashes every regular file and PutObject's it as
// <prefix><hash> - the SAME key naming, the SAME transient
// `scan-staging/{task_id}/` prefix (stagingUploadParamsFromTask == nativeScan
// StagingParams) and the SAME []transport.OutputFile shape (ContentHash/FileName
// /SizeBytes/FileExtension + StagingKey) the `mc cp` script produced, so the
// API's pre-store consult fetches real bytes either way. Best-effort exactly as
// before: the caller logs any error here and degrades to the metadata-only
// consult; staging never fails a task and is never retried.
func (e *Executor) ceStageOutputsForScan(ctx context.Context, helperImage, volName string,
	creds *transport.Credentials, task *transport.Task) ([]transport.OutputFile, error) {
	if task == nil {
		return nil, fmt.Errorf("ce staging: nil task")
	}
	tmp, err := os.MkdirTemp("", "ce-scan-stage-")
	if err != nil {
		return nil, fmt.Errorf("ce staging: scan-staging workspace: %w", err)
	}
	defer os.RemoveAll(tmp)
	// UNDER1S: the archive read at result-file time already holds this volume's
	// tar (container exited, bytes immutable): reuse it, else run the helper.
	var out bytes.Buffer
	if pre, ok := ceTakePrefetchedTar(volName, task.ID); ok {
		out.Write(pre)
	} else {
		cmd := execCommandContext(ctx, "docker", "run", "--rm",
			"-v", volName+":/data:ro", helperImage, "tar", "cf", "-", "-C", "/data/output", ".")
		cmd.Stdout = &out
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		if rerr := cmd.Run(); rerr != nil {
			return nil, fmt.Errorf("ce staging: tar volume outputs %q: %v: %s",
				volName, rerr, strings.TrimSpace(stderr.String()))
		}
	}
	if _, terr := ceReadTarIntoDir(out.Bytes(), tmp); terr != nil {
		return nil, terr
	}
	// (the staging pass mirrors whatever the node produced - CapBytes 0, no loan
	// attr - exactly as stagingUploadParamsFromTask renders for the `mc` script)
	// OPT4: the durable upload tars THIS SAME volume with THIS SAME command
	// right after the consult. The container has exited, so the bytes cannot
	// change in between: hand the tar BYTES (not a dir - staging keeps its
	// own temp dir and its own defer RemoveAll, so no ownership changes and
	// no leak path is introduced) to ceUploadInProcess so the post-container
	// leg pays ONE docker helper run instead of two. Failure modes are
	// untouched: a staging error still degrades to the metadata-only
	// consult, and the upload falls back to its own tar.
	staged, serr := stageNativeOutputsForScan(ctx, task, creds, tmp)
	if serr != nil {
		return staged, serr
	}
	ceHandoffStagedTar(volName, task.ID, out.Bytes())
	return staged, nil
}

// --- OPT4 one-hop hand-off: the scan-staging pass and the durable upload
// tar THIS volume with THIS command back to back (the container already
// exited, so the bytes are identical). One docker run instead of two.
// The hand-off carries the TAR BYTES, not a host dir: staging keeps its
// own temp dir and its own defer RemoveAll exactly as before, and the
// upload untars the cached bytes into a fresh dir through ceReadTarIntoDir
// - the SAME trust boundary as the fallback path - so every failure mode
// stays identical. Single slot keyed by volume+taskID: a different task,
// a retry after consumption, or an empty staging tar misses and pays its
// own tar, and an unconsumed entry is overwritten by the next staging
// pass, so nothing leaks past one task.
var ceStagedHandoffMu sync.Mutex
var ceStagedHandoffKey = ""
var ceStagedHandoffTar = []byte{}

func ceStagedHandoffKeyFor(volName string, taskID int) string {
	return volName + "|" + strconv.Itoa(taskID)
}

func ceHandoffStagedTar(volName string, taskID int, tarBytes []byte) {
	ceStagedHandoffMu.Lock()
	ceStagedHandoffKey = ceStagedHandoffKeyFor(volName, taskID)
	ceStagedHandoffTar = tarBytes
	ceStagedHandoffMu.Unlock()
}

// ceTakeStagedTar claims the handed-off tar for this exact volume+task.
// A miss (staging failed, a retry inside retryOp after consumption, an
// empty volume, or a non-CE caller) means "tar it yourself" - the
// verbatim pre-OPT4 path, so behaviour is identical either way.
func ceTakeStagedTar(volName string, taskID int) ([]byte, bool) {
	key := ceStagedHandoffKeyFor(volName, taskID)
	ceStagedHandoffMu.Lock()
	tar := []byte{}
	if key == ceStagedHandoffKey {
		tar = ceStagedHandoffTar
		ceStagedHandoffKey = ""
		ceStagedHandoffTar = []byte{}
	}
	ceStagedHandoffMu.Unlock()
	if len(tar) == 0 {
		return []byte{}, false
	}
	return tar, true
}

// ceCleanupStagedScanOutputs is the CE twin of executor.go's cleanupStagedOut
// puts: minio-go RemoveObject over exactly this task's own
// `scan-staging/{task_id}/` prefix - the same keys stageNativeOutputsForScan
// just wrote, and the same `mc rm -r <prefix>` the sidecar ran. Best-effort
// identical: every per-object failure is swallowed and only reported, so an
// empty prefix or a storage hiccup never fails the task; an error here only
// means an orphaned transient blob for the API-side sweep.
func ceCleanupStagedScanOutputs(ctx context.Context, task *transport.Task,
	creds *transport.Credentials) error {
	if task == nil {
		return nil
	}
	return cleanupNativeStagedOutputs(ctx, task, creds)
}

// ceStageCheckpointExportForScan is the CE twin of stageCheckpointExportForS
// can: the SAME volume tar through the SAME alpine helper (that half already
// used checkpointHelperImage, not the mc image), but the PUT is a minio-go
// PutObject at the SAME fixed key (checkpointExportStagingKey) in the SAME
// tenant bucket instead of `mc cp`. Best-effort like the original: the caller
// logs any error and degrades to a metadata-only consult.
func (e *Executor) ceStageCheckpointExportForScan(ctx context.Context, helperImage string,
	creds *transport.Credentials, task *transport.Task, checkpointVolume string) (string, int64, error) {
	if task == nil {
		return "", 0, fmt.Errorf("ce staging: nil task")
	}
	dir, err := os.MkdirTemp("", "checkpoint-export-")
	if err != nil {
		return "", 0, fmt.Errorf("checkpoint export workspace: %w", err)
	}
	defer os.RemoveAll(dir)
	tarCmd := execCommandContext(ctx, "docker", "run", "--rm",
		"-v", checkpointVolume+":/data:ro",
		"-v", dir+":/out",
		helperImage, "tar", "czf", "/out/checkpoint.tar", "-C", "/data", ".")
	var tarStderr bytes.Buffer
	tarCmd.Stderr = &tarStderr
	if terr := tarCmd.Run(); terr != nil {
		return "", 0, fmt.Errorf("tar checkpoint volume %q: %v: %s",
			checkpointVolume, terr, strings.TrimSpace(tarStderr.String()))
	}
	tarPath := filepath.Join(dir, "checkpoint.tar")
	info, err := os.Stat(tarPath)
	if err != nil {
		return "", 0, fmt.Errorf("stat staged checkpoint tar: %w", err)
	}
	key := checkpointExportStagingKey(task.ID)
	client, cerr := s3ClientFor(creds.MinIO)
	if cerr != nil {
		return "", 0, cerr
	}
	if perr := putNativeStaged(ctx, client, task.Payload.MinioBucket, key, tarPath); perr != nil {
		return "", 0, perr
	}
	return key, info.Size(), nil
}
