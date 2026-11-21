package executor

import "testing"

// workerIdentity labels sidecars so a shared host's reaper never touches another
// worker's containers. In embedded mode it must equal the fixed embedded name
// the config package pins (config.EmbeddedWorkerName), whatever WORKER_NAME says.
func TestWorkerIdentityEmbeddedModeIsFixedName(t *testing.T) {
	for _, name := range []string{"mod-worker-ce", "mod-worker", "", "other"} {
		t.Setenv("WORKER_MODE", "embedded")
		t.Setenv("WORKER_NAME", name)
		if got := workerIdentity(); got != "mod-worker-embedded" {
			t.Fatalf("embedded workerIdentity(WORKER_NAME=%q) = %q, want mod-worker-embedded", name, got)
		}
	}
}

func TestWorkerIdentityEmbeddedModeCaseInsensitive(t *testing.T) {
	t.Setenv("WORKER_MODE", " Embedded ")
	t.Setenv("WORKER_NAME", "mod-worker-ce")
	if got := workerIdentity(); got != "mod-worker-embedded" {
		t.Fatalf("workerIdentity() = %q, want mod-worker-embedded", got)
	}
}

func TestWorkerIdentityRemoteModeKeepsEnvName(t *testing.T) {
	t.Setenv("WORKER_MODE", "remote")
	t.Setenv("WORKER_NAME", "gpu-box-7")
	if got := workerIdentity(); got != "gpu-box-7" {
		t.Fatalf("workerIdentity() = %q, want gpu-box-7", got)
	}
}

func TestWorkerIdentityUnknownWhenNothingSet(t *testing.T) {
	t.Setenv("WORKER_MODE", "")
	t.Setenv("WORKER_NAME", "")
	if got := workerIdentity(); got != "unknown" {
		t.Fatalf("workerIdentity() = %q, want unknown", got)
	}
}
