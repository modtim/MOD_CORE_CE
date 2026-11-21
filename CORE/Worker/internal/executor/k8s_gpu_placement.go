package executor

// K8s GPU placement — k8s-reduction-engine W21. Go port of the T6b selector
// the Python GPU-placement selector choose_gpu_placement (parity-tested
// against testdata/gpu_placement_cases.json, generated FROM the Python), plus
// the cluster side: cluster capabilities are derived from Node labels (GFD +
// the gpu-sharing README node-mode labels) and allocatable MIG resources, and
// cached per backend.
//
// Contract:
//   whole       nvidia.com/gpu: N           + device-plugin.config=exclusive
//   time_slice  nvidia.com/gpu: 1           + device-plugin.config=time-sliced-<R>
//   mig         nvidia.com/mig-<profile>: 1 + mig.config.state=success
//
// Degrade rule: no GPU nodes visible / node list fails (e.g. missing RBAC
// `nodes: list`) / explicit MOD_GPU_MIG_PROFILE → today's gpuResources path
// (whole nvidia.com/gpu). A failed list is logged once per failure streak.

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// Placement modes (Python GpuPlacement.mode).
const (
	GPUPlacementNone      = "none"
	GPUPlacementWhole     = "whole"
	GPUPlacementTimeSlice = "time_slice"
	GPUPlacementMIG       = "mig"
)

// Node labels (README + GFD).
const (
	LabelMIGConfigState = "nvidia.com/mig.config.state"
	LabelGFDProduct     = "nvidia.com/gpu.product"
	LabelGFDMemoryMiB   = "nvidia.com/gpu.memory" // MiB per physical GPU
	LabelGFDMIGStrategy = "nvidia.com/mig.strategy"
	LabelGFDReplicas    = "nvidia.com/gpu.replicas"
)

// Optional per-task demand hints carried in container_env (same carrier as
// MOD_GPU_COUNT / MOD_GPU_MIG_PROFILE in k8s_taskspec.go).
const (
	EnvGPUVRAMMB    = "MOD_GPU_VRAM_MB"   // per-device VRAM footprint in MB
	EnvGPUShareable = "MOD_GPU_SHAREABLE" // "1"/"true": tolerates time-slice co-tenancy
	EnvGPUPrefer    = "MOD_GPU_PREFER"    // auto|whole|time_slice|mig
)

// DefaultGPUCapsTTL is how long a node-derived capability snapshot is reused.
const DefaultGPUCapsTTL = 60 * time.Second

// migProfileOrder is the contract profile set, smallest slice first.
var migProfileOrder = []string{"1g.10gb", "2g.20gb", "3g.40gb"}

var migProfileVRAMGB = map[string]float64{"1g.10gb": 10, "2g.20gb": 20, "3g.40gb": 40}

// ValidMIGProfile reports whether p is one of the README contract profiles.
func ValidMIGProfile(p string) bool { _, ok := migProfileVRAMGB[p]; return ok }

// GPUDemand is the parsed fit-gate demand (Python parse_demand output).
// VRAMGB <= 0 means unknown footprint.
type GPUDemand struct {
	Count     int     `json:"count"`
	VRAMGB    float64 `json:"vram_gb"`
	Shareable bool    `json:"shareable"`
}

// GPUClusterCaps mirrors the Python cluster_caps dict. Zero values take the
// Python defaults (replicas 4, memory unknown; "whole" always available).
type GPUClusterCaps struct {
	Modes             []string `json:"modes"`
	MIGProfiles       []string `json:"mig_profiles"`
	TimeSliceReplicas int      `json:"time_slice_replicas"`
	GPUMemoryGB       float64  `json:"gpu_memory_gb"`

	// HasExclusiveNodes: some node carries device-plugin.config=exclusive.
	// Not part of the Python caps; gates the exclusive selector for Count==1
	// whole placements so we never pin to a label no node has.
	HasExclusiveNodes bool `json:"-"`
}

// GPUPlacement is the decision (Python GpuPlacement).
type GPUPlacement struct {
	Mode         string            `json:"mode"`
	Count        int               `json:"count"`
	MIGProfile   string            `json:"mig_profile"`
	Resources    map[string]int64  `json:"resources"`
	NodeSelector map[string]string `json:"node_selector"`
	Reason       string            `json:"reason"`
}

