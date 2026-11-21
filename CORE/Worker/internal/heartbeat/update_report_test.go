package heartbeat

// FX.62 p3a1 — the heartbeat's `last_update_error` contract, the signal
// services/worker_update_rolling.py (FX.62 p3a2) holds a rolling campaign on.
//
// The tri-state is the whole point, and it is the same nil-vs-empty contract
// the FX.141 host_bound_ports fix established:
//
//   never attempted an update cycle -> key OMITTED. The manager keeps whatever
//                                     it has; a worker that was never asked to
//                                     drain is never mistaken for one whose
//                                     upgrade failed.
//   the cycle FAILED                -> last_update_error: "<reason>" -> the
//                                     campaign marks the step failed and HOLDS
//                                     with that reason instead of waiting out
//                                     the health timeout for "no heartbeat".
//   the cycle COMPLETED             -> last_update_error: "" — a real answer
//                                     ("the cycle ended, nothing failed"), NOT
//                                     the same as "nothing to say".
//
// Reuses newSendTestHeartbeat/captureHeartbeatBody from modelcache_scores_test.

import (
    "context"
    "encoding/json"
    "testing"
)

// TestSend_LastUpdateErrorOmittedBeforeAnyCycle pins the untouched default: a
// worker that has never attempted a self-update sends nothing, so the manager
// cannot read "failed" off a healthy fleet.
func TestSend_LastUpdateErrorOmittedBeforeAnyCycle(t *testing.T) {
    srv, got := captureHeartbeatBody(t)
    defer srv.Close()

    h := newSendTestHeartbeat(srv)
    h.send(context.Background())

    if _, ok := (*got)["last_update_error"]; ok {
        t.Fatal("last_update_error must be OMITTED when no update cycle was attempted")
    }
}

// TestSend_LastUpdateErrorFailureIsSent pins the failure signal: the reason
// selfUpdate returned rides the beat verbatim, which is what turns the
// campaign's step into `failed` and holds the rollout with a readable reason.
func TestSend_LastUpdateErrorFailureIsSent(t *testing.T) {
    srv, got := captureHeartbeatBody(t)
    defer srv.Close()

    h := newSendTestHeartbeat(srv)
    h.ReportUpdateError("download: HTTP 404")
    h.send(context.Background())

    v, ok := (*got)["last_update_error"]
    if !ok {
        t.Fatalf("expected last_update_error in payload, got keys %#v", *got)
    }
    s, ok := v.(string)
    if !ok {
        t.Fatalf("last_update_error not a string: %#v", v)
    }
    if s != "download: HTTP 404" {
        t.Fatalf("last_update_error = %q, want the reason verbatim", s)
    }
}

// TestSend_ClearUpdateErrorSendsEmpty pins the explicit clear: after a cycle
// completes the key is sent as "" rather than dropped, so the manager replaces
// its stale reading of the previous failure instead of carrying it forward.
func TestSend_ClearUpdateErrorSendsEmpty(t *testing.T) {
    srv, got := captureHeartbeatBody(t)
    defer srv.Close()

    // One server, two beats: the capture decodes each beat's body into the map
    // it holds, so the second send replaces the first beat's reading — which is
    // exactly the manager's view (beat N replaces the stored value, it does not
    // add to it).
    h := newSendTestHeartbeat(srv)
    h.ReportUpdateError("release signature check: invalid")
    h.send(context.Background())
    if _, ok := (*got)["last_update_error"]; !ok {
        t.Fatal("the failed cycle must have put the key on the beat")
    }

    h.ClearUpdateError()
    h.send(context.Background())

    v, ok := (*got)["last_update_error"]
    if !ok {
        t.Fatalf("a completed cycle must SEND last_update_error, not omit it; keys %#v", *got)
    }
    s, ok := v.(string)
    if !ok {
        t.Fatalf("last_update_error not a string: %#v", v)
    }
    b, err := json.Marshal(s)
    if err != nil {
        t.Fatalf("marshal: %v", err)
    }
    if string(b) != `""` {
        t.Fatalf("the clear must serialise as \"\" (got %s)", b)
    }
}

// TestUpdateErrorTruncated pins the cap: a daemon's whole stderr must not ride
// every subsequent beat.
func TestUpdateErrorTruncated(t *testing.T) {
    srv, got := captureHeartbeatBody(t)
    defer srv.Close()

    h := newSendTestHeartbeat(srv)
    h.ReportUpdateError(repeatX(maxUpdateErrorChars + 500))
    h.send(context.Background())

    v, ok := (*got)["last_update_error"]
    if !ok {
        t.Fatal("expected last_update_error in payload")
    }
    s, ok := v.(string)
    if !ok {
        t.Fatalf("last_update_error not a string: %#v", v)
    }
    if len(s) != maxUpdateErrorChars {
        t.Fatalf("reported reason length %d, want cap %d", len(s), maxUpdateErrorChars)
    }
}

// repeatX builds a long reason without pulling in strings.
func repeatX(n int) string {
    out := ""
    for i := 0; i < n; i++ {
        out += "x"
    }
    return out
}
