package executor

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func tsTask(env map[string]string) *transport.Task {
	return &transport.Task{ID: 4242, Payload: transport.TaskPayload{
		ProcessingImage: "harbor.example/modtim/albedo:1",
		ContainerEnv:    env,
		Cmd:             []string{"run"},
		MemoryLimitMB:   2048,
		CPULimit:        1.5,
	}}
}

var tsCreds = &transport.Credentials{
	MinIO: transport.MinIOCredentials{
		Endpoint: "https://s3.example:9000", AccessKey: "AK", SecretKey: "SK", SessionToken: "TOK",
	},
	Harbor: transport.HarborCredentials{Registry: "harbor.example", Username: "u", Password: "p"},
}

func TestTaskSpecFromTaskEnvAndCreds(t *testing.T) {
	spec, err := TaskSpecFromTask(tsTask(map[string]string{"API_URL": "http://api", "FOO": "bar"}), tsCreds,
		TaskSpecOptions{APIURL: "http://worker-api:80"})
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"MOD_TASK_ID":         "4242",
		"API_URL":             "http://worker-api:80",
		"FOO":                 "bar",
		"MINIO_ENDPOINT":      "s3.example:9000",
		"MINIO_ACCESS_KEY":    "AK",
		"MINIO_SECRET_KEY":    "SK",
		"MINIO_SESSION_TOKEN": "TOK",
	}
	for k, v := range want {
		if spec.Env[k] != v {
			t.Errorf("env %s = %q, want %q", k, spec.Env[k], v)
		}
	}
	if _, ok := spec.Env["HARBOR_PASSWORD"]; ok {
		t.Error("harbor creds leaked into a non-platform task")
	}
	if spec.Image != "harbor.example/modtim/albedo:1" || len(spec.Cmd) != 1 {
		t.Errorf("image/cmd = %q %v", spec.Image, spec.Cmd)
	}
	if spec.Resources.MemoryMB != 2048 || spec.Resources.CPUCores != 1.5 {
		t.Errorf("resources = %+v", spec.Resources)
	}
	if spec.Egress != EgressDefaultDeny {
		t.Errorf("egress = %q, want default-deny for a plain user node", spec.Egress)
	}
	if spec.GPU.Count != 0 {
		t.Errorf("gpu = %+v, want none", spec.GPU)
	}
}

