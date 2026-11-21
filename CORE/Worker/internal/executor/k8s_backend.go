package executor

// K8sBackend — k8s-reduction-engine T2. Translates a TaskSpec into a single
// K8s Job (restartPolicy=Never, backoffLimit=0, 1 pod) and implements the
// Seam-1 Backend contract (backend.go / design.md) against it.
//
// Task I/O is unchanged: the pod receives the same S3 creds + API_URL env
// the Docker path injects and pushes its outputs to S3 itself, so no PVC is
// mounted here.
//
// Egress: the pod is stamped with TaskPodSelectorKey=TaskPodSelectorValue plus
// `mod.tech/egress-mode: deny|user-egress`, the exact podSelector labels of
// CORE/Worker/k8s/networkpolicy/{default-deny-egress,user-egress}.yaml. The
// selector key/value defaults below MUST match the MOD_TASK_POD_SELECTOR_KEY /
// MOD_TASK_POD_SELECTOR_VALUE those manifests are rendered with.

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

const (
	k8sBackendName = "k8s"

	// Labels.
	LabelPartOf      = "app.kubernetes.io/part-of"
	LabelPartOfValue = "mod-core"
	LabelTask        = "mod.task"
	LabelJob         = "mod.job" // our own job→pod link (fake clients don't set batch.kubernetes.io/job-name)
	LabelEgressMode  = "mod.tech/egress-mode"
	EgressModeDeny   = "deny"
	EgressModeUser   = "user-egress"

	// AnnotationEgressAllowlist (GB.EGR.3) mirrors CORE/API/services/
	// k8s_stack_netpol.py's ANNOTATION_EGRESS_ALLOWLIST exactly: it marks a
	// TaskSpec.EgressAllowlist pod that landed on plain user-egress (no
	// per-host CiliumNetworkPolicy) because the cluster isn't running Cilium
	// (FQDNMode != "cilium"). Fail-open by design (GB.EGR §6a): the task still
	// runs, just unenforced, rather than fail-closed with no Cilium to enforce
	// against.
	AnnotationEgressAllowlist = "mod.tech/egress-allowlist"
	EgressAllowlistUnenforced = "unenforced"

	// LabelTenantID (W9) stamps the task's tenant on the pod so a tenant
	// stack namespace's `mod-tenant-isolation` NetworkPolicy can admit ONLY
	// that tenant's task pods. Omitted when the tenant is unknown — such a
	// pod then reaches no tenant stack (fail-closed).
	LabelTenantID = "mod.tech/tenant-id"

	DefaultTaskPodSelectorKey   = "mod.tech/task-pod"
	DefaultTaskPodSelectorValue = "true"

	// k8sAffinityHostnameLabel is the key the API's merge_affinity (services/
	// k8s_node_routing.py HOSTNAME_LABEL_KEY) uses in its preferred terms —
	// the dedupe key when appending them to an existing affinity.
	k8sAffinityHostnameLabel = "kubernetes.io/hostname"

	// GPU node-selector contract (gpu-sharing README).
	LabelDevicePluginConfig = "nvidia.com/device-plugin.config"
	DevicePluginExclusive   = "exclusive"
	ResourceGPU             = corev1.ResourceName("nvidia.com/gpu")
	ResourceMIGPrefix       = "nvidia.com/mig-"

	DefaultJobTTLSeconds    int32 = 3600
	DefaultPendingFailAfter       = 2 * time.Minute
	k8sTaskContainerName          = "task"

	// FQDNMode values (GB.EGR.4 selects one into K8sBackend.FQDNMode based on
	// MOD_K8S_EGRESS_FQDN + Cilium CRD discovery).
	FQDNModeOff    = "off"
	FQDNModeCilium = "cilium"

	// SidecarSamplerImageTunableKey (1680) is the platform-settings key an
	// operator uses to point the GPU-sampler sidecar at their own mirror of
	// the image (same GS.PSR.7 heartbeat-`tunables` delivery as
	// worker.gpu_tenant_scrub_image in gpu_tenant_scrub.go). No new env var.
	SidecarSamplerImageTunableKey = "worker.k8s_sidecar_sampler_image"
)

// pendingFailureReasons are container "waiting" reasons that mean the pod will
// not make progress on its own. They are reported as TaskFailed once they have
// persisted for PendingFailAfter (ErrImagePull flaps with ImagePullBackOff).
var pendingFailureReasons = map[string]bool{
	"ImagePullBackOff":           true,
	"ErrImagePull":               true,
	"InvalidImageName":           true,
	"CreateContainerConfigError": true,
	"CreateContainerError":       true,
}

