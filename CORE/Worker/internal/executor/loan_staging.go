package executor

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/minio/minio-go/v7"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// loan_staging.go — GC.MWL.12 (launch-completion-megaplan Group C,
// manager-work-loans). Sibling of native_staging.go / k8s_staging.go: same
// minio-go staging idiom storage_migration.go already established for
// stamping S3 user-metadata (its Sha256 skip-check key), applied here to
// the two loan-specific marks a borrower's own output upload must carry
// before anything downstream — including the API's collect step,
// `services/mesh_loan_transfer.py` `collect_loan` — may treat an object as
// a result:
//
//   - "uncollected" (+ the loan id) on every output a loaned run's OWN node
//     containers write to the borrower's bucket. `collect_loan`'s own
//     docstring already says outputs are "staged, not results" before the
//     owner pulls them; this metadata is the durable, storage-side twin of
//     that state, readable directly off the object (StatObject) without
//     needing the owner's DB row — a future collect-time check, or a
//     direct bucket audit, can tell "loan output, not yet collected" apart
//     from an ordinary result by the object alone.
//   - "partial" (+ the loan id when known) on whatever a killed or failed
//     node managed to write before it died. Retained for the tenant —
//     never silently discarded — but IsPartialOutput must gate every
//     downstream consumer (collect, staged-manifest, UI) out of treating
//     it as a completed result.
//
// minio-go's PutObjectOptions.UserMetadata keys arrive back through
// StatObject as "X-Amz-Meta-<Key>"; this file uses the same convention
// storage_migration.go already established (its "Sha256" key arrives as
// "X-Amz-Meta-Sha256").
//
// Every output-upload surface stamps the same markers, built in ONE place
// (this file) so the key strings and marker values can never drift:
//
//   - the worker's own minio-go upload for native (imageless) tasks — the
//     surface native_staging.go owns (GP.5) — passes LoanOutputMetadata /
//     PartialOutputMetadata straight to PutObject; and
//   - the Docker mc-sidecar upload script (upload_script.go, executor.go's
//     uploadSidecar) and the K8s stage-out container (k8s_staging.go) both
//     route through buildUploadScript, which renders the SAME map into the
//     `mc cp --attr "Key=Value;..."` flag via LoanOutputAttr.
//
// The mc `--attr` flag (minio/mc `cp --attr`) takes semicolon-separated
// key=value pairs that minio-go surfaces back as X-Amz-Meta-<Key>, the same
// convention the native path relies on. Non-loan tasks (LoanID == "") get no
// --attr flag at all, so their upload command is byte-for-byte what it was
// before this file existed.
const (
	// LoanStatusMetaKey is the user-metadata key naming a loan output's
	// collection state. Absent entirely = an ordinary, non-loan output —
	// exactly what every task produces today.
	LoanStatusMetaKey = "Mod-Loan-Status"
	// LoanIDMetaKey names which WorkLoan (services/mesh_loans.py
	// `WorkLoan.id`) an output belongs to. Empty/absent on a non-loan
	// output, or when the loan id was not known at stamp time.
	LoanIDMetaKey = "Mod-Loan-Id"

	// LoanStatusUncollected marks a loaned run's COMPLETE output, staged on
	// the borrower's bucket but not yet pulled by the owner's collect step
	// (GC.MWL.9). Never a result until `mesh_loan_transfer.collect_loan`'s
	// own status gate lets the owner pull it.
	LoanStatusUncollected = "uncollected"
	// LoanStatusPartial marks output from a node killed or failed
	// mid-flight — retained for the tenant, but never mistaken for a
	// completed result by any downstream consumer.
	LoanStatusPartial = "partial"
)

// LoanOutputMetadata builds the S3 user-metadata map to stamp on every
// object a loaned run's OWN successful node-output upload writes to the
// borrower's bucket. loanID is the WorkLoan.id the run was dispatched for
// (transport.TaskPayload.LoanID); a genuine loaned dispatch always has one,
// but an empty loanID still stamps the "uncollected" status (an operator
// can still find the loan from the borrower's own WorkflowRun ->
// MeshLoanEvent trail).
func LoanOutputMetadata(loanID string) map[string]string {
	m := map[string]string{LoanStatusMetaKey: LoanStatusUncollected}
	if loanID != "" {
		m[LoanIDMetaKey] = loanID
	}
	return m
}

