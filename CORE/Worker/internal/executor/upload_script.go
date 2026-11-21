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
func buildUploadScript(p uploadScriptParams) string {
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
