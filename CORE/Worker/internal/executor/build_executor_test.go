package executor

import (
	"testing"
	"time"
)

func TestBuildKitTranslateBuildxArgv_OK(t *testing.T) {
	step := []string{
		"docker", "buildx", "build",
		"--builder", "default",
		"--provenance=false", "--sbom=false", "--pull=false",
		"-f", "Dockerfile",
		"-t", "mod/my-node:abc123",
		"some/context/dir",
	}
	got, err := translateBuildxArgv(step)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := []string{
		"buildctl-daemonless.sh", "build",
		"--frontend", "dockerfile.v0",
		"--local", "context=/workspace",
		"--local", "dockerfile=/workspace",
		"--opt", "filename=Dockerfile",
		"--output", "type=image,name=mod/my-node:abc123,push=false",
	}
	if len(got) != len(want) {
		t.Fatalf("argv length mismatch: got %v want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("argv[%d] = %q, want %q (full got=%v)", i, got[i], want[i], got)
		}
	}
}

func TestBuildKitTranslateBuildxArgv_LongFlags(t *testing.T) {
	step := []string{
		"docker", "buildx", "build",
		"--file", "sub/Dockerfile.cuda",
		"--tag", "mod/gpu-node:xyz",
		"ctx",
	}
	got, err := translateBuildxArgv(step)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got[len(got)-1] != "type=image,name=mod/gpu-node:xyz,push=false" {
		t.Fatalf("unexpected output opt: %v", got)
	}
	if got[len(got)-3] != "filename=sub/Dockerfile.cuda" {
		t.Fatalf("unexpected filename opt: %v", got)
	}
}

func TestBuildKitTranslateBuildxArgv_RejectsWrongShape(t *testing.T) {
	cases := [][]string{
		{},
		{"docker", "buildx"},
		{"buildctl", "build"},
		{"docker", "build", "-t", "x"},
	}
	for _, step := range cases {
		if _, err := translateBuildxArgv(step); err == nil {
			t.Fatalf("expected error for argv %v, got nil", step)
		}
	}
}

func TestBuildKitTranslateBuildxArgv_RejectsMissingFlags(t *testing.T) {
	// -t present, -f missing.
	if _, err := translateBuildxArgv([]string{"docker", "buildx", "build", "-t", "x:y", "ctx"}); err == nil {
		t.Fatal("expected error when -f is missing")
	}
	// -f present, -t missing.
	if _, err := translateBuildxArgv([]string{"docker", "buildx", "build", "-f", "Dockerfile", "ctx"}); err == nil {
		t.Fatal("expected error when -t is missing")
	}
	// -t with no value.
	if _, err := translateBuildxArgv([]string{"docker", "buildx", "build", "-f", "Dockerfile", "-t"}); err == nil {
		t.Fatal("expected error when -t has no value")
	}
}

func TestBuildKitResolveTimeout(t *testing.T) {
	if got := resolveTimeout(&BuildTaskSpec{}); got != DefaultBuildTimeout {
		t.Fatalf("zero Timeout: got %v, want default %v", got, DefaultBuildTimeout)
	}
	if got := resolveTimeout(&BuildTaskSpec{Timeout: -1}); got != DefaultBuildTimeout {
		t.Fatalf("negative Timeout: got %v, want default %v", got, DefaultBuildTimeout)
	}
	custom := 5 * time.Minute
	if got := resolveTimeout(&BuildTaskSpec{Timeout: custom}); got != custom {
		t.Fatalf("explicit Timeout: got %v, want %v", got, custom)
	}
}

func TestBuildKitNewContainerConfig_NoEnv(t *testing.T) {
	spec := &BuildTaskSpec{TaskID: "t1", RecipeID: "image_build"}
	cfg := newContainerConfig("moby/buildkit:rootless", []string{"buildctl-daemonless.sh", "build"}, spec)
	if cfg.Env != nil {
		t.Fatalf("expected no Env, got %v", cfg.Env)
	}
	if cfg.Image != "moby/buildkit:rootless" {
		t.Fatalf("unexpected image: %s", cfg.Image)
	}
	if cfg.Labels["mod.task_id"] != "t1" {
		t.Fatalf("unexpected labels: %v", cfg.Labels)
	}
}

func TestBuildKitNewHostConfig_Sandboxed(t *testing.T) {
	spec := &BuildTaskSpec{TaskID: "t1", ContextDir: "/host/ctx"}
	hc := newHostConfig(spec)

	if hc.Privileged {
		t.Fatal("HostConfig must not be privileged")
	}
	if hc.NetworkMode != "none" {
		t.Fatalf("expected NetworkMode none, got %q", hc.NetworkMode)
	}
	if len(hc.CapDrop) != 1 || hc.CapDrop[0] != "ALL" {
		t.Fatalf("expected CapDrop [ALL], got %v", hc.CapDrop)
	}
	if hc.Resources.PidsLimit == nil || *hc.Resources.PidsLimit != DefaultBuildPidsLimit {
		t.Fatalf("expected PidsLimit %d, got %v", DefaultBuildPidsLimit, hc.Resources.PidsLimit)
	}
	if hc.Resources.Memory != DefaultBuildMemoryBytes {
		t.Fatalf("expected Memory %d, got %d", DefaultBuildMemoryBytes, hc.Resources.Memory)
	}
	for _, bind := range hc.Binds {
		if bind == "/var/run/docker.sock:/var/run/docker.sock" {
			t.Fatal("must never mount the host docker socket")
		}
	}
}
