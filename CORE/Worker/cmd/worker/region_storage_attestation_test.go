package main

// fortress-4.5 (S) — the Go worker puts `region` + `storage` into its SIGNED
// worker_contribution (main.go, executeTask). The API lifts those string keys
// out of the contribution JSONB into node_run_attestations.attested_region /
// attested_storage (services/node_run_attestations.py _insert_attestation),
// and the 4.4 compliance-tag enforcement reads the columns — NULL means
// "not attested" (never "allowed"). These tests pin the three things that
// matter:
//
//   * the region claim matches utils/residency.zone_from_tags EXACTLY for the
//     tags the worker itself sends on the heartbeat (WORKER_HARDWARE_TAGS
//     'region:<zone>'), so the column and the API's computed zone agree;
//   * an unknown zone / empty bucket OMITS the key (no empty-string claim);
//   * the keys are added BEFORE SignContribution, so the Ed25519 signature
//     covers them — Verify passes with them present, and fails when only the
//     region value changes.
//
// package main (cmd/worker): the contribution is built in main.go, so the
// helpers under test live there; the signing itself is proof_signer.go's job,
// covered in the executor package.

import (
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func TestWorkerResidencyZone(t *testing.T) {
	cases := []struct {
		name string
		tags []string
		want string
	}{
		{"plain us", []string{"region:us"}, "us"},
		{"mixed tags", []string{"cuda", "region:eu"}, "eu"},
		{"case-insensitive", []string{"Region:US"}, "us"},
		{"whitespace", []string{" region:eu "}, "eu"},
		{"no tag => omitted", []string{"cuda", "cpu"}, ""},
		{"unknown zone => omitted", []string{"cuda", "region:apac"}, ""},
		{"bare prefix => omitted", []string{"region:"}, ""},
		{"first valid wins", []string{"region:apac", "region:us"}, "us"},
	}
	for _, c := range cases {
		if got := workerResidencyZone(c.tags); got != c.want {
			t.Errorf("%s: workerResidencyZone(%v) = %q, want %q", c.name, c.tags, got, c.want)
		}
	}
}

func TestStorageTargetLabel(t *testing.T) {
	task := transport.Task{
		ID:      7,
		Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"},
	}
	if got := storageTargetLabel(&task); got != "tenant-1-files" {
		t.Errorf("storageTargetLabel bucket = %q, want tenant-1-files", got)
	}
	empty := transport.Task{Payload: transport.TaskPayload{MinioBucket: "   "}}
	if got := storageTargetLabel(&empty); got != "" {
		t.Errorf("blank bucket must yield \"\" (key omitted), got %q", got)
	}
	if got := storageTargetLabel(nil); got != "" {
		t.Errorf("nil task must yield \"\", got %q", got)
	}
}

// The signature must cover the new keys: they are inserted into the map
// before SignContribution, so canonicalPayload (keys sorted at call time)
// includes them, and the SAME map is what rides the wire beside the
// signature — the API verifier re-canonicalizes those exact keys.
func TestContributionRegionStorageSigned(t *testing.T) {
	signer, err := executor.NewRunProofSigner("9:8:1")
	if err != nil {
		t.Fatalf("NewRunProofSigner: %v", err)
	}
	defer signer.Zero()

	contribution := map[string]interface{}{
		"task_id":                 int64(7),
		"worker_name":             "worker-a",
		"outputs_sha256_manifest": "abc:out.png",
		"region":                  "eu",
		"storage":                 "tenant-1-files",
	}
	sig, signErr := signer.SignContribution(contribution)
	if signErr != nil {
		t.Fatalf("SignContribution: %v", signErr)
	}
	if !executor.Verify(sig, contribution) {
		t.Fatal("signature must verify with region/storage inside the signed payload")
	}

	tampered := map[string]interface{}{}
	for k, v := range contribution {
		tampered[k] = v
	}
	tampered["region"] = "us"
	if executor.Verify(sig, tampered) {
		t.Fatal("Verify must fail when the signed region claim changes")
	}
}

// Unknown claims stay OUT of the signed payload: the contribution is the old
// shape byte-for-byte, so an older/unsigned-region worker keeps verifying and
// the API columns stay NULL (not attested), never "".
func TestContributionOmitsUnknownClaims(t *testing.T) {
	signer, err := executor.NewRunProofSigner("9:8:2")
	if err != nil {
		t.Fatalf("NewRunProofSigner: %v", err)
	}
	defer signer.Zero()

	contribution := map[string]interface{}{
		"task_id":                 int64(7),
		"worker_name":             "worker-a",
		"outputs_sha256_manifest": "abc:out.png",
	}
	if workerResidencyZone([]string{"cuda"}) != "" {
		t.Fatal("unknown zone must not produce a claim")
	}
	sig, signErr := signer.SignContribution(contribution)
	if signErr != nil {
		t.Fatalf("SignContribution: %v", signErr)
	}
	if !executor.Verify(sig, contribution) {
		t.Fatal("unsigned-region contribution must still verify (old shape unchanged)")
	}
	withKey := map[string]interface{}{}
	for k, v := range contribution {
		withKey[k] = v
	}
	withKey["region"] = ""
	if executor.Verify(sig, withKey) {
		t.Fatal("an empty-string region is a different payload — must not verify")
	}
}
