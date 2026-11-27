// stack-ha-reconciliation group 1: observation.
//
// The manager cannot know whether a stack is still serving. It only ever knew
// the outcome of the last deploy task it dispatched, which says nothing about
// what happened in the hours since. Measured on the dev deployment: two vLLM
// stacks exited on 2026-07-28 and the database still reported them `running`
// two days later, holding 15 of 16 GB of VRAM out of the node-task pool the
// whole time.
//
// This file supplies the observed half. It reads the Docker daemon rather than
// any record of what the worker was told to deploy — a stack that was deployed
// successfully and then died must read as absent, which is exactly the case a
// dispatch-outcome record gets wrong.

package executor

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
)

// composeProjectLabel is set by `docker compose` on every container it creates.
// stackrunner.go always invokes compose with an explicit `-p <project>`, so the
// label carries exactly the project name the manager computes for the stack.
const composeProjectLabel = "com.docker.compose.project"

// haClassLabel carries the stack's ha_class from the manager, stamped onto
// every service by _compose_yaml. The worker cannot query the database, so a
// classification it must act on has to travel with the stack itself.
const haClassLabel = "com.example.ha-class"

// replaceableHAClasses mirrors REPLACEABLE_HA_CLASSES in
// services/stack_reconciler.py — the classes the manager will re-place onto
// another host without asking anyone.
//
// That list IS the fencing rule, and the symmetry is the argument: fencing
// exists to stop two live copies, and for a class the manager already
// re-places freely, a second copy is by definition acceptable. Fencing those
// buys nothing and costs availability. `snapshot_restore` and `external` are
// NOT here — they carry data, two writers lose it, and they still fence.
var replaceableHAClasses = map[string]bool{
	"stateless":    true,
	"cache_backed": true,
}

// composeServiceLabel identifies the service within the project.
const composeServiceLabel = "com.docker.compose.service"

// stackListBudget bounds the container enumeration.
//
// Named rather than inlined because §3.8a is precisely about this number: the
// fence failure it records was a deadline exceeded, and a deadline nobody can
// see or measure gets treated as infinite until the day it is not.
const stackListBudget = 15 * time.Second

// stackListOptions builds the container query for observation or for fencing.
//
// Pure, and separate from the call, because the difference between these two
// option sets IS §3.8a: `All: true` is correct for observation and was the
// cause of the fence timeout. Extracted so a test can assert the fencing query
// never regresses to enumerating the host's entire history — the regression
// would be silent, and would only surface as a fence that times out during a
// partition, which is the worst possible moment to discover it.
func stackListOptions(runningOnly bool) container.ListOptions {
	args := filters.NewArgs(filters.Arg("label", composeProjectLabel))
	if runningOnly {
		// Applied by the DAEMON, so exited containers are never serialised or
		// sent. Filtering after the fact would leave the cost exactly where it
		// is — the expense is in the daemon walking and marshalling them.
		args.Add("status", "running")
	}
	return container.ListOptions{All: !runningOnly, Filters: args}
}

// managedProjectPrefix scopes observation to projects MOD deploys.
//
// A host runs compose projects MOD knows nothing about — on the dev box the
// first report included `mod` (31 of 35 containers: MOD Core itself), `deploy`
// and `vclick3d`. Shipping those makes every one of them an "orphan stack" in
// the drift report, which buries the real signal and would have the reconciler
// reasoning about the platform's own containers.
//
// The prefix is exact rather than heuristic: the manager names every stack
// project `modstack<id>g<generation>` — the default in `_project_name` and the
// blue/green rename in the migrate path are the only two producers, and both
// use that form.
var managedProjectPrefix = func() string {
	if v := strings.TrimSpace(os.Getenv("MOD_STACK_PROJECT_PREFIX")); v != "" {
		return v
	}
	return "modstack"
}()

