//go:build windows

package main

import (
	"fmt"
	"os"
)

// RouteOpLog routes a single operational (non-tenant) worker log line to the
// Windows native-logging destination for a service-managed worker
// (GSEC sz-native-apps-workers 5.6).
//
// Contract: callers must NEVER pass tenant task stdout/stderr content here
// — only the worker's own operational log lines (startup, heartbeat,
// scheduling, errors about the worker itself). Task output is scrubbed and
// shipped separately via the task-log outbox (see
// internal/executor.NativeBackend.Logs), not through this path.
//
// TODO(native-macos-worker 5.4): wire this to the Windows Event Log via
// golang.org/x/sys/windows/svc/eventlog (RegisterEventSource + Info/Error)
// once that dependency is added to go.mod/go.sum. Until then, operational
// lines are written to stderr, which the worker's Windows service wrapper
// already redirects to its configured log file.
func RouteOpLog(line string) {
	fmt.Fprintln(os.Stderr, line)
}
