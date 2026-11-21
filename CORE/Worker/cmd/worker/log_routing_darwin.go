//go:build darwin

package main

import (
	"fmt"
	"os"
)

// RouteOpLog routes a single operational (non-tenant) worker log line to the
// macOS native-logging destination for a launchd-managed worker
// (GSEC sz-native-apps-workers 5.6).
//
// Contract: callers must NEVER pass tenant task stdout/stderr content here
// — only the worker's own operational log lines (startup, heartbeat,
// scheduling, errors about the worker itself). Task output is scrubbed and
// shipped separately via the task-log outbox (see
// internal/executor.NativeBackend.Logs), not through this path.
//
// TODO(native-macos-worker 5.4): wire this to macOS unified logging
// (os_log / os.Logger) via cgo so entries show up under `log show
// --predicate 'subsystem == "com.example.worker"'` and in Console.app.
// Until that cgo integration lands, operational lines are written to
// stderr, which launchd already captures to the configured StandardErrorPath
// in the worker's launchd plist.
func RouteOpLog(line string) {
	fmt.Fprintln(os.Stderr, line)
}