// K8sBackend runs ephemeral node tasks as K8s Jobs. One instance is bound to
// one namespace (namespace-per-tenant); build one per tenant namespace.
type K8sBackend struct {
	Client    kubernetes.Interface
	Namespace string

	// Dynamic is the dynamic client used to create the per-task
	// CiliumNetworkPolicy (GB.EGR.2/GB.EGR.3) for EgressAllowlist tasks when
	// FQDNMode == FQDNModeCilium. nil when FQDNMode == FQDNModeOff (no CNP is
	// ever created in that mode, so a nil Dynamic is safe).
	Dynamic dynamic.Interface
	// FQDNMode is FQDNModeCilium or FQDNModeOff (GB.EGR.4 selects it from
	// MOD_K8S_EGRESS_FQDN + Cilium CRD discovery). Zero value behaves as
	// FQDNModeOff (NewK8sBackend sets it explicitly so callers that build a
	// K8sBackend by hand still get the safe default).
	FQDNMode string
	// DNSNamespace is where kube-dns lives, for the CNP's DNS-egress rule
	// (BuildFQDNPolicy's dnsNamespace param). NewK8sBackend defaults this to
	// DefaultFQDNDNSNamespace ("kube-system").
	DNSNamespace string

	// TaskPodSelectorKey/Value is the task-pod label the NetworkPolicies select on.
	TaskPodSelectorKey   string
	TaskPodSelectorValue string

	// Count==1 whole-GPU placement policy. The README contract only REQUIRES
	// the exclusive selector for Count>1 (time-sliced nodes reject >1 at
	// allocation). For Count==1 the default is NO selector — the task may land
	// on an exclusive or a time-sliced node. Set SingleGPUNodeSelector (e.g.
	// "exclusive" or "time-sliced-4") to pin Count==1 tasks explicitly.
	SingleGPUNodeSelector string

	// GPUCaps (W21) caches node-derived GPU capabilities for placement
	// (k8s_gpu_placement.go). nil / empty cluster → gpuResources as before.
	GPUCaps *GPUCapsCache

	// ClusterCaps (18.5) is the TARGET cluster's stored cluster_caps dict
	// (K8sClusterConfig.cluster_caps on the API, JSONB: modes / mig_profiles /
	// gpu_memory_gb / prometheus_url / probe_error). Only the
	// prometheus_url key is read here, by sidecarSamplerNeeded (k8s_sidecar_sampler.go),
	// to decide whether the GPU-sampler sidecar is attached. nil = the worker
	// has no caps for this cluster (older API / probe never ran) → NO sidecar:
	// the sampler image is a placeholder the builder lane has not shipped yet,
	// so it is only attached when the cluster registry positively says
	// "this cluster has no Prometheus". A caps dict WITHOUT a usable
	// prometheus_url is the sidecar path, matching the API's
	// prometheus_url_from_caps gate.
	ClusterCaps map[string]interface{}

	// SidecarSamplerImage is the GPU-sampler sidecar's image (1680). NewK8sBackend
	// defaults it to SidecarSamplerImage (k8s_sidecar_sampler.go); an operator
	// whose fleet mirrors that image under their own registry host overrides it
	// with the worker.gpu_tenant_scrub-style platform-settings tunable
	// `worker.k8s_sidecar_sampler_image`, delivered on the heartbeat `tunables`
	// map (same GS.PSR.7 mechanism gpu_tenant_scrub.go's
	// worker.gpu_tenant_scrub_image uses) via SetTunables. Empty/blank = default.
	// BuildJob reads it fresh per task, so a Dashboard change applies mid-flight.
	SidecarSamplerImage string
	// Tunables is the worker's shared platform-settings store (nil until
	// SetTunables runs, exactly like Executor.SetTunables / NativeBackend.SetTunables).
	Tunables *tunables.Store

	TTLSecondsAfterFinished int32
	// PendingFailAfter is how long an image-pull/config error or an
	// Unschedulable pod may persist before Poll reports TaskFailed.
	PendingFailAfter time.Duration

	now func() time.Time
}

var _ Backend = (*K8sBackend)(nil)

