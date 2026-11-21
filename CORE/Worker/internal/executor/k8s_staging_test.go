package executor

import (
	"context"
	"encoding/base64"
	"errors"
	"os/exec"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	k8stesting "k8s.io/client-go/testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func stagingFixture() *K8sStagingSpec {
	return &K8sStagingSpec{
		Image:       "harbor.example/modtim/minio-client:latest",
		MinIO:       transport.MinIOCredentials{MCHost: "http://ak:sk@versitygw:7070", Endpoint: "versitygw:7070", AccessKey: "ak", SecretKey: "sk"},
		DownloadCmd: "true && mc cp myminio/b/h /data/input/input_x.png",
		UploadCmd:   buildUploadCmd(&transport.Task{Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"}}),
	}
}

func stagedSpec() *TaskSpec {
	return &TaskSpec{Image: "harbor.example/nodes/blur:1", Env: map[string]string{"TASK_ID": "42", "MINIO_ACCESS": "ak"}}
}

func envMap(c corev1.Container) map[string]string {
	m := map[string]string{}
	for _, e := range c.Env {
		m[e.Name] = e.Value
	}
	return m
}

func TestK8sStagingPodSpecShape(t *testing.T) {
	b, cs := newTestK8s(t)
	st := stagingFixture()
	h, err := b.SubmitStaged(context.Background(), stagedSpec(), st)
	if err != nil {
		t.Fatalf("SubmitStaged: %v", err)
	}
	ns, name, _ := parseHandle(h)
	job, err := cs.BatchV1().Jobs(ns).Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get job: %v", err)
	}
	ps := job.Spec.Template.Spec
	if ps.ShareProcessNamespace == nil || !*ps.ShareProcessNamespace {
		t.Fatal("staged pod must share the process namespace (stage-out task-exit detection)")
	}
	if len(ps.InitContainers) != 1 || ps.InitContainers[0].Name != k8sStageInContainerName {
		t.Fatalf("want one stage-in initContainer, got %+v", ps.InitContainers)
	}
	if len(ps.Containers) != 2 || ps.Containers[0].Name != k8sTaskContainerName || ps.Containers[1].Name != k8sStageOutContainerName {
		t.Fatalf("want [task stage-out] in order, got %v", []string{ps.Containers[0].Name})
	}
	vols := map[string]corev1.Volume{}
	for _, v := range ps.Volumes {
		vols[v.Name] = v
	}
	if vols[k8sDataVolumeName].EmptyDir == nil {
		t.Fatal("data volume must be an emptyDir")
	}
	if d := vols[k8sSignalVolumeName].DownwardAPI; d == nil || !strings.Contains(d.Items[0].FieldRef.FieldPath, K8sTaskDoneAnnotation) {
		t.Fatal("signal volume must project the task-done annotation")
	}
	for _, c := range append(ps.InitContainers, ps.Containers...) {
		found := false
		for _, m := range c.VolumeMounts {
			if m.Name == k8sDataVolumeName && m.MountPath == "/data" {
				found = true
			}
		}
		if !found {
			t.Fatalf("container %s missing /data mount", c.Name)
		}
	}
	task := ps.Containers[0]
	if task.Image != "harbor.example/nodes/blur:1" || len(task.Command) != 0 {
		t.Fatalf("task container must be unmodified (image/entrypoint), got %q %v", task.Image, task.Command)
	}
	if _, ok := envMap(task)["MC_HOST_myminio"]; ok {
		t.Fatal("task env must come from TaskSpec only (as on Docker), not the staging env")
	}
	for _, c := range []corev1.Container{ps.InitContainers[0], ps.Containers[1]} {
		for _, k := range stagingEnvKeys {
			if ref := secretRefFor(c, k); ref == nil || ref.Name != stagingSecretName(name) || ref.Key != k {
				t.Fatalf("%s: %s must come from the staging secret, got %+v", c.Name, k, ref)
			}
		}
		if c.Image != st.Image {
			t.Fatalf("%s: image %q, want mc image", c.Name, c.Image)
		}
	}
	in := strings.Join(ps.InitContainers[0].Command, " ")
	if !strings.Contains(in, "mkdir -p /data/input /data/output") || !strings.Contains(in, st.DownloadCmd) {
		t.Fatalf("stage-in script missing prep/download: %s", in)
	}
	out := strings.Join(ps.Containers[1].Command, " ")
	for _, want := range []string{"/proc/self/cgroup", "/mod-signal/task-done", "export_result.json", "UPLOAD,", stageOutDoneMarker, "myminio/tenant-1-files/"} {
		if !strings.Contains(out, want) {
			t.Fatalf("stage-out script missing %q", want)
		}
	}
}

func TestK8sSubmitStagedNilIsPlainSubmit(t *testing.T) {
	b, _ := newTestK8s(t)
	j := b.BuildJobStaged(stagedSpec(), nil)
	ps := j.Spec.Template.Spec
	if len(ps.InitContainers) != 0 || len(ps.Containers) != 1 || ps.ShareProcessNamespace != nil {
		t.Fatal("nil staging must produce the unchanged W1 job")
	}
}

func TestK8sStagingFromTaskSkips(t *testing.T) {
	creds := &transport.Credentials{Harbor: transport.HarborCredentials{Registry: "harbor.example"}}
	task := &transport.Task{Payload: transport.TaskPayload{SkipDownload: true, SkipUpload: true, SourceFileLocation: "h"}}
	st, err := K8sStagingFromTask(task, creds)
	if err != nil {
		t.Fatal(err)
	}
	if st.DownloadCmd != "" || st.UploadCmd != "" || !strings.HasSuffix(st.Image, "/modtim/minio-client:latest") {
		t.Fatalf("unexpected staging spec: %+v", st)
	}
	if strings.Contains(stageOutScript(st), "mc cp") {
		t.Fatal("SkipUpload must not upload")
	}
}

func TestParseStageOutLogs(t *testing.T) {
	logs := "noise\nUPLOAD,abc,out.png,12,png\nUPLOAD,def,b.txt,3,txt\n" + stageOutDoneMarker + "\n"
	outs, err := ParseStageOutLogs(logs)
	if err != nil || len(outs) != 2 || outs[0] != (transport.OutputFile{ContentHash: "abc", FileName: "out.png", SizeBytes: 12, FileExtension: "png"}) {
		t.Fatalf("manifest parse: %v %+v", err, outs)
	}

	rj := `{"outputs":[{"location":"minio://bkt/hash9","file_name":"v.json","file_extension":"json","size_bytes":5}]}`
	logs = stageOutResultPrefix + "validation_result.json:" + base64.StdEncoding.EncodeToString([]byte(rj)) + "\nUPLOAD,zzz,x,1,x\n" + stageOutDoneMarker
	outs, err = ParseStageOutLogs(logs)
	if err != nil || len(outs) != 1 || outs[0].ContentHash != "hash9" || outs[0].FileName != "v.json" {
		t.Fatalf("result json should win: %v %+v", err, outs)
	}

	empty := stageOutResultPrefix + "export_result.json:" + base64.StdEncoding.EncodeToString([]byte(`{"outputs":[]}`)) + "\nUPLOAD,k,f.bin,9,bin\n" + stageOutDoneMarker
	outs, _ = ParseStageOutLogs(empty)
	if len(outs) != 1 || outs[0].ContentHash != "k" {
		t.Fatalf("empty result json must fall back to manifest: %+v", outs)
	}

	outs, err = ParseStageOutLogs(stageOutDoneMarker)
	if err != nil || outs == nil || len(outs) != 0 {
		t.Fatalf("no outputs must be empty non-nil: %v %#v", err, outs)
	}

	if _, err = ParseStageOutLogs("UPLOAD,a,b,1,c\n"); err == nil || !strings.Contains(err.Error(), stageOutFailedErr) {
		t.Fatalf("missing marker must be StageOutFailed, got %v", err)
	}
}

func TestK8sCollectStagedOutputsNoMarker(t *testing.T) {
	b, _ := newTestK8s(t)
	h, _ := b.SubmitStaged(context.Background(), stagedSpec(), stagingFixture())
	addStagedPod(t, b, h, corev1.PodStatus{})
	// fake clientset logs are "fake logs" — no manifest marker.
	if _, err := b.CollectStagedOutputs(context.Background(), h); err == nil ||
		MapTaskStatus(&TaskStatus{Phase: TaskFailed, Err: err.Error()}).Reason != ReasonStageOutFailed {
		t.Fatalf("want StageOutFailed, got %v", err)
	}
}

func addStagedPod(t *testing.T, b *K8sBackend, h *Handle, status corev1.PodStatus) *corev1.Pod {
	t.Helper()
	ns, name, _ := parseHandle(h)
	job, err := b.Client.BatchV1().Jobs(ns).Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name + "-pod", Namespace: ns, Labels: job.Spec.Template.Labels,
			CreationTimestamp: metav1.Now()},
		Spec:   job.Spec.Template.Spec,
		Status: status,
	}
	if _, err := b.Client.CoreV1().Pods(ns).Create(context.Background(), pod, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	return pod
}