// StackObservation is what this host currently hosts for one compose project.
//
// Keyed by project name rather than stack id on purpose: the manager derives
// the project from the stack (`_project_name`), including the storage_refs
// override used by blue/green migration generations. Matching on the string
// both sides compute keeps generation handling correct without the worker
// needing to understand generations at all.
type StackObservation struct {
	Project string `json:"project"`
	// Running is the count of containers in the project that are actually up.
	Running int `json:"running"`
	// Total counts every container in the project, running or not, so the
	// manager can tell "partially up" from "entirely gone".
	Total    int      `json:"total"`
	// Down counts containers that should be running (author's restart intent,
	// see stackloop.go) but are not. -1 = not computed (loop disabled).
	Down     int      `json:"down"`
	// DownServices lists those containers (service, original policy, exit).
	DownServices []DownService `json:"down_services,omitempty"`
	Services []string `json:"services,omitempty"`
	// ServiceStates is the per-compose-service posture this beat (STACK-HEALTH-STATUS-1126):
	// state, compose healthcheck word, exit code and the daemon's restart count, for
	// EVERY container of the project (running or not) that carries a service label.
	// A separate key from `services` (the bare name list) so every existing consumer
	// of that key stays byte-identical; the manager reads this into
	// `StackObservation.service_states` and derives `status_health` from it.
	// One-shot services (exited 0, restart no) are healthy — see serviceIsBad.
	ServiceStates []ServiceState `json:"service_states,omitempty"`
	// HAClass is the manager's classification, read off the container labels.
	// Empty when the stack was deployed before the label existed — treated as
	// fenceable, because an unknown stack might hold data.
	HAClass string `json:"ha_class,omitempty"`
	// VramGB is the GPU memory this project's RUNNING containers are actually
	// holding right now, summed across every compute process the driver
	// attributes to a PID inside one of the project's containers (MiB→GB).
	//
	// Zero means UNMEASURED, not "measured zero": no GPU on this host, no
	// nvidia-smi, or the daemon could not map a PID to a container. The manager
	// side treats an absent/zero value as "no measurement" and falls back to the
	// declared VRAM guess — it must never be read as "this stack uses no VRAM".
	// This is the ONLY per-stack serving-VRAM measurement the platform has; it
	// feeds StackProfile.serving_vram_gb via the characterization sampler.
	VramGB float64 `json:"vram_gb,omitempty"`
	// Gateway is the raw scrape of this project's SGLang model gateway, when one
	// is running: {"workers": <parsed /workers JSON>, "metrics": "<raw text>"}
	// (metrics may be absent when :29000 is unreachable). Parsed server-side —
	// see services/stack_observation.record_observations. Absent when the
	// project has no gateway container or the scrape timed out; never an error.
	Gateway map[string]interface{} `json:"gateway,omitempty"`
}

// ServiceState is one compose service's posture on this beat (STACK-HEALTH-STATUS-1126).
// `state` is the daemon's word; `health` the compose healthcheck's word (absent =
// no healthcheck); `exit` the last exit code (-1 = never/not reported); `restarts`
// the daemon's RestartCount - what separates a crash-loop from a one-shot that
// finished. `bad` is this file's verdict (serviceIsBad), precomputed so the
// manager and the route gate read one word instead of re-deriving the rule.
type ServiceState struct {
	Service string `json:"service"`
	State   string `json:"state"`
	Health  string `json:"health,omitempty"`
	Exit    int    `json:"exit"`
	Restarts int   `json:"restarts"`
	Bad     bool   `json:"bad"`
}

// serviceIsBad is the worker's own verdict on one service posture.
//
// A service is BAD when it is not serving: exited/restarting/dead/paused, or
// running while its healthcheck says "unhealthy". A one-shot that exited 0 with
// restart intent `no`/`false` is NOT bad - a finished job is not a broken
// service (owner: one-shot services exited 0, restart no are healthy). `created`
// (never started) is not bad either: the loop's stackDownCount already reports
// containers that SHOULD run but do not; this verdict only answers "is what is
// here broken".
func serviceIsBad(state, health, intent string, exit, restarts int) bool {
	switch state {
	case "running":
		// RestartCount>0 on a RUNNING container is the daemon's own crash
		// accounting: it restarted this container after it died. A crash-loop
		// that happens to be mid-cycle still reads running - the count is what
		// catches it (a manual docker start never bumps RestartCount).
		return health == "unhealthy" || restarts > 0
	case "restarting", "paused", "dead":
		return true
	case "exited":
		if exit != 0 {
			return true
		}
		// exit 0: bad only if Docker itself would start it again (always /
		// unless-stopped). on-failure after a 0 exit does NOT restart
		// (containerWantsRunning's rule), so it is a finished job: healthy.
		return intent == "always" || intent == "unless-stopped"
	}
	return false
}

// Fenceable reports whether losing the manager should stop this stack.
//
// Empty class fences: a stack deployed before the label existed, or by
// something that did not stamp one, might be a database. Defaulting the
// unknown case to "keep serving" would trade a rare availability problem for a
// rare data-loss one.
func (o StackObservation) Fenceable() bool {
	return !replaceableHAClasses[strings.TrimSpace(o.HAClass)]
}

