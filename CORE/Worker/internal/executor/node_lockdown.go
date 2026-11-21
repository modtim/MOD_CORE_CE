// GSEC FX.115 p1a — node container lockdown flags (report-first).
//
// Three container-level lockdown knobs, each with its OWN platform setting
// (GS.PSR.7 — the API-pushed heartbeat `tunables` map, see
// CORE/API/services/platform_settings.py + routers/worker_management.py
// _WORKER_TUNABLE_KEYS) and each taking one of three modes:
//
//	off     — this lockdown layer adds nothing.
//	report  — DEFAULT: log one structured line per node container saying
//	          what WOULD change, and change nothing.
//	enforce — actually apply the change to the task's Config/HostConfig.
//
//	worker.node_lockdown_nonroot        -> Config.User = nodeLockdownUID
//	worker.node_lockdown_cap_drop_all   -> HostConfig.CapDrop = ["ALL"]
//	worker.node_lockdown_no_new_privs   -> SecurityOpt += "no-new-privileges"
//
// Cap-drop-ALL and no-new-privileges are already what the executor ships
// today (#85), so under report/enforce they mostly CONFIRM the existing
// posture — that is the point of report-first: the fleet's audit trail
// records the posture per container before anyone flips a behaviour. The
// non-root flag is the genuinely new one and the one most likely to break a
// node, which is why FX.115 p2 (the dry-run job over the system node set)
// exists. This layer never REMOVES a posture the executor already applies —
// "off" means "this layer adds nothing", not "un-harden the fleet".
//
// Exemptions: no node payload field declares "needs root/caps" today, so the
// interim exception surface is the explicit allow-list setting
// worker.node_lockdown_exempt_nodes (comma-separated node kinds). FX.115 p3
// adds the per-node exception list with a recorded reason; when that lands,
// the API will mark such nodes and this function's exempt() grows one more
// arm without touching any caller.
//
// No env vars: the platform-settings registry is the only behavioural
// source (agent rule), so every mode read passes "" for the envVar arg.
package executor