func term(name string, code int32, reason string) corev1.ContainerStatus {
	return corev1.ContainerStatus{Name: name, State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{ExitCode: code, Reason: reason}}}
}

func running(name string) corev1.ContainerStatus {
	return corev1.ContainerStatus{Name: name, State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}}
}

func TestK8sStagedPollMapping(t *testing.T) {
	cases := []struct {
		name       string
		status     corev1.PodStatus
		phase      TaskPhase
		reason     string
		wantSignal string
	}{
		{"stage-in failed", corev1.PodStatus{InitContainerStatuses: []corev1.ContainerStatus{term(k8sStageInContainerName, 1, "Error")}},
			TaskFailed, ReasonStageInFailed, ""},
		{"task running", corev1.PodStatus{InitContainerStatuses: []corev1.ContainerStatus{term(k8sStageInContainerName, 0, "Completed")},
			ContainerStatuses: []corev1.ContainerStatus{running(k8sTaskContainerName), running(k8sStageOutContainerName)}},
			TaskRunning, "", ""},
		{"task done, stage-out still uploading ⇒ running + signalled", corev1.PodStatus{
			ContainerStatuses: []corev1.ContainerStatus{term(k8sTaskContainerName, 0, "Completed"), running(k8sStageOutContainerName)}},
			TaskRunning, "", "0"},
		{"task failed ⇒ failed + signalled", corev1.PodStatus{
			ContainerStatuses: []corev1.ContainerStatus{running(k8sStageOutContainerName), term(k8sTaskContainerName, 3, "Error")}},
			TaskFailed, ReasonContainerExit, "3"},
		{"stage-out failed", corev1.PodStatus{
			ContainerStatuses: []corev1.ContainerStatus{term(k8sStageOutContainerName, 1, "Error"), term(k8sTaskContainerName, 0, "Completed")}},
			TaskFailed, ReasonStageOutFailed, ""},
		{"stage-out OOM is still a stage failure", corev1.PodStatus{
			ContainerStatuses: []corev1.ContainerStatus{term(k8sTaskContainerName, 0, "Completed"), term(k8sStageOutContainerName, 137, "OOMKilled")}},
			TaskFailed, ReasonStageOutFailed, ""},
		{"all done", corev1.PodStatus{
			ContainerStatuses: []corev1.ContainerStatus{term(k8sStageOutContainerName, 0, "Completed"), term(k8sTaskContainerName, 0, "Completed")}},
			TaskSucceeded, "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			b, _ := newTestK8s(t)
			h, err := b.SubmitStaged(context.Background(), stagedSpec(), stagingFixture())
			if err != nil {
				t.Fatal(err)
			}
			pod := addStagedPod(t, b, h, tc.status)
			st, err := b.Poll(context.Background(), h)
			if err != nil {
				t.Fatal(err)
			}
			if st.Phase != tc.phase {
				t.Fatalf("phase %v want %v (%s)", st.Phase, tc.phase, st.Err)
			}
			if u := MapTaskStatus(st); tc.reason != "" && u.Reason != tc.reason {
				t.Fatalf("reason %q want %q (%s)", u.Reason, tc.reason, st.Err)
			} else if tc.reason == ReasonStageOutFailed && (!u.ShouldRetry || u.OOMKilled) {
				t.Fatalf("stage-out failure should retry and not be task OOM: %+v", u)
			}
			got, _ := b.Client.CoreV1().Pods(pod.Namespace).Get(context.Background(), pod.Name, metav1.GetOptions{})
			if got.Annotations[K8sTaskDoneAnnotation] != tc.wantSignal {
				t.Fatalf("task-done annotation %q want %q", got.Annotations[K8sTaskDoneAnnotation], tc.wantSignal)
			}
		})
	}
}