// NewK8sBackend builds a K8sBackend for one namespace. Callers load the
// clientset via internal/k8sclient.LoadClientset(cfg.KubeconfigPath) when
// cfg.IsK8sExecutorBackend(); tests pass k8s.io/client-go/kubernetes/fake.
func NewK8sBackend(client kubernetes.Interface, namespace string) *K8sBackend {
	if namespace == "" {
		namespace = "default"
	}
	return &K8sBackend{
		Client: client,
		// 1680: the sampler image is a field, not just the package constant, so
		// an operator with their own mirror can override it (sidecarSamplerImage()
		// re-reads the platform-settings tunable fresh per task). Default = the
		// constant from k8s_sidecar_sampler.go (same package).
		SidecarSamplerImage:     SidecarSamplerImage,
		Namespace:               namespace,
		FQDNMode:                FQDNModeOff,
		DNSNamespace:            DefaultFQDNDNSNamespace,
		TaskPodSelectorKey:      DefaultTaskPodSelectorKey,
		TaskPodSelectorValue:    DefaultTaskPodSelectorValue,
		TTLSecondsAfterFinished: DefaultJobTTLSeconds,
		PendingFailAfter:        DefaultPendingFailAfter,
		GPUCaps:                 NewGPUCapsCache(client),
		now:                     time.Now,
	}
}

// SetTunables wires the shared platform-settings store this backend consults
// for the GPU-sampler sidecar image (1680, GS.PSR.7). Optional: with no store
// the sidecar keeps its default image.
func (b *K8sBackend) SetTunables(t *tunables.Store) {
	b.Tunables = t
}

// sidecarSamplerImage resolves the sidecar's image for ONE task: the
// platform-settings tunable (worker.k8s_sidecar_sampler_image, delivered on
// the heartbeat `tunables` map) wins when set; otherwise the field
// (NewK8sBackend's default = SidecarSamplerImage); otherwise the constant.
// Read fresh per BuildJob, like every other tunable reader, so an operator
// retargeting their mirror applies mid-flight without restarting the worker.
func (b *K8sBackend) sidecarSamplerImage() string {
	if b.Tunables != nil {
		if v := b.Tunables.String(SidecarSamplerImageTunableKey, "", ""); v != "" {
			return v
		}
	}
	if s := strings.TrimSpace(b.SidecarSamplerImage); s != "" {
		return s
	}
	return SidecarSamplerImage
}

func (b *K8sBackend) clock() time.Time {
	if b.now != nil {
		return b.now()
	}
	return time.Now()
}

var dnsUnsafe = regexp.MustCompile(`[^a-z0-9-]+`)

// EnvTenantID is the authoritative tenant carrier TaskSpecFromTask sets from
// the claimed task's tenant_id; TENANT_ID (set by the API in container_env on
// most dispatch paths) is the fallback.
const EnvTenantID = "MOD_TENANT_ID"

// tenantLabelFromEnv returns the sanitised tenant label value, or "" if unknown.
func tenantLabelFromEnv(env map[string]string) string {
	for _, k := range []string{EnvTenantID, "TENANT_ID"} {
		if v := labelValue(strings.TrimSpace(env[k]), 63); v != "" {
			return v
		}
	}
	return ""
}

// taskIDFromEnv picks a stable task id from the env the caller already sets;
// falls back to a random id (TaskSpec carries no explicit task id).
func taskIDFromEnv(env map[string]string) string {
	for _, k := range []string{"MOD_TASK_ID", "TASK_ID", "NODE_RUN_ID"} {
		if v := strings.TrimSpace(env[k]); v != "" {
			return v
		}
	}
	buf := make([]byte, 6)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}

// labelValue sanitizes s into a valid label value / DNS-1123 fragment.
func labelValue(s string, max int) string {
	s = dnsUnsafe.ReplaceAllString(strings.ToLower(s), "-")
	if len(s) > max {
		s = s[:max]
	}
	return strings.Trim(s, "-")
}

func jobNameFor(taskID string) string {
	buf := make([]byte, 3)
	_, _ = rand.Read(buf)
	// "mod-task-" (9) + id (<=46) + "-" + 6 hex = <=62 chars.
	id := labelValue(taskID, 46)
	if id == "" {
		id = "x"
	}
	return "mod-task-" + id + "-" + hex.EncodeToString(buf)
}

