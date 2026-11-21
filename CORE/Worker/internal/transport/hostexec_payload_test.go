package transport

import (
	"encoding/json"
	"testing"
)

// TestHostExecFields_Decode_HostExecTask verifies that a host-exec poll
// payload round-trips through json.Unmarshal so every field the executor
// needs is populated (hx.7d, opsx worker-host-exec).
//
// This JSON mirrors the FLAT dict the API puts into ApiTaskQueue.task_payload
// for a host-exec task (worker_host_exec_service.py, task_payload= block) —
// job_id/argv/cwd/env/run_as/timeout_s/mode are top-level keys, not nested
// under a "host_exec" object: the /workers/tasks/poll response forwards
// task_payload verbatim (see TaskPayload.TaskType doc comment).
func TestHostExecFields_Decode_HostExecTask(t *testing.T) {
	const raw = `{
		"processing_image": "modtim/hostexec-runner:latest",
		"minio_bucket": "tenant-1-files",
		"task_type": "host-exec",
		"job_id": 42,
		"argv": ["python", "-m", "mod_core.hostexec", "--check"],
		"cwd": "/opt/mod_core",
		"env": {"MOD_DEBUG": "1", "MOD_TENANT": "tenant-1"},
		"run_as": "moduser",
		"timeout_s": 300,
		"mode": "check"
	}`

	var tp TaskPayload
	if err := json.Unmarshal([]byte(raw), &tp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if tp.TaskType != "host-exec" {
		t.Errorf("task_type = %q, want %q", tp.TaskType, "host-exec")
	}
	if tp.ProcessingImage != "modtim/hostexec-runner:latest" {
		t.Errorf("processing_image = %q", tp.ProcessingImage)
	}

	if tp.HostExecJobID != 42 {
		t.Errorf("job_id = %d, want 42", tp.HostExecJobID)
	}
	if len(tp.HostExecArgv) != 4 || tp.HostExecArgv[0] != "python" {
		t.Errorf("argv = %v", tp.HostExecArgv)
	}
	if tp.HostExecCwd != "/opt/mod_core" {
		t.Errorf("cwd = %q", tp.HostExecCwd)
	}
	if len(tp.HostExecEnv) != 2 || tp.HostExecEnv["MOD_DEBUG"] != "1" {
		t.Errorf("env = %v", tp.HostExecEnv)
	}
	if tp.HostExecRunAs != "moduser" {
		t.Errorf("run_as = %q", tp.HostExecRunAs)
	}
	if tp.HostExecTimeoutS != 300 {
		t.Errorf("timeout_s = %d, want 300", tp.HostExecTimeoutS)
	}
	if tp.HostExecMode != "check" {
		t.Errorf("mode = %q, want %q", tp.HostExecMode, "check")
	}
}

// TestHostExecFields_Decode_NormalTask verifies that a regular
// container-processing task payload leaves every host-exec field zero.
func TestHostExecFields_Decode_NormalTask(t *testing.T) {
	const raw = `{
		"processing_image": "modtim/albedo:latest",
		"source_file_location": "abc123.sha256",
		"file_extension": ".png",
		"minio_bucket": "tenant-1-files",
		"container_env": {"MOD_MODE": "production"}
	}`

	var tp TaskPayload
	if err := json.Unmarshal([]byte(raw), &tp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if tp.HostExecJobID != 0 || tp.HostExecArgv != nil || tp.HostExecCwd != "" ||
		tp.HostExecEnv != nil || tp.HostExecRunAs != "" || tp.HostExecTimeoutS != 0 ||
		tp.HostExecMode != "" {
		t.Errorf("host-exec fields should all be zero/nil, got job_id=%d argv=%v cwd=%q env=%v run_as=%q timeout_s=%d mode=%q",
			tp.HostExecJobID, tp.HostExecArgv, tp.HostExecCwd, tp.HostExecEnv, tp.HostExecRunAs, tp.HostExecTimeoutS, tp.HostExecMode)
	}
	// task_type (an existing field) still decodes normally alongside the
	// new flat fields — no tag collision introduced.
	if tp.TaskType != "" {
		t.Errorf("task_type = %q, want empty", tp.TaskType)
	}
}

// TestHostExecFields_BackwardsCompatible verifies that an older API payload
// (no host-exec keys at all) still unmarshals the remaining fields correctly.
func TestHostExecFields_BackwardsCompatible(t *testing.T) {
	const raw = `{
		"processing_image": "modtim/albedo:latest",
		"source_file_location": "sha.sha256",
		"file_extension": ".jpg",
		"minio_bucket": "tenant-2-files",
		"container_env": {"KEY": "VAL"},
		"gpu_required": true,
		"container_timeout": 120
	}`

	var tp TaskPayload
	if err := json.Unmarshal([]byte(raw), &tp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if tp.ProcessingImage != "modtim/albedo:latest" {
		t.Errorf("processing_image = %q", tp.ProcessingImage)
	}
	if tp.HostExecJobID != 0 || tp.HostExecArgv != nil {
		t.Errorf("host-exec fields should be zero/nil on an old-style payload")
	}
}