func TestK8sStagingScriptsParse(t *testing.T) {
	if _, err := exec.LookPath("sh"); err != nil {
		t.Skip("no sh")
	}
	st := stagingFixture()
	for name, s := range map[string]string{"stage-in": stageInScript(st), "stage-out": stageOutScript(st)} {
		if out, err := exec.Command("sh", "-n", "-c", s).CombinedOutput(); err != nil {
			t.Fatalf("%s script does not parse: %v\n%s", name, err, out)
		}
	}
}

func secretRefFor(c corev1.Container, key string) *corev1.SecretKeySelector {
	for _, e := range c.Env {
		if e.Name == key && e.ValueFrom != nil {
			return e.ValueFrom.SecretKeyRef
		}
	}
	return nil
}

// W13: staging creds live in a per-task Secret owned by the Job, never as
// plain values in the pod spec, and never on the task container.
func TestK8sStagingCredsSecret(t *testing.T) {
	b, cs := newTestK8s(t)
	st := stagingFixture()
	st.MinIO = transport.MinIOCredentials{MCHost: "http://AKX:SKX@versitygw:7070", Endpoint: "versitygw:7070", AccessKey: "AKX", SecretKey: "SKX"}
	h, err := b.SubmitStaged(context.Background(), stagedSpec(), st)
	if err != nil {
		t.Fatalf("SubmitStaged: %v", err)
	}
	ns, name, _ := parseHandle(h)
	job, _ := cs.BatchV1().Jobs(ns).Get(context.Background(), name, metav1.GetOptions{})
	sec, err := cs.CoreV1().Secrets(ns).Get(context.Background(), stagingSecretName(name), metav1.GetOptions{})
	if err != nil {
		t.Fatalf("staging secret not created: %v", err)
	}
	if sec.Type != corev1.SecretTypeOpaque || sec.Labels[DefaultTaskPodSelectorKey] != DefaultTaskPodSelectorValue || sec.Labels[LabelJob] != name {
		t.Fatalf("secret type/labels wrong: %v %v", sec.Type, sec.Labels)
	}
	want := map[string]string{"MC_HOST_myminio": st.MinIO.MCHost, "MINIO_ENDPOINT": "versitygw:7070", "MINIO_ACCESS": "AKX", "MINIO_SECRET": "SKX"}
	if len(sec.StringData) != len(want) {
		t.Fatalf("secret data keys: %v", sec.StringData)
	}
	for k, v := range want {
		if sec.StringData[k] != v {
			t.Fatalf("secret %s = %q, want %q", k, sec.StringData[k], v)
		}
	}
	if len(sec.OwnerReferences) != 1 {
		t.Fatalf("want one ownerReference, got %+v", sec.OwnerReferences)
	}
	or := sec.OwnerReferences[0]
	if or.Kind != "Job" || or.APIVersion != "batch/v1" || or.Name != name || or.UID != job.UID {
		t.Fatalf("ownerReference wrong: %+v", or)
	}

	ps := job.Spec.Template.Spec
	for _, c := range append(ps.InitContainers, ps.Containers...) {
		for _, e := range c.Env {
			for _, secret := range []string{"AKX", "SKX", st.MinIO.MCHost} {
				if strings.Contains(e.Value, secret) {
					t.Fatalf("%s: plain-value cred in pod spec: %s=%q", c.Name, e.Name, e.Value)
				}
			}
		}
		for _, ef := range c.EnvFrom {
			if ef.SecretRef != nil && c.Name == k8sTaskContainerName {
				t.Fatal("task container must not envFrom the staging secret")
			}
		}
	}
	task := ps.Containers[0]
	for _, k := range stagingEnvKeys {
		if secretRefFor(task, k) != nil {
			t.Fatalf("task container must not reference staging secret key %s", k)
		}
	}
	if _, ok := envMap(task)["MC_HOST_myminio"]; ok {
		t.Fatal("task container must not get MC_HOST_myminio")
	}
}

