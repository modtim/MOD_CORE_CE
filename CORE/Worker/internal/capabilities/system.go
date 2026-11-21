package capabilities

import (
	"bufio"
	"os"
	"os/exec"
	"strconv"
	"strings"
)

// SystemState carries the host-OS telemetry the API uses to decide whether
// a HardwareProfile's calibration data has gone stale. Reported in every
// heartbeat. The API compares incoming values against the baseline_*
// snapshot taken at the last calibration completion to detect drift.
//
// AirGapped is a deployment-posture declaration (env var
// WORKER_AIR_GAPPED), not a detection. Air-gapped boxes are exempt from
// the package-delta ratchet because nothing is changing under them
// between calibrations.
type SystemState struct {
	KernelVersion string `json:"kernel_version"`
	OSDistro      string `json:"os_distro"`
	OSVersion     string `json:"os_version"`
	PackageCount  int    `json:"package_count"`
	AirGapped     bool   `json:"air_gapped"`
}

// DetectSystemState reads the host's kernel / OS / package state once at
// agent startup. Called by heartbeat.New(); the result is cached for the
// lifetime of the agent process. Re-detection only happens on agent
// restart — kernel and OS don't change without a reboot, and re-counting
// installed packages every heartbeat would be wasteful.
func DetectSystemState() SystemState {
	return SystemState{
		KernelVersion: detectKernelVersion(),
		OSDistro:      detectOSDistro(),
		OSVersion:     detectOSVersion(),
		PackageCount:  detectPackageCount(),
		AirGapped:     detectAirGapped(),
	}
}

func detectKernelVersion() string {
	out, err := exec.Command("uname", "-r").Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// parseOSRelease reads /etc/os-release into a map. Used to extract both ID
// (distro slug, e.g. "ubuntu", "rhel", "alpine") and VERSION_ID (e.g.
// "22.04", "9", "3.19"). Missing file → empty map; missing keys → "".
func parseOSRelease(path string) map[string]string {
	out := map[string]string{}
	f, err := os.Open(path)
	if err != nil {
		return out
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		idx := strings.Index(line, "=")
		if idx < 0 {
			continue
		}
		key := strings.TrimSpace(line[:idx])
		val := strings.Trim(strings.TrimSpace(line[idx+1:]), `"'`)
		out[key] = val
	}
	return out
}

func detectOSDistro() string {
	rel := parseOSRelease("/etc/os-release")
	return rel["ID"]
}

func detectOSVersion() string {
	rel := parseOSRelease("/etc/os-release")
	return rel["VERSION_ID"]
}

// detectPackageCount tries Debian/Ubuntu first (dpkg-query is the most
// common case), then RHEL/Fedora/Rocky/Alma (rpm), then Alpine (apk),
// then Arch (pacman). Returns 0 when nothing is found — that's a valid
// signal too (source-built minimal containers, NixOS) and the API treats
// a 0 baseline + 0 current as "no drift signal", neutralising the
// ratchet for that worker.
func detectPackageCount() int {
	// dpkg-query -f '.\n' -W prints one dot per installed package, then
	// `wc -l` counts them. Cleaner than `dpkg -l | tail -n +6 | wc -l`
	// because we skip the table-header rows.
	if n := runCount("sh", "-c", "dpkg-query -f '.\n' -W 2>/dev/null | wc -l"); n > 0 {
		return n
	}
	// rpm -qa lists all installed packages, one per line.
	if n := runCount("sh", "-c", "rpm -qa 2>/dev/null | wc -l"); n > 0 {
		return n
	}
	// apk info (Alpine).
	if n := runCount("sh", "-c", "apk info 2>/dev/null | wc -l"); n > 0 {
		return n
	}
	// pacman -Q (Arch).
	if n := runCount("sh", "-c", "pacman -Q 2>/dev/null | wc -l"); n > 0 {
		return n
	}
	return 0
}

func runCount(name string, args ...string) int {
	out, err := exec.Command(name, args...).Output()
	if err != nil {
		return 0
	}
	val, err := strconv.Atoi(strings.TrimSpace(string(out)))
	if err != nil {
		return 0
	}
	return val
}

// detectAirGapped reads WORKER_AIR_GAPPED. Truthy values: "1", "true",
// "yes", "on" (case-insensitive). Anything else, including unset, is
// false. Air-gapped is a *declaration* by the operator — we don't try
// to detect it via probes because a temporarily-firewalled box would
// look air-gapped one minute and not the next, oscillating the
// staleness policy.
func detectAirGapped() bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv("WORKER_AIR_GAPPED")))
	switch v {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}
