package executor

// 1665q (CE Gap W2) — the CE credentials bundle carries an EMPTY harbor block
// (1665l Gap W: the API issues harbor_* as unset when DOCKER_REGISTRY is empty,
// and transport.go decodes a missing/null `harbor` key into zero-value
// HarborCredentials). The container pipeline must then reference images as-is,
// with no registry prefix, from the local daemon or the default public registry
// — NOT fail closed. The paid/SaaS posture is unchanged: a registry with
// missing creds, or creds with no registry host (the host/creds mismatch the
// original 401 regression was), stays fatal.

import (
	"context"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func TestNoRegistryPosture_EmptyBlockIsNotAMisconfiguration(t *testing.T) {
	if !noRegistryPosture(transport.HarborCredentials{}) {
		t.Fatal("an empty harbor block (no registry, no creds) must be the CE no-registry posture")
	}
	if !noRegistryPosture(transport.HarborCredentials{Registry: "   "}) {
		t.Fatal("whitespace-only registry with no creds must be the CE no-registry posture")
	}
	if noRegistryPosture(transport.HarborCredentials{Username: "u", Password: "p"}) {
		t.Fatal("creds present with no registry host is NOT the no-registry posture")
	}
	if noRegistryPosture(transport.HarborCredentials{Registry: "reg.example"}) {
		t.Fatal("a configured registry is never the no-registry posture")
	}
}

func TestHarborRegistryForTask_NoRegistryUsesTaskImageAsIs(t *testing.T) {
	got, err := harborRegistryForTask(
		transport.TaskPayload{ProcessingImage: "minio-client:latest", NodeKind: "image_input"},
		transport.HarborCredentials{},
	)
	if err != nil {
		t.Fatalf("CE no-registry task must not fail closed, got: %v", err)
	}
	if got != "" {
		t.Errorf("no-registry task must resolve to no host, got %q", got)
	}
}

func TestHarborRegistryForTask_NoRegistryAndNoImageStillFatal(t *testing.T) {
	_, err := harborRegistryForTask(transport.TaskPayload{NodeKind: "image_input"}, transport.HarborCredentials{})
	if err == nil {
		t.Fatal("a task with no image AND no registry is a misconfiguration — must fail closed")
	}
}

func TestHarborRegistryForTask_CredsWithoutHostStillFatal(t *testing.T) {
	_, err := harborRegistryForTask(
		transport.TaskPayload{ProcessingImage: "img:latest"},
		transport.HarborCredentials{Username: "u", Password: "p"},
	)
	if err == nil {
		t.Fatal("expected a fatal error when Harbor creds are present but the registry host is empty")
	}
	if !strings.Contains(err.Error(), "harbor_registry") {
		t.Errorf("error should name the Vault key, got: %v", err)
	}
}

func TestHarborRegistryForTask_RegistryWithCredsUnchanged(t *testing.T) {
	got, err := harborRegistryForTask(
		transport.TaskPayload{ProcessingImage: "img:latest"},
		transport.HarborCredentials{Registry: "  reg.example/  ", Username: "u", Password: "p"},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "reg.example" {
		t.Errorf("expected the trimmed registry host, got %q", got)
	}
}

func TestSidecarImageRef_NoRegistryHasNoSidecar(t *testing.T) {
	// 1665w inverts the phase-1 fallback: CE never names the public upstream
	// mc client at all. Every staging leg is in process under the posture
	// (ce_inprocess_staging.go: minio-go + the alpine tar helper), so the
	// no-registry ref must be the EMPTY ref - nothing to pull, and any
	// caller that still tried to pull it would hand "" to the daemon.
	if got := sidecarImageRef(""); got != "" {
		t.Errorf("no-registry posture must yield no sidecar ref at all, got %q", got)
	}
}

func TestSidecarImageRef_RegistryPrefixUnchanged(t *testing.T) {
	if got := sidecarImageRef("reg.example"); got != "reg.example/modtim/minio-client:latest" {
		t.Errorf("expected the Harbor-prefixed sidecar ref, got %q", got)
	}
}

// The digest-pinning supply-chain guard must survive the CE carve: a task that
// pins an image by digest still executes exactly that artifact, with no host
// silently prepended.
func TestResolveProcessingImage_NoRegistryKeepsDigestPin(t *testing.T) {
	e := testExecutor([]string{"cpu"}, "ignored")
	got := e.resolveProcessingImage(
		transport.TaskPayload{ProcessingImage: "minio-client", ImageDigest: "sha256:deadbeef"},
		"",
	)
	if got != "minio-client@sha256:deadbeef" {
		t.Errorf("expected the digest-pinned bare ref, got %q", got)
	}
}

// Run() must still fail closed, before touching the daemon, for the optional shape.
func TestRun_RegistryWithMissingHostStillFatal(t *testing.T) {
	e := &Executor{}
	task := &transport.Task{
		ID:      92,
		Payload: transport.TaskPayload{NodeKind: "image_input", ProcessingImage: "minio-client:latest"},
	}
	creds := &transport.Credentials{Harbor: transport.HarborCredentials{Username: "u", Password: "p"}}

	_, err := e.Run(context.Background(), task, creds, Callbacks{})
	if err == nil {
		t.Fatal("expected Run() to fail closed when Harbor creds have no registry host")
	}
	if !strings.Contains(err.Error(), "harbor credentials") {
		t.Fatalf("expected the harbor-credentials guard to fire, got %q", err.Error())
	}
}