// gpuResources applies the README resource-name contract.
func (b *K8sBackend) gpuResources(g GPURequest) (corev1.ResourceList, map[string]string) {
	if g.MIGProfile != "" {
		n := g.Count
		if n <= 0 {
			n = 1
		}
		return corev1.ResourceList{
			corev1.ResourceName(ResourceMIGPrefix + g.MIGProfile): *resource.NewQuantity(int64(n), resource.DecimalSI),
		}, nil
	}
	if g.Count <= 0 {
		return nil, nil
	}
	rl := corev1.ResourceList{ResourceGPU: *resource.NewQuantity(int64(g.Count), resource.DecimalSI)}
	switch {
	case g.Count > 1:
		return rl, map[string]string{LabelDevicePluginConfig: DevicePluginExclusive}
	case b.SingleGPUNodeSelector != "":
		return rl, map[string]string{LabelDevicePluginConfig: b.SingleGPUNodeSelector}
	}
	return rl, nil
}

// BuildJob renders the Job for spec (exported for inspection/tests; no API call).
func (b *K8sBackend) BuildJob(spec *TaskSpec) *batchv1.Job {
	taskID := taskIDFromEnv(spec.Env)
	name := jobNameFor(taskID)

	// GB.EGR.3: EgressAllowlist narrows to a per-task CiliumNetworkPolicy
	// (Submit, below) ONLY when the cluster runs Cilium — the pod itself
	// still just needs to reach user-egress (deny would block it before the
	// CNP narrows it). Without Cilium there is nothing to enforce the
	// allowlist with; rather than fail-closed (denying a task the operator
	// asked to reach specific hosts), it runs on unenforced user-egress with
	// an audit annotation (GB.EGR §6a amendment).
	var annotations map[string]string
	egress := EgressModeDeny
	switch spec.Egress {
	case EgressUserEgress:
		egress = EgressModeUser
	case EgressAllowlist:
		if b.FQDNMode == FQDNModeCilium {
			egress = EgressModeDeny // narrowed by the CNP created in Submit
		} else {
			egress = EgressModeUser
			annotations = map[string]string{AnnotationEgressAllowlist: EgressAllowlistUnenforced}
		}
	}
	selKey, selVal := b.TaskPodSelectorKey, b.TaskPodSelectorValue
	if selKey == "" {
		selKey, selVal = DefaultTaskPodSelectorKey, DefaultTaskPodSelectorValue
	}
	labels := map[string]string{
		LabelPartOf:     LabelPartOfValue,
		LabelTask:       labelValue(taskID, 63),
		LabelJob:        name,
		LabelEgressMode: egress,
		selKey:          selVal,
	}
	if tenant := tenantLabelFromEnv(spec.Env); tenant != "" {
		labels[LabelTenantID] = tenant
	}

	env := make([]corev1.EnvVar, 0, len(spec.Env))
	for _, k := range sortedKeys(spec.Env) {
		env = append(env, corev1.EnvVar{Name: k, Value: spec.Env[k]})
	}

	limits := corev1.ResourceList{}
	if spec.Resources.CPUCores > 0 {
		limits[corev1.ResourceCPU] = *resource.NewMilliQuantity(int64(spec.Resources.CPUCores*1000), resource.DecimalSI)
	}
	if spec.Resources.MemoryMB > 0 {
		limits[corev1.ResourceMemory] = *resource.NewQuantity(spec.Resources.MemoryMB*1024*1024, resource.BinarySI)
	}

	// 14.13 (D-GPU Layer 2): when the API shipped a k8s_gpu fragment, it is the
	// AUTHORITATIVE GPU demand (translated against the target cluster's stored
	// GI.2 probe), so it is applied verbatim on top of the CPU/mem limits and
	// the structural labels/annotations and drives the nodeSelector +
	// schedulerName. The worker's own node-derived GPU placement
	// (placedGPUResources, W21) is the ABSENT-block fallback and is skipped —
	// running both would double-request GPUs. Existing CPU/mem limits and every
	// structural label/annotation are never dropped.
	var nodeSel map[string]string
	var schedulerName string
	var affinity *corev1.Affinity
	if spec.K8sGPU != nil {
		for name, raw := range spec.K8sGPU.Resources.Limits {
			if q, err := parseResourceQuantity(raw); err == nil {
				limits[corev1.ResourceName(name)] = q
			}
		}
		if len(spec.K8sGPU.NodeSelector) > 0 {
			nodeSel = map[string]string{}
			for k, v := range spec.K8sGPU.NodeSelector {
				nodeSel[k] = v
			}
		}
		schedulerName = spec.K8sGPU.SchedulerName
		labels = mergeStringMap(labels, spec.K8sGPU.Labels)
		annotations = mergeStringMap(annotations, spec.K8sGPU.Annotations)
		// GI.6: the API's node-routing preferred terms (soft nodeAffinity).
		// base is nil here — this Job builder sets no affinity of its own —
		// but the merge keeps the rules symmetric with merge_affinity on the
		// API side: append preferred, never replace required, never touch
		// nodeSelector.
		affinity = mergeK8sAffinity(nil, spec.K8sGPU.Affinity)
	} else {
		gpu, sel := b.placedGPUResources(spec)
		for k, v := range gpu {
			limits[k] = v
		}
		nodeSel = sel
	}

	container := corev1.Container{
		Name:            k8sTaskContainerName,
		Image:           spec.Image,
		Args:            spec.Cmd, // Cmd overrides the image CMD, like Docker's Cmd (ENTRYPOINT kept)
		Env:             env,
		ImagePullPolicy: corev1.PullIfNotPresent,
	}
	if len(limits) > 0 {
		container.Resources.Limits = limits
	}

	// containers[0] is always the task container; the 18.5 sidecar (below) is
	// appended after it. Declared here so the dshm mount below can target it.
	containers := []corev1.Container{container}

	var volumes []corev1.Volume
	if spec.Resources.ShmSizeMB > 0 {
		size := resource.NewQuantity(spec.Resources.ShmSizeMB*1024*1024, resource.BinarySI)
		volumes = append(volumes, corev1.Volume{
			Name: "dshm",
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{
				Medium: corev1.StorageMediumMemory, SizeLimit: size,
			}},
		})
		containers[0].VolumeMounts = append(containers[0].VolumeMounts, corev1.VolumeMount{Name: "dshm", MountPath: "/dev/shm"})
	}

	// 18.5: no Prometheus URL in the target cluster's stored caps → attach the
	// GPU-sampler sidecar (k8s_sidecar_sampler.go) plus its shared emptyDir, so
	// the API's read-back (parse_sidecar_samples) still learns peak VRAM + util.
	// The mount goes on BOTH containers: the sampler writes there, the task
	// container sees the same file. Completion accounting stays on the task
	// container (Parallelism/Completions = 1 below is untouched).
	if sidecarSamplerNeeded(b.ClusterCaps) {
		containers[0].VolumeMounts = append(containers[0].VolumeMounts, sidecarSamplerMount())
		containers = append(containers, sidecarSamplerContainer(b.sidecarSamplerImage(), SidecarSampleIntervalS))
		volumes = append(volumes, sidecarSamplerVolume())
	}

	backoff := int32(0)
	one := int32(1)
	ttl := b.TTLSecondsAfterFinished
	automount := false
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: b.Namespace, Labels: labels, Annotations: annotations},
		Spec: batchv1.JobSpec{
			BackoffLimit:            &backoff,
			Parallelism:             &one,
			Completions:             &one,
			TTLSecondsAfterFinished: &ttl,
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels, Annotations: annotations},
				Spec: corev1.PodSpec{
					RestartPolicy:                corev1.RestartPolicyNever,
					AutomountServiceAccountToken: &automount,
					NodeSelector:                 nodeSel,
					Affinity:                     affinity,
					SchedulerName:                schedulerName,
					Containers:                   containers,
					Volumes:                      volumes,
				},
			},
		},
	}
}