type normCaps struct {
	modes    map[string]bool
	profiles []string
	replicas int
	memGB    float64
}

func normaliseCaps(c *GPUClusterCaps) normCaps {
	n := normCaps{modes: map[string]bool{GPUPlacementWhole: true}, replicas: 4}
	if c == nil {
		return n
	}
	for _, m := range c.Modes {
		n.modes[m] = true
	}
	seen := map[string]bool{}
	for _, p := range c.MIGProfiles {
		if ValidMIGProfile(p) && !seen[p] {
			seen[p] = true
			n.profiles = append(n.profiles, p)
		}
	}
	if n.modes[GPUPlacementMIG] && len(n.profiles) == 0 {
		n.profiles = append(n.profiles, migProfileOrder...)
	}
	sort.Slice(n.profiles, func(i, j int) bool {
		return migProfileVRAMGB[n.profiles[i]] < migProfileVRAMGB[n.profiles[j]]
	})
	if c.TimeSliceReplicas == 4 || c.TimeSliceReplicas == 8 {
		n.replicas = c.TimeSliceReplicas
	}
	n.memGB = c.GPUMemoryGB
	return n
}

func placeWhole(count int, reason string) GPUPlacement {
	return GPUPlacement{Mode: GPUPlacementWhole, Count: count,
		Resources:    map[string]int64{string(ResourceGPU): int64(count)},
		NodeSelector: map[string]string{LabelDevicePluginConfig: DevicePluginExclusive}, Reason: reason}
}

func placeMIG(profile, reason string) GPUPlacement {
	return GPUPlacement{Mode: GPUPlacementMIG, Count: 1, MIGProfile: profile,
		Resources:    map[string]int64{ResourceMIGPrefix + profile: 1},
		NodeSelector: map[string]string{LabelMIGConfigState: "success"}, Reason: reason}
}

func placeTimeSlice(replicas int, reason string) GPUPlacement {
	return GPUPlacement{Mode: GPUPlacementTimeSlice, Count: 1,
		Resources:    map[string]int64{string(ResourceGPU): 1},
		NodeSelector: map[string]string{LabelDevicePluginConfig: fmt.Sprintf("time-sliced-%d", replicas)}, Reason: reason}
}

func tryMIG(vram float64, c normCaps) (string, string) {
	if !c.modes[GPUPlacementMIG] {
		return "", "cluster advertises no MIG"
	}
	if vram <= 0 {
		return "", "VRAM demand unknown; cannot size a MIG slice"
	}
	for _, p := range c.profiles {
		if vram <= migProfileVRAMGB[p] {
			return p, fmt.Sprintf("%g GB fits MIG %s", vram, p)
		}
	}
	return "", fmt.Sprintf("%g GB exceeds largest available MIG profile", vram)
}

func tryTimeSlice(vram float64, shareable bool, c normCaps, forced bool) (bool, string) {
	if !c.modes[GPUPlacementTimeSlice] {
		return false, "cluster advertises no time-slicing"
	}
	if !(shareable || forced) {
		return false, "demand not marked shareable (time-slicing has no VRAM isolation)"
	}
	if vram <= 0 {
		return false, "VRAM demand unknown; cannot check time-slice budget"
	}
	if c.memGB <= 0 {
		return false, "GPU memory unknown; cannot check time-slice budget"
	}
	budget := c.memGB / float64(c.replicas)
	if vram > budget {
		return false, fmt.Sprintf("%g GB exceeds advisory time-slice budget %g GB", vram, budget)
	}
	return true, fmt.Sprintf("%g GB within advisory budget %g GB (no isolation)", vram, budget)
}

