package transport

// fortress 2.3b — the worker's ephemeral-key signature rides the completion
// body as flat "worker_signature" + "worker_contribution" objects (same
// idiom as hf_cache_report / lockdown_report). These tests pin the WIRE
// shape only (the signing itself is proof_signer.go's job, covered there):
//
//   * a TaskResult carrying a signature serializes worker_signature with
//     the EXACT json tags executor.WorkerProofSignature emits (alg /
//     key_id / public_key_b64 / payload_sha256 / signature_b64 /
//     signed_at) — the API verifier (run_proof_bundle.py
//     verify_worker_signature) reads those names, so the transport mirror
//     struct must never drift;
//   * the signed contribution travels beside it under
//     worker_contribution — the verifier checks the signature against the
//     exact payload bytes it covered, it cannot rebuild them API-side;
//   * a TaskResult without one OMITS both keys entirely (omitempty), so
//     unsigned completions stay byte-for-byte unchanged.

import (
	"encoding/json"
	"strings"
	"testing"
)

// TestTaskResultWorkerSignaturePresent: the signature object carries every
// WorkerProofSignature tag the Python verifier expects, and the
// contribution rides beside it.
func TestTaskResultWorkerSignaturePresent(t *testing.T) {
    contribution := map[string]interface{}{
        "task_id": int64(7),
        "worker_name": "w-1",
        "outputs_sha256_manifest": "abc:out.png",
    }
    result := TaskResult{
        Outputs: []OutputFile{},
        LogTail: "done",
        WorkerSignature: &WorkerSignature{
            Algorithm:     "ed25519",
            KeyID:         "0123456789abcdef",
            PublicKeyB64:  "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            PayloadSHA256: strings.Repeat("a", 64),
            SignatureB64:  strings.Repeat("Ag", 64) + "AA=",
            SignedAt:      "2026-10-11T00:00:00Z",
        },
        WorkerContribution: contribution,
    }
    encoded, err := json.Marshal(result)
    if err != nil {
        t.Fatalf("marshal: %v", err)
    }
    var got map[string]any
    if err := json.Unmarshal(encoded, &got); err != nil {
        t.Fatalf("unmarshal: %v", err)
    }
    inner, ok := got["worker_signature"]
    if !ok {
        t.Errorf("worker_signature missing, got %s", encoded)
        return
    }
    m, ok := inner.(map[string]any)
    if !ok {
        t.Errorf("worker_signature is not an object, got %s", encoded)
        return
    }
    for _, key := range []string{"alg", "key_id", "public_key_b64", "payload_sha256", "signature_b64", "signed_at"} {
        if _, has := m[key]; !has {
            t.Errorf("worker_signature.%s missing, got %s", key, encoded)
        }
    }
    if v, _ := m["alg"].(string); v != "ed25519" {
        t.Errorf("worker_signature.alg = %v, want ed25519", m["alg"])
    }
    contrib, ok := got["worker_contribution"]
    if !ok {
        t.Errorf("worker_contribution missing, got %s", encoded)
        return
    }
    cm, ok := contrib.(map[string]any)
    if !ok {
        t.Errorf("worker_contribution is not an object, got %s", encoded)
        return
    }
    for _, key := range []string{"task_id", "worker_name", "outputs_sha256_manifest"} {
        if _, has := cm[key]; !has {
            t.Errorf("worker_contribution.%s missing, got %s", key, encoded)
        }
    }
}

// TestTaskResultWorkerSignatureAbsent: no signature was produced (signing
// failed or was skipped), so BOTH keys are omitted — the ordinary
// completion body is byte-for-byte unchanged.
func TestTaskResultWorkerSignatureAbsent(t *testing.T) {
    result := TaskResult{
        Outputs: []OutputFile{},
        LogTail: "done",
    }
    encoded, err := json.Marshal(result)
    if err != nil {
        t.Fatalf("marshal: %v", err)
    }
    for _, key := range []string{"worker_signature", "worker_contribution"} {
        if strings.Contains(string(encoded), key) {
            t.Errorf("expected %s omitted when nil, got %s", key, encoded)
        }
    }
}

// TestTaskResultWorkerSignatureWithOtherReports: the signature coexists with
// the other flat optional fields — none of the four may clobber another.
func TestTaskResultWorkerSignatureWithOtherReports(t *testing.T) {
    result := TaskResult{
        Outputs: []OutputFile{},
        HFCacheReport: &HFCacheReport{Moved: 1, Verified: 1, Quarantined: 0},
        LockdownReport: map[string]any{"node_kind": "hf_cache_relayout", "exempt": true},
        WorkerSignature: &WorkerSignature{
            Algorithm: "ed25519", KeyID: "ff", PublicKeyB64: "AAA=",
            PayloadSHA256: "b", SignatureB64: "AAA=", SignedAt: "2026-10-11T00:00:00Z",
        },
        WorkerContribution: map[string]interface{}{"task_id": int64(1)},
    }
    encoded, err := json.Marshal(result)
    if err != nil {
        t.Fatalf("marshal: %v", err)
    }
    var got map[string]any
    if err := json.Unmarshal(encoded, &got); err != nil {
        t.Fatalf("unmarshal: %v", err)
    }
    for _, key := range []string{"hf_cache_report", "lockdown_report", "worker_signature", "worker_contribution"} {
        if _, ok := got[key]; !ok {
            t.Errorf("%s missing when all optional fields present, got %s", key, encoded)
        }
    }
}
