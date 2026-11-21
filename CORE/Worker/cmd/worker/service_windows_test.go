//go:build windows

package main

import (
	"context"
	"testing"
	"time"

	"golang.org/x/sys/windows/svc"
)

// TestModWorkerSvcHandlerStop verifies the SCM handler's core contract: on a
// Stop request it reports StopPending, cancels the shared root context
// (the same one the SIGTERM/SIGINT path cancels), waits for the caller's
// done signal, then reports Stopped and returns. This is what was entirely
// missing before GP.6a — the binary never called StartServiceCtrlDispatcher,
// so a real SCM would time out (Error 1053) and Stop-Service had nothing to
// talk to.
func TestModWorkerSvcHandlerStop(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	var cancelled bool
	stop := func() {
		cancelled = true
		cancel()
	}
	done := make(chan struct{})

	h := &modWorkerSvcHandler{stop: stop, done: done}

	requests := make(chan svc.ChangeRequest, 1)
	status := make(chan svc.Status, 8)

	go func() {
		// Simulate the job loop finishing shortly after ctx is cancelled,
		// same as main()'s `defer close(runDone)` after wg.Wait() returns.
		<-ctx.Done()
		time.Sleep(10 * time.Millisecond)
		close(done)
	}()

	go func() {
		requests <- svc.ChangeRequest{Cmd: svc.Stop}
	}()

	exitCode := make(chan uint32, 1)
	go func() {
		_, code := h.Execute(nil, requests, status)
		exitCode <- code
	}()

	var states []svc.State
	deadline := time.After(5 * time.Second)
	for len(states) < 4 {
		select {
		case s := <-status:
			states = append(states, s.State)
		case <-deadline:
			t.Fatalf("timed out waiting for status transitions, got %v", states)
		}
	}

	want := []svc.State{svc.StartPending, svc.Running, svc.StopPending, svc.Stopped}
	if len(states) != len(want) {
		t.Fatalf("states = %v, want %v", states, want)
	}
	for i := range want {
		if states[i] != want[i] {
			t.Fatalf("states = %v, want %v", states, want)
		}
	}
	if !cancelled {
		t.Fatal("Execute did not cancel the shared root context on Stop")
	}
	select {
	case code := <-exitCode:
		if code != 0 {
			t.Fatalf("exit code = %d, want 0", code)
		}
	case <-time.After(time.Second):
		t.Fatal("Execute did not return after reporting Stopped")
	}
}

// TestModWorkerSvcHandlerStopTimesOut verifies the bounded wait: if the job
// loop never signals done, Execute still reports Stopped and returns instead
// of hanging the SCM shutdown forever.
func TestModWorkerSvcHandlerStopTimesOut(t *testing.T) {
	orig := windowsServiceStopTimeoutForTest
	windowsServiceStopTimeoutForTest = 20 * time.Millisecond
	defer func() { windowsServiceStopTimeoutForTest = orig }()

	_, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{}) // never closed

	h := &modWorkerSvcHandler{stop: cancel, done: done}
	requests := make(chan svc.ChangeRequest, 1)
	status := make(chan svc.Status, 8)
	requests <- svc.ChangeRequest{Cmd: svc.Stop}

	doneExec := make(chan struct{})
	go func() {
		h.Execute(nil, requests, status)
		close(doneExec)
	}()

	select {
	case <-doneExec:
	case <-time.After(2 * time.Second):
		t.Fatal("Execute did not return within the bounded stop timeout")
	}
}