// ChooseGPUPlacement is the Go port of choose_gpu_placement. prefer ∈
// auto|whole|time_slice|mig ("" = auto; unknown values are treated as auto
// rather than raising, since this runs on the task path).
func ChooseGPUPlacement(d GPUDemand, caps *GPUClusterCaps, prefer string) GPUPlacement {
	switch prefer {
	case "auto", GPUPlacementWhole, GPUPlacementTimeSlice, GPUPlacementMIG:
	default:
		prefer = "auto"
	}
	if d.Count <= 0 {
		return GPUPlacement{Mode: GPUPlacementNone, Resources: map[string]int64{},
			NodeSelector: map[string]string{}, Reason: "no GPU demand"}
	}
	c := normaliseCaps(caps)
	vram := d.VRAMGB

	if d.Count > 1 {
		note := ""
		if prefer != "auto" && prefer != GPUPlacementWhole {
			note = fmt.Sprintf(" (prefer=%s ignored)", prefer)
		}
		return placeWhole(d.Count, fmt.Sprintf("multi-GPU demand (%d) → exclusive whole GPUs%s", d.Count, note))
	}
	if prefer == GPUPlacementWhole {
		return placeWhole(1, "whole GPU preferred")
	}

	var notes []string
	if prefer == GPUPlacementTimeSlice {
		ok, why := tryTimeSlice(vram, d.Shareable, c, true)
		if ok {
			return placeTimeSlice(c.replicas, "time-slice preferred: "+why)
		}
		notes = append(notes, "time_slice rejected: "+why)
	}
	profile, why := tryMIG(vram, c)
	if profile != "" {
		return placeMIG(profile, strings.Join(append(notes, why), "; "))
	}
	if prefer == GPUPlacementMIG {
		notes = append(notes, "mig rejected: "+why)
	}
	if prefer != GPUPlacementTimeSlice {
		if ok, whyTS := tryTimeSlice(vram, d.Shareable, c, false); ok {
			return placeTimeSlice(c.replicas, strings.Join(append(notes, whyTS), "; "))
		}
	}
	return placeWhole(1, strings.Join(append(notes, "fallback: whole GPU"), "; "))
}

// GPUCapsFromNodes derives cluster caps from Node labels + allocatable.
// Returns nil when no node looks like an NVIDIA GPU node (→ caller degrades).
//
//   - whole:      always (Python default); HasExclusiveNodes when a node is
//     labelled device-plugin.config=exclusive.
//   - time_slice: a node labelled device-plugin.config=time-sliced-4|8 (only
//     labelled nodes count — the placement pins that label). Replicas = the
//     smallest R seen (largest per-replica budget); memory = the smallest
//     nvidia.com/gpu.memory (MiB) across time-sliced nodes (conservative).
//   - mig:        allocatable nvidia.com/mig-<contract profile> > 0 on a node
//     whose mig.config.state=success and mig.strategy != single.
func GPUCapsFromNodes(nodes []corev1.Node) *GPUClusterCaps {
	caps := &GPUClusterCaps{}
	gpuNodes := 0
	replicas := 0
	memMiB := 0.0
	profiles := map[string]bool{}
	for i := range nodes {
		n := &nodes[i]
		lb := n.Labels
		isGPU := lb[LabelGFDProduct] != ""
		if q, ok := n.Status.Allocatable[ResourceGPU]; ok && q.Value() > 0 {
			isGPU = true
		}
		migOK := lb[LabelMIGConfigState] == "success" && lb[LabelGFDMIGStrategy] != "single"
		for rn, q := range n.Status.Allocatable {
			name := string(rn)
			if !strings.HasPrefix(name, ResourceMIGPrefix) || q.Value() <= 0 {
				continue
			}
			isGPU = true
			if p := strings.TrimPrefix(name, ResourceMIGPrefix); migOK && ValidMIGProfile(p) {
				profiles[p] = true
			}
		}
		if !isGPU {
			continue
		}
		gpuNodes++
		cfg := lb[LabelDevicePluginConfig]
		switch cfg {
		case DevicePluginExclusive:
			caps.HasExclusiveNodes = true
		case "time-sliced-4", "time-sliced-8":
			r, _ := strconv.Atoi(strings.TrimPrefix(cfg, "time-sliced-"))
			if replicas == 0 || r < replicas {
				replicas = r
			}
			if m, err := strconv.ParseFloat(lb[LabelGFDMemoryMiB], 64); err == nil && m > 0 {
				if memMiB == 0 || m < memMiB {
					memMiB = m
				}
			}
		}
	}
	if gpuNodes == 0 {
		return nil
	}
	caps.Modes = []string{GPUPlacementWhole}
	if replicas > 0 {
		caps.Modes = append(caps.Modes, GPUPlacementTimeSlice)
		caps.TimeSliceReplicas = replicas
		caps.GPUMemoryGB = memMiB / 1024
	}
	if len(profiles) > 0 {
		caps.Modes = append(caps.Modes, GPUPlacementMIG)
		for _, p := range migProfileOrder {
			if profiles[p] {
				caps.MIGProfiles = append(caps.MIGProfiles, p)
			}
		}
	}
	return caps
}

