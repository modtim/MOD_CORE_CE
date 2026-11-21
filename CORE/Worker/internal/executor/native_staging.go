package executor

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// native_staging.go — GP.5 (launch-completion-megaplan Group P).
//
// Host-side staging for native (imageless) tasks. The container pipeline
// moves task bytes with `mc` inside a minio-client sidecar against an
// ephemeral Docker volume; a native run has neither a sidecar nor a
// volume, so this file performs the same contract with the worker's own
// minio-go client:
//
//   - inputs: download each key into the host input dir, verifying the
//     sha256 the task metadata promises (a mismatch rejects the file);
//   - outputs: hash every file in the host output dir, upload as
//     <prefix><hash>, and produce the same manifest entries
//     (parseUploadManifest) the sidecar script prints, so callers see the
//     identical []transport.OutputFile either way.
//
// The container path (buildDownloadCmd / buildUploadScript) is untouched.

// defaultNativeCredRecheck is how close to expiry we proactively re-mint
// task storage credentials before a native output upload. Per-task
// credentials are short-lived and a native run can legitimately outlive
// them (a render that runs for hours).
const defaultNativeCredRecheck = 60 * time.Second

// nativeCredRecheckMargin returns the proactive re-mint margin, honoring
// MOD_NATIVE_CRED_RECHECK so an operator can widen/narrow it.
func nativeCredRecheckMargin() time.Duration {
	if v := strings.TrimSpace(os.Getenv("MOD_NATIVE_CRED_RECHECK")); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return defaultNativeCredRecheck
}

// s3ClientFor builds a minio-go client from task-scoped credentials. The
// endpoint is taken from creds.Endpoint (host or scheme://host, the same
// string the mc sidecar path carries as MINIO_ENDPOINT); the scheme is
// honored instead of assuming HTTP. When Endpoint is empty the host is
// recovered from MCHost (the MC_HOST_myminio value).
func s3ClientFor(c transport.MinIOCredentials) (*minio.Client, error) {
	host := strings.TrimSpace(c.Endpoint)
	if host == "" {
		host = mchostToEndpoint(c.MCHost)
	}
	if host == "" {
		return nil, fmt.Errorf("executor: native staging: no endpoint (MINIO endpoint and mc_host both empty)")
	}
	useSSL := strings.HasPrefix(strings.ToLower(host), "https://")
	if !strings.Contains(host, "://") {
		host = "http://" + host
	}
	endpoint := strings.TrimPrefix(strings.TrimPrefix(host, "http://"), "https://")
	client, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(c.AccessKey, c.SecretKey, c.SessionToken),
		Secure: useSSL,
	})
	if err != nil {
		return nil, fmt.Errorf("executor: native staging: s3 client: %w", err)
	}
	return client, nil
}

// mchostToEndpoint recovers a bare host:port from a MC_HOST_myminio value
// (scheme://access:secret@host[:port]). The secret may contain ':', so the
// user part is everything before the last '@'; url.Parse handles it.
func mchostToEndpoint(mchost string) string {
	u, err := url.Parse(strings.TrimSpace(mchost))
	if err != nil || u.Host == "" {
		return ""
	}
	return u.Host
}

// sha256HexFile returns the lowercase hex sha256 of a local file.
func sha256HexFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// normalizeHash compares digests case-insensitively.
func normalizeHash(h string) string {
	return strings.ToLower(strings.TrimSpace(h))
}

// removeQuiet removes a temp file, ignoring a missing file.
func removeQuiet(path string) {
	_ = os.Remove(path)
}

// fileExtensionRe allowlists a tenant-supplied file extension: an optional
// leading dot plus 1-16 alphanumerics. R-154: the extension is formatted
// into a host-side staging path; separators, "..", embedded dots, NUL and
// oversized strings must be rejected, not smuggled into the filename.
var fileExtensionRe = regexp.MustCompile(`^\.?[A-Za-z0-9]{1,16}$`)

// normalizeStagingExtension validates a tenant-supplied extension and
// returns the dotted form staging has always written ("png" -> ".png",
// ".mp4" -> ".mp4"); an empty extension stays empty (staging then names
// the file with no extension, as before).
func normalizeStagingExtension(ext, where string) (string, error) {
	if ext == "" {
		return "", nil
	}
	if !fileExtensionRe.MatchString(ext) {
		return "", fmt.Errorf("executor: refusing %s file extension %q (must be 1-16 alphanumerics with an optional leading dot)", where, ext)
	}
	return "." + strings.TrimPrefix(ext, "."), nil
}