// mergeStringMap returns base with kvs merged in (a kvs key overrides a base
// key). Returns base when kvs is empty so a nil base stays nil. Used to apply
// the 14.13 k8s_gpu labels/annotations on top of the structural ones without
// dropping the existing ones.
func mergeStringMap(base, kvs map[string]string) map[string]string {
	if len(kvs) == 0 {
		return base
	}
	if base == nil {
		base = map[string]string{}
	}
	for k, v := range kvs {
		base[k] = v
	}
	return base
}

// mergeK8sAffinity (GI.6) merges the API-shipped affinity (the
// k8s_node_routing.py merge_affinity output — soft hostname preferred terms)
// onto an existing affinity, with the same rules that side uses:
//   - only nodeAffinity preferredDuringSchedulingIgnoredDuringExecution terms
//     are appended; required terms are never touched or replaced;
//   - terms are deduped by their kubernetes.io/hostname values — an existing
//     term wins over an incoming one for the same hostname;
//   - nodeSelector is never touched (it lives outside Affinity);
//   - incoming nil/empty returns base unchanged; an EMPTY base returns
//     incoming unchanged.
//
// A deep copy of the incoming affinity is made before merging (via its JSON
// form) so a fragment carried by more than one task attempt can never be
// mutated.
func mergeK8sAffinity(base, incoming *corev1.Affinity) *corev1.Affinity {
	if incoming == nil {
		return base
	}
	in := &corev1.Affinity{}
	if raw, err := json.Marshal(incoming); err == nil && len(raw) > 0 {
		_ = json.Unmarshal(raw, in)
	} else {
		in = incoming
	}
	if in.NodeAffinity == nil || len(in.NodeAffinity.PreferredDuringSchedulingIgnoredDuringExecution) == 0 {
		return base
	}
	if base == nil {
		return in
	}
	out := *base
	var na corev1.NodeAffinity
	if base.NodeAffinity != nil {
		na = *base.NodeAffinity
		// Copy the preferred slice (not just its header) so appending below
		// can never write into the caller's backing array.
		pref := make([]corev1.PreferredSchedulingTerm, len(na.PreferredDuringSchedulingIgnoredDuringExecution))
		copy(pref, na.PreferredDuringSchedulingIgnoredDuringExecution)
		na.PreferredDuringSchedulingIgnoredDuringExecution = pref
	}
	out.NodeAffinity = &na
	taken := map[string]struct{}{}
	for _, term := range na.PreferredDuringSchedulingIgnoredDuringExecution {
		for _, expr := range term.Preference.MatchExpressions {
			if expr.Key == k8sAffinityHostnameLabel {
				for _, v := range expr.Values {
					taken[v] = struct{}{}
				}
			}
		}
	}
	for _, term := range in.NodeAffinity.PreferredDuringSchedulingIgnoredDuringExecution {
		hostnames := []string{}
		for _, expr := range term.Preference.MatchExpressions {
			if expr.Key == k8sAffinityHostnameLabel {
				hostnames = append(hostnames, expr.Values...)
			}
		}
		if len(hostnames) == 0 {
			continue
		}
		add := true
		for _, v := range hostnames {
			if _, ok := taken[v]; ok {
				add = false
				break
			}
		}
		if add {
			na.PreferredDuringSchedulingIgnoredDuringExecution = append(na.PreferredDuringSchedulingIgnoredDuringExecution, term)
			for _, v := range hostnames {
				taken[v] = struct{}{}
			}
		}
	}
	return &out
}

