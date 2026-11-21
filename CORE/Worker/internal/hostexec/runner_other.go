//go:build !linux && !windows && !darwin

// Package hostexec: stub for GOOS values other than linux, windows and
// darwin. Those three have their own native DIRECT runner (runner.go,
// runner_windows.go, runner_darwin.go respectively); every remaining GOOS
// gets a Run that always errors, so the package still compiles and links
// for those builds without offering host-exec there.
package hostexec

import (
	"context"
	"fmt"
	"runtime"
	"time"
)

// Request describes a host-exec invocation. Kept in sync with runner.go's
// linux definition field-for-field.
type Request struct {
	Argv    []string
	Cwd     string
	Env     map[string]string
	RunAs   string
	Timeout time.Duration
	Stdin   []byte
}

// Frame is one chunk of a run's stdout or stderr.
type Frame struct {
	Stream string
	Data   []byte
}

// Run always fails on non-linux hosts: host-exec needs the process-group
// and credential-switch syscalls runner.go implements for linux.
func Run(_ context.Context, _ Request, _ func(Frame)) (int, error) {
	return -1, fmt.Errorf("hostexec: not supported on %s", runtime.GOOS)
}
