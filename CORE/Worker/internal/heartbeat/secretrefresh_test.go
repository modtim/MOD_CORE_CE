package heartbeat

import (
	"os"
	"strings"
	"testing"
)

// 30.6.8 / c8-W2 — the heartbeat must advertise `secret_refresh` so the API's
// c8-A refresh gate can decide, BEFORE dispatching the c8-W1 refresh_secret_files
// StackOp, whether this worker can refresh a stack's secret files in place or
// must be marked redeploy_required.
//
// No heartbeat test inspects the map send() builds (send() needs a live config/
// monitor), so — following filesecrets_test.go, which tests the exact value
// send() emits (h.fileSecretsCapable()) rather than the wire map — T1 tests the
// helper send() calls (secretRefreshCapable) AND asserts at source level that
// send() emits the key. Both fail against the pre-c8-W2 heartbeat.go: the helper
// symbol `secretRefreshCapable` does not exist there (compile-fail), and the
// emitted-key grep finds nothing.
func TestSecretRefreshAdvertised(t *testing.T) {
	// (1) The capability is a property of the binary: constant true, never
	// computed from config, never probed.
	if !secretRefreshCapable() {
		t.Fatalf("this binary implements refresh_secret_files → secret_refresh must be true")
	}

	// (2) send() must actually emit the key into the heartbeat payload.
	text, err := os.ReadFile("heartbeat.go")
	if err != nil {
		t.Fatalf("cannot read heartbeat.go source to prove the emitted key: %v", err)
	}
	if !strings.Contains(string(text), `"secret_refresh":`) {
		t.Fatalf("heartbeat.go must emit the secret_refresh key in the send() payload")
	}
}

// T2 — the neighbouring `file_secrets` capability is untouched by c8-W2: the
// helper still exists, still answers true with no provider wired, and send()
// still emits the key exactly as before.
func TestFileSecretsStillAdvertised(t *testing.T) {
	h := &Heartbeat{}
	if !h.fileSecretsCapable() {
		t.Fatalf("no provider wired must still report file_secrets=true (13.5b backward compat)")
	}
	h.OnFileSecrets(func() bool { return false })
	if h.fileSecretsCapable() {
		t.Fatalf("unverified containerized worker must still report file_secrets=false")
	}
	text, err := os.ReadFile("heartbeat.go")
	if err != nil {
		t.Fatalf("cannot read heartbeat.go source: %v", err)
	}
	if !strings.Contains(string(text), `"file_secrets":      fileSecretsCapable,`) {
		t.Fatalf("the file_secrets entry in the send() payload must be unchanged")
	}
}