// GPUCapsCache lists Nodes at most once per TTL. Failure → nil caps (degrade)
// for the TTL, warned once per failure streak. Needs RBAC `nodes: list`
// (cluster-scope).
type GPUCapsCache struct {
	Client kubernetes.Interface
	TTL    time.Duration
	Log    *slog.Logger

	mu      sync.Mutex
	fetched time.Time
	caps    *GPUClusterCaps
	failing bool
	now     func() time.Time
}

// NewGPUCapsCache builds a cache with DefaultGPUCapsTTL.
func NewGPUCapsCache(client kubernetes.Interface) *GPUCapsCache {
	return &GPUCapsCache{Client: client, TTL: DefaultGPUCapsTTL}
}

func (c *GPUCapsCache) clock() time.Time {
	if c.now != nil {
		return c.now()
	}
	return time.Now()
}

// Refresh re-lists Nodes when the snapshot is older than TTL.
func (c *GPUCapsCache) Refresh(ctx context.Context) {
	if c == nil || c.Client == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	ttl := c.TTL
	if ttl <= 0 {
		ttl = DefaultGPUCapsTTL
	}
	now := c.clock()
	if !c.fetched.IsZero() && now.Sub(c.fetched) < ttl {
		return
	}
	c.fetched = now
	lctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	nodes, err := c.Client.CoreV1().Nodes().List(lctx, metav1.ListOptions{})
	if err != nil {
		if !c.failing {
			log := c.Log
			if log == nil {
				log = slog.Default()
			}
			log.Warn("k8s gpu placement: node list failed; falling back to whole nvidia.com/gpu (needs RBAC nodes:list)", "err", err)
		}
		c.failing = true
		c.caps = nil
		return
	}
	c.failing = false
	c.caps = GPUCapsFromNodes(nodes.Items)
}

// Snapshot returns the last derived caps (nil = unknown → degrade).
func (c *GPUCapsCache) Snapshot() *GPUClusterCaps {
	if c == nil {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.caps
}

// gpuDemandFromSpec builds the demand from GPURequest.Count + env hints.
func gpuDemandFromSpec(spec *TaskSpec) (GPUDemand, string) {
	d := GPUDemand{Count: spec.GPU.Count}
	if v, err := strconv.ParseFloat(strings.TrimSpace(spec.Env[EnvGPUVRAMMB]), 64); err == nil && v > 0 {
		d.VRAMGB = v / 1024
	}
	switch strings.ToLower(strings.TrimSpace(spec.Env[EnvGPUShareable])) {
	case "1", "true", "yes", "on":
		d.Shareable = true
	}
	prefer := strings.ToLower(strings.TrimSpace(spec.Env[EnvGPUPrefer]))
	if prefer == "" {
		prefer = "auto"
	}
	return d, prefer
}

// placedGPUResources is gpuResources with cluster-aware placement applied.
// Explicit MIG profile, no GPU, or no known caps → gpuResources unchanged.
func (b *K8sBackend) placedGPUResources(spec *TaskSpec) (corev1.ResourceList, map[string]string) {
	g := spec.GPU
	if g.Count <= 0 || g.MIGProfile != "" {
		return b.gpuResources(g)
	}
	caps := b.GPUCaps.Snapshot()
	if caps == nil {
		return b.gpuResources(g)
	}
	d, prefer := gpuDemandFromSpec(spec)
	p := ChooseGPUPlacement(d, caps, prefer)
	switch p.Mode {
	case GPUPlacementMIG, GPUPlacementTimeSlice:
		rl := corev1.ResourceList{}
		for k, v := range p.Resources {
			rl[corev1.ResourceName(k)] = *resource.NewQuantity(v, resource.DecimalSI)
		}
		return rl, p.NodeSelector
	case GPUPlacementWhole:
		if p.Count == 1 && (!caps.HasExclusiveNodes || b.SingleGPUNodeSelector != "") {
			// No node labelled exclusive (pinning would strand the pod) or the
			// operator pinned Count==1 explicitly → today's behaviour.
			return b.gpuResources(g)
		}
		rl := corev1.ResourceList{ResourceGPU: *resource.NewQuantity(int64(p.Count), resource.DecimalSI)}
		return rl, p.NodeSelector
	}
	return b.gpuResources(g)
}