func TestK8sStagingSecretDeletedOnJobCreateFailure(t *testing.T) {
	b, cs := newTestK8s(t)
	cs.PrependReactor("create", "jobs", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("admission denied")
	})
	if _, err := b.SubmitStaged(context.Background(), stagedSpec(), stagingFixture()); err == nil {
		t.Fatal("want error when job create fails")
	}
	secs, _ := cs.CoreV1().Secrets(b.Namespace).List(context.Background(), metav1.ListOptions{})
	if len(secs.Items) != 0 {
		t.Fatalf("staging secret leaked after job-create failure: %v", secs.Items[0].Name)
	}
}

func TestK8sStagingOwnerPatchFailureCleansUp(t *testing.T) {
	b, cs := newTestK8s(t)
	cs.PrependReactor("patch", "secrets", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("forbidden")
	})
	if _, err := b.SubmitStaged(context.Background(), stagedSpec(), stagingFixture()); err == nil {
		t.Fatal("want error when owner patch fails")
	}
	secs, _ := cs.CoreV1().Secrets(b.Namespace).List(context.Background(), metav1.ListOptions{})
	jobs, _ := cs.BatchV1().Jobs(b.Namespace).List(context.Background(), metav1.ListOptions{})
	if len(secs.Items) != 0 || len(jobs.Items) != 0 {
		t.Fatalf("want secret+job cleaned up, got %d secrets %d jobs", len(secs.Items), len(jobs.Items))
	}
}

func TestK8sCancelCascades(t *testing.T) {
	b, cs := newTestK8s(t)
	h, err := b.SubmitStaged(context.Background(), stagedSpec(), stagingFixture())
	if err != nil {
		t.Fatal(err)
	}
	var policy *metav1.DeletionPropagation
	cs.PrependReactor("delete", "jobs", func(a k8stesting.Action) (bool, runtime.Object, error) {
		policy = a.(k8stesting.DeleteActionImpl).DeleteOptions.PropagationPolicy
		return false, nil, nil
	})
	if err := b.Cancel(context.Background(), h); err != nil {
		t.Fatal(err)
	}
	if policy == nil || *policy != metav1.DeletePropagationBackground {
		t.Fatalf("Cancel must delete with Background propagation (GCs pod + staging secret), got %v", policy)
	}
}