func TestTaskSpecFromTaskShmSizeMB(t *testing.T) {
	task := tsTask(nil)
	task.Payload.ShmSizeMB = 1024
	spec, err := TaskSpecFromTask(task, tsCreds, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if spec.Resources.ShmSizeMB != 1024 {
		t.Errorf("ShmSizeMB = %d, want 1024", spec.Resources.ShmSizeMB)
	}
}

func TestTaskSpecFromTaskShmSizeMBZero(t *testing.T) {
	task := tsTask(nil)
	task.Payload.ShmSizeMB = 0
	spec, err := TaskSpecFromTask(task, tsCreds, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if spec.Resources.ShmSizeMB != 0 {
		t.Errorf("ShmSizeMB = %d, want 0", spec.Resources.ShmSizeMB)
	}
}

func TestTaskSpecFromTaskNoSessionTokenWhenEmpty(t *testing.T) {
	c := *tsCreds
	c.MinIO.SessionToken = ""
	spec, err := TaskSpecFromTask(tsTask(nil), &c, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := spec.Env["MINIO_SESSION_TOKEN"]; ok {
		t.Error("empty session token must be omitted")
	}
	if _, ok := spec.Env["API_URL"]; ok {
		t.Error("API_URL must be dropped when the worker has none")
	}
}

func TestTaskSpecFromTaskPlatformGetsHarborAndEgress(t *testing.T) {
	spec, err := TaskSpecFromTask(tsTask(map[string]string{"PLATFORM_TASK": "1"}), tsCreds, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if spec.Env["HARBOR_USERNAME"] != "u" || spec.Env["HARBOR_REGISTRY"] != "harbor.example" {
		t.Errorf("platform harbor env missing: %v", spec.Env)
	}
	if spec.Egress != EgressUserEgress || spec.Env["MOD_EGRESS_ALLOWED"] != "true" {
		t.Errorf("egress = %q env=%q", spec.Egress, spec.Env["MOD_EGRESS_ALLOWED"])
	}
}

func TestTaskSpecFromTaskAllowedEgress(t *testing.T) {
	// GB.EGR.1: the AllowedEgress-only verdict (no NEEDS_INTERNET) narrows to
	// EgressAllowlist and carries the declared hosts, so K8sBackend can build
	// a per-task Cilium FQDN policy instead of opening 0.0.0.0/0.
	task := tsTask(nil)
	task.Payload.AllowedEgress = []string{"api.example.com"}
	spec, err := TaskSpecFromTask(task, tsCreds, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if spec.Egress != EgressAllowlist {
		t.Errorf("egress = %q, want allowlist", spec.Egress)
	}
	if len(spec.EgressHosts) != 1 || spec.EgressHosts[0] != "api.example.com" {
		t.Errorf("EgressHosts = %v, want [api.example.com]", spec.EgressHosts)
	}
}

func TestTaskSpecFromTaskAllowedEgressMutationSafe(t *testing.T) {
	task := tsTask(nil)
	task.Payload.AllowedEgress = []string{"api.example.com"}
	spec, err := TaskSpecFromTask(task, tsCreds, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	spec.EgressHosts[0] = "mutated.example.com"
	if task.Payload.AllowedEgress[0] != "api.example.com" {
		t.Error("TaskSpec.EgressHosts must be a copy, not an alias of payload.AllowedEgress")
	}
}

func TestTaskSpecFromTaskNeedsInternetOnlyIsPlainUserEgress(t *testing.T) {
	// NEEDS_INTERNET (no declared allowlist) is the pre-existing "open
	// egress, no allowlist" verdict — it must NOT become EgressAllowlist.
	task := tsTask(map[string]string{"NEEDS_INTERNET": "1"})
	spec, err := TaskSpecFromTask(task, tsCreds, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if spec.Egress != EgressUserEgress {
		t.Errorf("egress = %q, want user-egress", spec.Egress)
	}
	if len(spec.EgressHosts) != 0 {
		t.Errorf("EgressHosts = %v, want none for a plain NEEDS_INTERNET task", spec.EgressHosts)
	}
}

func TestTaskSpecFromTaskNeedsInternetWinsOverAllowedEgress(t *testing.T) {
	// SelectNetworkMode checks NEEDS_INTERNET before AllowedEgress, so a task
	// declaring both gets the open egress verdict, not the narrowed
	// allowlist one (matches SelectNetworkMode's own priority order).
	task := tsTask(map[string]string{"NEEDS_INTERNET": "1"})
	task.Payload.AllowedEgress = []string{"api.example.com"}
	spec, err := TaskSpecFromTask(task, tsCreds, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if spec.Egress != EgressUserEgress {
		t.Errorf("egress = %q, want user-egress (NEEDS_INTERNET takes priority)", spec.Egress)
	}
	if len(spec.EgressHosts) != 0 {
		t.Errorf("EgressHosts = %v, want none when NEEDS_INTERNET wins", spec.EgressHosts)
	}
}

func TestTaskSpecFromTaskGPU(t *testing.T) {
	task := tsTask(nil)
	task.Payload.GPURequired = true
	spec, _ := TaskSpecFromTask(task, tsCreds, TaskSpecOptions{})
	if spec.GPU.Count != 1 || spec.GPU.MIGProfile != "" {
		t.Errorf("gpu_required -> %+v, want 1 whole GPU", spec.GPU)
	}

	task = tsTask(map[string]string{EnvGPUMIGProfile: "1g.10gb"})
	spec, _ = TaskSpecFromTask(task, tsCreds, TaskSpecOptions{})
	if spec.GPU.Count != 1 || spec.GPU.MIGProfile != "1g.10gb" {
		t.Errorf("MIG -> %+v", spec.GPU)
	}

	task = tsTask(map[string]string{EnvGPUCount: "2"})
	task.Payload.GPURequired = true
	spec, _ = TaskSpecFromTask(task, tsCreds, TaskSpecOptions{})
	if spec.GPU.Count != 2 {
		t.Errorf("MOD_GPU_COUNT=2 -> %+v", spec.GPU)
	}
}

func TestTaskSpecFromTaskImageResolution(t *testing.T) {
	task := tsTask(nil)
	task.Payload.ProcessingImage = ""
	task.Payload.NodeKind = "albedo"
	task.Payload.GPUVariants = map[string]string{"cuda": "harbor.example/modtim/albedo:cuda"}
	spec, err := TaskSpecFromTask(task, tsCreds, TaskSpecOptions{HardwareTags: []string{"CUDA"}})
	if err != nil {
		t.Fatal(err)
	}
	if spec.Image != "harbor.example/modtim/albedo:cuda" {
		t.Errorf("image = %q", spec.Image)
	}

	task.Payload.GPUVariants = nil
	if _, err := TaskSpecFromTask(task, &transport.Credentials{}, TaskSpecOptions{}); err == nil {
		t.Error("expected error: no image and no harbor registry")
	}
}

type fakeStackObs struct{ called bool }

func (f *fakeStackObs) ObserveStacks(context.Context) ([]StackObservation, error) {
	f.called = true
	return nil, errors.New("k8s")
}

func TestK8sStackObserverRouting(t *testing.T) {
	f := &fakeStackObs{}
	UseK8sStackObserver(f)
	defer UseK8sStackObserver(nil)
	e := &Executor{log: discardLogForTaskSpecTest()}
	if _, err := e.ObserveStacks(context.Background()); err == nil || !f.called {
		t.Fatal("ObserveStacks did not route to the k8s observer")
	}
	if got, err := e.FenceStacks(context.Background()); err != nil || got != nil {
		t.Fatalf("FenceStacks under k8s should be a no-op, got %v %v", got, err)
	}
}

func discardLogForTaskSpecTest() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestTaskSpecDefaultIODirEnv(t *testing.T) {
	spec, err := TaskSpecFromTask(tsTask(nil), tsCreds, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"INPUT_DIR":   "/data/input",
		"INPUT_PATH":  "/data/input",
		"OUTPUT_DIR":  "/data/output",
		"OUTPUT_PATH": "/data/output",
	}
	for k, v := range want {
		if spec.Env[k] != v {
			t.Errorf("env %s = %q, want %q", k, spec.Env[k], v)
		}
	}

	spec, err = TaskSpecFromTask(tsTask(map[string]string{"OUTPUT_DIR": "/x"}), tsCreds, TaskSpecOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if spec.Env["OUTPUT_DIR"] != "/x" {
		t.Errorf("OUTPUT_DIR = %q, want server-set /x", spec.Env["OUTPUT_DIR"])
	}
	if spec.Env["INPUT_DIR"] != "/data/input" {
		t.Errorf("INPUT_DIR = %q, want default /data/input", spec.Env["INPUT_DIR"])
	}
}