import (
	"context"
	"strings"
	"sync"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// Tunable keys (registry spelling == wire spelling; the API's
// _WORKER_TUNABLE_KEYS maps them identity-wise).
const (
	nonrootKey     = "worker.node_lockdown_nonroot"
	capDropAllKey  = "worker.node_lockdown_cap_drop_all"
	noNewPrivsKey  = "worker.node_lockdown_no_new_privs"
	exemptNodesKey = "worker.node_lockdown_exempt_nodes"
)

// FX.115 p1b knobs: the seccomp profile (Docker's default, or a named MOD
// profile), read-only rootfs + writable scratch tmpfs, and the resource-limit
// floors (pids / memory / cpu). Each mode knob is its OWN platform setting
// taking off|report|enforce (default report), exactly p1a's pattern; the
// _cap keys are the layer's requested values, applied only when they do not
// go BELOW what the payload/executor already set.
const (
	seccompKey         = "worker.node_lockdown_seccomp"
	seccompProfileKey  = "worker.node_lockdown_seccomp_profile"
	rootfsKey          = "worker.node_lockdown_readonly_rootfs"
	limitsPidsKey      = "worker.node_lockdown_limits_pids"
	limitsPidsCapKey   = "worker.node_lockdown_limits_pids_cap"
	limitsMemoryKey    = "worker.node_lockdown_limits_memory"
	limitsMemoryCapKey = "worker.node_lockdown_limits_memory_cap_mb"
	limitsCpuKey       = "worker.node_lockdown_limits_cpu"
	limitsCpuCapKey    = "worker.node_lockdown_limits_cpu_cap_cores"
)

// FX.115 p3 — the DERIVED heartbeat key the API pushes: the live,
// un-expired models.NodeLockdownException rows UNION the legacy
// worker.node_lockdown_exempt_nodes allow-list, comma-separated as `kind`
// (whole node, the legacy shape) or `kind:flag` (ONE flag relaxed). Expiry
// is resolved on the API (an expired row is simply absent), so this side
// never parses a date and the exception's recorded reason stays the API's
// business — the worker only needs "which flags are relaxed for this node".
const exemptListKey = "worker.node_lockdown_exempt_list"

// modNodeProfileName is the ONLY profile name this layer can actually attach:
// the worker's embedded node profile (sandbox.go seccomp-node.json). "docker"
// (the default value of worker.node_lockdown_seccomp_profile) means "leave
// Docker's built-in default in force", which is what an un-seccomp'd container
// already gets — nothing to write, and never a step DOWN from the worker's
// stricter embedded profile.
const modNodeProfileName = "mod-node"

// bytesPerMB / nanoPerCPU are the unit conversions the limit floors are
// written in (the Docker SDK wants memory in bytes and CPU in 1e-9 cores).
const bytesPerMB = int64(1024 * 1024)
const nanoPerCPU = int64(1000 * 1000 * 1000)

// FX.115 p4 — the RUNTIME knob (hosted gVisor). p1a/p1b/p3 shape the
// container's own posture; this one picks WHICH OCI runtime the daemon runs
// it under, which is the only defence against a kernel-side escape. It is a
// HOST capability: runsc must already be registered in the docker daemon, and
// this repo never installs it and never restarts docker. So the rule is
// report-first AND fail-soft — the worker asks the daemon for its runtime
// list, and an absent runsc is a FALLBACK to the default (runc) plus the
// note "runsc_unavailable" in the report, never a failed task.
const (
	gvisorKey        = "worker.node_runtime_gvisor"
	gvisorGPUKey     = "worker.node_runtime_gvisor_gpu"
	runscRuntimeName = "runsc"
)

// runscProbeTTL bounds how long the daemon's runtime list is cached. A busy
// fleet creates containers back-to-back; one `docker info` per container is
// pure overhead, and the list only changes when someone edits
// /etc/docker/daemon.json (which needs a daemon restart, i.e. never from
// here). A FAILED probe is not cached, so a briefly-unreachable daemon
// cannot pin "runsc absent" for the whole shift.
const runscProbeTTL = 10 * time.Minute

// scratchPaths are the writable scratch locations the read-only-rootfs knob
// adds when it enforces, in the SAME order and with the SAME options the
// hardened sandbox (sandbox.go applyHardenedSandbox) uses, so the two layers
// never disagree about what a node container may write to. /dev/shm is NOT
// here: it is ShmSize (host-backed), already sized by the executor and by
// ephemeral.go, and a tmpfs there would replace it.
var scratchPaths = []string{"/tmp", "/var/tmp", "/run", "/root", "/home"}
var scratchTmpfsOpts = map[string]string{
	"/tmp":     "rw,size=256m,mode=1777",
	"/var/tmp": "rw,size=64m,mode=1777",
	"/run":     "rw,size=16m,mode=0755",
	"/root":    "rw,size=32m,mode=0755",
	"/home":    "rw,size=32m,mode=0755",
}

// nodeLockdownUID is the uid node containers drop to when
// worker.node_lockdown_nonroot is enforced. 65534 is the nobody-adjacent
// uid the worker already uses for its egress-proxy sidecar (egress_proxy.go
// egressProxyUser), so no MOD-owned bind mount (all tenant-owned, root-
// owned) suddenly becomes unwritable: the uid is unprivileged but reserved,
// and MOD never chowns tenant data to it.
const nodeLockdownUID = "65534"

// LockdownMode is one of the three modes every knob accepts.
type LockdownMode string // "off" | "report" | "enforce"

// LockdownFlag is one knob's verdict for ONE node container: the mode that
// was in effect, whether the container's current posture LACKS the flag
// (Needed — i.e. what would change), and whether this layer actually wrote
// it (Applied — true only under enforce, and only when Needed).
type LockdownFlag struct {
	Mode            LockdownMode
	Missing         bool // the container's current posture lacks this flag
	Needed, Applied bool // Needed = the flag WOULD change anything
	// (mode != off AND Missing); Applied = this layer actually wrote it.
}

// LockdownPlan is applyNodeLockdown's verdict for one node container.
// Exempt nodes get Needed=false/Applied=false on every flag with the
// reason recorded, so the report line distinguishes "already hardened"
// from "deliberately not hardened".
//
// FX.115 p1b extends the SAME plan with the seccomp profile, the read-only
// rootfs + writable scratch tmpfs, and the resource-limit floors, so one
// structured line per container carries the whole lockdown posture.
type LockdownPlan struct {
	Nonroot, CapDrop, NoPrivs LockdownFlag
	Seccomp, Rootfs           LockdownFlag
	// Pids/Memory/CPU are the resource-limit knobs: "Missing" means the
	// container has NO cap for that resource (unlimited), which is what the
	// layer would add. A cap the payload/executor already chose is never
	// rewritten, tighter or looser, so Missing is false there.
	Pids, Memory, CPU LockdownFlag
	// NodeKind is the plan's subject, copied off the payload so planMap is
	// self-describing (FX.115 p2: the API keys its per-(node kind, flag)
	// verdict table on exactly this value).
	Exempt       bool
	ExemptReason string
	UID          string
	NodeKind     string
	// The p1b requests resolved off the registry, echoed in the report line
	// whether or not they were applied (0 = this layer requests no floor).
	SeccompProfile                 string
	PidsCap, MemoryCapMB, CPUCores int64
	// ScratchTmpfs lists the writable scratch paths the layer would/could put
	// on tmpfs under a read-only rootfs, so the report line names them.
	ScratchTmpfs []string
	// FX.115 p3: the flags the per-node exception table relaxes for THIS node
	// (a whole-node exemption never lands here — exempt() short-circuits it),
	// so the report line names which knobs the exception touched and the API
	// can resolve the recorded reason off the row itself.
	RelaxedFlags []string
	// FX.115 p4: the runtime knob's verdict. Gvisor is the usual mode triple;
	// the extra fields below are the ONLY place this layer can tell the owner
	// WHY it did not switch, since a missing runsc and a GPU node both mean
	// "needed=false" without being "already hardened".
	Gvisor LockdownFlag
	// GvisorRuntime is the runtime name this layer would/could use ("runsc"),
	// GvisorNote names the reason it did not: "gpu_skipped" or
	// "runsc_unavailable" (empty = nothing blocked the switch).
	GvisorRuntime, GvisorNote string
	// GvisorRuntimes lists the runtime names the daemon reported at probe
	// time (empty = the probe failed or never ran), so the report shows the
	// host's actual capability rather than a bare yes/no.
	GvisorRuntimes []string
}

// modes resolves the three knob modes off the tunables store. The store may
// be nil (an executor whose heartbeat has not delivered tunables yet) —
// then every knob is its default, "report": report-first is the shipped
// default, so an untouched fleet logs its lockdown posture without changing
// a single container.
func modes(t *tunables.Store) (
	nonroot, capDrop, noPrivs, seccomp, rootfs, pids, memory, cpu LockdownMode,
) {
	pick := func(key string) LockdownMode {
		if t == nil {
			return "report"
		}
		return LockdownMode(t.String(key, "", "report", "off", "report", "enforce"))
	}
	return pick(nonrootKey), pick(capDropAllKey), pick(noNewPrivsKey),
		pick(seccompKey), pick(rootfsKey),
		pick(limitsPidsKey), pick(limitsMemoryKey), pick(limitsCpuKey)
}

// caps resolves the layer's requested limit VALUES (pids, memory MB, CPU
// cores). 0 = "this layer requests no floor for that resource" — a mode can
// be enforce without a number, in which case the knob reports and writes
// nothing. There is no env fallback (the registry is the only behavioural
// source), and a negative setting is treated as "no floor" rather than an
// uncapping request.
func caps(t *tunables.Store) (pidsCap, memoryCapMB, cpuCores int64) {
	if t == nil {
		return 0, 0, 0
	}
	pick := func(key string) int64 {
		v := int64(t.Int(key, 0))
		if v > 0 {
			return v
		}
		return 0
	}
	return pick(limitsPidsCapKey), pick(limitsMemoryCapKey), pick(limitsCpuCapKey)
}

// seccompProfileName resolves WHICH profile the seccomp knob asks for.
// "docker" (the default) means "Docker's built-in default profile" — the
// worker ships its own embedded node profile (sandbox.go seccomp-node.json),
// so "docker" is a request to step DOWN onto it, which this layer never does
// (it never removes a posture); any other name is a MOD-named profile the
// layer would attach, and an unset/blank store means "docker".
func seccompProfileName(t *tunables.Store) string {
	if t == nil {
		return "docker"
	}
	name := strings.ToLower(strings.TrimSpace(
		t.String(seccompProfileKey, "", "docker")))
	if name == "" {
		return "docker"
	}
	return name
}

// gvisorMode resolves the runtime knob's mode. Its default is "off", NOT
// p1a's "report": the p1 knobs mostly CONFIRM a posture the fleet already
// ships, whereas switching every node container onto a different OCI runtime
// is a decision the owner has to take knowingly, and on a host with no runsc
// installed it is a no-op either way. A nil store (no heartbeat yet) and an
// unrecognized value both land on "off", so an untouched fleet never even
// probes the daemon.
func gvisorMode(t *tunables.Store) LockdownMode {
	if t == nil {
		return "off"
	}
	return LockdownMode(t.String(gvisorKey, "", "off", "off", "report", "enforce"))
}

// gvisorGPUAllowed reads the GPU sub-flag (default false — see the setting's
// description: gVisor's GPU path is nvproxy and most CUDA/ROCm tooling fails
// under it).
func gvisorGPUAllowed(t *tunables.Store) bool {
	if t == nil {
		return false
	}
	return t.Bool(gvisorGPUKey, "", false)
}

// gpuRequested reports whether this node container asks the daemon for GPU
// hardware. The executor attaches GPUs two ways and BOTH must count: NVIDIA
// via DeviceRequests (capabilities [[gpu]] + the nvidia driver), and AMD/
// Intel/NPU via raw --device mounts (/dev/dxg, /dev/dri, /dev/kfd, ...).
// A tailscale sidecar's /dev/net/tun is NOT a GPU (see the path list below),
// and neither is any other bind. This is the simple documented rule the p4
// GPU skip obeys: any GPU request at all means "do not switch to runsc"
// unless the GPU sub-flag says otherwise.
func gpuRequested(hc *container.HostConfig) bool {
	if hc == nil {
		return false
	}
	for _, req := range hc.Resources.DeviceRequests {
		for _, caps := range req.Capabilities {
			for _, c := range caps {
				if strings.EqualFold(strings.TrimSpace(c), "gpu") {
					return true
				}
			}
		}
	}
	for _, d := range hc.Resources.Devices {
		p := strings.ToLower(strings.TrimSpace(d.PathInContainer))
		if p == "/dev/dxg" || p == "/dev/dri" || p == "/dev/kfd" ||
			p == "/dev/hip" || p == "/dev/nvidia0" ||
			strings.HasPrefix(p, "/dev/nvidia") {
			return true
		}
	}
	return false
}

// runscAvailable asks the docker daemon whether the runsc OCI runtime is
// registered, and caches the answer for runscProbeTTL (see the constant).
// Returns (true, names) when runsc IS available, (false, names) when the
// daemon answered and runsc is simply not among its runtimes, and (false,
// names) with an empty names list when the probe itself failed — the caller
// treats all three as "do not switch", and only the report distinguishes
// "absent" from "could not tell". A failed probe is NOT cached, so a
// transiently-unreachable daemon cannot pin a wrong answer for a shift.
// Tests replace this package var with a fake Info() call.
// runscProbeTimeout bounds the daemon `info` call: a blackholed daemon must
// not stall container creation, and a timeout is just "could not tell".
const runscProbeTimeout = 5 * time.Second

// runscProbe is the daemon-info probe, a package var so tests can hand a
// FAKE Info() (no live docker daemon in unit tests, and this task forbids
// running one). Returns (runscPresent, runtimeNames); an errored or timed-out
// call yields (false, empty).
var runscProbe = func(cli *client.Client) (bool, []string) {
	if cli == nil {
		return false, []string{}
	}
	inner, cancel := context.WithTimeout(context.Background(), runscProbeTimeout)
	defer cancel()
	info, err := cli.Info(inner)
	if err != nil {
		return false, []string{}
	}
	var names []string
	for name := range info.Runtimes {
		names = append(names, name)
	}
	// Sorted so the report line is stable across runs (map order is not).
	for i := 1; i < len(names); i++ {
		for j := i; j > 0 && names[j] < names[j-1]; j-- {
			names[j], names[j-1] = names[j-1], names[j]
		}
	}
	for _, n := range names {
		if n == runscRuntimeName {
			return true, names
		}
	}
	return false, names
}

// runscCache memoizes the probe for runscProbeTTL. A probe that ERRORED
// (present=false AND no names at all) is never cached, so a transiently
// unreachable daemon cannot pin "runsc absent" for a whole shift; a daemon
// that answered and simply has no runsc IS cached (that is the stable answer
// until someone edits daemon.json, which needs a daemon restart).
var (
	runscCacheMu      sync.Mutex
	runscCacheValid   bool
	runscCacheAt      time.Time
	runscCachePresent bool
	runscCacheNames   []string
)

func runscAvailable(cli *client.Client) (bool, []string) {
	runscCacheMu.Lock()
	if runscCacheValid && time.Since(runscCacheAt) < runscProbeTTL {
		present, names := runscCachePresent, runscCacheNames
		runscCacheMu.Unlock()
		return present, names
	}
	runscCacheMu.Unlock()
	present, names := runscProbe(cli)
	runscCacheMu.Lock()
	if len(names) > 0 {
		runscCacheValid, runscCacheAt = true, time.Now()
		runscCachePresent, runscCacheNames = present, names
	}
	runscCacheMu.Unlock()
	return present, names
}

// dockerClient is the executor's docker client, handed to the runtime probe.
// A package var (this repo's stubbable-var convention) rather than a new
// parameter on applyNodeLockdown, so the p1a/p1b/p3 call sites and their
// tests stay byte-identical; executor.go sets it once per task right before
// the call, and unit tests stub runscProbe instead of a client.
var dockerClient *client.Client

// setDockerClient hands this layer the executor's docker client for the
// runtime probe. executor.go calls it immediately before applyNodeLockdown;
// leaving it nil (a degenerate caller, or a unit test that never wires one)
// means "cannot probe", which is exactly the fail-soft path: no runtime
// switch, note "runsc_unavailable", the task still runs under runc.
func setDockerClient(cli *client.Client) {
	dockerClient = cli
}

// exempt reports whether this node is exempt from lockdown and why. Today
// the only exception surface is the explicit node-kind allow-list setting
// (worker.node_lockdown_exempt_nodes); matching is case-insensitive on the
// trimmed kind.
//
// FX.115 p3 adds the API-side per-node exception table, delivered as the
// DERIVED exemptListKey: a BARE entry there exempts the WHOLE node exactly
// like the legacy setting (reason "exempt_list_setting"), and a `kind:flag`
// entry relaxes ONE flag, which relaxedFlags handles (see applyNodeLockdown,
// where a relaxed flag keeps Missing but gets Needed/Applied false). The
// legacy setting is checked first and keeps working as a fallback/union for
// one release, so an older API that only sends it is unaffected.
func exempt(t *tunables.Store, p transport.TaskPayload) (bool, string) {
	kind := strings.TrimSpace(p.NodeKind)
	if kind == "" {
		// A node with no kind cannot match either list.
		return false, ""
	}
	if legacyExempt(t, kind) {
		return true, "exempt_nodes_setting"
	}
	whole, _ := exemptEntries(t)
	if whole[strings.ToLower(kind)] {
		return true, "exempt_list_setting"
	}
	return false, ""
}

// legacyExempt matches the node kind against the legacy allow-list setting
// (worker.node_lockdown_exempt_nodes), case-insensitively on the trimmed
// kind. Kept separate from exempt() so the p3 union above reads in order.
func legacyExempt(t *tunables.Store, kind string) bool {
	list := ""
	if t != nil {
		// No env fallback: the registry is the only behavioural source.
		list = t.String(exemptNodesKey, "", "")
	}
	for _, k := range strings.Split(list, ",") {
		if strings.EqualFold(strings.TrimSpace(k), kind) {
			return true
		}
	}
	return false
}

// exemptEntries parses the DERIVED p3 list (exemptListKey) into whole-node
// kinds and per-kind relaxed flags. Entries are comma-separated and each is
// either `kind` (whole node) or `kind:flag` (ONE flag relaxed) — the API
// builds it off the live, un-expired models.NodeLockdownException rows UNION
// the legacy allow-list, so expiry is resolved THERE and this side never
// parses a date. Kind matching is case-insensitive (lower-cased here); an
// entry with no flag after the colon is treated as whole-node, and a blank
// entry is inert.
func exemptEntries(t *tunables.Store) (map[string]bool, map[string]map[string]bool) {
	whole := map[string]bool{}
	per := map[string]map[string]bool{}
	if t == nil {
		return whole, per
	}
	list := t.String(exemptListKey, "", "")
	for _, raw := range strings.Split(list, ",") {
		entry := strings.TrimSpace(raw)
		if entry == "" {
			continue
		}
		name, flag := entry, ""
		i := strings.IndexByte(entry, ':')
		if i >= 0 {
			name = strings.TrimSpace(entry[:i])
			flag = strings.ToLower(strings.TrimSpace(entry[i+1:]))
		}
		name = strings.ToLower(strings.TrimSpace(name))
		if name == "" {
			continue
		}
		if flag == "" {
			whole[name] = true
			continue
		}
		if _, ok := per[name]; !ok {
			per[name] = map[string]bool{}
		}
		per[name][flag] = true
	}
	return whole, per
}

// relaxedFlags returns the flags the p3 exception table relaxes for this
// node kind (lower-cased flag names). A whole-node exemption never reaches
// here (exempt() short-circuits it), so this is the per-flag surface only.
// "scratch_tmpfs" names the writable-scratch set the rootfs knob adds.
func relaxedFlags(t *tunables.Store, kind string) map[string]bool {
	_, per := exemptEntries(t)
	if m, ok := per[strings.ToLower(strings.TrimSpace(kind))]; ok {
		return m
	}
	return map[string]bool{}
}

// hasCapDropAll / hasNoNewPrivs read the posture the executor ALREADY built
// for this container, so "Needed" means "this node container would actually
// change", not "the setting is on".
func hasCapDropAll(hc *container.HostConfig) bool {
	if hc == nil {
		return false
	}
	for _, c := range hc.CapDrop {
		if strings.EqualFold(strings.TrimSpace(c), "ALL") {
			return true
		}
	}
	return false
}

// hasRunscRuntime reads the runtime the executor ALREADY chose, so "Needed"
// means "this node container would actually change" — a payload that already
// asked for runsc (payload.UseSandboxRuntime via sandbox.go SelectRuntime) is
// already hardened and this layer never re-writes or removes a runtime.
func hasRunscRuntime(hc *container.HostConfig) bool {
	if hc == nil {
		return false
	}
	return strings.EqualFold(strings.TrimSpace(hc.Runtime), runscRuntimeName)
}

func hasNoNewPrivs(hc *container.HostConfig) bool {
	if hc == nil {
		return false
	}
	for _, o := range hc.SecurityOpt {
		// "no-new-privileges" and the per-cap form "no-new-privileges:true"
		// both satisfy the flag.
		if o == "no-new-privileges" || strings.HasPrefix(o, "no-new-privileges:") {
			return true
		}
	}
	return false
}

// applyNodeLockdown writes the ENFORCED part of the lockdown onto the task's
// Config/HostConfig and returns the full plan so the caller can report it.
// report mode changes nothing; off changes nothing; enforce writes. An
// exempt node gets no writes under any mode. A nil config/hostConfig (a
// degenerate caller) is left alone and still reported.
func applyNodeLockdown(
	config *container.Config,
	hc *container.HostConfig,
	t *tunables.Store,
	p transport.TaskPayload,
) LockdownPlan {
	nr, cd, np, sc, rf, pd, mm, cc := modes(t)
	// FX.115 p4: the runtime knob is resolved here so the p3 relaxation block
	// below can switch it off for ONE node kind like every other flag.
	gv := gvisorMode(t)
	plan := LockdownPlan{
		Nonroot: LockdownFlag{Mode: nr},
		CapDrop: LockdownFlag{Mode: cd},
		NoPrivs: LockdownFlag{Mode: np},
		Seccomp: LockdownFlag{Mode: sc},
		Rootfs:  LockdownFlag{Mode: rf},
		Pids:    LockdownFlag{Mode: pd},
		Memory:  LockdownFlag{Mode: mm},
		CPU:     LockdownFlag{Mode: cc},
		// FX.115 p4: the runtime knob carries its mode even on an EXEMPT node
		// (the report must distinguish "exempt from gvisor" from "gvisor off").
		Gvisor: LockdownFlag{Mode: gv},
		UID:    nodeLockdownUID,
	}
	plan.SeccompProfile = seccompProfileName(t)
	plan.PidsCap, plan.MemoryCapMB, plan.CPUCores = caps(t)
	// The subject kind rides on the plan so the report (log line AND the
	// task-result map) is keyed by it without the caller re-reading the
	// payload. Trimmed: the exempt() match already trims, and the API groups
	// on the same spelling.
	plan.NodeKind = strings.TrimSpace(p.NodeKind)
	plan.Exempt, plan.ExemptReason = exempt(t, p)
	if plan.Exempt {
		return plan
	}
	// FX.115 p3: per-flag relaxation from the exception table. A relaxed flag
	// keeps its Missing verdict (the posture IS missing — that is what the
	// owner wants to see) but resolves to mode "off" for THAT flag only, so
	// Needed/Applied stay false and nothing is written; the report names the
	// flag as relaxed so the API can quote the exception's reason.
	relaxed := relaxedFlags(t, plan.NodeKind)
	if len(relaxed) > 0 {
		for _, name := range []string{"nonroot", "capdrop", "noprivs", "seccomp",
			"rootfs", "scratch_tmpfs", "limits_pids", "limits_memory", "limits_cpu",
			// FX.115 p4: the runtime knob is relaxable under the SAME p3
			// spelling — an exception row `kind:gvisor` keeps that node on the
			// daemon's default runtime (runc) with the reason recorded.
			"gvisor"} {
			if relaxed[name] {
				plan.RelaxedFlags = append(plan.RelaxedFlags, name)
			}
		}
		// Both the plan's reported mode AND the local mode the writes below
		// read must drop to "off", so the report says "this knob was
		// deliberately not applied" and the write path honours it.
		if relaxed["nonroot"] {
			nr, plan.Nonroot.Mode = "off", "off"
		}
		if relaxed["capdrop"] {
			cd, plan.CapDrop.Mode = "off", "off"
		}
		if relaxed["noprivs"] {
			np, plan.NoPrivs.Mode = "off", "off"
		}
		if relaxed["seccomp"] {
			sc, plan.Seccomp.Mode = "off", "off"
		}
		if relaxed["rootfs"] {
			rf, plan.Rootfs.Mode = "off", "off"
		}
		if relaxed["limits_pids"] {
			pd, plan.Pids.Mode = "off", "off"
		}
		if relaxed["limits_memory"] {
			mm, plan.Memory.Mode = "off", "off"
		}
		if relaxed["limits_cpu"] {
			cc, plan.CPU.Mode = "off", "off"
		}
		// FX.115 p4: the runtime knob relaxes under the same p3 spelling
		// (`kind:gvisor` in the exception list = this node stays on runc).
		if relaxed["gvisor"] {
			gv, plan.Gvisor.Mode = "off", "off"
		}
	}

	// Non-root: only a container that still runs as root (no User set, or an
	// explicit "0"/"root") is a candidate. A container that already names a
	// user is left alone — its own author chose that identity.
	uid := ""
	if config != nil {
		uid = strings.TrimSpace(config.User)
	}
	plan.Nonroot.Missing = uid == "" || uid == "0" || strings.EqualFold(uid, "root")
	plan.Nonroot.Needed = plan.Nonroot.Missing && nr != "off"
	if plan.Nonroot.Needed && nr == "enforce" && config != nil {
		config.User = nodeLockdownUID
		plan.Nonroot.Applied = true
	}

	plan.CapDrop.Missing = !hasCapDropAll(hc)
	plan.CapDrop.Needed = plan.CapDrop.Missing && cd != "off"
	if plan.CapDrop.Needed && cd == "enforce" && hc != nil {
		hc.CapDrop = append(hc.CapDrop, "ALL")
		plan.CapDrop.Applied = true
	}

	plan.NoPrivs.Missing = !hasNoNewPrivs(hc)
	plan.NoPrivs.Needed = plan.NoPrivs.Missing && np != "off"
	if plan.NoPrivs.Needed && np == "enforce" && hc != nil {
		hc.SecurityOpt = append(hc.SecurityOpt, "no-new-privileges")
		plan.NoPrivs.Applied = true
	}
	// --- FX.115 p1b: seccomp profile ---------------------------------------
	// The worker ALREADY attaches its own embedded node profile (sandbox.go
	// seccomp-node.json), which is stricter than Docker's built-in default, so
	// any container carrying a seccomp= option is covered: Missing=false under
	// every mode and this layer never touches it — it never steps a container
	// DOWN onto Docker's default and never removes a posture. A container with
	// NO seccomp= option is already on Docker's default profile, so the "docker"
	// profile name has nothing to write; only a NAMED MOD profile (mod-node =
	// the worker's embedded one) is a real change.
	plan.Seccomp.Missing = !hasSeccompOpt(hc)
	modOpt := modNodeSeccompOpt()
	plan.Seccomp.Needed = plan.Seccomp.Missing && sc != "off" &&
		plan.SeccompProfile == modNodeProfileName && modOpt != ""
	if plan.Seccomp.Needed && sc == "enforce" && hc != nil {
		hc.SecurityOpt = append(hc.SecurityOpt, modOpt)
		plan.Seccomp.Applied = true
	}

	// --- FX.115 p1b: read-only rootfs + writable scratch tmpfs -------------
	// Bind mounts (/data, an optional docker socket) keep their own rw/ro
	// config and ignore ReadonlyRootfs, so the tenant's I/O volume still works;
	// only the image layers become unwritable. The scratch paths below are the
	// ones well-behaved tools still need to write (see sandbox.go applyHardenedSandbox
	// for why /root and /home are in the set). A path that is already a BIND
	// destination is never turned into tmpfs — tmpfs would shadow the bind and
	// hide the tenant's data. Existing tmpfs entries are left alone, so the
	// hardened sandbox's sizes (and the node's own ephemeral sizes) win.
	plan.Rootfs.Missing = hc != nil && !hc.ReadonlyRootfs
	plan.Rootfs.Needed = plan.Rootfs.Missing && rf != "off"
	if plan.Rootfs.Needed {
		// Reported under every non-off mode so report names what enforce WOULD
		// add; only actually written under enforce.
		plan.ScratchTmpfs = scratchTmpfs(hc, rf == "enforce")
	}
	if plan.Rootfs.Needed && rf == "enforce" && hc != nil {
		hc.ReadonlyRootfs = true
		plan.Rootfs.Applied = true
	}

	// --- FX.115 p1b: resource-limit floors (pids / memory / cpu) -----------
	// A floor is only ever written where the container has NO cap (unset or
	// unlimited), never on top of a cap the payload/executor already chose:
	// that cap is the node's own declared budget and rewriting it in either
	// direction would change the node's contract, and going BELOW it would
	// OOM/starve a node that asked for more. So "Missing" here means
	// "unlimited", and an existing cap is reported as already-hardened.
	// GPU nodes are untouched by this block: device passthrough lives in
	// Resources.Devices/DeviceRequests, which this layer never reads or writes.
	applyLimitFloor(&plan.Pids, hc, pd, plan.PidsCap,
		hc == nil || hc.Resources.PidsLimit == nil ||
			*hc.Resources.PidsLimit <= 0,
		func(v int64) { hc.Resources.PidsLimit = &v })
	applyLimitFloor(&plan.Memory, hc, mm, plan.MemoryCapMB,
		hc == nil || hc.Resources.Memory <= 0,
		func(v int64) { hc.Resources.Memory = v * bytesPerMB })
	applyLimitFloor(&plan.CPU, hc, cc, plan.CPUCores,
		hc == nil || hc.Resources.NanoCPUs <= 0,
		func(v int64) { hc.Resources.NanoCPUs = v * nanoPerCPU })

	// --- FX.115 p4: the OCI runtime (hosted gVisor) ------------------------
	// The one knob here is NOT a HostConfig posture flag: it picks the runtime
	// the daemon runs the container under. gVisor (runsc) interposes its own
	// kernel, so it closes the kernel-side escape the p1 posture flags cannot
	// — but it is a HOST capability, and this repo never installs runsc and
	// never restarts docker. Hence the two fail-soft rules, both visible in
	// the report:
	//   * GPU skip (simple, documented): a container that REQUESTS GPU
	//     hardware — NVIDIA DeviceRequests capabilities [[gpu]] OR raw --device
	//     mounts (/dev/dxg, /dev/dri, /dev/kfd, ...) — stays on the daemon's
	//     default runtime unless worker.node_runtime_gvisor_gpu is explicitly
	//     on, because gVisor's GPU path is nvproxy and most CUDA/ROCm tooling
	//     fails under it. The report says "gpu_skipped".
	//   * runsc absent: the daemon's runtime list is the authority. No runsc
	//     means run under runc and report "runsc_unavailable" — NEVER a failed
	//     task, because a fleet whose host has no gVisor must keep running.
	// Report mode records exactly what enforce WOULD change and writes nothing.
	plan.Gvisor = LockdownFlag{Mode: gv}
	plan.GvisorRuntime = runscRuntimeName
	// gpuRequested is decided off the hostConfig the executor ALREADY built
	// (the GPU block above ran before this layer), so "GPU node" here means
	// "this container really asked for cards", not "the payload's flag".
	asksGPU := gpuRequested(hc)
	// The daemon is only asked when the knob is actually on: an untouched
	// fleet (gvisor off) never pays for an extra `docker info` per container.
	probeOK := false
	if gv != "off" {
		var runtimes []string
		probeOK, runtimes = runscAvailable(dockerClient)
		plan.GvisorRuntimes = runtimes
	}
	plan.Gvisor.Missing = !hasRunscRuntime(hc)
	// gpuOK is the whole GPU rule in one line: no GPU request at all, or the
	// owner explicitly allowed runsc on GPU nodes.
	gpuOK := !asksGPU || gvisorGPUAllowed(t)
	plan.Gvisor.Needed = plan.Gvisor.Missing && gv != "off" && gpuOK && probeOK
	// The note explains a NOT-needed verdict that is not "already hardened":
	// gpu_skipped wins over runsc_unavailable so an owner reading a GPU node
	// sees the reason that actually applies to it.
	if gv != "off" {
		if asksGPU && !gvisorGPUAllowed(t) {
			plan.GvisorNote = "gpu_skipped"
		} else if !probeOK {
			plan.GvisorNote = "runsc_unavailable"
		}
	}
	if plan.Gvisor.Needed && gv == "enforce" && hc != nil {
		hc.Runtime = runscRuntimeName
		plan.Gvisor.Applied = true
	}
	return plan
}

// applyLimitFloor fills ONE unlimited resource cap with the layer's floor.
// `unlimited` is decided by the caller (each resource reads a different field)
// and `write` is the caller's closure over that field, so this helper never
// has to know the field's shape. Nothing is written when a cap already exists,
// when the layer requested no floor, or when the mode is not enforce.
func applyLimitFloor(flag *LockdownFlag, hc *container.HostConfig,
	mode LockdownMode, cap int64, unlimited bool, write func(int64),
) {
	flag.Missing = unlimited
	flag.Needed = unlimited && mode != "off" && cap > 0
	if flag.Needed && mode == "enforce" && hc != nil {
		write(cap)
		flag.Applied = true
	}
}

// hasSeccompOpt reports whether the container already carries a seccomp
// profile (the worker's embedded one, or an operator's).
func hasSeccompOpt(hc *container.HostConfig) bool {
	if hc == nil {
		return false
	}
	for _, o := range hc.SecurityOpt {
		if strings.HasPrefix(o, "seccomp=") {
			return true
		}
	}
	return false
}

// modNodeSeccompOpt renders the worker's embedded node profile the way the
// Docker SDK wants it (INLINE JSON after "seccomp=" — a path is rejected).
// Empty string = "unavailable" (the embed failed), which makes the seccomp
// knob report Needed=false rather than writing a broken option.
func modNodeSeccompOpt() string {
	if len(embeddedSeccompProfile) == 0 {
		return ""
	}
	return "seccomp=" + string(embeddedSeccompProfile)
}

// scratchTmpfs returns the scratch paths the read-only-rootfs knob would put
// on tmpfs, skipping any path that is already a tmpfs entry or a bind-mount
// destination (a tmpfs there would shadow the bind and hide tenant data).
// `writing` selects the paths actually added; the list is reported either way
// so report mode names what enforce WOULD do.
func scratchTmpfs(hc *container.HostConfig, writing bool) []string {
	if hc == nil {
		return []string{}
	}
	taken := bindDestinations(hc.Binds)
	var added []string
	for _, p := range scratchPaths {
		if hc.Tmpfs != nil && hc.Tmpfs[p] != "" {
			continue // already a tmpfs — the hardened sandbox or the node owns it
		}
		if taken[p] {
			continue // a bind lands here: never shadow it
		}
		added = append(added, p)
		if writing {
			if hc.Tmpfs == nil {
				hc.Tmpfs = map[string]string{}
			}
			hc.Tmpfs[p] = scratchTmpfsOpts[p]
		}
	}
	return added
}

// bindDestinations maps every bind-mount destination path to true, for both
// the "host:container[:mode]" and named-volume "volname:container[:mode]"
// spellings the executor builds.
func bindDestinations(binds []string) map[string]bool {
	out := map[string]bool{}
	for _, b := range binds {
		parts := strings.Split(b, ":")
		if len(parts) < 2 {
			continue
		}
		dst := strings.TrimSpace(parts[1])
		if dst != "" {
			out[dst] = true
		}
	}
	return out
}

// LockdownReportOut is the out-param box executor.go's Run threads down to
// runContainerWithCredsInternal, mirroring ephemeral.go's ephemeralApplied
// box (Go has no pointer-to-map, so the box carries the map). Fields stays
// nil when the path never created a node container.
type LockdownReportOut struct {
	Fields map[string]any
}

// planMap is the machine-readable twin of planFields: the SAME verdict as a
// flat JSON object the worker rides back to the API on the task result
// ("lockdown_report"), so the manager can aggregate per (node kind, flag)
// without scraping the log line. FX.115 p2: the owner flips a flag only
// after the API's report endpoint says what WOULD change, and that input
// must not depend on a log pipeline. Keys match planFields' exactly.
func planMap(plan LockdownPlan) map[string]any {
	return map[string]any{
		"node_kind":           plan.NodeKind,
		"exempt":              plan.Exempt,
		"exempt_reason":       plan.ExemptReason,
		"nonroot_mode":        plan.Nonroot.Mode,
		"nonroot_missing":     plan.Nonroot.Missing,
		"nonroot_would_apply": plan.Nonroot.Needed,
		"nonroot_applied":     plan.Nonroot.Applied,
		"capdrop_mode":        plan.CapDrop.Mode,
		"capdrop_missing":     plan.CapDrop.Missing,
		"capdrop_would_apply": plan.CapDrop.Needed,
		"capdrop_applied":     plan.CapDrop.Applied,
		"noprivs_mode":        plan.NoPrivs.Mode,
		"noprivs_missing":     plan.NoPrivs.Missing,
		"noprivs_would_apply": plan.NoPrivs.Needed,
		"noprivs_applied":     plan.NoPrivs.Applied,
		"seccomp_mode":        plan.Seccomp.Mode,
		"seccomp_profile":     plan.SeccompProfile,
		"seccomp_missing":     plan.Seccomp.Missing,
		"seccomp_would_apply": plan.Seccomp.Needed,
		"seccomp_applied":     plan.Seccomp.Applied,
		"rootfs_mode":         plan.Rootfs.Mode,
		"rootfs_missing":      plan.Rootfs.Missing,
		"rootfs_would_apply":  plan.Rootfs.Needed,
		"rootfs_applied":      plan.Rootfs.Applied,
		"scratch_tmpfs":       plan.ScratchTmpfs,
		// FX.115 p3: which flags the per-node exception table relaxed for THIS node
		// (empty = no exception applied), so the API can quote each one's reason.
		"relaxed_flags": plan.RelaxedFlags,
		// FX.115 p4: the runtime knob (hosted gVisor). gvisor_note is the
		// fail-soft reason — "gpu_skipped" (GPU node, nvproxy not allowed) or
		// "runsc_unavailable" (the daemon has no runsc registered) — so the
		// owner can tell "deliberately not switched" from "already switched".
		"gvisor_mode":               plan.Gvisor.Mode,
		"gvisor_missing":            plan.Gvisor.Missing,
		"gvisor_would_apply":        plan.Gvisor.Needed,
		"gvisor_applied":            plan.Gvisor.Applied,
		"gvisor_runtime":            plan.GvisorRuntime,
		"gvisor_note":               plan.GvisorNote,
		"gvisor_runtimes":           plan.GvisorRuntimes,
		"limits_pids_mode":          plan.Pids.Mode,
		"limits_pids_missing":       plan.Pids.Missing,
		"limits_pids_would_apply":   plan.Pids.Needed,
		"limits_pids_applied":       plan.Pids.Applied,
		"limits_pids_cap":           plan.PidsCap,
		"limits_memory_mode":        plan.Memory.Mode,
		"limits_memory_missing":     plan.Memory.Missing,
		"limits_memory_would_apply": plan.Memory.Needed,
		"limits_memory_applied":     plan.Memory.Applied,
		"limits_memory_cap_mb":      plan.MemoryCapMB,
		"limits_cpu_mode":           plan.CPU.Mode,
		"limits_cpu_missing":        plan.CPU.Missing,
		"limits_cpu_would_apply":    plan.CPU.Needed,
		"limits_cpu_applied":        plan.CPU.Applied,
		"limits_cpu_cap_cores":      plan.CPUCores,
		"lockdown_uid":              plan.UID,
	}
}

// planFields flattens a plan into slog key/value pairs for the caller's one
// structured line per container ("lockdown-report ...").
func planFields(p transport.TaskPayload, plan LockdownPlan) []any {
	return []any{
		"node_kind", p.NodeKind,
		"exempt", plan.Exempt,
		"exempt_reason", plan.ExemptReason,
		"nonroot_mode", plan.Nonroot.Mode,
		"nonroot_missing", plan.Nonroot.Missing,
		"nonroot_would_apply", plan.Nonroot.Needed,
		"nonroot_applied", plan.Nonroot.Applied,
		"capdrop_mode", plan.CapDrop.Mode,
		"capdrop_missing", plan.CapDrop.Missing,
		"capdrop_would_apply", plan.CapDrop.Needed,
		"capdrop_applied", plan.CapDrop.Applied,
		"noprivs_mode", plan.NoPrivs.Mode,
		"noprivs_missing", plan.NoPrivs.Missing,
		"noprivs_would_apply", plan.NoPrivs.Needed,
		"noprivs_applied", plan.NoPrivs.Applied,
		// FX.115 p1b: seccomp profile, read-only rootfs + scratch tmpfs, and
		// the resource-limit floors, so one line carries the whole posture.
		"seccomp_mode", plan.Seccomp.Mode,
		"seccomp_profile", plan.SeccompProfile,
		"seccomp_missing", plan.Seccomp.Missing,
		"seccomp_would_apply", plan.Seccomp.Needed,
		"seccomp_applied", plan.Seccomp.Applied,
		"rootfs_mode", plan.Rootfs.Mode,
		"rootfs_missing", plan.Rootfs.Missing,
		"rootfs_would_apply", plan.Rootfs.Needed,
		"rootfs_applied", plan.Rootfs.Applied,
		"scratch_tmpfs", plan.ScratchTmpfs,
		// FX.115 p3: the exception table's per-flag relaxation for this container.
		"relaxed_flags", plan.RelaxedFlags,
		// FX.115 p4: the runtime knob, same verdict as planMap.
		"gvisor_mode", plan.Gvisor.Mode,
		"gvisor_missing", plan.Gvisor.Missing,
		"gvisor_would_apply", plan.Gvisor.Needed,
		"gvisor_applied", plan.Gvisor.Applied,
		"gvisor_runtime", plan.GvisorRuntime,
		"gvisor_note", plan.GvisorNote,
		"gvisor_runtimes", plan.GvisorRuntimes,
		"limits_pids_mode", plan.Pids.Mode,
		"limits_pids_missing", plan.Pids.Missing,
		"limits_pids_would_apply", plan.Pids.Needed,
		"limits_pids_applied", plan.Pids.Applied,
		"limits_pids_cap", plan.PidsCap,
		"limits_memory_mode", plan.Memory.Mode,
		"limits_memory_missing", plan.Memory.Missing,
		"limits_memory_would_apply", plan.Memory.Needed,
		"limits_memory_applied", plan.Memory.Applied,
		"limits_memory_cap_mb", plan.MemoryCapMB,
		"limits_cpu_mode", plan.CPU.Mode,
		"limits_cpu_missing", plan.CPU.Missing,
		"limits_cpu_would_apply", plan.CPU.Needed,
		"limits_cpu_applied", plan.CPU.Applied,
		"limits_cpu_cap_cores", plan.CPUCores,
		"lockdown_uid", plan.UID,
	}
}
