package executor

// 18.5 (launch-completion-megaplan): the k8s GPU-sampler sidecar SPEC.
//
// 14.14 taught the API to fold the sidecar's raw output into a completed task's
// peak_vram_mb (services/k8s_task_metrics.py parse_sidecar_samples +
// readback_for_completed_task) when the cluster's Prometheus read path has
// nothing to read. This file is the worker half: K8sBackend's Job builder
// (k8s_backend.go BuildJob) appends the sampler container plus its shared
// emptyDir volume to the task Job ONLY when the target cluster's stored caps
// carry no Prometheus URL — the same gate the API applies
// (k8s_task_metrics.prometheus_url_from_caps: absent / non-http(s) -> None ->
// callers take the sidecar path).
//
// The dicts here MIRROR the Python constants of the same name in
// services/k8s_task_metrics.py (SIDECAR_SAMPLER_NAME / IMAGE / MOUNT_PATH,
// SIDECAR_SAMPLE_FILE_NAME / INTERVAL_S, SIDECAR_SAMPLER_COMMAND / VOLUME /
// CONTAINER) verbatim: name, image, mount path, file, interval, command,
// volume, env, resource requests/limits. Keep the two in step when either
// changes — the read-back parser is the API's, so the FORMAT is the API's
// contract: one `<epoch> <vram_used_mb> <util_pct>` line per second
// (parse_sidecar_samples splits on whitespace/commas and reads fields 2 and 3).
//
// Best-effort by design: the sampler runs ALONGSIDE the `task` container and
// must never decide the task's outcome, so BuildJob leaves the Job's
// completion accounting (Parallelism/Completions = 1) on the task container —
// the sidecar is not a completion signal.

import (
	"strconv"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
)

// Identical values to the Python side (see header).
const (
	SidecarSamplerName      = "mod-gpu-sampler"
	SidecarSamplerImage     = "registry.example.com/mod/gpu-sampler:latest"
	SidecarSamplerMountPath = "/tmp/mod-gpu-samples"
	SidecarSampleFileName   = "samples.csv"
	SidecarSampleIntervalS  = 1
	SidecarSamplerVolume    = "mod-gpu-samples"
	SidecarIntervalEnvVar   = "MOD_GPU_SAMPLER_INTERVAL_S"
)

// Caps key the gate reads — cluster_caps["prometheus_url"] on the API's
// K8sClusterConfig JSONB (services/k8s_cluster_caps.probe_cluster_caps).
const CapsPrometheusURLKey = "prometheus_url"

// sidecarSamplePath is the file the sampler appends to:
// /tmp/mod-gpu-samples/samples.csv.
func sidecarSamplePath() string {
	return SidecarSamplerMountPath + "/" + SidecarSampleFileName
}

// sidecarSamplerCommand mirrors SIDECAR_SAMPLER_COMMAND byte-for-byte: a 1 Hz
// loop that stamps an epoch second, then asks nvidia-smi for memory.used +
// utilization.gpu in comma-free form (awk -F', *' + print $1, $2), appending
// one `<epoch> <vram_used_mb> <util_pct>` line per iteration to the shared
// file. No GPU / no nvidia-smi -> the loop writes nothing and parse_sidecar_samples
// returns None, the same "unmeasured" outcome as no sidecar at all.
func sidecarSamplerCommand(intervalS int) string {
	iv := SidecarSampleIntervalS
	if intervalS > 0 {
		iv = intervalS
	}
	return "while true; do " +
		"printf '%s ' \"$(date +%s.%N)\"; " +
		"nvidia-smi --query-gpu=memory.used,utilization.gpu " +
		"--format=csv,noheader,nounits | awk -F', *' '{print $1, $2}'; " +
		"sleep " + strconv.Itoa(iv) + "; " +
		"done >> " + sidecarSamplePath()
}

// sidecarSamplerMount is the emptyDir mount appended to BOTH the task
// container and the sampler: the task container gets it so a worker that
// wants to tail its own samples can, and the sampler writes there.
func sidecarSamplerMount() corev1.VolumeMount {
	return corev1.VolumeMount{Name: SidecarSamplerVolume, MountPath: SidecarSamplerMountPath}
}

// sidecarSamplerVolume mirrors SIDECAR_SAMPLER_VOLUME: a plain emptyDir, so
// the samples live on a pod-local disk that survives for the pod's lifetime.
func sidecarSamplerVolume() corev1.Volume {
	return corev1.Volume{
		Name: SidecarSamplerVolume,
		VolumeSource: corev1.VolumeSource{
			EmptyDir: &corev1.EmptyDirVolumeSource{},
		},
	}
}

// sidecarSamplerContainer mirrors SIDECAR_SAMPLER_CONTAINER: tiny cpu/mem
// footprint (10m/16Mi requested, 100m/64Mi capped) so a sampler can never
// OOM or starve the workload it is measuring. The image is a PARAMETER, not
// the constant: SidecarSamplerImage is only the DEFAULT, so an operator whose
// fleet mirrors the sampler under another registry host can point the sidecar
// at their own copy (1680). An empty/blank image falls back to the default.
func sidecarSamplerContainer(image string, intervalS int) corev1.Container {
	img := strings.TrimSpace(image)
	if img == "" {
		img = SidecarSamplerImage
	}
	iv := SidecarSampleIntervalS
	if intervalS > 0 {
		iv = intervalS
	}
	lim := corev1.ResourceList{}
	lim[corev1.ResourceCPU] = *resource.NewMilliQuantity(100, resource.DecimalSI)
	lim[corev1.ResourceMemory] = *resource.NewQuantity(64*1024*1024, resource.BinarySI)
	req := corev1.ResourceList{}
	req[corev1.ResourceCPU] = *resource.NewMilliQuantity(10, resource.DecimalSI)
	req[corev1.ResourceMemory] = *resource.NewQuantity(16*1024*1024, resource.BinarySI)
	return corev1.Container{
		Name:    SidecarSamplerName,
		Image:   img,
		Command: []string{"sh", "-c", sidecarSamplerCommand(iv)},
		Env: []corev1.EnvVar{
			{Name: SidecarIntervalEnvVar, Value: strconv.Itoa(iv)},
		},
		VolumeMounts: []corev1.VolumeMount{sidecarSamplerMount()},
		Resources:    corev1.ResourceRequirements{Requests: req, Limits: lim},
	}
}

// prometheusURLUsable is the Go twin of prometheus_url_from_caps' truthiness:
// the caps dict's prometheus_url must be a string starting with http:// or
// https:// for the Prometheus read path to be usable.
func prometheusURLUsable(caps map[string]interface{}) bool {
	if caps == nil {
		return false
	}
	v, ok := caps[CapsPrometheusURLKey]
	if !ok {
		return false
	}
	switch s := v.(type) {
	case string:
		return strings.HasPrefix(s, "http://") || strings.HasPrefix(s, "https://")
	}
	return false
}

// sidecarSamplerNeeded is the gate BuildJob applies: the target cluster's
// stored caps are KNOWN and carry no usable Prometheus URL -> attach the
// sampler. A malformed URL is treated exactly like an absent one, so a
// cluster whose probe wrote garbage still gets samples.
//
// nil caps = the worker has no cluster_caps for this cluster (older API, or
// the probe never ran) -> NO sidecar: the sampler image is a placeholder the
// builder lane has not shipped yet, and a Job whose sidecar image cannot be
// pulled fails the task, so the spec is only attached when the operator's
// cluster registry positively says "this cluster has no Prometheus".
func sidecarSamplerNeeded(caps map[string]interface{}) bool {
	return caps != nil && !prometheusURLUsable(caps)
}
