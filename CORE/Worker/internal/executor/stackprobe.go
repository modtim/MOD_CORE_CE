package executor

// 30.6.1 c1-J4 (GX-c1 restore guard) — the READ-ONLY emptiness probe behind
// routers/persistent_stacks/persistent_stacks.py:_probe_stack_empty. Restoring an
// EXTERNAL (seed-committed) snapshot onto a stack that already holds data is not a
// restore, it is a merge over someone else's work, so the API asks this worker
// "what is actually in these volumes and this DB right now?" before it dispatches
// a single restore task.
//
// Read-only by construction: the volume mount is `:/data:ro`, the DB query is a
// `SELECT count(*)`, and nothing here stops, starts, pauses or writes anything.
//
// NEVER report 0 on a failure. Every failure path returns an error, which makes
// the task FAIL, which the API turns into 503 stack_probe_unavailable — an
// unknown emptiness is never treated as empty (fail closed). A volume that does
// not exist yet is the ONE case that legitimately counts as 0.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// probeVolumeName is the docker volume-name charset (the same one the daemon
// accepts). A name outside it can smuggle argv/CLI junk.
var probeVolumeName = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`)

// probeTableCountOut is the only shape psql -tAc may answer: a bare integer.
// Anything else is an error, never a 0.
var probeTableCountOut = regexp.MustCompile(`^[0-9]+$`)

// probeVolumeMissing is how `docker volume inspect` reports a volume that
// simply is not there — the contract's "counts as 0". Any other non-zero exit
// (daemon unreachable, bad name) must fail the task, never read as empty.
var probeVolumeMissing = regexp.MustCompile(`(?i)no such volume|does not exist|cannot find`)

// probeServiceName is the compose service-name charset. A name `compose ls`
// prints outside it is never handed to `compose exec`.
var probeServiceName = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`)

// probeUserTablesSQL counts the tables a stack's DB service actually holds,
// outside the engine's own catalog — the SQL the API's op contract names.
const probeUserTablesSQL = "select count(*) from information_schema.tables " +
	"where table_schema not in ('pg_catalog','information_schema') " +
	"and table_type='BASE TABLE'"

// probeVolumeAllowed is the cross-stack isolation gate. The API only ever sends
// the stack's OWN volumes, but the worker never trusts that: every name must
// match the docker charset AND carry this task's compose-project prefix, which is
// exactly how the API builds volume names (routers/persistent_stacks/_shared.py
// _stack_volumes returns f"{project}_{name}"). A name for another stack's volume
// is rejected before any exec runs, so a payload cannot read a foreign volume.
func probeVolumeAllowed(project, volume string) bool {
	if project == "" || volume == "" {
		return false
	}
	if !probeVolumeName.MatchString(volume) {
		return false
	}
	return strings.HasPrefix(volume, project+"_")
}

// countTopLevelEntries turns `ls -1A /data` output into a top-level entry count.
// lost+found is the filesystem's own repair artifact, not stack data, so it never
// makes a volume look non-empty. Blank lines are not entries.
func countTopLevelEntries(out string) int64 {
	var n int64
	for _, line := range strings.Split(out, "\n") {
		name := strings.TrimSpace(line)
		if name == "" || name == "lost+found" {
			continue
		}
		n++
	}
	return n
}

// parseTableCount accepts ONLY a bare integer on stdout (-tA suppresses the
// header/footers). An empty string, psql's own noise, or a locale-formatted
// value are all errors — never a 0.
func parseTableCount(out string) (int64, error) {
	s := strings.TrimSpace(out)
	if !probeTableCountOut.MatchString(s) {
		return 0, fmt.Errorf("unexpected table-count output %q", s)
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("parse table count %q: %w", s, err)
	}
	return n, nil
}

// volumeExists asks the daemon, WITHOUT mounting anything, whether the volume is
// there at all. A missing volume is the contract's "counts as 0"; a daemon error
// is NOT — it must fail the task, so the caller distinguishes them by exit text.
func volumeExists(ctx context.Context, volume string) (bool, error) {
	var out, stderr bytes.Buffer
	cmd := execCommandContext(ctx, "docker", "volume", "inspect", volume)
	cmd.Stdout = &out
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		why := strings.TrimSpace(stderr.String())
		// `docker volume inspect <missing>` says "no such volume" and exits 1;
		// anything else (daemon unreachable, bad name) must not read as empty.
		if probeVolumeMissing.MatchString(why) {
			return false, nil
		}
		return false, fmt.Errorf("volume inspect %q: %v: %s", volume, err, why)
	}
	return true, nil
}

