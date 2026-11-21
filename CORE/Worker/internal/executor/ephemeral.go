package executor

import (
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"

	"github.com/docker/docker/api/types/container"
)

// defaultEphemeralCapMB is the ceiling applied to a node's declared /dev/shm
// and /tmp sizes when the operator has not set MOD_WORKER_MAX_SHM_MB /
// MOD_WORKER_MAX_TMPFS_MB. Both are RAM-backed, so an uncapped declaration
// would let one node metadata file claim the host's memory.
const defaultEphemeralCapMB = int64(8192)

// ephemeralCapMB reads a per-resource cap from env. Unset, unparseable or
// non-positive values fall back to defaultEphemeralCapMB.
func ephemeralCapMB(envName string) int64 {
	if env := strings.TrimSpace(os.Getenv(envName)); env != "" {
		if v, err := strconv.ParseInt(env, 10, 64); err == nil && v > 0 {
			return v
		}
	}
	return defaultEphemeralCapMB
}

// clampEphemeralMB caps requested at the env-configured ceiling, logging when
// the node asked for more than the worker allows.
func clampEphemeralMB(resource, envName string, requested int64) (int64, bool) {
	limit := ephemeralCapMB(envName)
	if requested > limit {
		slog.Default().Warn("node ephemeral resource clamped to worker cap",
			"resource", resource,
			"requested_mb", requested,
			"cap_mb", limit,
			"cap_env", envName,
		)
		return limit, true
	}
	return requested, false
}

// setTmpfsSize rewrites (or appends) the size= option of a tmpfs mount's
// comma-separated option string, preserving every other option.
func setTmpfsSize(opts string, mb int64) string {
	size := fmt.Sprintf("size=%dm", mb)
	var parts []string
	replaced := false
	for _, p := range strings.Split(opts, ",") {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		if strings.HasPrefix(p, "size=") {
			if !replaced {
				parts = append(parts, size)
				replaced = true
			}
			continue
		}
		parts = append(parts, p)
	}
	if !replaced {
		parts = append(parts, size)
	}
	return strings.Join(parts, ",")
}

// ephemeralApplied reports what applyEphemeralResources actually wrote to the
// HostConfig. ShmMB/TmpfsMB are the post-clamp values that were set, 0 when
// that resource was left untouched (zero/negative request, or no /tmp tmpfs
// to rewrite). Clamped is true when either requested value had to be capped
// to the worker ceiling.
type ephemeralApplied struct {
	ShmMB   int64
	TmpfsMB int64
	Clamped bool
}

// applyEphemeralResources applies a node's declared ephemeral memory to its
// container HostConfig. shmMB>0 sets /dev/shm (ShmSize); tmpfsMB>0 rewrites
// the size of an existing /tmp tmpfs mount (it never creates one — /tmp is
// only a tmpfs when sandbox hardening put it there). Zero or negative values
// leave the worker defaults untouched. Each value is capped by
// MOD_WORKER_MAX_SHM_MB / MOD_WORKER_MAX_TMPFS_MB (default 8192). It returns
// the post-clamp values actually applied so the caller can report them.
func applyEphemeralResources(hc *container.HostConfig, shmMB, tmpfsMB int64) ephemeralApplied {
	var applied ephemeralApplied
	if hc == nil {
		return applied
	}
	if shmMB > 0 {
		mb, clamped := clampEphemeralMB("shm", "MOD_WORKER_MAX_SHM_MB", shmMB)
		hc.ShmSize = mb * 1024 * 1024
		applied.ShmMB = mb
		applied.Clamped = clamped
	}
	if tmpfsMB > 0 && hc.Tmpfs != nil {
		if opts, ok := hc.Tmpfs["/tmp"]; ok {
			mb, clamped := clampEphemeralMB("tmpfs", "MOD_WORKER_MAX_TMPFS_MB", tmpfsMB)
			hc.Tmpfs["/tmp"] = setTmpfsSize(opts, mb)
			applied.TmpfsMB = mb
			applied.Clamped = applied.Clamped || clamped
		}
	}
	return applied
}
