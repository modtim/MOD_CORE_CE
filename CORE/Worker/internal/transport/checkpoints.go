package transport

import (
	"encoding/json"
	"strings"
)

// CheckpointPrefix is what a node prints to report progress. Must match
// CHECKPOINT_LOG_PREFIX in routers/worker_tasks.py.
//
// A printed line beats every alternative: an endpoint the node calls needs
// credentials inside the container and a network path out of it, and breaks
// when the link is down — the normal operating state at the edge. Reading a
// file off the checkpoint volume needs the worker to mount it, which is not
// portable. Printing needs nothing a node does not already have, and every
// wrapper — Blender, COLMAP, a shell script — can print.
const CheckpointPrefix = "MOD_CHECKPOINT "

// ExtractCheckpoints pulls progress reports out of a batch of node output.
//
// Lenient on purpose: a malformed line is skipped, never surfaced as an error.
// This is a reporting convenience and must never be able to fail a task that is
// otherwise running fine.
func ExtractCheckpoints(lines []string) []Checkpoint {
	var out []Checkpoint
	for _, raw := range lines {
		// Found anywhere in the line, because real output is prefixed with
		// timestamps and stream names before it reaches us.
		idx := strings.Index(raw, CheckpointPrefix)
		if idx == -1 {
			continue
		}
		blob := strings.TrimSpace(raw[idx+len(CheckpointPrefix):])
		if blob == "" {
			continue
		}
		var cp Checkpoint
		if err := json.Unmarshal([]byte(blob), &cp); err != nil {
			continue
		}
		out = append(out, cp)
	}
	return out
}
