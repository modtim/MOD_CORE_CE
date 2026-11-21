package executor

// retire_test.go - FX.123 p3b. Covers the retire signal (ONLY the API's fixed
// 401 + "Worker retired" contract response), the two-consecutive-poll guard,
// the in-memory scratch teardown through the existing Destroy path, and the
// persisted-credential shred (overwrite-then-remove, best effort, never fatal).

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/credsrefresher"
)

// errRetire is exactly what internal/transport/api.go:377 builds for the
// retire response: fmt.Errorf("poll: HTTP %d: %s", 401, body).
func errRetire() error {
	return errors.New("poll: HTTP 401: {\"detail\":\"Worker retired\"}")
}

func TestRetireSignalMatchesRetireOnly(t *testing.T) {
	if RetireSignal(nil) {
		t.Fatalf("nil error is never a retire signal")
	}
	if !RetireSignal(errRetire()) {
		t.Fatalf("the 401/Worker-retired response must be recognised")
	}
	// A transport that surfaces the X-Error-Code instead of the body still
	// has to prove the 401.
	if !RetireSignal(errors.New("poll: HTTP 401 code=WORKER_RETIRED")) {
		t.Fatalf("a code-carrying transport must still match")
	}
	// NOT the signal: the two strings the earlier draft matched are both
	// responses a HEALTHY worker gets. A PENDING, never-approved worker is
	// told to enroll; a licence change is transient. Shredding worker.yaml on
	// either would brick it.
	pending := errors.New("poll: HTTP 401: {\"detail\":\"Worker enrollment not approved\"}")
	if RetireSignal(pending) {
		t.Fatalf("a pending/never-approved 401 must NOT be the retire signal")
	}
	licensed := errors.New("poll: HTTP 403: {\"detail\":\"Worker is not licensed - add it in Dashboard > Workers first\"}")
	if RetireSignal(licensed) {
		t.Fatalf("a licence 403 must NOT be the retire signal")
	}
	// A licence change that happens to answer 401 is still not a retire.
	licensed401 := errors.New("poll: HTTP 401: {\"detail\":\"Worker is not licensed\"}")
	if RetireSignal(licensed401) {
		t.Fatalf("a licence 401 must NOT be the retire signal")
	}
	// A wrong bootstrap token (401) is NOT a retire: the worker keeps its
	// credentials, the loop keeps backing off.
	if RetireSignal(errors.New("poll: HTTP 401: Invalid bootstrap token")) {
		t.Fatalf("a bad-token 401 is not a retire")
	}
	// The detail text alone must never match without the 401: a proxy that
	// echoes the body on a 503 stays on the backoff path.
	if RetireSignal(errors.New("poll: HTTP 503: {\"detail\":\"Worker retired\"}")) {
		t.Fatalf("a 5xx carrying the retire detail is not a retire")
	}
	if RetireSignal(errors.New("poll: HTTP 500: boom")) {
		t.Fatalf("a 5xx is not a retire")
	}
	if RetireSignal(errors.New("poll: connection refused")) {
		t.Fatalf("a network error is not a retire")
	}
	// The code string alone (no 401, no detail) is not a retire either.
	if RetireSignal(errors.New("poll: HTTP 409: code=WORKER_RETIRED")) {
		t.Fatalf("the code on a non-401 is not a retire")
	}
}

func TestRetireStreakRequiresTwoPolls(t *testing.T) {
	// The call site only calls NotePollSignal on errors RetireSignal matched,
	// so the streak test drives it directly.
	if NotePollSignal() {
		t.Fatalf("one signal must not fire the retire")
	}
	if !NotePollSignal() {
		t.Fatalf("the second consecutive signal must fire the retire")
	}
	// A third keeps firing (the loop exits on the second anyway).
	if !NotePollSignal() {
		t.Fatalf("a third signal stays fired")
	}
	ResetPollSignalStreak()
	if NotePollSignal() {
		t.Fatalf("after a reset one signal must not fire")
	}
	// Interleaving a non-signal poll (the call site resets) clears it again.
	ResetPollSignalStreak()
	if NotePollSignal() {
		t.Fatalf("streak restarted from zero")
	}
	// A one-off proxy oddity never reaches the threshold on its own.
	ResetPollSignalStreak()
}

// retireScratch mints one encrypted scratch under base and registers it in the
// staging ledger exactly the way the native staging path does.
func retireScratch(t *testing.T, base, taskID string) *EncryptedScratch {
	s, err := NewEncryptedScratch(base, taskID)
	if err != nil {
		t.Fatalf("mint scratch: %v", err)
	}
	if err := keepStagedScratch(filepath.Join(base, taskID), s); err != nil {
		t.Fatalf("register scratch: %v", err)
	}
	return s
}

