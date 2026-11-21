package executor

// 18.5: the GPU-sampler sidecar's Job-builder contract. The read-back half
// (parse_sidecar_samples) is the API's and already tested there; what is
// asserted here is the worker half: WHEN the sidecar is attached (the
// cluster's stored caps have no Prometheus URL) and WHAT it emits (the exact
// `<epoch> <vram_used_mb> <util_pct>` line format + the shared emptyDir both
// containers see).

import (
	"strings"
	"testing"

	batchv1 "k8s.io/api/batch/v1"
	fake "k8s.io/client-go/kubernetes/fake"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

func samplerBackend(t *testing.T, caps map[string]interface{}) *K8sBackend {
	t.Helper()
	b := NewK8sBackend(fake.NewSimpleClientset(), "tenant-7")
	b.ClusterCaps = caps
	return b
}

func samplerJob(t *testing.T, b *K8sBackend, shmMB int64) *batchv1.Job {
	t.Helper()
	job := b.BuildJob(&TaskSpec{
		Image:     "harbor/mod/node:1",
		Env:       map[string]string{"MOD_TASK_ID": "Task_9"},
		Resources: ResourceLimits{ShmSizeMB: shmMB},
	})
	if job == nil {
		t.Fatal("BuildJob returned no Job")
	}
	return job
}

// ── the gate ───────────────────────────────────────────────────────────────

func TestSidecarSamplerAttachedWhenCapsHaveNoPrometheusURL(t *testing.T) {
	b := samplerBackend(t, map[string]interface{}{"gpu_memory_gb": 80.0})
	ps := samplerJob(t, b, 0).Spec.Template.Spec
	if len(ps.Containers) != 2 {
		t.Fatalf("caps without a prometheus_url must attach the sampler: got %d containers", len(ps.Containers))
	}
	sam := ps.Containers[1]
	if sam.Name != SidecarSamplerName || sam.Image != SidecarSamplerImage {
		t.Fatalf("sampler identity: name=%s image=%s", sam.Name, sam.Image)
	}
}

func TestSidecarSamplerAbsentWhenPrometheusURLIsSet(t *testing.T) {
	b := samplerBackend(t, map[string]interface{}{
		CapsPrometheusURLKey: "http://prom.internal:9090/",
	})
	ps := samplerJob(t, b, 0).Spec.Template.Spec
	if len(ps.Containers) != 1 {
		t.Fatalf("a usable prometheus_url must NOT attach the sampler: got %d containers", len(ps.Containers))
	}
}

func TestSidecarSamplerGateMatchesPythonCapsRules(t *testing.T) {
	// The same truthiness table services/k8s_task_metrics.py
	// prometheus_url_from_caps encodes: an http(s) prometheus_url is usable,
	// everything else is not. nil caps is the one place the Go gate
	// deliberately differs from the Python caller (which still takes the
	// sidecar path): the worker does not attach a sampler for a cluster whose
	// caps were never probed, because the sampler image is a placeholder until
	// the builder lane ships it.
	var noCaps map[string]interface{}
	empty := map[string]interface{}{}
	blank := map[string]interface{}{}
	blank[CapsPrometheusURLKey] = ""
	nonHTTP := map[string]interface{}{}
	nonHTTP[CapsPrometheusURLKey] = "ftp://nope"
	numeric := map[string]interface{}{}
	numeric[CapsPrometheusURLKey] = 9090
	probed := map[string]interface{}{}
	probed["probe_error"] = "dns"
	httpCaps := map[string]interface{}{}
	httpCaps[CapsPrometheusURLKey] = "http://prom:9090"
	httpsCaps := map[string]interface{}{}
	httpsCaps[CapsPrometheusURLKey] = "https://prom:9090/metrics"

	// Known caps with no usable URL -> the sampler must be attached.
	if !sidecarSamplerNeeded(empty) || !sidecarSamplerNeeded(blank) ||
		!sidecarSamplerNeeded(nonHTTP) || !sidecarSamplerNeeded(numeric) ||
		!sidecarSamplerNeeded(probed) {
		t.Fatalf("caps with no usable prometheus_url must attach the sampler")
	}
	// A usable URL -> no sampler.
	if sidecarSamplerNeeded(httpCaps) || sidecarSamplerNeeded(httpsCaps) {
		t.Fatalf("a usable prometheus_url must NOT attach the sampler")
	}
	// Unknown caps -> no sampler (the Go-side safety rule).
	if sidecarSamplerNeeded(noCaps) {
		t.Fatalf("caps never probed must NOT attach the sampler")
	}
}

// ── the image override (1680) ──────────────────────────────────────────────

func TestSidecarSamplerImageOverrideReachesJobSpec(t *testing.T) {
	// An operator whose fleet mirrors the sampler under their own registry host
	// must be able to retarget the sidecar: the platform-settings tunable
	// (worker.k8s_sidecar_sampler_image, delivered on the heartbeat tunables map)
	// wins over the default constant, and the value must land in the built Job
	// spec's sampler container.
	b := samplerBackend(t, map[string]interface{}{"gpu_memory_gb": 80.0})
	tun := tunables.New()
	tun.Apply(map[string]interface{}{
		SidecarSamplerImageTunableKey: "mirror.example.com/mod/gpu-sampler:v2",
		"unrelated":                   "ignored",
	})
	b.SetTunables(tun)
	sam := samplerJob(t, b, 0).Spec.Template.Spec.Containers[1]
	if sam.Image != "mirror.example.com/mod/gpu-sampler:v2" {
		t.Fatalf("tunable image must reach the Job spec: got %s", sam.Image)
	}
	if sam.Name != SidecarSamplerName {
		t.Fatalf("override must not change sampler identity: name=%s", sam.Name)
	}
}

func TestSidecarSamplerImageDefaultsWhenTunableUnset(t *testing.T) {
	// No tunable (or a blank one) = the exported default constant, so existing
	// clusters keep the shipped image and the worker needs no config at all.
	b := samplerBackend(t, map[string]interface{}{"gpu_memory_gb": 80.0})
	tun := tunables.New()
	tun.Apply(map[string]interface{}{SidecarSamplerImageTunableKey: "   "})
	b.SetTunables(tun)
	sam := samplerJob(t, b, 0).Spec.Template.Spec.Containers[1]
	if sam.Image != SidecarSamplerImage {
		t.Fatalf("blank tunable must fall back to the default: got %s", sam.Image)
	}
}

// ── the shared emptyDir ────────────────────────────────────────────────────

func TestSidecarSamplerVolumeMountedInBothContainers(t *testing.T) {
	// shm MB > 0 so the Job also carries the dshm volume: the sampler volume
	// must be the SECOND one and both containers must still see it.
	b := samplerBackend(t, map[string]interface{}{"modes": []string{"whole"}})
	ps := samplerJob(t, b, 512).Spec.Template.Spec
	if len(ps.Containers) != 2 || len(ps.Volumes) != 2 {
		t.Fatalf("want 2 containers + 2 volumes, got %d/%d", len(ps.Containers), len(ps.Volumes))
	}
	if ps.Volumes[1].Name != SidecarSamplerVolume || ps.Volumes[1].EmptyDir == nil {
		t.Fatalf("sampler volume: %+v", ps.Volumes[1])
	}
	for i, c := range ps.Containers {
		found := false
		for _, m := range c.VolumeMounts {
			if m.Name == SidecarSamplerVolume && m.MountPath == SidecarSamplerMountPath {
				found = true
			}
		}
		if !found {
			t.Fatalf("container %d (%s) has no sampler mount: %+v", i, c.Name, c.VolumeMounts)
		}
	}
}

// ── the emitted format ─────────────────────────────────────────────────────

func TestSidecarSamplerCommandEmitsDocumentedFormat(t *testing.T) {
	// parse_sidecar_samples' contract: `<epoch> <vram_used_mb> <util_pct>` per
	// line. The command must stamp an epoch, ask nvidia-smi for used-mem +
	// util in comma-free form, and append to the shared file.
	cmd := sidecarSamplerCommand(SidecarSampleIntervalS)
	for _, want := range []string{
		"date +%s.%N",
		"nvidia-smi --query-gpu=memory.used,utilization.gpu",
		"--format=csv,noheader,nounits",
		"awk -F', *' '{print $1, $2}'",
		"sleep 1",
		"done >> " + sidecarSamplePath(),
	} {
		if !strings.Contains(cmd, want) {
			t.Fatalf("sampler command lost %q: %s", want, cmd)
		}
	}
	if sidecarSamplePath() != SidecarSamplerMountPath+"/"+SidecarSampleFileName {
		t.Fatalf("sample path: %s", sidecarSamplePath())
	}
	// The sampler container carries that command verbatim as sh -c.
	b := samplerBackend(t, map[string]interface{}{})
	sam := samplerJob(t, b, 0).Spec.Template.Spec.Containers[1]
	if len(sam.Command) != 3 || sam.Command[0] != "sh" || sam.Command[1] != "-c" ||
		sam.Command[2] != cmd {
		t.Fatalf("sampler command: %+v", sam.Command)
	}
	if len(sam.Env) != 1 || sam.Env[0].Name != SidecarIntervalEnvVar ||
		sam.Env[0].Value != "1" {
		t.Fatalf("sampler env: %+v", sam.Env)
	}
}