// countVolumeEntries counts a volume's top-level entries with a short-lived
// read-only mount — the same image/approach rehearse uses to inspect a scratch
// volume (stacksnapshot.go:inspectVolume runs `docker run --rm -v <vol>:/data:ro
// alpine …`). Discrete argv only, never `sh -c`, so a volume name can never be
// read as a shell payload.
func countVolumeEntries(ctx context.Context, volume string) (int64, error) {
	// Guard first: `docker run -v <name>:/data` would AUTO-CREATE a missing
	// volume, and leaving a stray empty volume behind on every probe is a
	// side effect this op must not have.
	exists, err := volumeExists(ctx, volume)
	if err != nil {
		return 0, err
	}
	if !exists {
		return 0, nil
	}
	var out, stderr bytes.Buffer
	cmd := execCommandContext(ctx, "docker", "run", "--rm", "-v", volume+":/data:ro",
		"alpine", "ls", "-1A", "/data")
	cmd.Stdout = &out
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return 0, fmt.Errorf("list volume %q: %v: %s", volume, err, strings.TrimSpace(stderr.String()))
	}
	return countTopLevelEntries(out.String()), nil
}

// countStackUserTables asks the stack's DB service for its user-table count.
// The probe payload carries only stack_project (see _dispatch_stack_task), so
// the service is discovered from the compose project and queried INSIDE the
// already-running container with `docker compose exec -T` — the same primitive
// stackreplica.go:replicaExecInService uses for db_replica_promote and the
// "mesh_reup" case in stackrunner.go uses for a service-local op. psql runs in
// the DB service's own container, never on the worker host, and the SQL is one
// discrete argv element (no shell). A service that has no psql fails; the
// first service that answers with an integer wins, and no answer at all is an
// error, never 0.
func (e *Executor) countStackUserTables(ctx context.Context, cb Callbacks,
	p transport.TaskPayload) (int64, error) {
	project := strings.TrimSpace(p.StackProject)
	if project == "" {
		return 0, fmt.Errorf("probe_empty missing stack_project")
	}
	out, err := e.composeCmd(ctx, cb, project, "", nil, "ls")
	if err != nil {
		return 0, fmt.Errorf("probe_empty: cannot list the services of %q: %w (%s)",
			project, err, strings.TrimSpace(out))
	}
	// `compose ls` prints one service name per line; keep only plausible names.
	// A name that is not a safe docker identifier is skipped, never executed.
	var services []string
	for _, line := range strings.Split(out, "\n") {
		name := strings.TrimSpace(line)
		if name == "" || !probeServiceName.MatchString(name) {
			continue
		}
		// A service name is a compose-project-relative name, so the project
		// prefix rule for VOLUMES does not apply here — but the charset does.
		// Skip the project's own name if `ls` ever echoes it.
		if name == project {
			continue
		}
		services = append(services, name)
	}
	if len(services) == 0 {
		return 0, fmt.Errorf("probe_empty: compose project %q exposes no services, "+
			"so the stack's table count is unknown", project)
	}
	var lastErr string
	for _, svc := range services {
		out, err := e.composeCmd(ctx, cb, project, "", nil,
			"exec", "-T", svc, "psql", "-tAc", probeUserTablesSQL)
		if err != nil {
			lastErr = fmt.Sprintf("%s: %v (%s)", svc, err, strings.TrimSpace(out))
			continue
		}
		n, perr := parseTableCount(out)
		if perr != nil {
			lastErr = fmt.Sprintf("%s: %s", svc, perr)
			continue
		}
		return n, nil
	}
	return 0, fmt.Errorf("probe_empty: no service of %q answered the table count (%s)",
		project, lastErr)
}

// probeStackEmpty runs the whole op and returns the result_payload as compact
// JSON: {"volumes":{"<vol>":<int>},"user_tables":<int|null>}. user_tables is
// null when ProbeLogical is false. Any failure — a rejected volume name, a
// daemon error, an unparseable count — is an error, so the task FAILS and the
// API refuses the restore (503), never seeing an empty probe.
func (e *Executor) probeStackEmpty(ctx context.Context, cb Callbacks,
	p transport.TaskPayload) (string, error) {
	// Validate EVERY name before a single exec touches the host.
	for _, v := range p.ProbeVolumes {
		if !probeVolumeAllowed(p.StackProject, v) {
			return "", fmt.Errorf("probe_empty: invalid probe volume %q for project %q",
				v, p.StackProject)
		}
	}
	var counts map[string]int64 = map[string]int64{}
	for _, v := range p.ProbeVolumes {
		n, err := countVolumeEntries(ctx, v)
		if err != nil {
			return "", fmt.Errorf("probe_empty: %w", err)
		}
		counts[v] = n
	}
	var tables any
	if p.ProbeLogical {
		n, err := e.countStackUserTables(ctx, cb, p)
		if err != nil {
			return "", fmt.Errorf("probe_empty: %w", err)
		}
		tables = n
	}
	out, err := json.Marshal(map[string]any{"volumes": counts, "user_tables": tables})
	if err != nil {
		return "", fmt.Errorf("probe_empty: encode result: %w", err)
	}
	return string(out), nil
}