// Healthy reports whether the project should count as hosted here.
//
// A project with zero running containers is NOT hosted, even though its
// stopped containers still exist and still carry the label. That distinction
// is the entire point: `docker ps -a` would have shown both dead vLLM stacks
// on the dev box, and reporting them as present is what let the manager go on
// believing they were serving.
func (o StackObservation) Healthy() bool { return o.Running > 0 }

// ObserveStacks lists the compose projects with containers on this host.
//
// Returns every project it can see, including fully-stopped ones, and lets the
// manager apply the Running > 0 rule. Reporting the stopped ones costs one
// line each and lets the manager distinguish "this host never had it" from
// "this host has it and it is down" — different failures needing different
// responses, and indistinguishable if we filtered here.
func (e *Executor) ObserveStacks(ctx context.Context) ([]StackObservation, error) {
	if o := k8sStackObserverActive(); o != nil { // k8s-reduction-engine W1: MOD_STACK_BACKEND=k8s
		return o.ObserveStacks(ctx)
	}
	return e.observeStacks(ctx, false)
}

// observeStacks enumerates compose projects, optionally only the running ones.
//
// `runningOnly` exists for fencing (§3.8a). Observation genuinely needs
// `All: true` — a project whose containers all exited is the case most worth
// reporting, and Total-vs-Running is how the manager tells "partially up" from
// "entirely gone". But that query is O(every container ever run on this host),
// and it is unbounded by construction: exited containers accumulate for the
// life of the box.
//
// Measured on the dev worker 2026-08-01: 2232 containers, of which 46 were
// running — roughly 2200 dead node-task containers from past workflow
// executions. Enumerating them exceeded the fencer's deadline, the fence
// failed, and (before §3.7) never retried. The failure mode is nasty because
// it is invisible until it matters: a host that has run for months fences
// slower than one deployed yesterday, with no other symptom, and the fence is
// exactly the operation you cannot afford to have time out.
//
// Fencing only ever stops containers that are RUNNING, so it has no use for
// the exited ones. Asking the daemon for the smaller set is not an
// optimisation — it removes the host's history from the critical path.
func (e *Executor) observeStacks(ctx context.Context, runningOnly bool) ([]StackObservation, error) {
	listCtx, cancel := context.WithTimeout(ctx, stackListBudget)
	defer cancel()

	started := time.Now()
	containers, err := e.docker.ContainerList(listCtx, stackListOptions(runningOnly))
	elapsed := time.Since(started)
	if err != nil {
		return nil, fmt.Errorf("listing containers took %s of a %s budget: %w",
			elapsed.Round(time.Millisecond), stackListBudget, err)
	}
	// The deadline is a budget to measure, not one to assume (§3.8a). Half of
	// it is the warning line: a host that has crept to half is on the path to
	// the fence failure above, and this is the only symptom it will show
	// before it gets there.
	if elapsed > stackListBudget/2 {
		e.log.Warn("container enumeration is approaching its budget — a fence "+
			"on this host may time out; consider pruning exited containers",
			"elapsed", elapsed.Round(time.Millisecond), "budget", stackListBudget,
			"containers", len(containers), "running_only", runningOnly)
	}

	byProject := make(map[string]*StackObservation)
	// Running container ids per project — the only ones worth sampling for GPU
	// memory, and the set ContainerTop can be asked about.
	runningByProject := make(map[string][]string)
	// Running gateway container per project (at most one is meaningful; the
	// first running gateway wins) — the container to scrape for the heartbeat.
	// We keep the full list entry: its .Ports (published) and .Image are already
	// in hand from the list, so the scrape needs ContainerInspect only for the
	// bridge IP.
	gatewayByProject := make(map[string]types.Container)
	for _, c := range containers {
		project := strings.TrimSpace(c.Labels[composeProjectLabel])
		if project == "" || !strings.HasPrefix(project, managedProjectPrefix) {
			continue
		}
		obs, ok := byProject[project]
		if !ok {
			obs = &StackObservation{Project: project}
			byProject[project] = obs
		}
		obs.Total++
		if v := strings.TrimSpace(c.Labels[haClassLabel]); v != "" {
			obs.HAClass = v
		}
		// State is the daemon's own word: "running", "exited", "created",
		// "restarting", "paused", "dead". Only "running" is serving. A
		// "restarting" container is crash-looping, which must not read as up.
		if c.State == "running" {
			obs.Running++
			runningByProject[project] = append(runningByProject[project], c.ID)
			if svc := strings.TrimSpace(c.Labels[composeServiceLabel]); svc != "" {
				obs.Services = append(obs.Services, svc)
			}
			if isGatewayImage(c.Image) && gatewayByProject[project].ID == "" {
				gatewayByProject[project] = c
			}
		}
	}

	// Per-stack serving VRAM — observation only. Fencing (runningOnly) has no
	// use for it and must stay on the fast path (§3.8a). A sampling failure
	// leaves VramGB at zero (unmeasured); it never fails the observation.
	if !runningOnly {
		// 1522 — self-heal an exited/created sidecar in an otherwise-healthy
		// stack. Uses the same container list already fetched above (`All:
		// true`), so it costs nothing extra and sees exactly the daemon state
		// this observation is reporting.
		// Worker-controlled start (stackloop.go): the API-driven restore loop
		// replaces the capped `docker start` self-heal, which cannot bring back a
		// container whose secret files are gone and gives up after 5 tries.
		if StackWorkerStartEnabled() {
			restartIntentMu.Lock()
			down := stackDownCount(containers, loadRestartIntent(), exitedBeforeBootFn(ctx, e.docker))
			restartIntentMu.Unlock()
			for p, obs := range byProject {
				if stackProjectHeld(p) {
					// Review2 N1: a restore/snapshot/... stopped it on
					// purpose (or is about to): not "down", no `down` key,
					// so the API neither restores nor re-labels it.
					obs.Down = -1
					obs.DownServices = nil
					continue
				}
				obs.Down = len(down[p])
				obs.DownServices = down[p]
			}
		} else {
			for _, obs := range byProject {
				obs.Down = -1
			}
			e.healExitedStackSidecars(ctx, containers, byProject)
		}
		// STACK-HEALTH-STATUS-1126 — per-service posture for the manager's
		// status_health derivation (observation path only, like VRAM sampling).
		// Same container list already in hand; one Inspect per labelled container
		// of a managed project. Best-effort: a failed inspect leaves that service
		// at its list-level values; it never fails the observation.
		collectServiceStates(ctx, e.docker, containers, byProject)
		e.sampleStackVRAM(ctx, byProject, runningByProject)
		e.sampleStackGateway(ctx, byProject, gatewayByProject)
	}

	out := make([]StackObservation, 0, len(byProject))
	for _, obs := range byProject {
		out = append(out, *obs)
	}
	return out, nil
}

