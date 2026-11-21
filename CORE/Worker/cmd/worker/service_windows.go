//go:build windows

package main

import (
	"context"
	"time"

	"golang.org/x/sys/windows/svc"
)

// windowsServiceName must match the name worker_install.ps1 passes to
// `sc.exe create` ($ServiceName = "MODWorker").
const windowsServiceName = "MODWorker"

// windowsServiceStopTimeoutForTest bounds how long the SCM handler waits for
// the job loop to actually exit after cancelling ctx before reporting
// Stopped anyway. The SCM itself also has its own timeout (commonly ~30s
// before it treats the service as hung), so this must not exceed that. A
// var (not a const) so tests can shrink it instead of sleeping 30s.
var windowsServiceStopTimeoutForTest = 30 * time.Second

// runMaybeAsService registers this process with the Windows Service Control
// Manager when it was launched as a service (worker_install.ps1's
// `sc.exe create MODWorker binPath= ...`), and is a no-op when launched from
// an interactive console (e.g. running mod-worker.exe by hand for debugging)
// — svc.IsWindowsService() tells them apart.
//
// Before this existed, the binary never called StartServiceCtrlDispatcher at
// all, so the real SCM would time out waiting for a status report and the
// service would fail to start with Error 1053, and Stop-Service had nothing
// to talk to and would just leave the process running (or the SCM would
// kill it outright, skipping the graceful-shutdown path entirely).
//
// stop is the SAME context.CancelFunc the SIGTERM/SIGINT path
// (signal.NotifyContext) uses in main() — the SCM Stop/Shutdown handler
// calls it too, so both shutdown triggers converge on identical downstream
// logic (drain, wg.Wait(), exit). done must be closed by the caller once the
// job loop has actually returned, so the handler can report Stopped for real
// instead of guessing.
func runMaybeAsService(_ context.Context, stop context.CancelFunc, done <-chan struct{}) {
	isService, err := svc.IsWindowsService()
	if err != nil || !isService {
		return
	}
	go func() {
		// svc.Run blocks for the lifetime of the service, dispatching SCM
		// control requests to Execute below. It returns once Execute
		// returns (on Stop/Shutdown). Errors here can't be surfaced
		// meaningfully — nothing is listening on stdout/stderr for a
		// service process anyway.
		_ = svc.Run(windowsServiceName, &modWorkerSvcHandler{stop: stop, done: done})
	}()
}

// modWorkerSvcHandler implements golang.org/x/sys/windows/svc.Handler.
type modWorkerSvcHandler struct {
	stop context.CancelFunc
	done <-chan struct{}
}

// Execute is invoked by the SCM dispatcher (via svc.Run) on its own
// goroutine. It must report StartPending -> Running promptly (this is the
// step that was missing entirely before — the cause of Error 1053), then
// service Stop/Shutdown requests by cancelling the shared root context and
// waiting (bounded) for the job loop to finish before reporting Stopped.
func (h *modWorkerSvcHandler) Execute(_ []string, requests <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	const accepted = svc.AcceptStop | svc.AcceptShutdown

	status <- svc.Status{State: svc.StartPending}
	status <- svc.Status{State: svc.Running, Accepts: accepted}

	for {
		select {
		case <-h.done:
			// The job loop exited on its own (e.g. a self-update restart
			// path, or the fetch-loop watchdog's os.Exit(1) — though that
			// one never reaches here since os.Exit skips all deferred
			// close(runDone) calls; SCM instead just sees the process die,
			// which sc.exe failure/failureflag already handles). Report a
			// clean stop either way.
			status <- svc.Status{State: svc.Stopped}
			return false, 0

		case req := <-requests:
			switch req.Cmd {
			case svc.Interrogate:
				status <- req.CurrentStatus

			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending}
				h.stop()
				select {
				case <-h.done:
				case <-time.After(windowsServiceStopTimeoutForTest):
				}
				status <- svc.Status{State: svc.Stopped}
				return false, 0
			}
		}
	}
}