// parseResourceQuantity converts a JSON-quantity value (a number, or a string
// such as "0.5" for KAI gpu-fraction) into a corev1 quantity. K8s quantities
// accept plain integers and decimals; anything that does not parse is skipped
// by the caller (a malformed entry never breaks the whole Job).
func parseResourceQuantity(raw interface{}) (resource.Quantity, error) {
	switch v := raw.(type) {
	case string:
		return resource.ParseQuantity(v)
	case float64:
		if v == math.Trunc(v) {
			return *resource.NewQuantity(int64(v), resource.DecimalSI), nil
		}
		return resource.ParseQuantity(strconv.FormatFloat(v, 'f', -1, 64))
	case int64:
		return *resource.NewQuantity(v, resource.DecimalSI), nil
	case int:
		return *resource.NewQuantity(int64(v), resource.DecimalSI), nil
	case int32:
		return *resource.NewQuantity(int64(v), resource.DecimalSI), nil
	default:
		return resource.Quantity{}, fmt.Errorf("unsupported quantity %T", raw)
	}
}

func sortedKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	// small maps; insertion sort keeps deps minimal
	for i := 1; i < len(keys); i++ {
		for j := i; j > 0 && keys[j] < keys[j-1]; j-- {
			keys[j], keys[j-1] = keys[j-1], keys[j]
		}
	}
	return keys
}