// collectServiceStates fills each project's ServiceStates from the container
// list plus one Inspect per labelled container (health + RestartCount are only
// in the Inspect). `bad` is serviceIsBad with the container's restart intent.
func collectServiceStates(ctx context.Context, d stackPolicyDocker,
	containers []types.Container, byProject map[string]*StackObservation) {
	restartIntentMu.Lock()
	recorded := loadRestartIntent()
	restartIntentMu.Unlock()
	for _, c := range containers {
		svc := strings.TrimSpace(c.Labels[composeServiceLabel])
		if svc == "" || !isManagedStackContainer(c) {
			continue
		}
		obs := byProject[c.Labels[composeProjectLabel]]
		if obs == nil {
			continue
		}
		intent := intentOf(c, recorded)
		st := ServiceState{Service: svc, State: c.State,
			Exit: exitCodeFromStatus(c.Status), Bad: false}
		insCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		info, err := d.ContainerInspect(insCtx, c.ID)
		cancel()
		if err == nil && info.ContainerJSONBase != nil {
			base := info.ContainerJSONBase
			if base.State != nil {
				if base.State.Health != nil && base.State.Health.Status != "" {
					st.Health = base.State.Health.Status
				}
				st.Restarts = base.RestartCount
				if base.State.ExitCode != 0 {
					st.Exit = base.State.ExitCode
				}
			}
		}
		st.Bad = serviceIsBad(st.State, st.Health, intent, st.Exit, st.Restarts)
		obs.ServiceStates = append(obs.ServiceStates, st)
	}
}

// gpuComputeAppsBudget bounds the nvidia-smi call so a wedged driver cannot
// stall an entire heartbeat's observation.
const gpuComputeAppsBudget = 5 * time.Second

// gpuMemQuery is overridable in tests; the default execs nvidia-smi and returns
// its raw compute-apps CSV. Returns ("", false) when no GPU / no nvidia-smi.
var gpuMemQuery = func(ctx context.Context) (string, bool) {
	smiCtx, cancel := context.WithTimeout(ctx, gpuComputeAppsBudget)
	defer cancel()
	out, err := exec.CommandContext(smiCtx, "nvidia-smi",
		"--query-compute-apps=pid,used_gpu_memory",
		"--format=csv,noheader,nounits").Output()
	if err != nil {
		// No GPU, nvidia-smi absent, or MIG/driver refusing the query. All of
		// these mean "no measurement", which is the caller's cue to leave VRAM
		// unmeasured rather than fabricate a zero.
		return "", false
	}
	return string(out), true
}

