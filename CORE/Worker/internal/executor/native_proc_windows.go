//go:build windows

package executor

import (
	"fmt"
	"os/exec"
	"sync"
	"unsafe"

	"golang.org/x/sys/windows"
)

// nativeHandle tracks a running native process on Windows. The process is
// placed in a Job Object with KILL_ON_JOB_CLOSE, so terminating the job
// reaches every child it spawned (a process group alone does not).
type nativeHandle struct {
	PID int
	mu  sync.Mutex
	job windows.Handle
}

func configureNativeProcess(cmd *exec.Cmd) {
	cmd.SysProcAttr = &windows.SysProcAttr{CreationFlags: windows.CREATE_NEW_PROCESS_GROUP}
}

func attachNativeProcess(cmd *exec.Cmd) (*nativeHandle, error) {
	pid := cmd.Process.Pid
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return nil, fmt.Errorf("creating job object: %w", err)
	}
	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{
		BasicLimitInformation: windows.JOBOBJECT_BASIC_LIMIT_INFORMATION{
			LimitFlags: windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
		},
	}
	if _, err := windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		windows.CloseHandle(job)
		return nil, fmt.Errorf("configuring job object: %w", err)
	}
	proc, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil {
		windows.CloseHandle(job)
		return nil, fmt.Errorf("opening process: %w", err)
	}
	defer windows.CloseHandle(proc)
	if err := windows.AssignProcessToJobObject(job, proc); err != nil {
		windows.CloseHandle(job)
		return nil, fmt.Errorf("assigning process to job: %w", err)
	}
	return &nativeHandle{PID: pid, job: job}, nil
}

// releaseNativeHandle closes the job handle once the task finishes. With
// KILL_ON_JOB_CLOSE this also reaps any children the task left running.
func releaseNativeHandle(h *nativeHandle) {
	if h == nil {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.job != 0 {
		windows.CloseHandle(h.job)
		h.job = 0
	}
}

func killProcessGroup(h *nativeHandle) error {
	if h == nil {
		return fmt.Errorf("invalid native handle")
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.job == 0 {
		return nil
	}
	if err := windows.TerminateJobObject(h.job, 1); err != nil {
		return fmt.Errorf("terminating job object: %w", err)
	}
	return nil
}