// Submit creates the Job. It does not wait for the pod.
func (b *K8sBackend) Submit(ctx context.Context, spec *TaskSpec) (*Handle, error) {
	if spec == nil || spec.Image == "" {
		return nil, errors.New("k8s backend: spec.Image is required")
	}
	if spec.GPU.Count > 0 && spec.GPU.MIGProfile == "" {
		b.GPUCaps.Refresh(ctx) // cached (TTL); failure degrades to whole GPU
	}
	job := b.BuildJob(spec)
	created, err := b.Client.BatchV1().Jobs(b.Namespace).Create(ctx, job, metav1.CreateOptions{})
	if err != nil {
		return nil, fmt.Errorf("k8s backend: create job: %w", err)
	}
	if b.FQDNMode == FQDNModeCilium && spec.Egress == EgressAllowlist {
		if err := b.createFQDNPolicy(ctx, created, spec.EgressHosts); err != nil {
			// The pod is stamped egress-mode=deny for this verdict (BuildJob)
			// specifically because the CNP is what's supposed to open it back
			// up to the allowlist — a Job with no CNP would just hang deny-all
			// forever. Fail closed: delete the Job rather than leave an
			// unenforced/broken task running.
			bg := metav1.DeletePropagationBackground
			if delErr := b.Client.BatchV1().Jobs(created.Namespace).Delete(ctx, created.Name, metav1.DeleteOptions{PropagationPolicy: &bg}); delErr != nil && !apierrors.IsNotFound(delErr) {
				return nil, fmt.Errorf("k8s backend: fqdn policy: %w (job cleanup also failed: %v)", err, delErr)
			}
			return nil, fmt.Errorf("k8s backend: fqdn policy: %w", err)
		}
	}
	return &Handle{Backend: k8sBackendName, ID: created.Namespace + "/" + created.Name}, nil
}

// createFQDNPolicy renders (BuildFQDNPolicy) and creates the CiliumNetworkPolicy
// that narrows job's pod (already on user-egress via egress-mode=deny + this
// CNP, see BuildJob) down to hosts. Owned by the Job so it is garbage
// collected with it (belt-and-suspenders alongside the explicit delete on
// Submit's error path and Cancel's Job delete).
func (b *K8sBackend) createFQDNPolicy(ctx context.Context, job *batchv1.Job, hosts []string) error {
	if b.Dynamic == nil {
		return errors.New("k8s backend: FQDNMode is cilium but Dynamic client is nil")
	}
	obj, err := BuildFQDNPolicy(job.Namespace, job.Name, job.UID, hosts, b.DNSNamespace)
	if err != nil {
		return err
	}
	if _, err := b.Dynamic.Resource(CiliumNetworkPolicyGVR).Namespace(job.Namespace).Create(ctx, obj, metav1.CreateOptions{}); err != nil {
		return fmt.Errorf("create CiliumNetworkPolicy: %w", err)
	}
	return nil
}

func parseHandle(h *Handle) (ns, name string, err error) {
	if h == nil || h.Backend != k8sBackendName {
		return "", "", errors.New("k8s backend: handle is not a k8s handle")
	}
	parts := strings.SplitN(h.ID, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", fmt.Errorf("k8s backend: malformed handle id %q (want namespace/job)", h.ID)
	}
	return parts[0], parts[1], nil
}

func (b *K8sBackend) jobPods(ctx context.Context, ns, job string) ([]corev1.Pod, error) {
	list, err := b.Client.CoreV1().Pods(ns).List(ctx, metav1.ListOptions{LabelSelector: LabelJob + "=" + job})
	if err != nil {
		return nil, err
	}
	return list.Items, nil
}

// Poll maps Job conditions + pod state to a TaskStatus.
func (b *K8sBackend) Poll(ctx context.Context, h *Handle) (*TaskStatus, error) {
	ns, name, err := parseHandle(h)
	if err != nil {
		return nil, err
	}
	job, err := b.Client.BatchV1().Jobs(ns).Get(ctx, name, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return &TaskStatus{Phase: TaskFailed, ExitCode: -1, Err: "job not found (deleted or TTL-expired)"}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("k8s backend: get job: %w", err)
	}
	pods, err := b.jobPods(ctx, ns, name)
	if err != nil {
		return nil, fmt.Errorf("k8s backend: list pods: %w", err)
	}
	// W3: staged pods (stage-in/task/stage-out) are judged per container so
	// the task container's exit never reports success before stage-out ends.
	if st, ok := b.stagedPollVerdict(ctx, ns, pods); ok {
		return st, nil
	}

	for _, c := range job.Status.Conditions {
		if c.Status != corev1.ConditionTrue {
			continue
		}
		switch c.Type {
		case batchv1.JobComplete, batchv1.JobSuccessCriteriaMet:
			return &TaskStatus{Phase: TaskSucceeded, ExitCode: 0}, nil
		case batchv1.JobFailed, batchv1.JobFailureTarget:
			st := &TaskStatus{Phase: TaskFailed, ExitCode: -1, Err: joinReason(c.Reason, c.Message)}
			if code, reason, ok := terminatedState(pods); ok {
				st.ExitCode = code
				if reason != "" {
					st.Err = joinReason(st.Err, reason)
				}
			}
			return st, nil
		}
	}
	// Job controller may lag the pod; trust a terminated container directly.
	if code, reason, ok := terminatedState(pods); ok {
		if code == 0 {
			return &TaskStatus{Phase: TaskSucceeded}, nil
		}
		return &TaskStatus{Phase: TaskFailed, ExitCode: code, Err: joinReason("container exited", reason)}, nil
	}
	if job.Status.Succeeded > 0 {
		return &TaskStatus{Phase: TaskSucceeded}, nil
	}
	if job.Status.Failed > 0 {
		return &TaskStatus{Phase: TaskFailed, ExitCode: -1, Err: "job pod failed"}, nil
	}
	if reason := b.stuckReason(pods); reason != "" {
		return &TaskStatus{Phase: TaskFailed, ExitCode: -1, Err: reason}, nil
	}
	// Pending (incl. transient pull/schedule errors) and Running both report running.
	return &TaskStatus{Phase: TaskRunning}, nil
}