// parseComputeApps turns `nvidia-smi --query-compute-apps=pid,used_gpu_memory
// --format=csv,noheader,nounits` output into pid → MiB.
//
// Pure so the parsing (the fiddly part: [N/A]/[Not Supported] rows a driver
// emits for processes it cannot introspect, blank lines, stray spaces) is
// unit-testable without a GPU. Rows without a numeric memory value are skipped
// rather than counted as zero, because "the driver could not read this process"
// is not "this process uses no VRAM".
func parseComputeApps(csv string) map[int]int64 {
	out := make(map[int]int64)
	for _, line := range strings.Split(strings.TrimSpace(csv), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		parts := strings.SplitN(line, ",", 2)
		if len(parts) != 2 {
			continue
		}
		pid, err := strconv.Atoi(strings.TrimSpace(parts[0]))
		if err != nil {
			continue
		}
		mib, err := strconv.ParseInt(strings.TrimSpace(parts[1]), 10, 64)
		if err != nil || mib < 0 {
			// "[N/A]", "[Not Supported]", "-", etc.
			continue
		}
		out[pid] += mib
	}
	return out
}

// sampleStackVRAM sums each project's running containers' GPU memory into
// obs.VramGB (MiB→GB). It maps the driver's compute-process PIDs to containers
// via `docker top` (host PIDs on both sides), so it needs neither host /proc
// access nor the worker sharing a PID namespace — the two facts the worker can
// already obtain, intersected.
//
// Best-effort throughout: no GPU processes, or a container the daemon will not
// enumerate, leaves that project unmeasured (VramGB stays 0) rather than
// erroring the whole observation.
func (e *Executor) sampleStackVRAM(ctx context.Context, byProject map[string]*StackObservation,
	runningByProject map[string][]string) {
	if len(runningByProject) == 0 {
		return
	}
	csv, ok := gpuMemQuery(ctx)
	if !ok {
		return // no GPU / no nvidia-smi — leave every project unmeasured.
	}
	pidMem := parseComputeApps(csv)
	if len(pidMem) == 0 {
		return // GPU present but nothing is using it — nothing to attribute.
	}
	for project, ids := range runningByProject {
		obs := byProject[project]
		if obs == nil {
			continue
		}
		var totalMiB int64
		for _, id := range ids {
			for _, pid := range e.containerHostPIDs(ctx, id) {
				if mib, has := pidMem[pid]; has {
					totalMiB += mib
				}
			}
		}
		if totalMiB > 0 {
			obs.VramGB = float64(totalMiB) / 1024.0
		}
	}
}

// sampleStackGateway scrapes each project's running SGLang gateway (if any) and
// stores the raw payload in obs.Gateway. It is observation-only and best-effort:
// a project with no gateway container, an unreachable gateway, or a scrape that
// blows its budget simply keeps Gateway nil so the key is omitted from the
// heartbeat. It never fails or errors the observation — the same "no measurement
// is not zero" discipline VramGB follows.
func (e *Executor) sampleStackGateway(ctx context.Context, byProject map[string]*StackObservation,
	gatewayByProject map[string]types.Container) {
	if len(gatewayByProject) == 0 {
		return
	}
	for project, container := range gatewayByProject {
		obs := byProject[project]
		if obs == nil || container.ID == "" {
			continue
		}
		containerIP := e.gatewayContainerIP(ctx, container.ID)
		candidates := gatewayCandidates(container, containerIP)
		if len(candidates) == 0 {
			continue
		}
		gw := scrapeGatewayEndpoints(ctx, candidates)
		if gw != nil {
			obs.Gateway = gw
		}
	}
}

// gatewayContainerIP returns the first non-link-local IP from ContainerInspect
// (the primary dial address), or "" on any error. The caller then falls back to
// the published-port candidate, and to omitting the scrape if even that is
// unavailable. Inspect is the only daemon call the scrape needs — published
// ports and the image already came in on the list entry.
func (e *Executor) gatewayContainerIP(ctx context.Context, containerID string) string {
	insCtx, cancel := context.WithTimeout(ctx, gatewayCallTimeout)
	defer cancel()
	info, err := e.docker.ContainerInspect(insCtx, containerID)
	if err != nil || info.NetworkSettings == nil {
		return ""
	}
	for _, ep := range info.NetworkSettings.Networks {
		if ep == nil {
			continue
		}
		if addr := strings.TrimSpace(ep.IPAddress); addr != "" && !strings.HasPrefix(addr, "fe80:") {
			return addr
		}
	}
	return ""
}

