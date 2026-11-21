//go:build windows

package main

import (
	"time"

	"golang.org/x/sys/windows/svc"
)

// windowsServiceName must equal the service name the HX9W installer block
// passes to `sc.exe create` (GAP-HX9W-WINDOWS-HOST-EXEC-SERVICE.md step 3:
// $ServiceName = "mod-host-exec"). A mismatch is what makes the SCM reject
// the start request (Error 1053) instead of handing it to this handler.
const windowsServiceName = "mod-host-exec"

// hostExecStopTimeout bounds how long the SCM handler waits for the serve
// loop to actually return after closing the listener before it reports
// Stopped anyway. Mirrors cmd/worker/service_windows.go:21 (the SCM itself
// treats a service as hung after its own ~30s, so this must not exceed
// that). A const: no test shrinks it (no Windows host in this repo's CI).
const hostExecStopTimeout = 30 * time.Second

// runMaybeAsService registers this process with the Windows Service Control
// Manager when it was launched as a service (`sc.exe create mod-host-exec
// binPath= ...`), and is a no-op when launched from an interactive console.
// the IsWindowsService probe tells the two apart.
//
// Without this the binary never called StartServiceCtrlDispatcher at all, so
// the SCM timed out waiting for the start status report and the service
// failed to start with Error 1053.
//
// Fail closed: if the IsWindowsService probe errors, this returns a no-op and
// helper keeps running as a plain console process. That is safe because the
// named-pipe SDDL (platform_windows.go:36 `D:P(A;;GA;;;SY)`) is the only
// trust boundary — the SCM adds no access control of its own, so losing the
// SCM wrapper cannot widen access.
//
// stop closes the listener (serve then returns, so the process exits through
// its normal path — nothing is killed mid-job); done is closed by main() once
// serve has returned, so the handler can report Stopped for real.
//
// Returns a wait func main() calls after serve returns: it blocks until the
// SCM dispatcher has actually unwound (svc.Run returned) or the bounded
// timeout elapsed, so the process does not exit while the SCM is still
// mid-dispatch.
func runMaybeAsService(stop func(), done <-chan struct{}) func() {
	isService, err := svc.IsWindowsService()
	if err != nil || !isService {
		return func() {}
	}
	ran := make(chan struct{})
	go func() {
		// svc.Run blocks for the lifetime of the service, dispatching SCM
		// control requests to Execute below. It returns once Execute returns
		// (on Stop/Shutdown). Errors here can't be surfaced meaningfully —
		// nothing is listening on stdout/stderr for a service process anyway.
		_ = svc.Run(windowsServiceName, &hostExecSvcHandler{stop: stop, done: done})
		close(ran)
	}()
	return func() {
		select {
		case <-ran:
		case <-time.After(hostExecStopTimeout):
		}
	}
}

// hostExecSvcHandler implements golang.org/x/sys/windows/svc.Handler.
type hostExecSvcHandler struct {
	stop func()
	done <-chan struct{}
}

// Execute is invoked by the SCM dispatcher (via svc.Run) on its own
// goroutine. It must report StartPending -> Running promptly (the step that
// was missing entirely before — the cause of Error 1053), then service Stop/
// Shutdown requests by closing the listener and waiting (bounded) for the
// serve loop to return before reporting Stopped. Shape mirrors
// cmd/worker/service_windows.go:67-102 exactly.
func (h *hostExecSvcHandler) Execute(_ []string, requests <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	const accepted = svc.AcceptStop | svc.AcceptShutdown

	status <- svc.Status{State: svc.StartPending}
	status <- svc.Status{State: svc.Running, Accepts: accepted}

	for {
		select {
		// The serve loop exited on its own (listen error, or the helper was
		// handed a terminal request by other means). Report a clean stop.
		case <-h.done:
			status <- svc.Status{State: svc.Stopped}
			return false, 0

		// req := <-requests:
		case req := <-requests:
			switch req.Cmd {
			case svc.Interrogate:
				status <- req.CurrentStatus

			// Stop/Shutdown: close the listener so serve's Accept returns and
			// main() finishes its normal shutdown path, then wait (bounded)
			// for that before reporting Stopped. Never kill mid-job.
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending}
				h.stop()
				select {
				case <-h.done:
				case <-time.After(hostExecStopTimeout):
				}
				status <- svc.Status{State: svc.Stopped}
				return false, 0
			}
		}
	}
}
