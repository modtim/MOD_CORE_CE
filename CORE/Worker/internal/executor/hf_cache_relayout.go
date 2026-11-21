package executor

// GSEC R-159 — one-time per-tenant HF weight-cache re-layout, executed
// natively on the worker host (no container). The API (the owner of record
// for who fetched which repo) sends per-repo-dir move decisions; this code
// only MOVES directories (rename within the same filesystem) and reports
// counts. It never deletes anything (ISO: no hard deletes) and never mixes
// tenants: every destination is derived from the plan, and a dir the plan
// does not cover is left exactly where it is.

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// HFCacheRelayoutResult is the terminal tally of one re-layout run.
type HFCacheRelayoutResult struct {
	Moved       int // repo dirs renamed out of the flat mod-models root
	Verified    int // moved dirs re-checked present at the destination
	Quarantined int // moved dirs whose destination was quarantine/
}

// RunHFCacheRelayout performs the moves described by spec. Root is the flat
// HF cache root on this host; every plan's Dir is the on-disk dir name under
// <Root>/mod-models (the sanitized org__name the fetcher wrote), and Dest is
// a path relative to Root ("public", "tenants/<id>", or "quarantine").
func RunHFCacheRelayout(ctx context.Context, spec *transport.HFCacheRelayoutSpec) (HFCacheRelayoutResult, error) {
	var result HFCacheRelayoutResult
	if spec == nil || spec.Root == "" {
		return result, fmt.Errorf("hf_cache_relayout: spec or root missing")
	}
	srcRoot := filepath.Join(spec.Root, "mod-models")
	for key, plan := range spec.Plans {
		if ctx.Err() != nil {
			return result, ctx.Err()
		}
		// The map key is the sanitized on-disk dir (org__name) — the same
		// value as plan.Dir; the API keys the plan map by it so both sides
		// agree on the directory to move. plan.Dir is authoritative.
		dir := filepath.Clean(plan.Dir)
		if dir == "" || dir == "." || dir == ".." ||
			strings.ContainsAny(dir, "\x00") || strings.Contains(dir, string(filepath.Separator)) {
			return result, fmt.Errorf("hf_cache_relayout: plan for %q has invalid dir %q", key, plan.Dir)
		}
		dest, ok := hfcacheDestPath(spec.Root, dir, plan.Dest)
		if !ok {
			// Unknown destination shape: fail loudly rather than guess
			// (the task is pinned to one worker; a visible failure beats a
			// wrong move — the flip gate stays closed until it is re-run).
			return result, fmt.Errorf("hf_cache_relayout: plan for %q has invalid dest %q", key, plan.Dest)
		}
		src := filepath.Join(srcRoot, dir)
		if _, err := os.Lstat(src); err != nil {
			if os.IsNotExist(err) {
				// Already moved by an earlier (idempotent) run — nothing to do.
				continue
			}
			return result, fmt.Errorf("hf_cache_relayout: stat %s: %w", src, err)
		}
		if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
			return result, fmt.Errorf("hf_cache_relayout: mkdir for %s: %w", dest, err)
		}
		if _, err := os.Lstat(dest); err == nil {
			// Both exist: a real collision. Never overwrite, never delete —
			// leave the source dir in place and fail so the operator sees it.
			return result, fmt.Errorf(
				"hf_cache_relayout: destination %s already exists; refusing to overwrite (re-run moves only what still sits in the flat root)", dest)
		}
		if err := os.Rename(src, dest); err != nil {
			return result, fmt.Errorf("hf_cache_relayout: move %s -> %s: %w", src, dest, err)
		}
		if _, err := os.Stat(dest); err != nil {
			return result, fmt.Errorf("hf_cache_relayout: verify %s after move: %w", dest, err)
		}
		result.Moved++
		result.Verified++
		if strings.HasPrefix(plan.Dest, "quarantine") {
			result.Quarantined++
		}
	}
	return result, nil
}

// hfcacheDestPath resolves a plan's destination to an absolute path and
// rejects anything that is not one of the three allowed subtrees under the
// cache root (public, tenants/<id>, quarantine) — a guard against a
// malformed plan moving weights anywhere on the host.
func hfcacheDestPath(root, dir, dest string) (string, bool) {
	if dir == "" || dest == "" {
		return "", false
	}
	if !filepath.IsAbs(root) {
		return "", false
	}
	cleanDir := filepath.Clean(dir)
	cleanDest := filepath.Clean(dest)
	if cleanDir == ".." || strings.HasPrefix(cleanDir, ".."+string(filepath.Separator)) {
		return "", false
	}
	parts := strings.Split(cleanDest, string(filepath.Separator))
	if len(parts) == 0 || strings.ContainsAny(strings.Join(parts, ""), "\x00") {
		return "", false
	}
	switch parts[0] {
	case "public", "quarantine":
		if len(parts) != 1 {
			return "", false
		}
	case "tenants":
		if len(parts) != 2 || !isAllDigits(parts[1]) {
			return "", false
		}
	default:
		return "", false
	}
	return filepath.Join(root, cleanDest, cleanDir), true
}

func isAllDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}