// containerHostPIDs returns the host PIDs of every process in a running
// container, via the daemon's `docker top`. Those PIDs are in the host PID
// namespace — the same namespace nvidia-smi reports compute-app PIDs in — which
// is what lets the two be intersected without the worker reading /proc.
//
// Returns nil on any error: the container may have exited between the list and
// this call, and one unreadable container must not sink the sample.
func (e *Executor) containerHostPIDs(ctx context.Context, containerID string) []int {
	topCtx, cancel := context.WithTimeout(ctx, gpuComputeAppsBudget)
	defer cancel()
	top, err := e.docker.ContainerTop(topCtx, containerID, nil)
	if err != nil {
		return nil
	}
	pidCol := -1
	for i, title := range top.Titles {
		if strings.EqualFold(strings.TrimSpace(title), "PID") {
			pidCol = i
			break
		}
	}
	if pidCol < 0 {
		return nil
	}
	pids := make([]int, 0, len(top.Processes))
	for _, proc := range top.Processes {
		if pidCol >= len(proc) {
			continue
		}
		if pid, err := strconv.Atoi(strings.TrimSpace(proc[pidCol])); err == nil {
			pids = append(pids, pid)
		}
	}
	return pids
}

// FenceStacks stops every managed stack running on this host.
//
// stack-ha-reconciliation §3.1. Called when the worker has not reached its
// manager within the lease window, WITHOUT being told to — a partitioned
// worker cannot be told anything, which is the whole point.
//
// `stop`, never `down`: fencing is about not serving, not about destroying.
// The volumes and the compose project stay intact so the stack can be started
// again if reconciliation decides it belongs here after all. Using `down -v`
// here would make a network partition permanently destroy tenant data.
//
// Returns what it stopped. Errors from individual projects are collected
// rather than aborting: one project refusing to stop must not leave the others
// running, since the manager will re-place ALL of them once its grace period
// expires.
func (e *Executor) FenceStacks(ctx context.Context) ([]string, error) {
	if so := k8sStackObserverActive(); so != nil { // k8s-reduction-engine W1/W5: fence = scale Deployments to 0
		if f, ok := so.(stackFencer); ok {
			return f.FenceStacks(ctx, e.log)
		}
		e.log.Warn("stack fencing requested but MOD_STACK_BACKEND=k8s and the stack observer has no K8s client — no-op")
		return nil, nil
	}
	// Running containers only (§3.8a). Fencing stops what is serving, and the
	// exited containers this host has accumulated are both irrelevant here and
	// the reason the enumeration timed out on 2026-08-01.
	obs, err := e.observeStacks(ctx, true)
	if err != nil {
		return nil, fmt.Errorf("cannot enumerate stacks to fence: %w", err)
	}

	var stopped []string
	var failures []string
	for _, o := range obs {
		if !o.Healthy() {
			continue // already not serving
		}
		if !o.Fenceable() {
			// Nothing here for a partition to corrupt, so stopping it only
			// makes an outage out of a manager restart. On 2026-08-13 this
			// stopped a model server three times over, because the API takes
			// longer to come back than the 120s fence lease allows.
			e.log.Info("fence: keeping stack, class is re-placeable",
				"project", o.Project, "ha_class", o.HAClass)
			continue
		}
		out, err := e.composeCmd(ctx, Callbacks{}, o.Project, "", nil, "stop")
		if err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v (%s)", o.Project, err, tail(out)))
			continue
		}
		stopped = append(stopped, o.Project)
	}

	if len(failures) > 0 {
		return stopped, fmt.Errorf("fence incomplete: %s", strings.Join(failures, "; "))
	}
	return stopped, nil
}

// sidecarRestartMaxAttemptsTunableKey is the platform_settings registry key
// operators can push (GS.PSR.7 — API-delivered worker tunables, no new env
// var) to change the retry cap below without a worker restart.
const sidecarRestartMaxAttemptsTunableKey = "worker.sidecar_restart_max_attempts"

// sidecarRestartMaxAttemptsDefault bounds how many times
// healExitedStackSidecars will `docker start` the same container across
// reconcile ticks before giving up on it.
const sidecarRestartMaxAttemptsDefault = 5

