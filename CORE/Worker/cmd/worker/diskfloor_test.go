package main

import (
	"bytes"
	"log/slog"
	"strconv"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// MAC.1.s13b (design 4.6) pre-claim free-disk gate tests. The seam var
// diskStatfs (cmd/worker/diskfloor.go) stands in for syscall.Statfs —
// same testability pattern as capabilities' nvidiaSMIRunner — so below/
// above floor and statfs failure are driven without carving a volume.

func fakeFree(gb int64) func(string) (int64, bool) {
    free := gb * diskGiB
    return func(string) (int64, bool) { return free, true }
}

func fakeFail() func(string) (int64, bool) {
    return func(string) (int64, bool) { return 0, false }
}

// warnCount counts the gate's warn lines captured in a discard-free text
// handler sink. The gate's contract is "once per transition", so the
// counts below are the assertions, not just smoke.
func warnCount(buf *bytes.Buffer) int {
    n := 0
    for _, line := range strings.Split(buf.String(), "\n") {
        if strings.Contains(line, "disk-space gate") {
            n += 1
        }
    }
    return n
}

func TestBelowFloor_withholdsClaim(t *testing.T) {
    sink := new(bytes.Buffer)
    log := slog.New(slog.NewTextHandler(sink, &slog.HandlerOptions{Level: slog.LevelWarn}))
    g := &diskFloorGate{}
    diskStatfs = fakeFree(2) // 2 GiB free vs the 10 GiB default floor
    if !g.BelowFloor(log, "/var/lib/mod-worker", nil) {
        t.Error("below floor should withhold the claim")
    }
    if warnCount(sink) != 1 {
        t.Error("first blocked round should log exactly one warn")
    }
    // Still blocked: no repeat spam, one log per transition.
    if !g.BelowFloor(log, "/var/lib/mod-worker", nil) {
        t.Error("still below floor should keep withholding")
    }
    if warnCount(sink) != 1 {
        t.Error("blocked->blocked must not log again")
    }
}

func TestAboveFloor_allowsClaim(t *testing.T) {
    sink := new(bytes.Buffer)
    log := slog.New(slog.NewTextHandler(sink, &slog.HandlerOptions{Level: slog.LevelWarn}))
    g := &diskFloorGate{}
    diskStatfs = fakeFree(500)
    if g.BelowFloor(log, "/var/lib/mod-worker", nil) {
        t.Error("above floor should allow the claim")
    }
    if warnCount(sink) != 0 {
        t.Error("open->open should not log")
    }
    // Drop below, then recover: exactly two transition logs.
    diskStatfs = fakeFree(1)
    if !g.BelowFloor(log, "/var/lib/mod-worker", nil) {
        t.Error("below floor should withhold the claim")
    }
    diskStatfs = fakeFree(500)
    if g.BelowFloor(log, "/var/lib/mod-worker", nil) {
        t.Error("recovered above floor should allow the claim")
    }
    if warnCount(sink) != 2 {
        t.Error("blocked then recovered = exactly two warn logs")
    }
}

func TestStatfsError_failsOpen(t *testing.T) {
    sink := new(bytes.Buffer)
    log := slog.New(slog.NewTextHandler(sink, &slog.HandlerOptions{Level: slog.LevelWarn}))
    g := &diskFloorGate{}
    diskStatfs = fakeFail()
    // Unmeasurable volume: claims proceed (fail open) and log once.
    if g.BelowFloor(log, "/var/lib/mod-worker", nil) {
        t.Error("statfs failure must not block claims")
    }
    if warnCount(sink) != 1 {
        t.Error("unknown-state transition should log exactly one warn")
    }
    // Repeated failures stay quiet.
    g.BelowFloor(log, "/var/lib/mod-worker", nil)
    g.BelowFloor(log, "/var/lib/mod-worker", nil)
    if warnCount(sink) != 1 {
        t.Error("unknown->unknown must not log again")
    }
    // Recovery to a measurable-above state clears state without a log
    // (we were never actually blocking).
    diskStatfs = fakeFree(500)
    if g.BelowFloor(log, "/var/lib/mod-worker", nil) {
        t.Error("measurable above floor should allow claims")
    }
    if warnCount(sink) != 1 {
        t.Error("unknown->above should not add a warn")
    }
}

func TestFloorFromAPI_tunableWins(t *testing.T) {
    sink := new(bytes.Buffer)
    log := slog.New(slog.NewTextHandler(sink, &slog.HandlerOptions{Level: slog.LevelWarn}))
    g := &diskFloorGate{}
    tun := tunables.New()
    tun.Apply(map[string]interface{}{diskFloorTunableKey: 250})
    diskStatfs = fakeFree(100)
    // 100 GiB free clears the built-in 10 GiB default but not the API's
    // 250 GiB floor: the server's opinion must win.
    if !g.BelowFloor(log, "/var/lib/mod-worker", tun) {
        t.Error("API-pushed 250 GiB floor should block at 100 GiB free")
    }
    if warnCount(sink) != 1 {
        t.Error("blocked transition logs one warn naming the needed figure")
    }
}

func TestFloorZero_meansNoOpinion(t *testing.T) {
    sink := new(bytes.Buffer)
    log := slog.New(slog.NewTextHandler(sink, &slog.HandlerOptions{Level: slog.LevelWarn}))
    g := &diskFloorGate{}
    tun := tunables.New()
    tun.Apply(map[string]interface{}{diskFloorTunableKey: 0})
    diskStatfs = fakeFree(0) // a completely full volume
    // 0 from the platform means "no opinion", not "freeze every worker".
    if g.BelowFloor(log, "/var/lib/mod-worker", tun) {
        t.Error("floor 0 must leave the gate open")
    }
    if warnCount(sink) != 0 {
        t.Error("no-opinion gate must not log")
    }
}

func TestDiskFloorGB_default(t *testing.T) {
    if diskFloorGB(nil) != 10 {
        t.Error("nil store falls back to the 10 GiB built-in default")
    }
    tun := tunables.New()
    if diskFloorGB(tun) != 10 {
        t.Error("unset tunable falls back to the built-in default")
    }
    tun.Apply(map[string]interface{}{diskFloorTunableKey: "32"})
    if diskFloorGB(tun) != 32 {
        t.Error("string-pushed tunable parses to 32")
    }
}

// MAC.1.s13b table test: below / exactly-at / above the floor, driven through
// the injected free-space func, and the flapping case asserting the warn log
// fires only on state changes (never once per poll spin).
func TestFloorTable_belowAtAboveAndFlapping(t *testing.T) {
    sink := new(bytes.Buffer)
    log := slog.New(slog.NewTextHandler(sink, &slog.HandlerOptions{Level: slog.LevelWarn}))
    g := &diskFloorGate{}
    // floor = the 10 GiB built-in default; the table drives free-space only.
    table := []struct {
        freeGB int64
        wantBelow bool
    }{
        {2, true},    // below: withhold
        {10, false},  // exactly at the floor: allowed (free < needed is false)
        {11, false},  // just above
        {0, true},    // completely full
        {10, false},  // recovered exactly to the floor
    }
    for _, row := range table {
        diskStatfs = fakeFree(row.freeGB)
        got := g.BelowFloor(log, "/var/lib/mod-worker", nil)
        if got != row.wantBelow {
            t.Error("free="+strconv.FormatInt(row.freeGB, 10)+" GiB vs 10 GiB floor: "+
                "expected below="+strconv.FormatBool(row.wantBelow)+
                " got "+strconv.FormatBool(got))
        }
    }
    // Every table row except 11->0 changes state, so exactly four warn lines.
    if warnCount(sink) != 4 {
        t.Error("table walk should log one warn per state change, expected 4")
    }
    // Flap hard across the floor: logs must stay bounded at one per change.
    for i := 0; i < 20; i += 1 {
        freeGB := int64(500)
        if i%2 == 0 {
            freeGB = 1
        }
        diskStatfs = fakeFree(freeGB)
        g.BelowFloor(log, "/var/lib/mod-worker", nil)
    }
    if warnCount(sink) != 24 {
        t.Error("20 flaps on top of the table's 4 = 24 warn lines")
    }
}
