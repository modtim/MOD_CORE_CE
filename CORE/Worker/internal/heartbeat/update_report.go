// FX.62 p3a1 — the worker's half of the ROLLING upgrade contract.
//
// FX.62 p3a2 (CORE/API/services/worker_update_rolling.py) sequences a campaign
// from exactly two heartbeat signals: `agent_version` (the worker upgraded) and
// `hardware_specs['last_update_error']` (the worker's update attempt FAILED).
// Without the second one a failed worker is indistinguishable from one still
// in flight, so the campaign sits in `requested` until the health timeout and
// then holds with "no heartbeat reporting vX" — a reason nobody can act on.
//
// So this file carries the failure OUT, and carries the clear explicitly:
//
//   never attempted an update cycle  -> key OMITTED ("this worker has nothing
//                                       to say"; the manager keeps whatever it
//                                       has, and a worker that never drained
//                                       is never mistaken for a failed one)
//   an attempt failed                -> last_update_error: "<reason>"
//   an attempt completed cleanly     -> last_update_error: ""  (the EMPTY
//                                       string is a real answer: "the last
//                                       cycle ended, and it did not fail")
//
// That tri-state is the same nil-vs-empty contract the FX.141 host_bound_ports
// fix established — absence and emptiness are different facts and must stay
// different on the wire, because the manager's hold decision reads exactly one
// of them as "failed".
//
// The version half of the contract needs no code here: `agent_version` is the
// stamp baked into the RUNNING binary (cmd/worker's `Version`, set by the
// Dockerfile/Makefile ldflags), and a self-update replaces the binary and then
// EXITS, so the process that reports the new version is the new binary. Stamping
// the target version from inside the old process would advertise a release that
// is not running — the loop the API's `_hb_since_update_req` guard exists to
// catch.
//
// Package heartbeat rather than selfupdate: selfupdate is the download/verify/
// swap ceremony, this is the reporting of its outcome, and the heartbeat owns
// the beat. main.go wires them together where the swap is attempted.

package heartbeat

import (
	"strings"
)

// maxUpdateErrorChars caps the reported reason. The manager truncates at 500
// (worker_update_rolling.py's step.error); capping lower here keeps the beat
// small — the reason is a human-readable sentence, not a stack trace, and a
// long one (a docker daemon's whole stderr) would ride EVERY subsequent beat.
const maxUpdateErrorChars = 300

// updateErr holds the tri-state above. nil pointer = "no cycle attempted"; a
// pointer to "" = "cycle completed, nothing failed"; a pointer to text = the
// failure reason. A pointer (not a plain string) because "" and "never asked"
// must not collapse, and the setter runs in the main loop while the beat is
// sent from its own goroutine.
//
// The field lives in Heartbeat (heartbeat.go); these are its methods.

// ReportUpdateError records that this process's self-update attempt FAILED,
// with the reason. Called by cmd/worker right where selfUpdate returns an
// error, so the reason is the actual ceremony failure (download HTTP, SHA-256
// mismatch, signature refusal, failed --version-exec, atomic-swap EACCES)
// rather than a generic "update failed".
func (h *Heartbeat) ReportUpdateError(reason string) {
	trimmed := strings.TrimSpace(reason)
	if len(trimmed) > maxUpdateErrorChars {
		trimmed = trimmed[:maxUpdateErrorChars]
	}
	h.updateErr.Store(&trimmed)
}

// ClearUpdateError records that a self-update attempt COMPLETED. The clear is
// explicit (empty string, not nil) so the next beat says "the cycle ended and
// did not fail" instead of dropping the key and leaving whatever the manager
// still remembers from the failed attempt before it.
func (h *Heartbeat) ClearUpdateError() {
	h.updateErr.Store(&emptyUpdateReason)
}

// updateErrorReport is the value to put on the beat, or nil when the key must
// be omitted (no update cycle has been attempted on this process).
func (h *Heartbeat) updateErrorReport() *string {
	return h.updateErr.Load()
}

var emptyUpdateReason = ""