// sidecarStartAttempts counts, per container ID, how many times this process
// has tried to `docker start` it back up (1522). Package-level rather than a
// field on Executor — there is exactly one Executor per worker process, and
// this mirrors the existing dockerCLI/managedProjectPrefix package-var
// convention already used in this file.
//
// 1522b: an entry used to simply outlive its container — recreated with a new
// ID, or removed outright — and never get read again, which meant the map
// grew by one stale entry per heal with no bound but the host's uptime.
// healExitedStackSidecars now prunes any ID absent from the container list
// observeStacks already fetched, on every heal pass, so the map is bounded by
// the number of containers THIS HOST CURRENTLY HAS, not every container it
// has ever hosted.
var sidecarStartAttempts sync.Map // containerID -> *int32 attempts so far

// sidecarStillDownWarned remembers the last state for which the "still down
// after max restart attempts" line was emitted per container ID, so a capped-
// out sidecar logs it once per state change rather than once per heartbeat.
// Pruned with the same sweep as sidecarStartAttempts, so it is bounded by the
// containers this host currently has.
var sidecarStillDownWarned sync.Map // containerID -> *string last warned state

// noteStillDown returns true the first time a container is seen capped-out in
// a given state (or in a NEW state after a state change), false for repeats.
func noteStillDown(id, state string) bool {
	raw, _ := sidecarStillDownWarned.LoadOrStore(id, new(string))
	last := raw.(*string)
	if *last == state {
		return false
	}
	*last = state
	return true
}

// composeOneoffLabel is stamped by `docker compose run` on a one-off container.
// A one-shot service (migrate, init, a schema bump) is a job, not a sidecar:
// it exits 0 by design and is EXPECTED to stay down. The self-heal loop must
// never start it back, never spend an attempt on it and never warn about it.
const composeOneoffLabel = "com.docker.compose.oneoff"

// oneShotInspectFormat is the `docker inspect` format used to tell a finished
// one-shot job apart from a wedged sidecar. ContainerList does not carry the
// exit code or the restart policy, so this is one extra inspect per already-
// exited container of an otherwise-healthy stack — a set bounded by the stack
// size, never by host history.
const oneShotInspectFormat = "{{.State.ExitCode}}|{{.HostConfig.RestartPolicy.Name}}"

// isFinishedOneShot reports true for an EXITED container that finished cleanly
// (exit 0) and was never meant to keep running: restart policy `no`/absent, or
// the compose one-off label. A non-zero exit, or a restart policy that asks
// for the container to be kept up, is a real failure and stays healable.
// A failed/odd inspect output reads as "not one-shot" so the old behaviour
// (heal it) is the fallback — the heal is capped, so a wrong guess cannot loop.
func isFinishedOneShot(ctx context.Context, c types.Container) bool {
	out, err := dockerCLI(ctx, "inspect", "-f", oneShotInspectFormat, c.ID)
	if err != nil {
		return false
	}
	exitStr, policy, _ := splitInspectPair(out)
	if strings.TrimSpace(exitStr) != "0" {
		return false
	}
	policy = strings.TrimSpace(policy)
	if policy == "" || policy == "no" {
		return true
	}
	return strings.TrimSpace(c.Labels[composeOneoffLabel]) == "True"
}

// splitInspectPair splits the `exit|policy` pair oneShotInspectFormat renders.
func splitInspectPair(out string) (string, string, bool) {
	i := strings.Index(out, "|")
	if i < 0 {
		return strings.TrimSpace(out), "", false
	}
	return strings.TrimSpace(out[:i]), strings.TrimSpace(out[i+1:]), true
}

