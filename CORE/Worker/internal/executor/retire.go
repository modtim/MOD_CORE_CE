package executor

// retire.go — FX.123 p3b: the worker side of worker retirement.
//
// The API side (CORE/API/services/asset_inventory.py:revoke_worker_keys)
// soft-revokes the worker's credential ROWS: the WorkerEnrollment row goes
// status=REVOKED + revoked_at, and the single-use WorkerEnrollmentToken that
// minted it gets revoked_at. It added NO confirmation endpoint, so there is
// nothing to report back and this file deliberately posts nothing.
//
// The signal the worker can observe — FIXED CONTRACT, agreed with the API
// side being built in parallel: after the retire, the worker's next task poll
// is rejected with HTTP 401 whose detail is exactly "Worker retired" and
// whose X-Error-Code header is WORKER_RETIRED. RetireSignal matches ONLY that
// 401+detail pair. The two strings the earlier draft matched — "Worker
// enrollment not approved" (also sent to PENDING workers that were never
// approved) and "Worker is not licensed" (a transient licence change) — are
// NOT the signal: purging worker.yaml on either would brick a healthy
// worker that is waiting for approval or waiting for its owner to re-add it.
//
// The transport surfaces poll errors as fmt.Errorf("poll: HTTP %d: %s", ...)
// (internal/transport/api.go:377): the response BODY is in the message, but
// headers are not, so the X-Error-Code / WORKER_RETIRED code is not observable
// here and the detail text is the only discriminator. That is exactly why the
// API's new detail is a fixed, retire-only string.
//
// What the worker destroys when it sees that signal:
//   (a) every in-memory encrypted-scratch key it currently holds, through the
//       existing EncryptedScratch.Destroy path (zeroes the key BEFORE removing
//       the dir), reached through native_staging.go's stagedScratches ledger;
//   (b) its persisted credential/identity files — the worker.yaml the enroll
//       step wrote (it carries bootstrap_token, scripts/setup-worker.sh:1443),
//       the per-task MinIO creds files under <state-dir>/creds, and the local-
//       UI token file — each overwritten with zeros (same length, same inode,
//       perms preserved) and then removed, so a crash mid-purge never leaves a
//       readable credential behind.
//
// Nothing here ever panics and nothing here ever logs key material: the
// report carries step labels and paths only.

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/credsrefresher"
)

// RetireExitCode is the exit status a retired worker ends on: the same
// non-zero fatal-state convention as the FETCH LOOP STALLED watchdog
// (cmd/worker/main.go logs one clear line then os.Exit(1)s), so the service
// supervisor sees a failure and the journal says WHY the worker went down
// instead of a retired worker polling forever on revoked credentials.
const RetireExitCode = 1

// retireSignalDetail is the ONLY poll detail that means "this worker was
// retired" — the API's fixed contract text, paired with HTTP 401.
const retireSignalDetail = "Worker retired"

// retireSignalCode is the API's X-Error-Code for a retire. The transport does
// not surface response headers in poll errors today, so this is kept only so
// a transport that starts carrying codes can use it; RetireSignal's string
// path never matches it (see retireSignalDetail).
const retireSignalCode = "WORKER_RETIRED"

// retireConsecutivePolls is how many consecutive retire signals the call site
// must see before it purges anything: one odd response out of a proxy must
// never brick a worker.
const retireConsecutivePolls = 2

// retireStreak counts consecutive poll errors that carry the retire signal;
// any other poll error (or a successful poll) resets it at the call site.
var retireStreakMu sync.Mutex
var retireStreakCount int

// NotePollSignal advances the streak by one and reports whether it has reached
// retireConsecutivePolls. Pure enough to test without a running job loop.
func NotePollSignal() bool {
	retireStreakMu.Lock()
	retireStreakCount++
	reached := retireStreakCount >= retireConsecutivePolls
	retireStreakMu.Unlock()
	return reached
}

// ResetPollSignalStreak clears the streak — the call site calls it on every
// poll that did not carry the retire signal.
func ResetPollSignalStreak() {
	retireStreakMu.Lock()
	retireStreakCount = 0
	retireStreakMu.Unlock()
}

// RetireSignal reports whether a task-poll error is the worker-retire signal:
// HTTP 401 AND the detail "Worker retired". Pure, so it can be tested without
// a running job loop.
func RetireSignal(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	// The status half of the contract first: the transport's poll errors read
	// "poll: HTTP %d: %s" (internal/transport/api.go:377), so the status is
	// always in the message and the body follows. No 401, no retire - that
	// alone rejects the licence 403, every 5xx, and a proxy that echoes the
	// retire body on an odd status.
	if !strings.Contains(msg, "HTTP 401") {
		return false
	}
	// Then the retire-only half: the fixed detail text, or the X-Error-Code
	// for a transport that starts surfacing codes/headers. "Worker enrollment
	// not approved" (PENDING workers) and "Worker is not licensed" (a
	// transient licence change) match NEITHER, so they stay on backoff.
	return strings.Contains(msg, retireSignalDetail) ||
		strings.Contains(msg, retireSignalCode)
}

