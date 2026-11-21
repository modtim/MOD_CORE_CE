// FX.123.s2 — full-disk-encryption (FDE) reporting on the heartbeat.
//
// The manager's disk-encryption view (FX.123.s3, CORE/API) reads the
// heartbeat's `disk_encryption` block:
//
//   {"status": "encrypted"|"unencrypted"|"unknown",
//    "method": "luks"|"bitlocker"|"filevault"|""}
//
// The contract is deliberately tri-state and NEVER a guess: when the
// platform probe fails, times out, returns garbage, or simply cannot see
// the answer (a WSL worker, whose real boot volume is the Windows host's,
// locked or not), the status is "unknown" and the manager carries the
// previous reading forward rather than acting on a wrong one.
//
// This file is the OS-independent half: the re-probe cache, the mockable
// probe-runner seam (same testability pattern as capabilities'
// nvidiaSMIRunner) and the verdict normaliser. The per-OS halves
// (build tags linux/windows/darwin/other) supply diskEncProbeOS; the pure
// parsers live in diskenc_parse.go.
package heartbeat

import (
	"errors"
	"os/exec"
	"sync"
	"time"
)

// DiskEncryption is one probe verdict. Status "unknown" always pairs with
// an empty method: a method without a verdict would let the manager infer
// "encrypted" from a probe that never actually proved anything.
type DiskEncryption struct {
 Status string // "encrypted" | "unencrypted" | "unknown"
 Method string // "luks" | "bitlocker" | "filevault" | ""
}

// diskUnknown is the only value the whole chain falls back to.
func diskUnknown() DiskEncryption {
 return DiskEncryption{Status: "unknown", Method: ""}
}

// diskEncVerdict normalises a (status, method) pair into the wire shape.
// An "encrypted" verdict MUST name its method — reporting encryption
// without naming what proved it is a guess the API would act on. An
// "unencrypted" verdict may carry an empty method (Linux can say "no
// dm-crypt in this filesystem's ancestry" without naming a foreign
// method). Anything else collapses to unknown.
func diskEncVerdict(status, method string) DiskEncryption {
 if status != "encrypted" && status != "unencrypted" {
  return diskUnknown()
 }
 known := method == "luks" || method == "bitlocker" || method == "filevault"
 if status == "encrypted" && !known {
  return diskUnknown()
 }
 if !known && method != "" {
  return diskUnknown()
 }
 return DiskEncryption{Status: status, Method: method}
}

// diskEncReport is the heartbeat's `disk_encryption` block. Both keys are
// ALWAYS present on a binary that carries this code — an older binary
// omits the block and the manager keeps its previous reading, exactly as
// it does for an "unknown" value here.
func diskEncReport() map[string]interface{} {
 v := diskEncCached()
 return map[string]interface{}{"status": v.Status, "method": v.Method}
}

// diskEncReprobeEvery is how long a probe verdict stays cached. Heartbeats
// run every few seconds; lsblk / manage-bde / fdesetup are subprocesses,
// so re-probing on every beat would make the beat expensive and (on
// Windows) storm manage-bde. A var, not a const, so tests can force a
// re-probe without waiting ten minutes.
var diskEncReprobeEvery = 10 * time.Minute

// diskEncProbeTimeout bounds one probe subprocess. A wedged daemon or a
// hung manage-bde must never stall the heartbeat loop: the child is
// killed, the beat reports "unknown" for this round, and the next beat
// retries once the cache expires.
var diskEncProbeTimeout = 5 * time.Second

// diskEncRunner is the mockable probe seam — the same testability pattern
// capabilities' nvidiaSMIRunner uses. Unit tests swap it for a fake that
// replays captured lsblk / manage-bde / fdesetup output (or an error), so
// the whole detection contract is pinned in CI without a real LUKS,
// BitLocker, or FileVault host. It takes the full argv so one seam serves
// every platform.
var diskEncRunner = func(argv []string) (string, error) {
 return runDiskProbe(argv)
}

// diskEncCached returns the verdict for this heartbeat, re-probing at most
// once per diskEncReprobeEvery. Heartbeats run every few seconds and every
// probe is a subprocess (manage-bde even needs privilege), so the verdict
// is computed once and carried: the beat stays cheap and the API sees the
// same value until the box's encryption state could plausibly have changed.
// An "unknown" answer is cached like any other — a box whose probe is
// broken must not be hammered every beat — and retried next window, so a
// transient failure never sticks forever.
func diskEncCached() DiskEncryption {
 diskEncCacheMu.Lock()
 now := time.Now()
 if diskEncCacheHas && now.Sub(diskEncCacheAt) < diskEncReprobeEvery {
  v := diskEncCacheVal
  diskEncCacheMu.Unlock()
  return v
 }
 v := diskEncProbeOS()
 diskEncCacheAt, diskEncCacheVal, diskEncCacheHas = now, v, true
 diskEncCacheMu.Unlock()
 return v
}

var (
 diskEncCacheMu  sync.Mutex
 diskEncCacheAt  time.Time
 diskEncCacheVal DiskEncryption
 diskEncCacheHas bool
)

// runDiskProbe runs argv with a timeout and returns whatever it printed.
// exec.Command has no timeout of its own, so the child is started
// explicitly and a watchdog goroutine kills it once diskEncProbeTimeout
// passes; the kill surfaces as cmd.Wait()'s error, which every caller maps
// to "unknown". A probe must never block the heartbeat loop.
func runDiskProbe(argv []string) (string, error) {
 if len(argv) == 0 {
  return "", errors.New("disk probe: no command")
 }
 cmd := exec.Command(argv[0], argv[1:]...)
 done := make(chan struct{}, 1)
 var out []byte
 var waitErr error
 go func() {
  o, e := cmd.Output()
  out, waitErr = o, e
  done <- struct{}{}
 }()
 timer := time.NewTimer(diskEncProbeTimeout)
 select {
 case <-done:
  timer.Stop()
  if waitErr != nil {
   return "", waitErr
  }
  return string(out), nil
 case <-timer.C:
  if cmd.Process != nil {
   _ = cmd.Process.Kill()
  }
  return "", errors.New("disk probe: timed out")
 }
}