// healExitedStackSidecars `docker start`s any container of a RUNNING stack
// (Running > 0 — see StackObservation.Healthy) that the daemon reports
// `exited` or `created`, at most once per call — which is at most once per
// reconcile tick, since this runs from observeStacks on every heartbeat — with
// a capped attempt counter per container.
//
// Scoped to stacks with Running > 0 on purpose: a stack with ZERO running
// containers was either stopped on purpose (an explicit "stop" StackOp task,
// or this same host's own fence — FenceStacks runs `compose stop`, which
// stops every container in the project) or was never up here. Both must stay
// down, so this never touches them. The one case worth acting on is the
// partial failure this was written for — most of the stack's containers
// serving, one sidecar wedged in Exited/Created — which Total > Running
// already measures and is therefore left as the existing degraded signal:
// a container still down after the attempt cap stays counted in obs.Total
// without being counted in obs.Running, exactly as an un-healed one would be,
// so the manager already sees the stack as degraded with no new field.
//
// 1522: modstack22g0-tsegress-site-switchyard-1 sat Exited(127) for ~20h after
// a bind-mount shim race at container (re)create, even though the sidecar
// already carries `restart: unless-stopped` — Docker does not retry a
// container whose START failed, only one that exits after starting.
func (e *Executor) healExitedStackSidecars(ctx context.Context, containers []types.Container, byProject map[string]*StackObservation) {
	// 1522b: prune before anything else, and unconditionally (even when the
	// maxAttempts<=0 early-return below skips the restart loop entirely) — the
	// pruning is cheap, needs no extra Docker call (reuses the same
	// `containers` observeStacks already fetched), and otherwise a host
	// running with the feature tuned off would still leak one entry per
	// container churn.
	//
	// Deliberately NOT reset to zero for a container currently seen
	// "running": a healed container reads as "running" on the very next
	// tick, and this function only revisits an ID once it is next seen
	// "exited"/"created" again. Clearing its counter at that first "running"
	// sighting would forgive the attempt that produced it — exactly the
	// flapping case the cap exists for (starts, crashes before the next
	// tick, starts, crashes, ...) would then never climb past attempt 1 and
	// would restart forever. Leaving the counter in place until the ID
	// itself vanishes is what makes the cap mean anything for that
	// container; only a full recreate (new ID) gets a fresh budget.
	seen := make(map[string]struct{}, len(containers))
	for _, c := range containers {
		seen[c.ID] = struct{}{}
	}
	sidecarStartAttempts.Range(func(key, _ interface{}) bool {
		id, _ := key.(string)
		if _, present := seen[id]; !present {
			sidecarStartAttempts.Delete(key)
		}
		return true
	})
	sidecarStillDownWarned.Range(func(key, _ interface{}) bool {
		id, _ := key.(string)
		if _, present := seen[id]; !present {
			sidecarStillDownWarned.Delete(key)
		}
		return true
	})

	maxAttempts := sidecarRestartMaxAttemptsDefault
	if e.tunables != nil {
		maxAttempts = e.tunables.Int(sidecarRestartMaxAttemptsTunableKey, sidecarRestartMaxAttemptsDefault)
	}
	if maxAttempts <= 0 {
		return
	}
	for _, c := range containers {
		if c.State != "exited" && c.State != "created" {
			continue
		}
		project := strings.TrimSpace(c.Labels[composeProjectLabel])
		if project == "" {
			continue
		}
		obs := byProject[project]
		if obs == nil || !obs.Healthy() {
			// Running == 0 for this project: fenced, explicitly stopped by the
			// API, or never up. Not this loop's job to bring it back.
			continue
		}
		service := strings.TrimSpace(c.Labels[composeServiceLabel])

		// A finished one-shot job (migrate, init) is not a down sidecar: it
		// exits 0 by design and must stay down. Checked BEFORE the counter so
		// it never consumes an attempt and never reaches the "still down"
		// warning — the live xeon symptom was attempt 24 of a job that had
		// already succeeded.
		if c.State == "exited" && isFinishedOneShot(ctx, c) {
			continue
		}

		raw, _ := sidecarStartAttempts.LoadOrStore(c.ID, new(int32))
		counter := raw.(*int32)
		attempts := atomic.AddInt32(counter, 1)
		if int(attempts) > maxAttempts {
			// Warn once per container+state change, not once per reconcile
			// tick: a capped-out sidecar is re-sighted every heartbeat and
			// the repeat adds no information. The key is cleared when the
			// container's state changes or its ID vanishes (pruned below).
			if noteStillDown(c.ID, c.State) {
				e.log.Warn("stack sidecar self-heal: still down after max restart attempts; leaving it to the degraded-stack signal",
					"project", project, "service", service, "container", shortID(c.ID),
					"state", c.State, "attempts", attempts, "max_attempts", maxAttempts)
			}
			continue
		}

		out, err := dockerCLI(ctx, "start", c.ID)
		if err != nil {
			e.log.Warn("stack sidecar self-heal: docker start failed",
				"project", project, "service", service, "container", shortID(c.ID),
				"state", c.State, "attempt", attempts, "max_attempts", maxAttempts,
				"error", err, "output", strings.TrimSpace(out))
			continue
		}
		e.log.Warn("stack sidecar self-heal: restarted an exited/created container in a running stack",
			"project", project, "service", service, "container", shortID(c.ID),
			"state", c.State, "attempt", attempts, "max_attempts", maxAttempts)
	}
}
