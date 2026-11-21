package executor

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func newTestNativeBackend(t *testing.T) (*NativeBackend, string) {
	t.Helper()
	scratch, err := os.MkdirTemp("", "native-backend-test-*")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		os.RemoveAll(scratch)
	})
	return NewNativeBackend(scratch), scratch
}

func TestNativeSubmitCreateDirs(t *testing.T) {
	b, _ := newTestNativeBackend(t)
	spec := &TaskSpec{
		Env: map[string]string{"MOD_TASK_ID": "task-1"},
		Cmd: []string{"sh", "-c", "echo hi > \"$1\"/out.txt", "_", "{output_dir}"},
	}
	_, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
}

func TestNativeSubmitArgSubstitution(t *testing.T) {
	b, scratch := newTestNativeBackend(t)
	spec := &TaskSpec{
		Env: map[string]string{
			"MOD_TASK_ID": "task-2",
			"param_foo":   "bar",
		},
		NativeCommand: "sh",
		NativeArgs:    []string{"-c", `printf '%s|%s|%s' "$1" "$2" "$3" > "$2/out.txt"`, "_", "{input_dir}", "{output_dir}", "{param:param_foo}"},
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	if st := waitNative(t, b, h); st.Phase != TaskSucceeded {
		t.Fatalf("expected succeeded, got %+v", st)
	}

	taskDir := filepath.Join(scratch, "task-2")
	data, err := os.ReadFile(filepath.Join(taskDir, "output", "out.txt"))
	if err != nil {
		t.Fatalf("read output: %v", err)
	}
	want := filepath.Join(taskDir, "input") + "|" + filepath.Join(taskDir, "output") + "|bar"
	if string(data) != want {
		t.Errorf("substitution: got %q, want %q", string(data), want)
	}
}

func waitNative(t *testing.T, b *NativeBackend, h *Handle) *TaskStatus {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		st, err := b.Poll(context.Background(), h)
		if err != nil {
			t.Fatalf("Poll: %v", err)
		}
		if st.Phase != TaskRunning {
			return st
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("task %s still running after 5s", h.ID)
	return nil
}

func TestNativeSuccess(t *testing.T) {
	b, scratch := newTestNativeBackend(t)
	spec := &TaskSpec{
		Env: map[string]string{"MOD_TASK_ID": "task-3"},
		Cmd: []string{"sh", "-c", "echo hi > \"$1\"/out.txt", "_", "{output_dir}"},
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for {
		st, err := b.Poll(ctx, h)
		if err != nil {
			t.Fatalf("Poll: %v", err)
		}
		if st.Phase != TaskRunning {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}

	st, err := b.Poll(context.Background(), h)
	if err != nil {
		t.Fatalf("final Poll: %v", err)
	}
	if st.Phase != TaskSucceeded {
		t.Errorf("expected succeeded, got %+v", st)
	}

	// Verify output file exists
	outPath := filepath.Join(scratch, "task-3", "output", "out.txt")
	if _, err := os.Stat(outPath); os.IsNotExist(err) {
		t.Error("output file not created")
	}
}

func TestNativeNonZeroExit(t *testing.T) {
	b, _ := newTestNativeBackend(t)
	spec := &TaskSpec{
		Env: map[string]string{"MOD_TASK_ID": "task-4"},
		Cmd: []string{"sh", "-c", "exit 42"},
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for {
		st, err := b.Poll(ctx, h)
		if err != nil {
			t.Fatalf("Poll: %v", err)
		}
		if st.Phase != TaskRunning {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}

	st, err := b.Poll(context.Background(), h)
	if err != nil {
		t.Fatalf("final Poll: %v", err)
	}
	if st.Phase != TaskFailed {
		t.Errorf("expected failed, got %+v", st)
	}
	if st.ExitCode != 42 {
		t.Errorf("expected exit code 42, got %d", st.ExitCode)
	}
}

func TestNativeLogs(t *testing.T) {
	b, scratch := newTestNativeBackend(t)
	spec := &TaskSpec{
		Env: map[string]string{"MOD_TASK_ID": "task-6"},
		Cmd: []string{"sh", "-c", "echo hello from native"},
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}

	// Wait for completion
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for {
		st, err := b.Poll(ctx, h)
		if err != nil {
			t.Fatalf("Poll: %v", err)
		}
		if st.Phase != TaskRunning {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}

	// Get logs
	rc, err := b.Logs(context.Background(), h)
	if err != nil {
		t.Fatalf("Logs: %v", err)
	}
	defer rc.Close()

	_, err = io.ReadAll(rc)
	if err != nil {
		t.Fatalf("ReadAll: %v", err)
	}

	logPath := filepath.Join(scratch, "task-6", "task.log")
	logData, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatalf("read log file: %v", err)
	}

	// Log should contain our output
	if !strings.Contains(string(logData), "hello from native") {
		t.Errorf("log should contain output: %s", logData)
	}
}

func TestLogScrub(t *testing.T) {
	b, scratch := newTestNativeBackend(t)
	spec := &TaskSpec{
		Env: map[string]string{"MOD_TASK_ID": "task-scrub"},
		Cmd: []string{"sh", "-c", `echo "leaked path: $1/task-scrub/input/secret.bin"; echo "nothing sensitive here"`, "_", scratch},
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for {
		st, err := b.Poll(ctx, h)
		if err != nil {
			t.Fatalf("Poll: %v", err)
		}
		if st.Phase != TaskRunning {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}

	rc, err := b.Logs(context.Background(), h)
	if err != nil {
		t.Fatalf("Logs: %v", err)
	}
	defer rc.Close()

	scrubbed, err := io.ReadAll(rc)
	if err != nil {
		t.Fatalf("ReadAll: %v", err)
	}

	if strings.Contains(string(scrubbed), scratch) {
		t.Errorf("scrubbed log still contains raw scratch root %q: %s", scratch, scrubbed)
	}
	if !strings.Contains(string(scrubbed), scratchRootPlaceholder) {
		t.Errorf("scrubbed log missing placeholder %q: %s", scratchRootPlaceholder, scrubbed)
	}
	if !strings.Contains(string(scrubbed), "nothing sensitive here") {
		t.Errorf("scrubbed log should pass through non-sensitive line unchanged: %s", scrubbed)
	}
}

func TestNativeBadHandle(t *testing.T) {
	b, _ := newTestNativeBackend(t)
	ctx := context.Background()

	// Test Poll with wrong backend
	st, err := b.Poll(ctx, &Handle{Backend: "docker", ID: "abc"})
	if err == nil {
		t.Fatal("Poll with wrong backend should fail")
	}
	if st != nil {
		t.Errorf("Poll should return nil status on error, got %+v", st)
	}

	// Test Logs with wrong backend
	rc, err := b.Logs(ctx, &Handle{Backend: "docker", ID: "abc"})
	if err == nil {
		rc.Close()
		t.Fatal("Logs with wrong backend should fail")
	}

	// Test Cancel with wrong backend
	err = b.Cancel(ctx, &Handle{Backend: "docker", ID: "abc"})
	if err == nil {
		t.Fatal("Cancel with wrong backend should fail")
	}
}

func TestNativeBadSpec(t *testing.T) {
	b, _ := newTestNativeBackend(t)

	// Test nil spec
	_, err := b.Submit(context.Background(), nil)
	if err == nil {
		t.Fatal("Submit with nil spec should fail")
	}

	// Test missing MOD_TASK_ID
	_, err = b.Submit(context.Background(), &TaskSpec{Env: map[string]string{}})
	if err == nil {
		t.Fatal("Submit without MOD_TASK_ID should fail")
	}
}

// TestNativeArgEscaping verifies that special characters in params
// are passed as literal arguments, not interpreted by a shell.
func TestNativeArgEscaping(t *testing.T) {
	b, scratch := newTestNativeBackend(t)

	// A param containing shell syntax must reach the process as one literal
	// argv entry: no shell is involved in launching, so nothing is executed.
	dangerous := "x; touch pwned"
	spec := &TaskSpec{
		Env: map[string]string{
			"MOD_TASK_ID": "task-7",
			"dangerous":   dangerous,
		},
		NativeCommand: "sh",
		NativeArgs:    []string{"-c", `printf '%s' "$1" > out.txt`, "_", "{param:dangerous}"},
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	if st := waitNative(t, b, h); st.Phase != TaskSucceeded {
		t.Fatalf("expected succeeded, got %+v", st)
	}

	taskDir := filepath.Join(scratch, "task-7")
	data, err := os.ReadFile(filepath.Join(taskDir, "out.txt"))
	if err != nil {
		t.Fatalf("read output: %v", err)
	}
	if string(data) != dangerous {
		t.Errorf("expected literal %q, got %q", dangerous, string(data))
	}
	if _, err := os.Stat(filepath.Join(taskDir, "pwned")); err == nil {
		t.Errorf("param was interpreted by a shell")
	}
}

// TestNativeConcurrentRun tests that multiple native backends can run concurrently.
func TestNativeConcurrentRun(t *testing.T) {
	b, _ := newTestNativeBackend(t)

	for i := 0; i < 3; i++ {
		spec := &TaskSpec{
			Env: map[string]string{"MOD_TASK_ID": fmt.Sprintf("concurrent-%d", i)},
			Cmd: []string{"sh", "-c", "echo running"},
		}
		_, err := b.Submit(context.Background(), spec)
		if err != nil {
			t.Fatalf("Submit %d: %v", i, err)
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	for {
		allDone := true
		for i := 0; i < 3; i++ {
			h := &Handle{Backend: "native", ID: fmt.Sprintf("concurrent-%d", i)}
			st, err := b.Poll(ctx, h)
			if err != nil {
				t.Fatalf("Poll %d: %v", i, err)
			}
			if st.Phase == TaskRunning {
				allDone = false
			}
		}
		if allDone {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
}