func joinReason(a, b string) string {
	switch {
	case a == "":
		return b
	case b == "":
		return a
	}
	return a + ": " + b
}

// terminatedState returns the task container's terminated exit code, if any.
func terminatedState(pods []corev1.Pod) (int, string, bool) {
	for _, p := range pods {
		for _, cs := range p.Status.ContainerStatuses {
			if t := cs.State.Terminated; t != nil {
				reason := t.Reason
				if t.Message != "" {
					reason = joinReason(reason, t.Message)
				}
				return int(t.ExitCode), reason, true
			}
		}
		if p.Status.Phase == corev1.PodFailed && p.Status.Reason != "" {
			// e.g. Evicted / DeadlineExceeded with no container status
			return -1, joinReason(p.Status.Reason, p.Status.Message), true
		}
	}
	return 0, "", false
}

// stuckReason reports a non-progressing pod (image pull / config error /
// Unschedulable) that has persisted for PendingFailAfter.
func (b *K8sBackend) stuckReason(pods []corev1.Pod) string {
	grace := b.PendingFailAfter
	if grace <= 0 {
		grace = DefaultPendingFailAfter
	}
	now := b.clock()
	for _, p := range pods {
		age := now.Sub(p.CreationTimestamp.Time)
		for _, cs := range p.Status.ContainerStatuses {
			if w := cs.State.Waiting; w != nil && pendingFailureReasons[w.Reason] && age >= grace {
				return joinReason(w.Reason, w.Message)
			}
		}
		for _, c := range p.Status.Conditions {
			if c.Type == corev1.PodScheduled && c.Status == corev1.ConditionFalse &&
				c.Reason == corev1.PodReasonUnschedulable {
				since := c.LastTransitionTime.Time
				if since.IsZero() {
					since = p.CreationTimestamp.Time
				}
				if now.Sub(since) >= grace {
					return joinReason("Unschedulable", c.Message)
				}
			}
		}
	}
	return ""
}

// Logs streams the task container's logs from the Job's pod.
func (b *K8sBackend) Logs(ctx context.Context, h *Handle) (io.ReadCloser, error) {
	ns, name, err := parseHandle(h)
	if err != nil {
		return nil, err
	}
	pods, err := b.jobPods(ctx, ns, name)
	if err != nil {
		return nil, fmt.Errorf("k8s backend: list pods: %w", err)
	}
	if len(pods) == 0 {
		return nil, fmt.Errorf("k8s backend: no pod yet for job %s", h.ID)
	}
	// backoffLimit=0 ⇒ at most one pod; pick the newest defensively.
	pod := pods[0]
	for _, p := range pods[1:] {
		if p.CreationTimestamp.After(pod.CreationTimestamp.Time) {
			pod = p
		}
	}
	return b.Client.CoreV1().Pods(ns).GetLogs(pod.Name, &corev1.PodLogOptions{Container: k8sTaskContainerName}).Stream(ctx)
}

// Cancel deletes the Job (and, via Background propagation, its pod). Idempotent.
func (b *K8sBackend) Cancel(ctx context.Context, h *Handle) error {
	ns, name, err := parseHandle(h)
	if err != nil {
		return err
	}
	bg := metav1.DeletePropagationBackground
	err = b.Client.BatchV1().Jobs(ns).Delete(ctx, name, metav1.DeleteOptions{PropagationPolicy: &bg})
	if err != nil && !apierrors.IsNotFound(err) {
		return fmt.Errorf("k8s backend: delete job: %w", err)
	}
	return nil
}