// PartialOutputMetadata builds the S3 user-metadata map to stamp on
// whatever a killed or failed node managed to write before it died. loanID
// may be empty — a partial marker applies to ANY killed/failed run, loaned
// or not; the loan id is attached only when the run was in fact a loan.
func PartialOutputMetadata(loanID string) map[string]string {
	m := map[string]string{LoanStatusMetaKey: LoanStatusPartial}
	if loanID != "" {
		m[LoanIDMetaKey] = loanID
	}
	return m
}

// IsLoanUncollected reports whether object metadata (as read back via
// minio.ObjectInfo.UserMetadata, or an equivalent map) marks an object an
// uncollected loan output.
func IsLoanUncollected(meta map[string]string) bool {
	return meta != nil && meta[LoanStatusMetaKey] == LoanStatusUncollected
}

// IsPartialOutput reports whether object metadata marks an object a
// partial (killed/failed mid-flight) output — must never be treated as a
// completed result.
func IsPartialOutput(meta map[string]string) bool {
	return meta != nil && meta[LoanStatusMetaKey] == LoanStatusPartial
}

// LoanIDFromMetadata reads back which loan an object belongs to, "" if the
// metadata carries none.
func LoanIDFromMetadata(meta map[string]string) string {
	if meta == nil {
		return ""
	}
	return meta[LoanIDMetaKey]
}

// LoanOutputAttr renders LoanOutputMetadata(loanID) for the mc-sidecar and
// K8s stage-out `mc cp --attr "..."` flag (the shared buildUploadScript
// path, Gap A). It returns "" when the flag must be omitted, which callers
// treat as "do not pass the flag at all" — so a non-loan task's `mc cp`
// line stays byte-for-byte what it was before loans existed.
//
// The loan id is not blindly trusted: a genuinely loaned dispatch carries a
// UUID or an integer WorkLoan.id, but LoanID rides the task payload over the
// wire and the value ends up interpolated into a shell command the sidecar
// runs. So:
//
//   - empty loanID → no flag. Today's non-loan tasks all have LoanID == ""
//     and must keep their exact upload command; the borrower's own
//     WorkflowRun -> MeshLoanEvent trail is the source of truth for a loan
//     the worker could not be told the id of (same stance LoanOutputMetadata
//     documents for the minio-go path).
//   - non-empty but not shell-safe (quote, semicolon, space, shell
//     metachar, or non-UTF-8) → no flag + a log line, rather than risk
//     breaking out of the argument.
//
// Everything else (UUID, integer, or any other shell-safe token) is stamped.
func LoanOutputAttr(loanID string) string {
	if loanID == "" {
		return ""
	}
	if !shellSafeMetaValue(loanID) {
		slog.Default().Warn("skipping loan marker on output upload: loan id not shell-safe", "loan_id", loanID)
		return ""
	}
	return loanMetadataAttr(LoanOutputMetadata(loanID))
}

// loanMetadataAttr renders a metadata map (as built by LoanOutputMetadata /
// PartialOutputMetadata) as a single shell-safe, double-quoted `--attr`
// argument: `Key1=Val1;Key2=Val2`. Keys are the fixed Mod-Loan-* constants
// (alphanumeric + hyphen) and the values are already vetted (fixed status
// strings, or a shell-safe loan id), so no embedded quote/semicolon/space
// can break out of the argument. Sorted for a deterministic string. Empty
// map → "".
func loanMetadataAttr(meta map[string]string) string {
	if len(meta) == 0 {
		return ""
	}
	keys := make([]string, 0, len(meta))
	for k := range meta {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+meta[k])
	}
	return `"` + strings.Join(parts, ";") + `"`
}

// shellSafeMetaValue reports whether s is safe to interpolate as an S3
// user-metadata VALUE inside the `mc cp --attr "..."` argument. The value
// lives between the outer double quotes and `;`-separated key=value pairs,
// so it must not itself contain a double quote, a semicolon, or any shell
// metacharacter; we accept only [A-Za-z0-9._-]. Loan ids are UUIDs (hex +
// `-`) or integers, so this is permissive for every real id while still
// rejecting anything that could break out of the argument.
func shellSafeMetaValue(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z':
		case r >= 'A' && r <= 'Z':
		case r >= '0' && r <= '9':
		case r == '.' || r == '_' || r == '-':
		default:
			return false
		}
	}
	return true
}