// assertStagingDestInside rejects a destination that does not land as a
// plain file directly inside dir once the filesystem cleans it. R-154
// defense in depth: the extension is allowlisted, but the other pieces of
// the staged name (e.g. NodeKind) are still tenant-influenced, so the
// final cleaned path is checked to stay within the staging dir.
func assertStagingDestInside(dir, dest string) error {
	if filepath.IsAbs(dest) {
		return fmt.Errorf("executor: staging destination %q is an absolute path, refusing", dest)
	}
	clean := filepath.Clean(dest)
	rel, err := filepath.Rel(dir, filepath.Join(dir, clean))
	if err != nil {
		return fmt.Errorf("executor: staging destination %q: %w", dest, err)
	}
	if rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) ||
		strings.ContainsAny(rel, string(filepath.Separator)+`\`) || strings.ContainsRune(rel, 0) {
		return fmt.Errorf("executor: staging destination %q is not a plain file inside staging dir %q, refusing", dest, dir)
	}
	return nil
}

// isAuthError reports whether an S3 operation error is an authentication
// failure (as opposed to, say, a missing object). It walks the whole
// error chain: minio-go's ErrorResponse is usually wrapped in a fmt.Errorf
// and minio.ToErrorResponse only type-switches on the top-level error, so
// errors.As is the reliable check.
func isAuthError(err error) bool {
	if err == nil {
		return false
	}
	var resp minio.ErrorResponse
	if errors.As(err, &resp) && resp.Code != "" {
		switch resp.Code {
		case "AccessDenied", "SignatureDoesNotMatch", "InvalidAccessKeyId",
			"RequestTimeTooSkewed", "TokenExpire", "SecurityTokenExpired":
			return true
		}
	}
	msg := err.Error()
	return strings.Contains(msg, "InvalidAccessKeyId") ||
		strings.Contains(msg, "SignatureDoesNotMatch")
}

// StageNativeInputs downloads a task's inputs into inputDir (host working
// directory) and verifies each file's sha256 against the task metadata.
//
// It mirrors the container contract in buildDownloadCmd: a multi-file task
// is named by cog id ("<cog_id><ext>") and a single-file task lands as
// "input_<node_kind><ext>" — exactly the names the container sees under
// /data/input, so a native recipe's {input_dir} behaves identically. A
// hash mismatch is a hard failure: the staged file is removed and the
// error names the promised vs actual digest.
func StageNativeInputs(ctx context.Context, client *minio.Client, payload transport.TaskPayload, inputDir string) error {
	if client == nil {
		return fmt.Errorf("executor: StageNativeInputs: nil client")
	}
	if payload.MinioBucket == "" {
		return fmt.Errorf("executor: StageNativeInputs: no minio bucket")
	}
	// FX.123: with an encrypted-scratch factory installed the task's input
	// bytes are sealed per task instead of laid down as plaintext. Without
	// one (the default, and what production wires today) the plaintext path
	// below is byte-for-byte the old one.
	if scratchFactory != nil {
		return stageNativeInputsEncrypted(ctx, client, payload, inputDir)
	}
	// MAC.1.s13: OS side-channel discipline for this task's scratch dir
	// (darwin: crash-report suppression + Time Machine/Spotlight/iCloud
	// exclusion; every other OS: no-op). Fail-soft — the ONLY error it can
	// return is a scratch root sitting inside a path iCloud syncs, which is a
	// real cross-box leak, not a nuisance.
	if err := os.MkdirAll(inputDir, 0o755); err != nil {
		return fmt.Errorf("executor: StageNativeInputs: creating %s: %w", inputDir, err)
	}
	refs, rerr := stagingFileRefs(payload)
	if rerr != nil {
		return rerr
	}
	for _, r := range refs {
		if err := assertStagingDestInside(inputDir, r.dest); err != nil {
			return err
		}
		if err := downloadAndVerifyObject(ctx, client, payload.MinioBucket, r.key, r.hash, filepath.Join(inputDir, r.dest)); err != nil {
			return fmt.Errorf("executor: StageNativeInputs: %w", err)
		}
	}
	// (the plaintext input dir is NOT removed here: the native process the
	// task runs reads it, and the scratch tree's own cleanup owns it)
	_ = inputDir
	return nil
}

// downloadAndVerifyObject downloads bucket/key into dest (via a temp file
// in dest's directory) and rejects on a sha256 mismatch, removing the
// staged file.
func downloadAndVerifyObject(ctx context.Context, client *minio.Client, bucket, key, wantHash, dest string) error {
	tmp := dest + ".part"
	obj, err := client.GetObject(ctx, bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return fmt.Errorf("getting %s/%s: %w", bucket, key, err)
	}
	f, err := os.Create(tmp)
	if err != nil {
		obj.Close()
		return err
	}
	_, cerr := io.Copy(f, obj)
	gerr := obj.Close() // Close surfaces errors from the GetObject stream
	ferr := f.Close()
	if cerr != nil {
		removeQuiet(tmp)
		return fmt.Errorf("downloading %s/%s: %w", bucket, key, cerr)
	}
	if gerr != nil {
		removeQuiet(tmp)
		return fmt.Errorf("downloading %s/%s: %w", bucket, key, gerr)
	}
	if ferr != nil {
		removeQuiet(tmp)
		return fmt.Errorf("staging %s: %w", tmp, ferr)
	}

	if wantHash != "" {
		got, herr := sha256HexFile(tmp)
		if herr != nil {
			removeQuiet(tmp)
			return fmt.Errorf("hashing %s: %w", dest, herr)
		}
		if normalizeHash(got) != normalizeHash(wantHash) {
			removeQuiet(tmp)
			return fmt.Errorf("content hash mismatch for %s/%s: want %s got %s", bucket, key, wantHash, got)
		}
	}

	if err := os.Rename(tmp, dest); err != nil {
		removeQuiet(tmp)
		return fmt.Errorf("placing %s: %w", dest, err)
	}
	return nil
}

// nativeObjectMaxBytes caps an object pulled into memory. An object past the cap
// is refused with an error; it is never truncated and hash-checked.
const nativeObjectMaxBytes = 256 << 20

// readCappedObjectBody reads r to EOF but never more than maxBytes+1 bytes. A
// body past maxBytes is an error and no bytes are returned, so a truncated
// body is never hash-checked or decoded. (Extracted from fetchAndVerifyObject
// so the bound can be tested with a small cap; behaviour is unchanged.)
func readCappedObjectBody(r io.Reader, maxBytes int64) ([]byte, error) {
	buf, err := io.ReadAll(io.LimitReader(r, maxBytes+1))
	if err != nil {
		return nil, err
	}
	if int64(len(buf)) > maxBytes {
		return nil, fmt.Errorf("object exceeds %d bytes", maxBytes)
	}
	return buf, nil
}

// fetchAndVerifyObject pulls bucket/key into memory (never onto the scratch
// tree) and rejects a sha256 mismatch.
func fetchAndVerifyObject(ctx context.Context, client *minio.Client, bucket, key, wantHash string) ([]byte, error) {
	obj, err := client.GetObject(ctx, bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, fmt.Errorf("getting %s/%s: %w", bucket, key, err)
	}
	buf, rerr := readCappedObjectBody(obj, nativeObjectMaxBytes)
	cerr := obj.Close()
	if rerr != nil {
		_ = buf
		return nil, fmt.Errorf("downloading %s/%s: %w", bucket, key, rerr)
	}
	if cerr != nil {
		_ = buf
		return nil, fmt.Errorf("downloading %s/%s: %w", bucket, key, cerr)
	}
	if wantHash != "" {
		digest := sha256.Sum256(buf)
		got := hex.EncodeToString(digest[:])
		if normalizeHash(got) != normalizeHash(wantHash) {
			_ = buf
			return nil, fmt.Errorf("content hash mismatch for %s/%s: want %s got %s", bucket, key, wantHash, got)
		}
	}
	return buf, nil
}

// stageNativeInputsEncrypted is the FX.123 encrypted variant of StageNativeInputs:
// the task's bytes travel S3 -> memory -> sealed (AES-256-GCM) file, and only
// at the last step are they opened into the task's own 0700 input dir so the
// native process can read them.
//
// FX.123.s1b — the destroy point moved from here to the run's teardown. A
// Destroy on this defer ended the scratch when staging RETURNED, so the native
// process never had its inputs. Now the minted scratch is handed to the
// backend (TakeStagedScratch) and lives from staging through the run; the run
// level cleanup (NativeBackend.Cancel, deferred by dispatchNativeTask) removes
// the decrypted files and Destroys — success, failure, cancel and panic alike.
//
// Trade-off (deliberate, and the reason this is not a stronger guarantee): the
// process being run is an ordinary host process, so its inputs must exist as
// readable bytes for the run's duration. What the layer buys is (a) the bytes
// are never on disk in the open — sealed until the last step, inside a 0700
// dir only the worker user can enter, each file 0600; (b) teardown is
// unconditional: the key is zeroed and the whole task dir removed, so no task
// bytes survive the run even when the process panics or is killed. A worker
// that needs the inputs never plaintext at all must run the node in a
// container (the container backend's volume staging), not natively.
//
// inputDir is <scratchRoot>/<taskID>/input, so the scratch root and task ID
// are recovered from it without changing any call signature.
func stageNativeInputsEncrypted(ctx context.Context, client *minio.Client, payload transport.TaskPayload, inputDir string) (errRet error) {
	taskDir := filepath.Dir(inputDir)
	scratch, err := scratchFactory(filepath.Dir(taskDir), filepath.Base(taskDir))
	if err != nil {
		_ = inputDir
		return err
	}
	// Every path that does NOT hand the scratch to a run destroys it here: the
	// key is zeroed and the dir removed, so a failed/cancelled staging leaves
	// nothing behind. Only the success path below leaves it alive, and it is
	// then owned by the run's teardown.
	// (Destroy zeroes the key BEFORE removing the dir)
	// (nothing here logs or formats key material)
	handoffFailed := true
	defer func() {
		if !handoffFailed || scratch == nil {
			return
		}
		derr := scratch.Destroy()
		if derr != nil && errRet == nil {
			errRet = fmt.Errorf("executor: StageNativeInputs: destroying scratch: %w", derr)
		}
	}()
	refs, rerr := stagingFileRefs(payload)
	if rerr != nil {
		return rerr
	}
	for _, r := range refs {
		buf, ferr := fetchAndVerifyObject(ctx, client, payload.MinioBucket, r.key, r.hash)
		if ferr != nil {
			return ferr
		}
		if err := scratch.WriteFile(r.dest, buf); err != nil {
			return err
		}
		_ = buf
	}
	// The sealed bytes are opened into the task's input dir (0600 files inside
	// the 0700 task dir) immediately before exec — this is the only moment the
	// native process can see its inputs.
	if err := os.MkdirAll(inputDir, 0o700); err != nil {
		return fmt.Errorf("executor: StageNativeInputs: creating %s: %w", inputDir, err)
	}
	if err := os.Chmod(inputDir, 0o700); err != nil {
		return fmt.Errorf("executor: StageNativeInputs: tightening %s: %w", inputDir, err)
	}
	for _, r := range refs {
		plain, perr := scratch.ReadFile(r.dest)
		if perr != nil {
			return perr
		}
		dest := filepath.Join(inputDir, r.dest)
		if err := assertStagingDestInside(inputDir, r.dest); err != nil {
			return err
		}
		if err := os.WriteFile(dest, plain, 0o600); err != nil {
			return fmt.Errorf("executor: StageNativeInputs: opening %s: %w", dest, err)
		}
		_ = plain
	}
	// Hand the live scratch to the run that is about to be submitted.
	if err := keepStagedScratch(inputDir, scratch); err != nil {
		return err
	}
	handoffFailed = false
	return nil
}

// BuildNativeOutputManifest computes the output manifest for a native run's
// output directory: for every file it returns the same transport.OutputFile
// entries parseUploadManifest would build from the sidecar script's
// "UPLOAD,<key>,<fname>,<size>,<ext>" lines — same hash-named key, same
// fields. The total-size cap (uploadScriptParams.CapBytes, the wizard
// sandbox limit) is enforced before anything is returned, the same
// pre-check the sidecar script does with its exit 64.
func BuildNativeOutputManifest(outputDir string, params uploadScriptParams) ([]transport.OutputFile, error) {
	entries, err := scanOutputDir(outputDir, params)
	if err != nil {
		return nil, err
	}
	manifest := make([]string, 0, len(entries))
	for _, e := range entries {
		manifest = append(manifest, fmt.Sprintf("UPLOAD,%s%s,%s,%d,%s", params.Prefix, e.hash, e.name, e.size, e.ext))
	}
	// Round-trip through the container parser so both backends are pinned
	// to the identical manifest contract by one code path.
	return parseUploadManifest(strings.Join(manifest, "\n")), nil
}

// nativeOutputEntry is one staged output file, pre-upload.
type nativeOutputEntry struct {
	name string
	hash string
	size int64
	ext  string
}

// scanOutputDir lists the regular files in outputDir with their sha256,
// size and extension, enforcing the total-size cap first (the sidecar
// script's exit-64 pre-check, ported to Go).
func scanOutputDir(outputDir string, params uploadScriptParams) ([]nativeOutputEntry, error) {
	dir, err := os.Open(outputDir)
	if err != nil {
		return nil, fmt.Errorf("executor: scanOutputDir: opening %s: %w", outputDir, err)
	}
	defer dir.Close()
	names, err := dir.Readdirnames(-1)
	if err != nil {
		return nil, fmt.Errorf("executor: scanOutputDir: reading %s: %w", outputDir, err)
	}
	var total int64
	for _, name := range names {
		fi, err := os.Stat(filepath.Join(outputDir, name))
		if err != nil || !fi.Mode().IsRegular() {
			continue
		}
		total += fi.Size()
	}
	if params.CapBytes > 0 && total > int64(params.CapBytes) {
		return nil, fmt.Errorf("executor: scanOutputDir: sandbox output too large: total=%d cap=%d", total, params.CapBytes)
	}
	var entries []nativeOutputEntry
	for _, name := range names {
		path := filepath.Join(outputDir, name)
		fi, err := os.Stat(path)
		if err != nil || !fi.Mode().IsRegular() {
			continue
		}
		hash, err := sha256HexFile(path)
		if err != nil {
			return nil, fmt.Errorf("executor: scanOutputDir: hashing %s: %w", name, err)
		}
		ext := ""
		if i := strings.LastIndex(name, "."); i > 0 {
			ext = name[i+1:]
		}
		entries = append(entries, nativeOutputEntry{name: name, hash: hash, size: fi.Size(), ext: ext})
	}
	return entries, nil
}

// EnsureCredsFresh is the re-mint decision helper for long native runs:
// if the task's storage credentials expire within margin it calls reMint
// exactly once and returns the fresh credentials; fresh (or expiry-less)
// credentials pass through untouched and reMint is not called. With
// reMint == nil, near-expiry credentials are an error rather than being
// used as-is.
func EnsureCredsFresh(ctx context.Context, creds *transport.Credentials, taskID int, margin time.Duration, reMint func(context.Context, int) (*transport.Credentials, error)) (*transport.Credentials, error) {
	if creds == nil {
		return nil, fmt.Errorf("executor: EnsureCredsFresh: nil credentials")
	}
	expires := creds.MinIO.ExpiresAt
	if expires.IsZero() || time.Until(expires) >= margin {
		return creds, nil
	}
	if reMint == nil {
		return nil, fmt.Errorf("executor: EnsureCredsFresh: credentials expire in %s (within the %s margin) and no re-mint is available",
			time.Until(expires).Round(time.Second), margin)
	}
	fresh, err := reMint(ctx, taskID)
	if err != nil {
		return nil, fmt.Errorf("executor: EnsureCredsFresh: re-minting task %d credentials: %w", taskID, err)
	}
	return fresh, nil
}

// UploadNativeOutputs uploads every file in outputDir as <prefix><hash>
// (the sidecar contract) and returns the OutputFile manifest. Before the
// first upload it proactively re-mints the task's credentials when they
// are within nativeCredRecheckMargin() of expiry (a native run can
// outlive the original credential TTL), and on an authentication error it
// re-mints once and retries the single failed object. reMint == nil
// disables re-minting entirely (tests).
func UploadNativeOutputs(ctx context.Context, task *transport.Task, creds *transport.Credentials, outputDir string, reMint func(context.Context, int) (*transport.Credentials, error)) ([]transport.OutputFile, error) {
	if task == nil {
		return nil, fmt.Errorf("executor: UploadNativeOutputs: nil task")
	}
	fresh, err := EnsureCredsFresh(ctx, creds, task.ID, nativeCredRecheckMargin(), reMint)
	if err != nil {
		return nil, err
	}
	client, err := s3ClientFor(fresh.MinIO)
	if err != nil {
		return nil, err
	}
	params := uploadScriptParamsFromTask(task)
	entries, err := scanOutputDir(outputDir, params)
	if err != nil {
		return nil, err
	}

	remintedFlag := false
	var manifest []transport.OutputFile
	for _, e := range entries {
		key := params.Prefix + e.hash
		path := filepath.Join(outputDir, e.name)
		uerr := uploadAndVerifyOutput(ctx, client, params.Bucket, key, path, e.hash)
		if uerr == nil {
			manifest = append(manifest, outputEntryToFile(e))
			continue
		}
		if !isAuthError(uerr) || reMint == nil || remintedFlag {
			return nil, fmt.Errorf("executor: UploadNativeOutputs: %w", uerr)
		}
		// One-shot fallback: re-mint, then retry THIS object exactly once.
		nc, merr := reMint(ctx, task.ID)
		if merr != nil {
			return nil, fmt.Errorf("executor: UploadNativeOutputs: auth failure on %s and re-mint failed: %w", key, merr)
		}
		nclient, cerr := s3ClientFor(nc.MinIO)
		if cerr != nil {
			return nil, fmt.Errorf("executor: UploadNativeOutputs: rebuilding client after re-mint: %w", cerr)
		}
		client = nclient
		remintedFlag = true
		if uerr2 := uploadAndVerifyOutput(ctx, client, params.Bucket, key, path, e.hash); uerr2 != nil {
			return nil, fmt.Errorf("executor: UploadNativeOutputs: retry after re-mint failed on %s: %w", key, uerr2)
		}
		manifest = append(manifest, outputEntryToFile(e))
	}
	return manifest, nil
}

func outputEntryToFile(e nativeOutputEntry) transport.OutputFile {
	return transport.OutputFile{
		ContentHash:   e.hash,
		FileName:      e.name,
		SizeBytes:     e.size,
		FileExtension: e.ext,
	}
}

// uploadAndVerifyOutput uploads path as bucket/key and re-hashes the
// object on the way back, so a corrupt object is rejected the same way a
// corrupt download is.
func uploadAndVerifyOutput(ctx context.Context, client *minio.Client, bucket, key, path, wantHash string) error {
	fi, err := os.Stat(path)
	if err != nil {
		return err
	}
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := client.PutObject(ctx, bucket, key, f, fi.Size(), minio.PutObjectOptions{}); err != nil {
		return fmt.Errorf("uploading %s/%s: %w", bucket, key, err)
	}
	obj, err := client.GetObject(ctx, bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return fmt.Errorf("verifying %s/%s: %w", bucket, key, err)
	}
	defer obj.Close()
	h := sha256.New()
	if _, err := io.Copy(h, obj); err != nil {
		return fmt.Errorf("verifying %s/%s: %w", bucket, key, err)
	}
	got := hex.EncodeToString(h.Sum(nil))
	if normalizeHash(got) != normalizeHash(wantHash) {
		return fmt.Errorf("post-upload hash mismatch for %s/%s: want %s got %s", bucket, key, wantHash, got)
	}
	return nil
}

// ScratchFactory is the optional FX.123 encrypted-scratch hook. nil — the
// default, and what production wires today — means the native staging path
// keeps using the plaintext host scratch dirs it always has, byte for byte.
// A worker that installs a factory (executor.NewEncryptedScratch) gets an
// app-level AES-256-GCM layer over one task's scratch files instead: the
// key is random per task, held in memory only, and Destroy() runs on a
// defer in the staging function itself, so the key is zeroed and the dir
// removed on success AND on every early return.
type ScratchFactory = func(baseDir, taskID string) (ScratchCipher, error)

// scratchFactory is the installed hook (nil = plaintext staging).
var scratchFactory ScratchFactory

// SetScratchFactory installs the encrypted-scratch factory. Deliberately a
// setter, not a new parameter: StageNativeInputs/StageInputs keep their
// signatures, so every existing caller and test stays on the default path.
func SetScratchFactory(fn ScratchFactory) { scratchFactory = fn }

// stagedScratches is the FX.123.s1b hand-off: a staging pass that succeeded
// leaves its minted scratch alive (the native process has to read its inputs),
// keyed by the task dir it staged into, until the run's teardown destroys it.
// Without this map the scratch would be destroyed when staging returned and
// the run would see no inputs at all.
var stagedScratchMu sync.Mutex
var stagedScratches = make(map[string]ScratchCipher)

// keepStagedScratch records a successfully staged scratch for the run.
func keepStagedScratch(taskDir string, scratch ScratchCipher) error {
	if scratch == nil {
		return fmt.Errorf("executor: StageNativeInputs: scratch factory returned no scratch")
	}
	stagedScratchMu.Lock()
	// A scratch already waiting on this task dir means the task was staged
	// twice without a teardown in between: destroy the older one rather than
	// leak its key/dir.
	older := stagedScratches[taskDir]
	stagedScratches[taskDir] = scratch
	stagedScratchMu.Unlock()
	if older != nil {
		_ = older.Destroy()
	}
	_ = taskDir
	return nil
}

// takeStagedScratch removes and returns the scratch staged for taskDir
// (nil when there is none — the plaintext path never registers one).
func takeStagedScratch(taskDir string) ScratchCipher {
	stagedScratchMu.Lock()
	scratch := stagedScratches[taskDir]
	if scratch != nil {
		delete(stagedScratches, taskDir)
	}
	stagedScratchMu.Unlock()
	return scratch
}

// destroyStagedScratch is the run-level teardown for the encrypted scratch:
// the decrypted input files are removed (they are inside the task dir, which
// Destroy removes wholesale) and the key is zeroed. Idempotent and never
// fatal, so it is safe on the run's defer.
func destroyStagedScratch(taskDir string) error {
	scratch := takeStagedScratch(taskDir)
	if scratch == nil {
		return nil
	}
	return scratch.Destroy()
}

// stagingFileRefs is the single source of truth for what a task's input
// staging must fetch: object key, promised sha256, and the plain filename
// inside the task's input dir. Both staging paths (plaintext dirs and the
// encrypted scratch) build their work list here, so the two can never
// disagree about which bytes a task needs.
type nativeStagingRef struct {
	key  string // object key (== content hash, both staging paths)
	hash string // promised sha256
	dest string // filename inside the task's input dir
}

func stagingFileRefs(payload transport.TaskPayload) ([]nativeStagingRef, error) {
	var refs []nativeStagingRef
	if len(payload.InputFiles) > 0 {
		for i, f := range payload.InputFiles {
			if f.ContentHash == "" {
				continue
			}
			fx, err := normalizeStagingExtension(f.FileExtension, fmt.Sprintf("input_files[%d] (cog %d)", i, f.CogID))
			if err != nil {
				// (the error text names the extension, never a secret)
				return nil, err
			}
			refs = append(refs, nativeStagingRef{key: f.ContentHash, hash: f.ContentHash, dest: fmt.Sprintf("%d%s", f.CogID, fx)})
		}
		if len(refs) == 0 {
			return nil, fmt.Errorf("executor: StageNativeInputs: task has input_files but none carry a content_hash")
		}
		return refs, nil
	}
	if payload.SourceFileLocation == "" {
		return nil, fmt.Errorf("executor: StageNativeInputs: no input (no input_files and empty source_file_location)")
	}
	ext, err := normalizeStagingExtension(payload.FileExtension, "payload")
	if err != nil {
		return nil, err
	}
	refs = append(refs, nativeStagingRef{
		key:  payload.SourceFileLocation,
		hash: payload.SourceFileLocation,
		// (the name is built from NodeKind + a validated extension)
		dest: fmt.Sprintf("input_%s%s", payload.NodeKind, ext),
	})
	return refs, nil
}

// NativeStager stages a native task's bytes between S3 and the host
// scratch dir. NativeBackend stays a pure process runner; the stager
// keeps the S3 dependency out of it and is injectable in tests.
type NativeStager interface {
	// StageInputs downloads the task's inputs into inputDir (host dir).
	StageInputs(ctx context.Context, task *transport.Task, creds *transport.Credentials, inputDir string) error
	// CollectOutputs uploads outputDir and returns the manifest.
	CollectOutputs(ctx context.Context, task *transport.Task, creds *transport.Credentials, outputDir string) ([]transport.OutputFile, error)
}

// MinIONativeStager is the production NativeStager: minio-go against the
// task's minted storage credentials, with proactive re-minting for long
// runs (GP.5).
type MinIONativeStager struct {
	// ReMint fetches fresh credentials for a task (APITransport.GetCredentials
	// in production). nil disables re-minting.
	ReMint func(ctx context.Context, taskID int) (*transport.Credentials, error)
}

var _ NativeStager = (*MinIONativeStager)(nil)

// StageInputs implements NativeStager.
func (s *MinIONativeStager) StageInputs(ctx context.Context, task *transport.Task, creds *transport.Credentials, inputDir string) error {
	client, err := s3ClientFor(creds.MinIO)
	if err != nil {
		return err
	}
	return StageNativeInputs(ctx, client, task.Payload, inputDir)
}

// CollectOutputs implements NativeStager. GC.MWL.12: a task dispatched for
// a work loan (task.Payload.LoanID set — see transport.TaskPayload) is
// routed through UploadLoanedNativeOutputs (loan_staging.go) instead, so
// every object lands in the borrower's own bucket pre-marked "uncollected"
// — the owner's collect step is the only thing allowed to turn it into a
// result. Every existing (non-loan) task has LoanID == "" and takes the
// unmarked path exactly as before.
func (s *MinIONativeStager) CollectOutputs(ctx context.Context, task *transport.Task, creds *transport.Credentials, outputDir string) ([]transport.OutputFile, error) {
	if task != nil && task.Payload.LoanID != "" {
		return UploadLoanedNativeOutputs(ctx, task, creds, outputDir, task.Payload.LoanID, s.ReMint)
	}
	return UploadNativeOutputs(ctx, task, creds, outputDir, s.ReMint)
}

// SetStager attaches the byte-staging implementation to a NativeBackend.
// Without a stager, Submit/CollectOutputs behave exactly as the GP.2
// process runner did (empty input dir, no upload).
func (n *NativeBackend) SetStager(s NativeStager) { n.Stager = s }

// SubmitStaged is the GP.5 entry point for imageless tasks: stage the
// task's inputs into <scratch>/<task>/input (the same names the container
// sees under /data/input), then start the process exactly like Submit.
func (n *NativeBackend) SubmitStaged(ctx context.Context, spec *TaskSpec, task *transport.Task, creds *transport.Credentials) (*Handle, error) {
	if n.Stager == nil || task == nil {
		return n.Submit(ctx, spec)
	}
	// GSEC FX.17 / R-069: carry the task's tenant onto the spec and run
	// the tenant gate BEFORE inputs are staged — a refused tenant task
	// must not burn staging work or leave staged bytes in the scratch
	// tree. (Submit re-checks the gate for the direct-Submit path.)
	if spec.TenantID == 0 && task.TenantID > 0 {
		spec.TenantID = task.TenantID
	}
	if err := n.tenantCodeGate(spec); err != nil {
		return nil, fmt.Errorf("executor: NativeBackend.SubmitStaged: %w", err)
	}
	taskID, err := taskIDFromSpec(spec)
	if err != nil {
		return nil, fmt.Errorf("executor: NativeBackend.SubmitStaged: %w", err)
	}
	// MAC.1.s13: OS side-channel discipline for this task's scratch dir
	// (darwin: crash-report suppression + Time Machine/Spotlight/iCloud
	// exclusion; every other OS: no-op). Fail-soft - the ONLY error it can
	// return is a scratch root sitting inside a path iCloud syncs, which is a
	// real cross-box leak, not a nuisance. Runs BEFORE any byte is staged so a
	// refused root leaves nothing behind.
	//
	// The spec is handed over too so the redirect (TMPDIR/XDG_CACHE_HOME/HF_HOME/
	// PIP_CACHE_DIR and friends) lands in the task's NativeEnv: the tools the
	// curated recipes run must not scatter caches into the owner's home, and two
	// tasks on one box must not fight over one shared cache.
	// The refusal is NOT swallowed: the iCloud check lives in the build-tagged
	// hook (darwin-only - the synced paths are darwin paths), and it is the one
	// hard failure MAC.1.s13 allows.
	applyScratchEnvRedirect(spec, filepath.Join(n.ScratchRoot, taskID))
	if derr := applyScratchDiscipline(filepath.Join(n.ScratchRoot, taskID), spec); derr != nil {
		// Fail-soft everywhere except the iCloud-synced root: nothing was staged
		// yet, so there is no scratch to tear down.
		return nil, fmt.Errorf("executor: NativeBackend.SubmitStaged: %w", derr)
	}
	inputDir := filepath.Join(n.ScratchRoot, taskID, "input")
	if err := n.Stager.StageInputs(ctx, task, creds, inputDir); err != nil {
		// FX.123.s1b: staging failed after it had minted an encrypted scratch
		// (download error, hash mismatch, cancel): tear that scratch down here so
		// no key/dir is left behind. No-op for the plaintext path.
		_ = destroyStagedScratch(inputDir)
		return nil, fmt.Errorf("executor: NativeBackend.SubmitStaged: staging inputs: %w", err)
	}
	h, err := n.Submit(ctx, spec)
	if err != nil {
		// FX.123.s1b: the inputs were staged (encrypted scratch handed off) but
		// no run exists to tear it down — do it here, then fail as before.
		_ = destroyStagedScratch(inputDir)
		return nil, err
	}
	// Record the transport task/credentials on the run so Cancel (Gap B) can
	// recover a loaned run's partial outputs — the Backend.Cancel signature
	// carries only the Handle. Submit has already created the run keyed by
	// taskID, so this is a plain field set on it.
	n.mu.Lock()
	if run, ok := n.runs[taskID]; ok {
		run.task = task
		run.creds = creds
	}
	n.mu.Unlock()
	return h, nil
}

// CollectOutputs uploads <scratch>/<task>/output after the process has
// exited successfully (the caller gates on Poll) and returns the OutputFile
// manifest. Refusing a non-succeeded run keeps a failed or cancelled task
// from publishing partial outputs.
func (n *NativeBackend) CollectOutputs(ctx context.Context, h *Handle, task *transport.Task, creds *transport.Credentials) ([]transport.OutputFile, error) {
	if n.Stager == nil {
		return nil, nil
	}
	if h == nil || h.Backend != "native" {
		return nil, fmt.Errorf("executor: NativeBackend.CollectOutputs: bad handle")
	}
	if task == nil {
		return nil, fmt.Errorf("executor: NativeBackend.CollectOutputs: nil task")
	}
	n.mu.Lock()
	run, ok := n.runs[h.ID]
	n.mu.Unlock()
	if !ok {
		return nil, fmt.Errorf("executor: NativeBackend.CollectOutputs: unknown task %q", h.ID)
	}
	run.mu.Lock()
	phase := run.status.Phase
	run.mu.Unlock()
	if phase != TaskSucceeded {
		return nil, fmt.Errorf("executor: NativeBackend.CollectOutputs: task %q is %s, not succeeded", h.ID, phase)
	}
	outputDir := filepath.Join(n.ScratchRoot, h.ID, "output")
	// Fortress 3.5 (943): when the consult seam is wired (SetNativeScanConsult,
	// main.go next to SetOutputScanAuth) this is the REAL production path —
	// dispatchNativeTask's backend.CollectOutputs call lands here on every
	// native task, not only in tests. nil (the default, and what every
	// existing install sees until main.go wires it) keeps this byte-for-byte
	// the pre-943 behavior: zero staging hops, straight to the durable write.
	if nativeScanConsult == nil {
		return n.Stager.CollectOutputs(ctx, task, creds, outputDir)
	}
	return n.collectOutputsScanned(ctx, task, creds, outputDir)
}

// collectOutputsScanned is the stage -> consult -> (refuse | durable write)
// SEQUENCE, extracted so NativeBackend.CollectOutputs (the one real caller,
// reached from dispatchNativeTask through the NativeStager contract) and the
// CollectNativeOutputsScanned test seam share one implementation. See the
// ordering proof on the (now-removed) duplicate: cleanup is deferred here,
// inside this function, so it runs strictly AFTER the consult call below on
// every return path (blocked early-return, durable-collect error, success).
func (n *NativeBackend) collectOutputsScanned(ctx context.Context, task *transport.Task,
	creds *transport.Credentials, outputDir string) ([]transport.OutputFile, error) {
	staged := []transport.OutputFile{}
	// Best-effort staging (926's posture): a staging failure is swallowed and
	// the consult proceeds with no descriptors, i.e. the pre-existing
	// metadata-only consult. A staging failure must NEVER fail the task.
	if staged2, stageErr := stageNativeOutputsForScan(ctx, task, creds, outputDir); stageErr != nil {
		_ = stageErr
	} else {
		staged = staged2
		// Registered ONLY on a successful stage (926's shape): the transient
		// prefix exists, so it must be removed once the consult is done.
		//
		// The cleanup context is a FRESH bounded one, not the caller's ctx: the
		// caller (dispatchNativeTask) hands us a 30s budget that the consult and
		// the durable upload already spend, so a cleanup sharing it would be born
		// expired. 10s mirrors nativeLogTail's own post-hoc bounded context.
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		defer func() {
			if cerr := cleanupNativeStagedOutputs(cleanupCtx, task, creds); cerr != nil {
				_ = cerr
			}
		}()
	}

	if blocked, reason := nativeScanConsult(ctx, task, staged, true); blocked {
		// Nothing durable was written: the refusal is the native twin of the
		// Docker path's blocked Result.
		return nil, fmt.Errorf("%s", reason)
	}

	// The durable write: the UNCHANGED native contract (re-mint + one-shot
	// auth retry + post-upload verify all live inside Stager.CollectOutputs).
	return n.Stager.CollectOutputs(ctx, task, creds, outputDir)
}

// ---------------------------------------------------------------------------
// Fortress 3.5 (943) — NATIVE-path pre-store scan staging.
//
// 926 (ae6230c55) added the pre-store staging pass to the Docker path only:
// the worker copies the task's output bytes to the transient
// `scan-staging/{task_id}/<hash>` prefix in its OWN tenant bucket, sends
// per-output staging_key descriptors to the API's pre-store consult
// (worker_tasks.py check_task_output_scan -> task_output_scan.py
// _validate_staging_key / _scan_staged_bytes), and removes the transient
// blobs AFTER the consult fetched them. A native run has no minio-client
// sidecar and no ephemeral volume, so the same contract runs here with the
// worker's own minio-go client — same creds (creds.MinIO, re-minted by the
// durable path), no new env vars, no presigned URLs, same key format.
//
// The Docker-path functions (stageOutputsForScan / cleanupStagedOutputs /
// stagingCleanupCmd in executor.go) stay untouched; these are the native
// twins, kept in THIS file so the container path reads unchanged.
// ---------------------------------------------------------------------------

// nativeScanStagingParams renders the transient staging prefix for a native
// task. task.ID is load-bearing: task_output_scan.py:_validate_staging_key
// pins `scan-staging/{task.id}/` with task.id being the API task row id the
// consult URL carries, i.e. the same id transport.Task.ID mirrors.
//
// Deliberately NOT stagingUploadParamsFromTask (upload_script.go): that one
// honours the wizard_test_run OutputPrefix and the sandbox output cap, and a
// staging pass must mirror WHATEVER the node produced (926's comment: "no cap
// (the staging pass mirrors whatever the node produced), no loan attr
// (transient blobs are never collected)"). Same rule here — a transient blob
// is never loan-stamped, so the borrower's collect step never sees it.
func nativeScanStagingParams(task *transport.Task) uploadScriptParams {
	return uploadScriptParams{
		Bucket:   task.Payload.MinioBucket,
		Prefix:   fmt.Sprintf("scan-staging/%d/", task.ID),
		CapBytes: 0,
		LoanAttr: "",
	}
}

// stageNativeOutputsForScan is the native twin of executor.go's
// stageOutputsForScan (926): hash every regular file in outputDir and PutObject
// it as scan-staging/{task_id}/<hash> in the task's own tenant bucket, then
// return the descriptors carrying StagingKey so the pre-store consult can fetch
// REAL bytes instead of metadata only.
//
// BEST-EFFORT, exactly as on the Docker path: the CALLER logs any error here and
// proceeds with the pre-existing metadata-only consult. A staging failure must
// never fail the task. Unlike UploadNativeOutputs there is no re-mint-retry loop
// here — 926's note is that staging is "never retried (retryOp would double the
// sidecar cost on the hot completion path)"; the native equivalent is one
// bounded pass, and the durable upload right after it still re-mints and retries
// on its own.
//
// ORDERING (mirrors 926 verbatim): this function ONLY uploads. Cleanup is
// cleanupNativeStagedOutputs, a separate call the caller defers until AFTER the
// consult has fetched the bytes — the consult is an HTTP hop made after this
// function returns, so deleting here would remove the objects before the API
// ever fetched them.
func stageNativeOutputsForScan(ctx context.Context, task *transport.Task, creds *transport.Credentials,
	outputDir string) ([]transport.OutputFile, error) {
	if task == nil {
		return nil, fmt.Errorf("executor: native scan staging: nil task")
	}
	params := nativeScanStagingParams(task)
	// CapBytes 0 => scanOutputDir imposes no total-size cap (the staging pass
	// mirrors whatever the node produced).
	entries, err := scanOutputDir(outputDir, params)
	if err != nil {
		return nil, err
	}
	client, err := s3ClientFor(creds.MinIO)
	if err != nil {
		// No endpoint: staging is impossible, which is a staging failure, not a
		// task failure — the caller degrades to the metadata-only consult.
		return nil, err
	}
	staged := []transport.OutputFile{}
	for _, e := range entries {
		key := params.Prefix + e.hash
		path := filepath.Join(outputDir, e.name)
		// No post-upload verify here (the durable path does that already, and a
		// verify round-trip on transient bytes doubles the hop count for nothing
		// the scanner needs).
		if err := putNativeStaged(ctx, client, params.Bucket, key, path); err != nil {
			return nil, err
		}
		f := outputEntryToFile(e)
		f.StagingKey = key
		staged = append(staged, f)
	}
	return staged, nil
}

// putNativeStaged uploads one transient blob. Kept separate from
// uploadAndVerifyOutput so the durable contract (upload + re-hash verify) stays
// exactly where it was and the staging pass stays one PutObject.
func putNativeStaged(ctx context.Context, client *minio.Client, bucket, key, path string) error {
	fi, err := os.Stat(path)
	if err != nil {
		return err
	}
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := client.PutObject(ctx, bucket, key, f, fi.Size(), minio.PutObjectOptions{}); err != nil {
		return fmt.Errorf("executor: native scan staging put %s/%s: %w", bucket, key, err)
	}
	return nil
}

// cleanupNativeStagedOutputs removes the transient scan-staging/{task_id}/
// prefix in a SEPARATE pass from the staging upload — the caller defers this
// until after the pre-store consult fetched the bytes (see the ordering note on
// stageNativeOutputsForScan). Best-effort: every per-object failure is swallowed
// and only reported, so a bucket with nothing to remove or a MinIO hiccup never
// fails the task; an error here only means an orphaned transient blob for the
// API-side sweep (services/scan_staging_sweep.py, already scheduled daily in
// services/scheduler.py) to pick up later. These objects are referenced by NO
// DB record, so removing them is not a record delete (ISO 27001 posture).
//
// minio-go v7.0.100 spells the verb RemoveObject (DeleteObject is the wire
// operation's name in its functional tests, not an exported method).
func cleanupNativeStagedOutputs(ctx context.Context, task *transport.Task, creds *transport.Credentials) error {
	if task == nil {
		return nil
	}
	params := nativeScanStagingParams(task)
	client, err := s3ClientFor(creds.MinIO)
	if err != nil {
		return err
	}
	var firstErr error
	for obj := range client.ListObjects(ctx, params.Bucket,
		minio.ListObjectsOptions{Prefix: params.Prefix, Recursive: true}) {
		if obj.Err != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("executor: native scan staging list %s/%s: %w", params.Bucket, params.Prefix, obj.Err)
			}
			continue
		}
		if obj.Key == "" || !strings.HasPrefix(obj.Key, params.Prefix) {
			// Anything outside this task's own staging prefix is never touched.
			continue
		}
		if derr := client.RemoveObject(ctx, params.Bucket, obj.Key, minio.RemoveObjectOptions{}); derr != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("executor: native scan staging remove %s/%s: %w", params.Bucket, obj.Key, derr)
			}
			continue
		}
	}
	return firstErr
}

// nativeScanConsult is the native path's seam onto the SAME pre-store consult
// the Docker path makes (executor.go scanOutputBeforeStore, POST
// /workers/tasks/{id}/output-scan-check). It is a seam, not a second
// implementation: design_fortress_35 rejects a Go copy of the posture decision,
// so main.go wires the Executor's own method here. nil - the default, and what
// every existing caller sees - means "no consult wired", so the native completion
// path stays byte-for-byte the pre-943 path (zero staging hops).
var nativeScanConsult func(ctx context.Context, task *transport.Task,
	staged []transport.OutputFile, uploadPending bool) (bool, string)

// SetNativeScanConsult installs the consult callback. nil restores the pre-943
// behaviour.
func SetNativeScanConsult(fn func(ctx context.Context, task *transport.Task,
	staged []transport.OutputFile, uploadPending bool) (bool, string)) {
	nativeScanConsult = fn
}

// CollectNativeOutputsScanned is the native completion SEQUENCE: stage -> consult
// -> (refuse | durable collect), with the transient cleanup DEFERRED so it runs
// strictly after the consult on every return path. It is the native twin of the
// Docker sequence in executor.go Run() (:1293), and it reuses the existing
// durable contract (NativeBackend.CollectOutputs) UNCHANGED for the real bucket
// write, so a non-refused native task is unchanged except for the staging hops.
//
// Best-effort staging: a staging failure is logged-and-swallowed and the consult
// proceeds with no descriptors - the pre-existing metadata-only consult - so the
// task is never failed by staging (926's posture, verbatim).
//
// ORDERING PROOF (the brief's "consult before rm"): cleanupNativeStagedOutputs is
// reached ONLY through the defer registered below, inside this function, AFTER
// the consult call has returned. Go runs defers at function exit, so the rm
// cannot precede the consult on any path - not the blocked early return, not the
// success return, not a collect error. And it is deliberately NOT folded into
// stageNativeOutputsForScan: that would delete the staged bytes in the same pass
// that created them, before the API's HTTP consult ever fetched them.
//
// A refusal returns (nil, reason) so the caller reports the task FAILED with the
// scan's reason and NOTHING durable is written - the native equivalent of the
// Docker path's blocked Result (ISO 27001: nothing is deleted, a refused object
// simply never lands).
//
// This is now a thin seam onto NativeBackend.CollectOutputs itself (the stage
// -> consult -> durable-write sequence lives there, in collectOutputsScanned,
// so dispatchNativeTask's ordinary backend.CollectOutputs call is the REAL
// production caller and not just this test-only wrapper).
func CollectNativeOutputsScanned(ctx context.Context, n *NativeBackend, h *Handle,
	task *transport.Task, creds *transport.Credentials) ([]transport.OutputFile, error) {
	if n == nil {
		return nil, fmt.Errorf("executor: native scan staging: nil backend")
	}
	return n.CollectOutputs(ctx, h, task, creds)
}
