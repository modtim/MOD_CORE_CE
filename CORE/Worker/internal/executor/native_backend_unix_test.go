//go:build !windows

package executor

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// nativeProcessGone reports whether pid is truly dead: either reaped+unlinked
// (ESRCH) or a zombie (state Z in /proc/<pid>/stat). In minimal container test
// environments a killed child whose reaper is gone can linger as a zombie
// with no one to collect it, where kill(pid, 0) still succeeds — so the
// ESRCH-only probe the flake suffered from reads a dead process as alive.
func nativeProcessGone(t *testing.T, pid int) bool {
	t.Helper()
	if err := syscall.Kill(pid, 0); errors.Is(err, syscall.ESRCH) {
		return true
	}
	if stat, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat"); err == nil {
		if idx := strings.LastIndexByte(string(stat), ')'); idx >= 0 && len(stat) > idx+2 && stat[idx+2] == 'Z' {
			return true
		}
	}
	return false
}

func TestNativeCancelKillsWholeGroup(t *testing.T) {
	b, scratch := newTestNativeBackend(t)
	spec := &TaskSpec{
		Env: map[string]string{"MOD_TASK_ID": "task-5"},
		Cmd: []string{"sh", "-c", "sleep 300 & echo $! > child.pid; wait"},
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}

	pidFile := filepath.Join(scratch, "task-5", "child.pid")
	var child int
	for i := 0; i < 100 && child == 0; i++ {
		if data, err := os.ReadFile(pidFile); err == nil {
			child, _ = strconv.Atoi(strings.TrimSpace(string(data)))
		}
		time.Sleep(20 * time.Millisecond)
	}
	if child == 0 {
		t.Fatal("child never started")
	}

	if err := b.Cancel(context.Background(), h); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	st := waitNative(t, b, h)
	if st.Phase != TaskFailed || st.Err != "cancelled" {
		t.Fatalf("expected cancelled failure, got %+v", st)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if nativeProcessGone(t, child) {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("grandchild %d survived Cancel", child)
}

func TestNativeReapsLeftoverChildrenOnExit(t *testing.T) {
	b, scratch := newTestNativeBackend(t)
	spec := &TaskSpec{
		Env: map[string]string{"MOD_TASK_ID": "task-8"},
		Cmd: []string{"sh", "-c", "sleep 300 >/dev/null 2>&1 & echo $! > child.pid"},
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	if st := waitNative(t, b, h); st.Phase != TaskSucceeded {
		t.Fatalf("expected succeeded, got %+v", st)
	}
	data, err := os.ReadFile(filepath.Join(scratch, "task-8", "child.pid"))
	if err != nil {
		t.Fatalf("read pid: %v", err)
	}
	child, _ := strconv.Atoi(strings.TrimSpace(string(data)))
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if nativeProcessGone(t, child) {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("leftover child %d survived task exit", child)
}
