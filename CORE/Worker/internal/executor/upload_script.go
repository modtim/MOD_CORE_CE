package executor

import (
	"fmt"
	"strconv"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// uploadScriptParams is everything that varies in the output-upload sidecar
// script. It is the single source of truth for both the Docker path
// ((*Executor).uploadSidecar) and the K8s stage-out container
// (buildUploadCmd → stageOutScript) — k8s-reduction-engine W17.
type uploadScriptParams struct {
	Bucket   string // MinIO bucket (payload.MinioBucket)
	Prefix   string // key prefix; non-empty only for wizard_test_run
	CapBytes int    // total-output cap; 0 disables the check
	// LoanAttr is the pre-rendered, shell-safe `mc cp --attr "..."` argument
	// that stamps loan outputs (see loan_staging.go LoanOutputAttr). "" for
	// every non-loan task, which keeps the generated `mc cp` line
	// byte-for-byte identical to the pre-loan script (the W17 golden tests
	// pin that).
	LoanAttr string
	// FoldCleanupPrefix is the transient scan-staging prefix whose staged blobs
	// this run must remove at the END of the SAME container (see
	// stagingCleanupFoldSuffix). Empty (the default) keeps the rendered script
	// byte-for-byte the pre-OPT4B text, which the golden tests pin. Only the
	// Docker-path durable upload sets it, and only after the pre-store consult
	// has already returned, so the strict "cleanup strictly after the consult"
	// ordering the staging note demands still holds.
	FoldCleanupPrefix string
}

// mcRemovePrefixCmd renders the one `mc rm -r --force` command that removes a
// prefix under a bucket. Shared by stagingCleanupCmd (the standalone cleanup
// sidecar) and by stagingCleanupFoldSuffix (the same command folded into the
// durable upload container), so the two can never drift apart.
// FIX 2 (closer 4e425ad615 finding, pre-existing - NOT caused by the lockdown):
// the current minio-client (`minio/mc`-based, the worker's minioClientImage)
// refuses a recursive prefix removal without `--force`: it prints
// "Removal requires --force flag" and deletes NOTHING, and the `|| true` hid
// the failure, so every scan-staging/{task_id}/ prefix stayed in the tenant
// bucket for the API-side sweep. Proven on cpu4 against a throwaway versitygw
// (hlfix-*): OLD leaves both task prefixes intact, NEW removes exactly the
// named prefix and never the bucket root or a sibling task's prefix.
// Exit-status semantics are unchanged: `|| true` still makes the cleanup
// best-effort (an empty prefix or a storage hiccup never fails the task), and
// the fold still re-exits the upload's own status.
func mcRemovePrefixCmd(bucket, prefix string) string {
	return `mc rm -r --force myminio/` + bucket + `/` + prefix + ` || true`
}

// stagingCleanupFoldSuffix wraps an already-rendered upload script so the
// scan-staging cleanup runs at the END of the SAME container, but ONLY when
// the upload itself succeeded, and WITHOUT changing the container's exit
// status: the upload's own status is captured in st and re-exited, so a failed
// upload still fails the sidecar (and still retries) exactly as before, and a
// failed upload leaves the transient blobs for the API-side sweep rather than
// deleting anything extra. Empty prefix returns "" (no fold).
func stagingCleanupFoldSuffix(p uploadScriptParams) string {
	if p.FoldCleanupPrefix == "" {
		return ""
	}
	return `(` + buildUploadScriptBody(p) + `); st=$?; ` +
		`if [ "$st" -eq 0 ]; then `+
			mcRemovePrefixCmd(p.Bucket, p.FoldCleanupPrefix)+
		`; fi; exit $st`
}

// uploadScriptParamsFromTask derives the params from the task payload.
//
// workflow-agentic-execution-loop §wizard-test-run-dispatch — when
// Purpose='wizard_test_run' the sidecar:
//
//	(a) prefixes every key with payload.OutputPrefix so outputs land under
//	    `_wizard_test_runs/{session_id}/{iteration_n}/<hash>` and never mix
//	    with user-visible production outputs.
//	(b) enforces a total-output size cap (SandboxMaxOutputMB, default 100MB)
//	    by summing file sizes BEFORE upload and aborting if exceeded.
func uploadScriptParamsFromTask(task *transport.Task) uploadScriptParams {
	p := uploadScriptParams{Bucket: task.Payload.MinioBucket}
	// GC.MWL.12 Gap A: a task dispatched for a work loan (task.Payload.LoanID
	// set) stamps every uploaded output object with the "uncollected" loan
	// marker via `mc cp --attr` — the same marker the worker's own minio-go
	// path stamps (native_staging.go CollectOutputs → UploadLoanedNativeOutputs).
	// LoanOutputAttr returns "" for non-loan and for not-shell-safe ids, so
	// only a genuinely loaned, valid id changes the script.
	p.LoanAttr = LoanOutputAttr(task.Payload.LoanID)
	if task.Payload.Purpose == "wizard_test_run" {
		if task.Payload.OutputPrefix != "" {
			p.Prefix = task.Payload.OutputPrefix
		}
		c := task.Payload.SandboxMaxOutputMB
		if c <= 0 {
			c = 100
		}
		p.CapBytes = c * 1024 * 1024
	}
	return p
}

// stagingUploadParamsFromTask renders the params for the Fortress 3.5 PRE-STORE
// staging pass: the worker copies the task's output bytes to a transient prefix
// `scan-staging/{task_id}/<hash>` in the SAME tenant bucket, so the API's
// pre-store consult (worker_tasks.py -> task_output_scan.py) can fetch real
// bytes via minio_client.MinioClient(task.tenant_id) instead of scanning
// metadata only. task_output_scan.py:_validate_staging_key pins exactly this
// prefix (`scan-staging/{task.id}/`, task.id being the same API task row id
// task.Payload... consult URL uses), so task.ID is load-bearing here.
//
// No cap (the staging pass mirrors whatever the node produced), no loan attr
// (transient blobs are never collected), and buildUploadScript is reused
// UNCHANGED — same creds, same sidecar image, no presigned URLs.
func stagingUploadParamsFromTask(task *transport.Task) uploadScriptParams {
	return uploadScriptParams{
		Bucket:   task.Payload.MinioBucket,
		Prefix:   "scan-staging/" + strconv.Itoa(task.ID) + "/",
		CapBytes: 0,
		LoanAttr: "",
	}
}

// durableUploadParamsFromTask is uploadScriptParamsFromTask PLUS the folded
// scan-staging cleanup: the SAME bucket / durable key prefix / CapBytes / loan
// attr the durable upload always used, with FoldCleanupPrefix set to this task's
// own `scan-staging/{task.id}/` prefix so the ONE container that lands the
// durable objects also removes the transient blobs the pre-store pass staged.
// The staging pass itself never sets it (its command must not delete what it
// just staged - see the ordering note on stageOutputsForScan), and the caller
// only ever runs this AFTER the pre-store consult has returned, so the
// "cleanup strictly after the consult fetched them" ordering is preserved.
func durableUploadParamsFromTask(task *transport.Task) uploadScriptParams {
	p := uploadScriptParamsFromTask(task)
	p.FoldCleanupPrefix = stagingUploadParamsFromTask(task).Prefix
	return p
}

// buildUploadScript returns the sh script run in the minio-client image:
// hash each /data/output file, upload as <prefix><hash>, and print one
// `UPLOAD,<key>,<fname>,<size>,<ext>` manifest line per file (parsed by
// parseUploadManifest). In sandbox mode sizes are pre-summed against
// CapBytes and the script exits 64 before uploading anything if exceeded.
//
// mc authenticates via the MC_HOST_myminio env var (see buildDownloadCmd);
// there is deliberately no `mc alias set`. Callers own the output channel:
// Docker reads the sidecar's stdout, K8s wraps this in stageOutScript
// (retry + MOD_STAGE_OUT_DONE marker).
// buildUploadScript renders the upload body plus, when FoldCleanupPrefix is
// set, the folded scan-staging cleanup (see stagingCleanupFoldSuffix). Every
// caller that leaves FoldCleanup empty gets the body verbatim - the golden
// strings in upload_script_test.go pin that.
// FOLD COMPOSITION FIX (found by this job's real-docker run, hlfix-* on cpu4):
// stagingCleanupFoldSuffix WRAPS the upload body in the subshell itself, so the
// folded script is the wrapped form ONLY. The old `body + suffix` rendered
// `...done(CAP=0 ...` - a /bin/sh syntax error (exit 2, "syntax error near
// unexpected token `('") that would also have run the upload TWICE - so the
// folded cleanup never ran at all in the Harbor/paid posture. Every no-fold
// caller still gets the body verbatim (the golden contract is unchanged).
func buildUploadScript(p uploadScriptParams) string {
	if p.FoldCleanupPrefix == "" {
		return buildUploadScriptBody(p)
	}
	return stagingCleanupFoldSuffix(p)
}

// buildUploadScriptBody is the sh script itself: hash each /data/output file,
// upload as <prefix><hash>, and print one `UPLOAD,<key>,<fname>,<size>,<ext>`
// manifest line per file (parsed by parseUploadManifest). In sandbox mode sizes
// are pre-summed against CapBytes and the script exits 64 before uploading
// anything if exceeded.
//
// mc authenticates via the MC_HOST_myminio env var (see buildDownloadCmd);
// there is deliberately no `mc alias set`. Callers own the output channel:
// Docker reads the sidecar's stdout, K8s wraps this in stageOutScript
// (retry + MOD_STAGE_OUT_DONE marker).
func buildUploadScriptBody(p uploadScriptParams) string {
	// `--attr` is appended only for loaned tasks (Gap A). p.LoanAttr is the
	// fully-rendered, double-quoted argument (LoanOutputAttr), so for every
	// non-loan task this stays "" and the generated `mc cp` line is
	// byte-for-byte the pre-loan script (pinned by the W17 golden tests).
	attr := ""
	if p.LoanAttr != "" {
		attr = " --attr " + p.LoanAttr
	}
	return fmt.Sprintf(
		`CAP=%d && `+
			`if [ "$CAP" -gt 0 ]; then `+
			`  total=0; `+
			`  for f in /data/output/*; do `+
			`    [ -f "$f" ] || continue; `+
			`    sz=$(stat -c%%s "$f" 2>/dev/null || stat -f%%z "$f"); `+
			`    total=$((total + sz)); `+
			`  done; `+
			`  if [ "$total" -gt "$CAP" ]; then `+
			`    echo "SANDBOX_OUTPUT_TOO_LARGE total=$total cap=$CAP" >&2; `+
			`    exit 64; `+
			`  fi; `+
			`fi && `+
			`for file in /data/output/*; do `+
			`  [ -f "$file" ] || continue; `+
			`  hash=$(sha256sum "$file" | cut -d' ' -f1); `+
			`  fname=$(basename "$file"); `+
			`  ext="${fname##*.}"; `+
			`  size=$(stat -c%%s "$file" 2>/dev/null || stat -f%%z "$file"); `+
			`  mc cp "$file" myminio/%s/%s$hash%s && `+
			`  echo "UPLOAD,%s$hash,$fname,$size,$ext"; `+
			`done`,
		p.CapBytes, p.Bucket, p.Prefix, attr, p.Prefix,
	)
}