// uploadAndVerifyOutputWithMeta is uploadAndVerifyOutput (native_staging.go)
// with an S3 user-metadata stamp attached to the PutObject call. Kept as its
// own small function (rather than adding a variadic/optional parameter to
// the existing one) so native_staging.go's own upload path is untouched —
// every existing (non-loan, non-partial) native upload still calls the
// original, unmarked function exactly as before.
func uploadAndVerifyOutputWithMeta(ctx context.Context, client *minio.Client, bucket, key, path, wantHash string, meta map[string]string) error {
	fi, err := os.Stat(path)
	if err != nil {
		return err
	}
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := client.PutObject(ctx, bucket, key, f, fi.Size(), minio.PutObjectOptions{UserMetadata: meta}); err != nil {
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

// uploadNativeOutputsWithMeta is UploadNativeOutputs (native_staging.go)
// generalized to stamp an arbitrary S3 user-metadata map on every object it
// writes. Reuses every other unexported helper native_staging.go already
// provides (scanOutputDir, EnsureCredsFresh, s3ClientFor, isAuthError,
// outputEntryToFile, nativeCredRecheckMargin, uploadScriptParamsFromTask) —
// the only behavioral difference from the original is the metadata stamp
// and the credential re-mint retry uses the metadata-aware upload too.
func uploadNativeOutputsWithMeta(ctx context.Context, task *transport.Task, creds *transport.Credentials, outputDir string, meta map[string]string, reMint func(context.Context, int) (*transport.Credentials, error)) ([]transport.OutputFile, error) {
	if task == nil {
		return nil, fmt.Errorf("executor: uploadNativeOutputsWithMeta: nil task")
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
		uerr := uploadAndVerifyOutputWithMeta(ctx, client, params.Bucket, key, path, e.hash, meta)
		if uerr == nil {
			manifest = append(manifest, outputEntryToFile(e))
			continue
		}
		if !isAuthError(uerr) || reMint == nil || remintedFlag {
			return nil, fmt.Errorf("executor: uploadNativeOutputsWithMeta: %w", uerr)
		}
		// One-shot fallback: re-mint, then retry THIS object exactly once —
		// mirrors UploadNativeOutputs's own retry contract.
		nc, merr := reMint(ctx, task.ID)
		if merr != nil {
			return nil, fmt.Errorf("executor: uploadNativeOutputsWithMeta: auth failure on %s and re-mint failed: %w", key, merr)
		}
		nclient, cerr := s3ClientFor(nc.MinIO)
		if cerr != nil {
			return nil, fmt.Errorf("executor: uploadNativeOutputsWithMeta: rebuilding client after re-mint: %w", cerr)
		}
		client = nclient
		remintedFlag = true
		if uerr2 := uploadAndVerifyOutputWithMeta(ctx, client, params.Bucket, key, path, e.hash, meta); uerr2 != nil {
			return nil, fmt.Errorf("executor: uploadNativeOutputsWithMeta: retry after re-mint failed on %s: %w", key, uerr2)
		}
		manifest = append(manifest, outputEntryToFile(e))
	}
	return manifest, nil
}

// UploadLoanedNativeOutputs is UploadNativeOutputs (native_staging.go) for a
// dispatched work loan: every output object gets LoanOutputMetadata(loanID)
// stamped at upload time, so the bytes land in the borrower's bucket
// already tagged "uncollected" — never presented as a result until the
// owner's collect step (`services/mesh_loan_transfer.py` `collect_loan`)
// pulls them. MinIONativeStager.CollectOutputs routes here automatically
// when task.Payload.LoanID is set.
func UploadLoanedNativeOutputs(ctx context.Context, task *transport.Task, creds *transport.Credentials, outputDir, loanID string, reMint func(context.Context, int) (*transport.Credentials, error)) ([]transport.OutputFile, error) {
	return uploadNativeOutputsWithMeta(ctx, task, creds, outputDir, LoanOutputMetadata(loanID), reMint)
}

// UploadPartialNativeOutputs uploads whatever a killed or failed node left
// behind in outputDir — same scan/hash/upload path as UploadNativeOutputs,
// but every object is stamped PartialOutputMetadata(loanID) instead of
// nothing. Unlike NativeBackend.CollectOutputs (which refuses outright for
// any non-succeeded task, publishing nothing), this is the one path that
// intentionally still uploads on failure: the bytes are retained for the
// tenant rather than silently discarded, just never presented as a
// completed result (IsPartialOutput must gate every downstream consumer).
// loanID is "" for an ordinary (non-loan) kill.
func UploadPartialNativeOutputs(ctx context.Context, task *transport.Task, creds *transport.Credentials, outputDir, loanID string, reMint func(context.Context, int) (*transport.Credentials, error)) ([]transport.OutputFile, error) {
	return uploadNativeOutputsWithMeta(ctx, task, creds, outputDir, PartialOutputMetadata(loanID), reMint)
}

// PartialCollector is an optional NativeStager capability: upload whatever
// exists in outputDir when the task did NOT succeed, stamped as partial.
// MinIONativeStager implements it; a test/no-op stager may not, in which
// case NativeBackend.CollectPartialOutputs no-ops with (nil, nil) — mirrors
// how NativeBackend.SubmitStaged/CollectOutputs degrade to no staging at
// all when Stager is nil.
type PartialCollector interface {
	CollectPartialOutputs(ctx context.Context, task *transport.Task, creds *transport.Credentials, outputDir, loanID string) ([]transport.OutputFile, error)
}

// CollectPartialOutputs implements PartialCollector for MinIONativeStager.
func (s *MinIONativeStager) CollectPartialOutputs(ctx context.Context, task *transport.Task, creds *transport.Credentials, outputDir, loanID string) ([]transport.OutputFile, error) {
	return UploadPartialNativeOutputs(ctx, task, creds, outputDir, loanID, s.ReMint)
}

var _ PartialCollector = (*MinIONativeStager)(nil)

// CollectPartialOutputs uploads <scratch>/<task>/output for a task that did
// NOT succeed (killed mid-flight or failed outright), stamping every
// object "partial" so no downstream consumer mistakes it for a completed
// result. Refuses (without uploading) a task that is still running
// (nothing final to collect yet) or one that DID succeed (that is
// CollectOutputs's job — a succeeded task's output is never "partial").
// n.Stager not implementing PartialCollector, or being nil, is a silent
// no-op (nil, nil) — the same "no staging configured" degrade
// NativeBackend.CollectOutputs already uses.
func (n *NativeBackend) CollectPartialOutputs(ctx context.Context, h *Handle, task *transport.Task, creds *transport.Credentials, loanID string) ([]transport.OutputFile, error) {
	if n.Stager == nil {
		return nil, nil
	}
	pc, ok := n.Stager.(PartialCollector)
	if !ok {
		return nil, nil
	}
	if h == nil || h.Backend != "native" {
		return nil, fmt.Errorf("executor: NativeBackend.CollectPartialOutputs: bad handle")
	}
	if task == nil {
		return nil, fmt.Errorf("executor: NativeBackend.CollectPartialOutputs: nil task")
	}
	n.mu.Lock()
	run, ok := n.runs[h.ID]
	n.mu.Unlock()
	if !ok {
		return nil, fmt.Errorf("executor: NativeBackend.CollectPartialOutputs: unknown task %q", h.ID)
	}
	run.mu.Lock()
	phase := run.status.Phase
	run.mu.Unlock()
	if phase == TaskRunning {
		return nil, fmt.Errorf("executor: NativeBackend.CollectPartialOutputs: task %q is still running — nothing to collect yet", h.ID)
	}
	if phase == TaskSucceeded {
		return nil, fmt.Errorf("executor: NativeBackend.CollectPartialOutputs: task %q succeeded — use CollectOutputs, not the partial path", h.ID)
	}
	outputDir := filepath.Join(n.ScratchRoot, h.ID, "output")
	return pc.CollectPartialOutputs(ctx, task, creds, outputDir, loanID)
}
