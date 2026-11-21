// Package executor — proof_signer.go (fortress 2.3): a per-run ephemeral
// signing key so the worker can sign its own contribution to a run's proof
// chain, independent of the platform's own Ed25519 signature (fortress 2.1/
// 2.2). Two signatures over the same run means a compromised platform key
// alone can never forge "the worker said this ran" — and a compromised
// worker key alone can never forge "the platform attests this ran".
//
// The key is generated fresh for each run, held only in memory for the
// lifetime of that run's signing, and zeroed immediately after use. It is
// never written to disk, never sent anywhere except as the public half
// embedded in the signature it produces, and never reused across runs.
package executor

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"time"
)

// WorkerSignatureAlgorithm is the algorithm name recorded in every
// WorkerProofSignature, mirroring the platform-side ALGORITHM constant in
// CORE/API/services/run_proof_bundle.py so a verifier can tell at a glance
// both signatures use the same primitive.
const WorkerSignatureAlgorithm = "ed25519"

// WorkerProofSignature is the worker-side half of a run's proof chain. It
// is self-contained: the public key travels with the signature because the
// key is ephemeral and never registered anywhere else, so a verifier has
// no other way to learn it.
type WorkerProofSignature struct {
	Algorithm     string `json:"alg"`
	KeyID         string `json:"key_id"`
	PublicKeyB64  string `json:"public_key_b64"`
	PayloadSHA256 string `json:"payload_sha256"`
	SignatureB64  string `json:"signature_b64"`
	SignedAt      string `json:"signed_at"`
}

// RunProofSigner generates (or, for a given run, loads) an ephemeral
// Ed25519 keypair and signs the worker's contribution to that run's proof.
// One instance is meant to live for exactly one run's lifetime; call
// Zero() when that run finishes so the private key does not linger in
// process memory any longer than necessary.
type RunProofSigner struct {
	runID      string
	public     ed25519.PublicKey
	private    ed25519.PrivateKey
	zeroed     bool
	nowFunc    func() time.Time
	randReader func() (ed25519.PublicKey, ed25519.PrivateKey, error)
}

// NewRunProofSigner generates a fresh ephemeral Ed25519 keypair for runID.
// The key lives only in this struct's memory; nothing is persisted.
func NewRunProofSigner(runID string) (*RunProofSigner, error) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("proof signer: generate ephemeral key for run %s: %w", runID, err)
	}
	return &RunProofSigner{
		runID:   runID,
		public:  pub,
		private: priv,
		nowFunc: time.Now,
	}, nil
}

// KeyID derives a short, non-reversible identifier for this run's ephemeral
// public key: the hex-encoded SHA-256 of the raw public key bytes,
// truncated to 16 hex chars. It is not secret — it is only a label a
// verifier can log/display without needing the whole base64 public key.
func (s *RunProofSigner) KeyID() string {
	sum := sha256.Sum256(s.public)
	return hex.EncodeToString(sum[:])[:16]
}

// canonicalPayload produces deterministic bytes for a worker contribution:
// a compact JSON object, keys sorted lexicographically, no extraneous
// whitespace. map[string]interface{} marshaled by encoding/json already
// sorts keys alphabetically and omits indentation, so this is a thin,
// explicit wrapper documenting that guarantee rather than relying on it
// silently.
func canonicalPayload(contribution map[string]interface{}) ([]byte, error) {
	keys := make([]string, 0, len(contribution))
	for k := range contribution {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	ordered := make(map[string]interface{}, len(contribution))
	for _, k := range keys {
		ordered[k] = contribution[k]
	}
	return json.Marshal(ordered)
}

// SignContribution signs the worker's own contribution to a run's proof
// (e.g. executed image digest, node outputs manifest hash, start/end
// timestamps the worker itself observed) and returns a
// WorkerProofSignature. It does not sign the platform's bundle — that
// cross-signature wiring is fortress 2.4 (two-signature verification). This
// only produces the worker's independent half.
func (s *RunProofSigner) SignContribution(contribution map[string]interface{}) (*WorkerProofSignature, error) {
	if s.zeroed || s.private == nil {
		return nil, fmt.Errorf("proof signer: run %s key already zeroed; cannot sign", s.runID)
	}
	payload, err := canonicalPayload(contribution)
	if err != nil {
		return nil, fmt.Errorf("proof signer: canonicalize contribution for run %s: %w", s.runID, err)
	}
	sum := sha256.Sum256(payload)
	signature := ed25519.Sign(s.private, payload)
	now := time.Now
	if s.nowFunc != nil {
		now = s.nowFunc
	}
	return &WorkerProofSignature{
		Algorithm:     WorkerSignatureAlgorithm,
		KeyID:         s.KeyID(),
		PublicKeyB64:  base64.StdEncoding.EncodeToString(s.public),
		PayloadSHA256: hex.EncodeToString(sum[:]),
		SignatureB64:  base64.StdEncoding.EncodeToString(signature),
		SignedAt:      now().UTC().Format(time.RFC3339Nano),
	}, nil
}

// Verify checks a WorkerProofSignature against the given contribution using
// the public key embedded in the signature itself. This is a local
// self-check only (e.g. for tests or pre-send sanity checks) — a remote
// verifier must perform the same check independently, trusting the
// embedded public key only insofar as it trusts the worker's identity
// through some other channel (out of scope for fortress 2.3).
func Verify(sig *WorkerProofSignature, contribution map[string]interface{}) bool {
	if sig == nil || sig.Algorithm != WorkerSignatureAlgorithm {
		return false
	}
	pubBytes, err := base64.StdEncoding.DecodeString(sig.PublicKeyB64)
	if err != nil || len(pubBytes) != ed25519.PublicKeySize {
		return false
	}
	sigBytes, err := base64.StdEncoding.DecodeString(sig.SignatureB64)
	if err != nil {
		return false
	}
	payload, err := canonicalPayload(contribution)
	if err != nil {
		return false
	}
	sum := sha256.Sum256(payload)
	if hex.EncodeToString(sum[:]) != sig.PayloadSHA256 {
		return false
	}
	return ed25519.Verify(ed25519.PublicKey(pubBytes), payload, sigBytes)
}

// Zero wipes the in-memory private key so it does not outlive the run it
// was generated for. Safe to call multiple times; safe to call on a nil
// receiver.
func (s *RunProofSigner) Zero() {
	if s == nil || s.zeroed {
		return
	}
	for i := range s.private {
		s.private[i] = 0
	}
	s.private = nil
	s.zeroed = true
}
