//go:build !linux && !darwin

package main

// sz-mac-worker.3 (launch-completion-megaplan): the everything-else half of the
// preemption controller's swap probe, so the Makefile's windows-amd64 target
// (and any future cross-compile) still links. Windows has no /proc/meminfo and
// no vm_stat; the worker there runs inside a container/WSL layer whose memory
// accounting the controller already gets from the executor's per-container
// readings, so this probe reports "no swap" — the conservative answer that makes
// the controller EVICT rather than pause (it never claims headroom it did not
// measure). Same name and signature as the linux/darwin halves:
// func swapPresent() bool.

func swapPresent() bool {
 return false
}