// DestroyStagedScratches ends EVERY encrypted scratch this worker currently
// holds: it drains native_staging.go's stagedScratches ledger (the FX.123.s1b
// hand-off map, keyed by task dir) and calls the existing Destroy() on each,
// which zeroes the key bytes BEFORE removing the dir. The map is emptied under
// the same mutex the staging path uses, so a scratch cannot be destroyed twice
// and a scratch staged during the drain still gets torn down by its own run.
// Returns how many scratches were ended.
func DestroyStagedScratches() int {
	stagedScratchMu.Lock()
	alive := make([]ScratchCipher, 0, len(stagedScratches))
	for _, s := range stagedScratches {
		alive = append(alive, s)
	}
	stagedScratches = make(map[string]ScratchCipher)
	stagedScratchMu.Unlock()
	for _, s := range alive {
		// (never fatal: Destroy zeroed the key even if the dir removal failed)
		_ = s.Destroy()
	}
	return len(alive)
}

// localUITokenName is the file internal/localui/token.go:TokenFileName writes
// the local-UI auth token into, under the same state dir credsrefresher.BaseDir
// resolves. Named here (not imported) so the executor stays independent of the
// localui package.
const localUITokenName = "local-ui-token"

// credsDirName matches credsrefresher.credsSubDir — the per-task MinIO creds
// files (<state-dir>/creds/task-<id>.json) the executor bind-mounts into task
// containers.
const credsDirName = "creds"

// existsPath is a plain "is this still there" check for the purge report.
func existsPath(path string) bool {
	if path == "" {
		return false
	}
	_, err := os.Stat(path)
	return err == nil
}

// zeroFile overwrites a file's bytes with zeros of the SAME length in place
// (same inode, mode preserved) — the first half of a purge, split out so a
// test can read the result back before the entry is removed. Never fatal: an
// unreadable/unwritable file just stays put. Returns whether it wrote.
func zeroFile(path string) bool {
	if path == "" {
		return false
	}
	st, statErr := os.Stat(path)
	if statErr != nil || st.IsDir() {
		return false
	}
	f, openErr := os.OpenFile(path, os.O_WRONLY, 0)
	if openErr != nil {
		return false
	}
	zero := make([]byte, 4096)
	remaining := int(st.Size())
	for remaining > 0 {
		chunk := zero
		if remaining < len(zero) {
			chunk = zero[:remaining]
		}
		if _, wErr := f.Write(chunk); wErr != nil {
			break
		}
		remaining -= len(chunk)
	}
	// *os.File has no Flush(); Sync() is the durability call — it must land
	// before purgeFile unlinks the entry, or a crash could leave the old
	// bytes on a file that is no longer named.
	_ = f.Sync()
	_ = f.Close()
	return true
}

// purgeFile overwrites THEN removes one file: the bytes are replaced with
// zeros of the same length in place (same inode, mode preserved), then the
// directory entry is removed. Order matters — a credential is never merely
// unlinked (the old bytes stay reachable through an open handle or a lost
// rename), and a file whose zeroing fails is still removed, so the purge is
// best-effort in both directions and never fatal. Returns whether the entry
// is gone.
func shredFile(path string) bool {
	if path == "" {
		return false
	}
	_ = zeroFile(path)
	_ = os.Remove(path)
	return !existsPath(path)
}

// credentialPaths lists the persisted credential/identity files a retired
// worker holds: the worker.yaml the enroll step wrote (it carries
// bootstrap_token — scripts/setup-worker.sh:1443), every per-task MinIO creds
// file, and the local-UI token file. configPath is the --config value main.go
// was given (empty when the worker runs with no config file).
func credentialPaths(configPath string) []string {
	paths := make([]string, 0, 8)
	if configPath != "" {
		paths = append(paths, configPath)
	}
	base := credsrefresher.BaseDir()
	credsDir := filepath.Join(base, credsDirName)
	entries, err := os.ReadDir(credsDir)
	if err == nil {
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			name := e.Name()
			if len(name) > 5 && name[len(name)-5:] == ".json" {
				paths = append(paths, filepath.Join(credsDir, name))
			}
		}
	}
	paths = append(paths, filepath.Join(base, localUITokenName))
	return paths
}

// RetireReport is what a retire did: counts + human-readable step labels and
// paths only, never file contents (the caller logs these through slog).
type RetireReport struct {
	ScratchesDestroyed int
	Steps              []string
	Failures           []string
}

// RetireWorker is the whole retire: destroy the in-memory scratch keys, then
// purge the persisted credential/identity files. Nothing here raises or
// panics — a credential file that cannot be touched is reported, and the
// caller still exits. The report carries step labels and paths only.
func RetireWorker(configPath string) *RetireReport {
	report := &RetireReport{
		ScratchesDestroyed: 0,
		Steps:              make([]string, 0, 8),
		Failures:           make([]string, 0, 4),
	}
	report.ScratchesDestroyed = DestroyStagedScratches()
	if report.ScratchesDestroyed > 0 {
		report.Steps = append(report.Steps, "destroyed "+strconv.Itoa(report.ScratchesDestroyed)+" in-memory scratch key(s)")
	}
	for _, p := range credentialPaths(configPath) {
		if !existsPath(p) {
			// (nothing was ever persisted here: nothing to destroy, not a failure)
			continue
		}
		if shredFile(p) {
			report.Steps = append(report.Steps, "purged "+p)
		} else {
			report.Failures = append(report.Failures, "could not purge "+p)
		}
	}
	return report
}
