package capabilities

import (
	"bytes"
	"context"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"
	"unicode/utf8"
)

// wslc (WSL containers) capability probe — launch-completion-megaplan GP.17c
// (design: the internal design notes).
//
// READ-ONLY by contract: the worker only ever runs `wsl.exe --version` and
// checks PATH for `wslc.exe`. It NEVER runs `wsl --update` or anything else
// that mutates the host — upgrading WSL is always the owner's explicit action
// (the design's live test may run on a tenant desktop that also hosts a
// WSL worker, so any host mutation from the worker is out of the question).
//
// wslc (the WSL container feature) requires WSL >= 2.9.3 plus the built-in
// `wslc.exe` CLI on PATH. This probe feeds the native Windows worker's
// capability profile so the manager can SEE wslc availability before GP.17d
// (WslcBackend) routes any task to it.

const (
	// wslcMinWSLVersion — "The WSL container feature requires WSL version
	// 2.9.3 or higher" (learn.microsoft.com/windows/wsl/wsl-container,
	// verified 2026-09-29 in the GP.17a design spike).
	wslcMinWSLVersion = "2.9.3"

	// wslcProbeTimeout — heartbeat-detection budget for one local version
	// query; a wedged wsl.exe must never block the heartbeat loop.
	wslcProbeTimeout = 5 * time.Second
)

// WslcCapability reports whether this host can run WSL containers (wslc).
type WslcCapability struct {
	Available  bool   `json:"available"`
	WSLVersion string `json:"wsl_version"`
	Reason     string `json:"reason"`
}

// commandRunner executes a prepared command and returns its combined output.
// Injectable so tests never shell out.
type commandRunner func(cmd *exec.Cmd) ([]byte, error)

// pathLookup is exec.LookPath's shape, injected for testability.
type pathLookup func(name string) (string, error)

var (
	wslVersionLineRe = regexp.MustCompile(`WSL\s+version:\s*([0-9][0-9A-Za-z.\-+]*)`)
	wslUTF16LEBOM    = []byte{0xFF, 0xFE}
)

// DetectWslc probes for the WSL container feature, read-only.
//
// goos is the OS to decide for (production passes runtime.GOOS; tests pass
// what they need since runtime.GOOS is fixed per test binary). run and look
// must be non-nil. Returns nil when goos is not "windows" — the profile
// field is omitted (nil) there so non-windows heartbeats are byte-identical
// to pre-GP.17c builds.
func DetectWslc(goos string, run commandRunner, look pathLookup) *WslcCapability {
	if goos != "windows" {
		return nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), wslcProbeTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "wsl.exe", "--version")
	out, err := run(cmd)
	if err != nil {
		return &WslcCapability{Available: false, Reason: "wsl --version failed: " + err.Error()}
	}

	version := parseWSLVersion(decodingWSLOutput(out))
	if version == "" {
		return &WslcCapability{Available: false, Reason: "wsl version not found in output"}
	}

	if cmp, err := compareVersions(version, wslcMinWSLVersion); err != nil || cmp < 0 {
		return &WslcCapability{
			Available:  false,
			WSLVersion: version,
			Reason:     "wsl version " + version + " < " + wslcMinWSLVersion,
		}
	}

	if _, err := look("wslc.exe"); err != nil {
		return &WslcCapability{
			Available:  false,
			WSLVersion: version,
			Reason:     "wslc.exe not found on PATH",
		}
	}

	return &WslcCapability{Available: true, WSLVersion: version, Reason: "ok"}
}

// decodingWSLOutput turns raw `wsl.exe` output into a Go string.
//
// `wsl --version` prints UTF-16LE on many builds (console API codepage) and
// UTF-8 on others, with or without a BOM:
//   - UTF-16LE BOM (FF FE)      → decode the rest as UTF-16LE
//   - UTF-8 BOM (EF BB BF)      → strip the BOM
//   - no BOM but UTF-16-ish (many embedded NULs) → decode as UTF-16LE
//   - otherwise                  → already valid text, use as-is
func decodingWSLOutput(out []byte) string {
	switch {
	case bytes.HasPrefix(out, wslUTF16LEBOM):
		return string(utf16.Decode(leBytesToUTF16(out[2:])))
	case bytes.HasPrefix(out, []byte{0xEF, 0xBB, 0xBF}):
		return string(out[3:])
	case utf8.Valid(out) && !looksLikeUTF16(out):
		return string(out)
	default:
		return string(utf16.Decode(leBytesToUTF16(out)))
	}
}

// looksLikeUTF16 heuristically detects BOM-less UTF-16LE: valid UTF-8 text
// almost never has a run of embedded NULs in the first bytes.
func looksLikeUTF16(b []byte) bool {
	nuls := 0
	for _, c := range b[:min(len(b), 64)] {
		if c == 0 {
			nuls++
		}
	}
	return nuls >= 4
}

func leBytesToUTF16(b []byte) []uint16 {
	if len(b)%2 == 1 {
		b = b[:len(b)-1]
	}
	u := make([]uint16, len(b)/2)
	for i := range u {
		u[i] = uint16(b[2*i]) | uint16(b[2*i+1])<<8
	}
	return u
}

// parseWSLVersion extracts the version from a "WSL version: X.Y.Z" line.
func parseWSLVersion(text string) string {
	m := wslVersionLineRe.FindStringSubmatch(text)
	if m == nil {
		return ""
	}
	return strings.TrimSpace(m[1])
}

// compareVersions compares dotted numeric versions; a missing component
// counts as 0 ("2.9" < "2.9.3"). Any non-numeric component makes the compare
// fail (the caller treats that as "not usable").
func compareVersions(a, b string) (int, error) {
	pa := strings.Split(a, ".")
	pb := strings.Split(b, ".")
	for i := 0; i < len(pa) || i < len(pb); i++ {
		var va, vb int
		var err error
		if i < len(pa) && pa[i] != "" {
			va, err = strconv.Atoi(pa[i])
			if err != nil {
				return 0, err
			}
		}
		if err == nil && i < len(pb) && pb[i] != "" {
			vb, err = strconv.Atoi(pb[i])
			if err != nil {
				return 0, err
			}
		}
		if va < vb {
			return -1, nil
		}
		if va > vb {
			return 1, nil
		}
	}
	return 0, nil
}
