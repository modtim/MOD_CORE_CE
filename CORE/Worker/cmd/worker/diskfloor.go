package main

import (
	"log/slog"
	"os"
	"sync/atomic"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// MAC.1.s13b (launch-completion-megaplan design 4.6): pre-claim free-disk
// gate. Before the worker claims ordinary task work it checks the free
// space on its working-set / scratch volume; below the floor it withholds
// claims for the poll round so it never starts a job that is guaranteed to
// fail on ENOSOS-ish disk pressure. The gate is a no-op on hosts where
// statfs is unavailable (fail open) and on a floor of <= 0 ("no opinion").

const (
	// diskFloorDefaultGB is the built-in floor until the API delivers one.
	// It should become (and is read as, when present) a platform-settings
	// registry entry — worker.claim_min_free_gb — pushed live on the heartbeat
	// `tunables` map (GS.PSR.7); there is deliberately no env var for it,
	// per the settings-not-env rule.
	diskFloorDefaultGB = 10
	diskFloorTunableKey = "worker.claim_min_free_gb"
	diskGiB = int64(1024) * 1024 * 1024
)

// diskStatfs returns free bytes on the filesystem holding path, ok=false
// when statfs is unavailable/fails. Seam var — same testability pattern as
// capabilities' nvidiaSMIRunner — unit tests swap it for a fake so the
// gate can be driven without carving up a real volume.
// The probe itself is per-OS (syscall.Statfs only exists on linux/darwin, so
// it lives in diskfloor_unix.go; diskfloor_windows.go reads GetDiskFreeSpaceEx
// there) — this seam stays mockable for the tests either way.
var diskStatfs = func(path string) (int64, bool) { return diskStatfsOS(path) }

// diskWorkRoot is the worker's scratch/working-set root — the same
// preference order initNativeBackend/initWslcSession and the self-cleanup
// pass use (executor.cleanupWorkRoot is package-private, so this mirrors
// it rather than reaching across packages). The gate measures the volume
// that actually holds per-task scratch, which is what ENOSPACE on a job
// would come from.
func diskWorkRoot() string {
    const root = "/var/lib/mod-worker"
    if _, err := os.Stat(root); err == nil {
        return root
    }
    return os.TempDir()
}

// diskFloorGB resolves the floor from the server-pushed tunables, falling
// back to diskFloorDefaultGB. 0 or negative means "no opinion" — the gate
// stays open (the API sending 0 must not freeze every worker).
func diskFloorGB(tun *tunables.Store) int64 {
	if tun == nil {
		return int64(diskFloorDefaultGB)
	}
	return int64(tun.Int(diskFloorTunableKey, diskFloorDefaultGB))
}

// diskFloorGate tracks the check outcome so the warn log fires once per
// transition (open -> blocked, blocked -> open, anything -> unknown), not
// once per poll spin. state: 0 = above floor, 1 = below floor,
// 2 = statfs unknown (fail open).
type diskFloorGate struct {
	state atomic.Int32
	// The last reading, kept so the heartbeat can explain WHY a green worker
	// is withholding claims (design 4.6): free bytes measured, the floor
	// actually applied, and whether a check has run at all (checked) — an
	// ungated first beat must not read "above floor" before the first probe.
	free    atomic.Int64
	floorGB atomic.Int64
	checked atomic.Bool
}

// BelowFloor performs one pre-claim check against workRoot and reports
// whether claiming must be withheld this round. Logs at warn level with
// the free and needed figures, but only when the state actually changes.
// A statfs error fails open: claims proceed, and the transition log says
// so once.
func (g *diskFloorGate) BelowFloor(log *slog.Logger, workRoot string,
	tun *tunables.Store) bool {
	floorGB := diskFloorGB(tun)
	g.floorGB.Store(floorGB)
	if floorGB <= 0 {
		// No opinion from the platform: gate open, keep state coherent.
		g.state.Store(0)
		return false
	}
	free, ok := diskStatfs(workRoot)
	if !ok {
		// Fail open — a box we cannot measure still runs work.
		g.checked.Store(true)
		if g.state.Swap(2) != 2 {
			log.Warn("disk-space gate: free space unknown (statfs failed on work root); "+
				"proceeding fail-open", "work_root", workRoot,
			"needed_gb", floorGB)
		}
		return false
	}
	needed := floorGB * diskGiB
	g.free.Store(free)
	g.checked.Store(true)
	if free < needed {
		if g.state.Swap(1) != 1 {
			log.Warn("disk-space gate: below floor — withholding claims this poll round",
				"work_root", workRoot, "free_gb", float64(free)/float64(diskGiB),
			"needed_gb", floorGB)
		}
		return true
	}
	if g.state.Swap(0) == 1 {
		log.Warn("disk-space gate: back above floor — resuming claims",
			"work_root", workRoot, "free_gb", float64(free)/float64(diskGiB),
		"needed_gb", floorGB)
	}
	return false
}

// Report shapes the gate's last reading for the heartbeat `disk_floor` key so
// key so the manager/Dashboard can see why a worker is not claiming:
// {state: "above_floor"|"below_floor"|"unknown", free_gb, needed_gb, work_root}.
// Returns nil until the first check has run — same omit-don't-fake contract as
// the other heartbeat providers: an absent key means "this binary has not
// probed yet", never "the volume is fine".
func (g *diskFloorGate) Report(workRoot string) map[string]interface{} {
	if !g.checked.Load() {
		return nil
	}
	floorGB := g.floorGB.Load()
	return map[string]interface{}{
		"state":     stateName[g.state.Load()],
		"free_gb":   float64(g.free.Load())/float64(diskGiB),
		"needed_gb": floorGB,
		"work_root": workRoot,
	}
}

// stateName maps the gate state to its wire spelling.
var stateName = map[int32]string{0: "above_floor", 1: "below_floor", 2: "unknown"}
