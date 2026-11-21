package heartbeat

import "testing"

// 13.5b / GX.S10b — the file_secrets capability reported on every heartbeat
// must be HONEST: the API's placement gate trusts it, so a containerized worker
// without the verified same-path bind must report false (else a file-secret
// stack is placed on a box that refuses it). The value send() emits is
// fileSecretsCapable(), so these cases are exactly the heartbeat flag.
func TestFileSecretsCapabilityHonest(t *testing.T) {
	// (1) No provider wired (older binary / not yet wired) → true, preserving
	// the pre-verification behaviour (key present, capability asserted).
	h := &Heartbeat{}
	if !h.fileSecretsCapable() {
		t.Fatalf("no provider wired must report file_secrets=true (backward compat)")
	}

	// (2) Host-process worker: the provider says "the daemon sees my root" → true.
	h = &Heartbeat{}
	h.OnFileSecrets(func() bool { return true })
	if !h.fileSecretsCapable() {
		t.Fatalf("verified/host-process worker must report file_secrets=true")
	}

	// (3) Containerized worker WITHOUT the verified bind: provider says false →
	// the heartbeat must report false, so the API stops placing file-secret
	// stacks here (the live-verified 13.5b bug).
	h = &Heartbeat{}
	h.OnFileSecrets(func() bool { return false })
	if h.fileSecretsCapable() {
		t.Fatalf("containerized worker without the verified bind must report file_secrets=false")
	}
}