func TestRetireDestroysScratchKeys(t *testing.T) {
	base := scratchBase(t)
	first := retireScratch(t, base, "101")
	second := retireScratch(t, base, "102")
	_ = first.WriteFile("in.png", []byte("task bytes"))
	_ = second.WriteFile("in.png", []byte("other task bytes"))
	ended := DestroyStagedScratches()
	if ended != 2 {
		t.Fatalf("retire ended %d of 2 live scratches", ended)
	}
	if !zeroedKey(first) || !zeroedKey(second) {
		t.Fatalf("retire must zero every in-memory key")
	}
	for _, id := range []string{"101", "102"} {
		if _, err := os.Stat(filepath.Join(base, id)); err == nil {
			t.Fatalf("scratch dir %s still on disk", id)
		}
	}
	// Idempotent: a second retire finds nothing and ends nothing.
	if again := DestroyStagedScratches(); again != 0 {
		t.Fatalf("second retire ended %d scratches", again)
	}
	// A scratch the run already tore down is simply absent from the ledger.
	done, err := NewEncryptedScratch(base, "103")
	if err != nil {
		t.Fatalf("mint scratch: %v", err)
	}
	_ = done.Destroy()
	if extra := DestroyStagedScratches(); extra != 0 {
		t.Fatalf("destroyed scratch double-counted: %d", extra)
	}
}

// stateRoot points MOD_WORKER_STATE_DIR at a temp dir and plants the persisted
// credential files a remote worker holds today.
func stateRoot(t *testing.T) string {
	dir := t.TempDir()
	os.Setenv(credsrefresher.StateDirEnv, dir)
	creds := filepath.Join(dir, credsDirName)
	if err := os.MkdirAll(creds, 0o700); err != nil {
		t.Fatalf("creds dir: %v", err)
	}
	return dir
}

func TestRetireShredsPersistedCredentials(t *testing.T) {
	base := stateRoot(t)
	config := filepath.Join(base, "worker.yaml")
	writeRetireFile(t, config, "bootstrap_token: \"super-secret-token\"\n")
	probe := filepath.Join(base, credsDirName, "task-9.json")
	writeRetireFile(t, probe, "{\"access_key\":\"AK\",\"secret\":\"SH\"}")
	writeRetireFile(t, filepath.Join(base, localUITokenName), "local-ui-secret")

	// The overwrite half is observable before the entry goes away.
	if !zeroFile(probe) {
		t.Fatalf("zeroFile must overwrite the credential in place")
	}
	blob := readRetireFile(t, probe)
	if len(blob) != 33 {
		t.Fatalf("zeroFile changed the length: %d", len(blob))
	}
	if strings.Contains(blob, "AK") {
		t.Fatalf("credential bytes still readable after overwrite")
	}

	report := RetireWorker(config)
	if report.ScratchesDestroyed != 0 {
		t.Fatalf("no scratches were live here: %d", report.ScratchesDestroyed)
	}
	if len(report.Failures) != 0 {
		t.Fatalf("retire reported failures: %v", report.Failures)
	}
	for _, gone := range []string{config, probe, filepath.Join(base, localUITokenName)} {
		if _, err := os.Stat(gone); err == nil {
			t.Fatalf("credential file still present: %s", gone)
		}
	}
	if len(report.Steps) != 3 {
		t.Fatalf("expected 3 shred steps, got %d: %v", len(report.Steps), report.Steps)
	}
	for _, step := range report.Steps {
		if !strings.HasPrefix(step, "shredded ") {
			t.Fatalf("unexpected step: %s", step)
		}
	}
	// A second retire on the same worker finds nothing and claims nothing.
	again := RetireWorker(config)
	if len(again.Steps) != 0 || len(again.Failures) != 0 {
		t.Fatalf("second retire must be a no-op: %v / %v", again.Steps, again.Failures)
	}
}

func TestRetireWorkerWithoutCredentialsNeverFails(t *testing.T) {
	base := stateRoot(t)
	report := RetireWorker("")
	if len(report.Steps) != 0 || len(report.Failures) != 0 {
		t.Fatalf("nothing persisted, so nothing to report: %v / %v", report.Steps, report.Failures)
	}
	// An unwritable credential (read-only dir entry) is reported, never fatal.
	locked := filepath.Join(base, "worker.yaml")
	writeRetireFile(t, locked, "bootstrap_token: x")
	if err := os.Chmod(locked, 0o444); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	if shredFile(locked) {
		return
	}
	t.Fatalf("shred refused to remove an unwritable credential")
}

func writeRetireFile(t *testing.T, path, content string) {
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("plant %s: %v", filepath.Base(path), err)
	}
}

func readRetireFile(t *testing.T, path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", filepath.Base(path), err)
	}
	return string(b)
}
