package executor

// k8s-reduction-engine W3: S3 input staging + output collection for K8s task pods.
//
// Mirrors the Docker path (executor.go Run): prepareDataVolume + downloadSidecar
// before the node runs, readResultJSON / uploadSidecar after it exits 0. The
// Docker sidecars are separate containers on the worker's platform network; on
// K8s they live in the task's own pod (see k8s/networkpolicy/default-deny-egress.yaml,
// which already allows DNS + API + in-cluster S3 for exactly these steps).
//
// Pod layout (only when submitted via SubmitStaged; Submit is unchanged):
//
//	volumes:        data (emptyDir, /data in every container), mod-signal (downwardAPI)
//	initContainers: stage-in  — mkdir/chmod /data/{input,output} + mc cp inputs → /data/input
//	containers:     task      — the node image, UNMODIFIED (entrypoint/cmd untouched) + /data mount
//	                stage-out — waits for the task to finish, then uploads /data/output (or
//	                            emits validation_result.json / export_result.json) and prints
//	                            a manifest the worker reads from its logs.
//
// How stage-out knows the task finished (design choice):
//
//   - Native sidecars (initContainer restartPolicy: Always) need K8s >= 1.29 and
//     still don't sequence "after main exits" — rejected.
//   - Wrapping the task command with a done-marker would modify user images'
//     entrypoints — rejected.
//   - A follow-up upload Job needs a PVC to see the task's files — rejected
//     (no PVC for task I/O, by design).
//
// Chosen: two independent signals, whichever fires first.
//
//  1. Fast path — shareProcessNamespace: true. stage-out scans /proc for any
//     process that is not the pause container (PID 1) and not in its own cgroup
//     (/proc/<pid>/cgroup is world-readable; no ptrace/caps needed). Once it has
//     SEEN such a process and it disappears, the task has exited. The "seen"
//     gate matters: kubelet keeps starting later containers when an earlier
//     one's image pull fails, so stage-out could otherwise find "no task" and
//     upload an empty /data/output before the task ever ran.
//  2. Authoritative path — the worker's Poll sees the task container's
//     Terminated status via the API and patches the pod annotation
//     mod.tech/task-done=<exit code>. A downwardAPI volume projects it to
//     /mod-signal/task-done (kubelet refresh, typically <= ~1 min). This
//     covers a task too short-lived for the /proc scan to observe, and tells
//     stage-out the exit code so a failed task's outputs are not uploaded.
//     Requires RBAC `patch pods` for the worker in the task namespace.
//
// Task success = task container exit 0 AND stage-out exit 0 (+ manifest).

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

const (
	k8sStageInContainerName  = "stage-in"
	k8sStageOutContainerName = "stage-out"
	k8sDataVolumeName        = "data"
	k8sSignalVolumeName      = "mod-signal"
	k8sSignalMountPath       = "/mod-signal"

	// K8sTaskDoneAnnotation is patched onto a staged pod by Poll once the task
	// container has terminated; value = its exit code.
	K8sTaskDoneAnnotation = "mod.tech/task-done"

	stageOutResultPrefix = "MOD_RESULT_JSON_B64:"
	stageOutDoneMarker   = "MOD_STAGE_OUT_DONE"

	// c11-J3b (30.6.11 k8s twin of c11-J3): stage-out ALSO prints result.json
	// under this SEPARATE prefix so ParseStageOutUsage can meter tokens. It is
	// not stageOutResultPrefix, so ParseStageOutLogs' outputs and the UPLOAD
	// manifest are unchanged; the 64 KiB shell gate in stageOutScript mirrors
	// parseUsageJSON's own cap (S9).
	// The line is metering-only: it never gates the upload and never fails a
	// task (any decode/parse error yields nil).
	stageOutUsagePrefix = "MOD_USAGE_JSON_B64:"
	// Stage failure markers; classifyReason maps them to ReasonStage*Failed.
	stageInFailedErr  = "StageInFailed"
	stageOutFailedErr = "StageOutFailed"

	stageOutLogMaxBytes = 16 << 20
)

// K8sStagingSpec carries the S3 staging side of a task. Deliberately NOT part
// of the locked TaskSpec; passed to SubmitStaged alongside it.
type K8sStagingSpec struct {
	Image       string // mc image — same one the Docker path uses
	MinIO       transport.MinIOCredentials
	DownloadCmd string // "" ⇒ nothing to download (only /data prep)
	UploadCmd   string // "" ⇒ skip upload (payload.SkipUpload); result JSON still collected
}

// K8sStagingFromTask builds the staging spec from the same inputs the Docker
// path uses (executor.go Run: minioClientImage, buildDownloadCmd, uploadSidecar).
func K8sStagingFromTask(task *transport.Task, creds *transport.Credentials) (*K8sStagingSpec, error) {
	if task == nil || creds == nil {
		return nil, errors.New("k8s staging: task and credentials are required")
	}
	harborReg, err := harborRegistry(creds.Harbor)
	if err != nil {
		return nil, fmt.Errorf("k8s staging: harbor credentials: %w", err)
	}
	p := task.Payload
	st := &K8sStagingSpec{
		Image: harborReg + "/modtim/minio-client:latest",
		MinIO: creds.MinIO,
	}
	if (p.SourceFileLocation != "" || len(p.InputFiles) > 0) && !p.SkipDownload {
		// R-154 follow-up: buildDownloadCmd fails closed on extensions or a
		// node kind that would escape /data/input (zero downloads staged).
		cmd, err := buildDownloadCmd(p)
		if err != nil {
			return nil, err
		}
		st.DownloadCmd = cmd
	}
	if !p.SkipUpload {
		st.UploadCmd = buildUploadCmd(task)
	}
	return st, nil
}

// buildUploadCmd is the upload script the Docker uploadSidecar runs — both
// call the shared buildUploadScript (upload_script.go, W17), so they cannot
// drift. UPLOAD,<key>,<fname>,<size>,<ext> lines are parsed by
// parseUploadManifest; stageOutScript adds the K8s retry + done marker.
func buildUploadCmd(task *transport.Task) string {
	return buildUploadScript(uploadScriptParamsFromTask(task))
}

// stageInScript = prepareDataVolume + downloadSidecar.
func stageInScript(st *K8sStagingSpec) string {
	s := `set -e; mkdir -p /data/input /data/output; chmod 0777 /data /data/input /data/output; ` +
		`echo "prep OK"`
	if st.DownloadCmd != "" {
		s += "; " + st.DownloadCmd + `; echo "stage-in OK: $(ls /data/input | wc -l) file(s)"`
	}
	return s
}

// stageOutScript waits for the task (see file header), then collects outputs.
// Result JSON (validation_result.json / export_result.json) is emitted
// base64'd; when one carries a non-empty outputs array the upload is skipped,
// matching Docker's "result JSON wins" rule (the worker re-validates it and,
// like Docker, falls back to the UPLOAD manifest when it does not parse).
func stageOutScript(st *K8sStagingSpec) string {
	var b strings.Builder
	b.WriteString(`self=$(cat /proc/self/cgroup); seen=0; ` +
		`while :; do ` +
		`  [ -s ` + k8sSignalMountPath + `/task-done ] && break; ` +
		`  alive=0; ` +
		`  for d in /proc/[0-9]*; do ` +
		`    [ "${d#/proc/}" = 1 ] && continue; ` +
		`    c=$(cat "$d/cgroup" 2>/dev/null) || continue; ` +
		`    [ -n "$c" ] && [ "$c" != "$self" ] && { alive=1; break; }; ` +
		`  done; ` +
		`  if [ $alive = 1 ]; then seen=1; elif [ $seen = 1 ]; then break; fi; ` +
		`  sleep 1; ` +
		`done; `)
	b.WriteString(`code=$(cat ` + k8sSignalMountPath + `/task-done 2>/dev/null || true); ` +
		`if [ -n "$code" ] && [ "$code" != 0 ]; then echo "task exited $code; skipping output upload"; echo ` + stageOutDoneMarker + `; exit 0; fi; `)
	b.WriteString(`haveresult=0; ` +
		`for f in validation_result.json export_result.json; do ` +
		`  [ -s "/data/output/$f" ] || continue; ` +
		`  echo "` + stageOutResultPrefix + `$f:$(base64 "/data/output/$f" | tr -d '\n')"; ` +
		`  tr -d ' \n\t\r' < "/data/output/$f" | grep -q '"outputs":\[{' && haveresult=1; ` +
		`done; `)
	// c11-J3b: SEPARATE step, deliberately AFTER the loop above so haveresult
	// and the UPLOAD manifest gate are untouched. Emits result.json under
	// stageOutUsagePrefix for ParseStageOutUsage (the token metering read).
	// The 64 KiB shell gate mirrors parseUsageJSON's own cap (S9); a bigger
	// file simply gets no usage line, never a failed task.
	b.WriteString(`if [ -s /data/output/result.json ] && ` +
		`[ "$(wc -c < /data/output/result.json)" -le 65536 ]; then ` +
		`echo "` + stageOutUsagePrefix + `$(base64 /data/output/result.json | tr -d '\n')"; fi; `)
	if st.UploadCmd != "" {
		b.WriteString(`if [ $haveresult = 0 ]; then ` +
			`n=0; until ( ` + st.UploadCmd + ` ) > /tmp/manifest; do ` +
			`  n=$((n+1)); [ $n -ge 3 ] && { echo "upload failed after $n attempts" >&2; exit 1; }; ` +
			`  sleep $((5*n)); ` +
			`done; cat /tmp/manifest; fi; `)
	}
	b.WriteString(`echo ` + stageOutDoneMarker)
	return b.String()
}

// stagingEnvKeys are the four vars Docker's runSidecarWithOutput passes. On
// K8s they live in a per-task Secret (k8s-reduction-engine W13) so the pod
// spec never carries the minted S3 creds as plain values (anyone with
// `get pods` in the task namespace could otherwise read them).
var stagingEnvKeys = []string{"MC_HOST_myminio", "MINIO_ENDPOINT", "MINIO_ACCESS", "MINIO_SECRET"}

// stagingSecretName derives the per-task staging Secret name from the Job name
// (<=62 chars + "-staging" — well inside the 253-char Secret name limit).
func stagingSecretName(jobName string) string { return jobName + "-staging" }

func stagingSecretData(m transport.MinIOCredentials) map[string]string {
	return map[string]string{
		"MC_HOST_myminio": m.MCHost,
		"MINIO_ENDPOINT":  m.Endpoint,
		"MINIO_ACCESS":    m.AccessKey,
		"MINIO_SECRET":    m.SecretKey,
	}
}

// stagingEnv references the per-task Secret key by key (not envFrom): only
// these four keys are ever exposed, and only to stage-in/stage-out.
func stagingEnv(secretName string) []corev1.EnvVar {
	out := make([]corev1.EnvVar, 0, len(stagingEnvKeys))
	for _, k := range stagingEnvKeys {
		out = append(out, corev1.EnvVar{Name: k, ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: secretName},
				Key:                  k,
			},
		}})
	}
	return out
}

// buildStagingSecret renders the per-task staging Secret for job (no API call).
func (b *K8sBackend) buildStagingSecret(job *batchv1.Job, st *K8sStagingSpec) *corev1.Secret {
	selKey, selVal := b.TaskPodSelectorKey, b.TaskPodSelectorValue
	if selKey == "" {
		selKey, selVal = DefaultTaskPodSelectorKey, DefaultTaskPodSelectorValue
	}
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      stagingSecretName(job.Name),
			Namespace: b.Namespace,
			Labels: map[string]string{
				LabelPartOf: LabelPartOfValue,
				LabelJob:    job.Name,
				selKey:      selVal,
			},
		},
		Type:       corev1.SecretTypeOpaque,
		StringData: stagingSecretData(st.MinIO),
	}
}

// BuildJobStaged is BuildJob plus the staging layout. st == nil ⇒ BuildJob.
func (b *K8sBackend) BuildJobStaged(spec *TaskSpec, st *K8sStagingSpec) *batchv1.Job {
	job := b.BuildJob(spec)
	if st == nil {
		return job
	}
	ps := &job.Spec.Template.Spec
	dataMount := corev1.VolumeMount{Name: k8sDataVolumeName, MountPath: "/data"}
	share := true
	ps.ShareProcessNamespace = &share
	ps.Volumes = append(ps.Volumes,
		corev1.Volume{Name: k8sDataVolumeName, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
		corev1.Volume{Name: k8sSignalVolumeName, VolumeSource: corev1.VolumeSource{DownwardAPI: &corev1.DownwardAPIVolumeSource{
			Items: []corev1.DownwardAPIVolumeFile{{
				Path:     "task-done",
				FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.annotations['" + K8sTaskDoneAnnotation + "']"},
			}},
		}}},
	)
	for i := range ps.Containers {
		if ps.Containers[i].Name == k8sTaskContainerName {
			ps.Containers[i].VolumeMounts = append(ps.Containers[i].VolumeMounts, dataMount)
		}
	}
	ps.InitContainers = append(ps.InitContainers, corev1.Container{
		Name:            k8sStageInContainerName,
		Image:           st.Image,
		Command:         []string{"/bin/sh", "-c", stageInScript(st)},
		Env:             stagingEnv(stagingSecretName(job.Name)),
		VolumeMounts:    []corev1.VolumeMount{dataMount},
		ImagePullPolicy: corev1.PullIfNotPresent,
	})
	// stage-out is appended AFTER task: kubelet starts containers in spec order.
	ps.Containers = append(ps.Containers, corev1.Container{
		Name:    k8sStageOutContainerName,
		Image:   st.Image,
		Command: []string{"/bin/sh", "-c", stageOutScript(st)},
		Env:     stagingEnv(stagingSecretName(job.Name)),
		VolumeMounts: []corev1.VolumeMount{
			dataMount,
			{Name: k8sSignalVolumeName, MountPath: k8sSignalMountPath, ReadOnly: true},
		},
		ImagePullPolicy: corev1.PullIfNotPresent,
	})
	return job
}

// SubmitStaged creates the Job with S3 staging. st == nil ⇒ identical to Submit.
func (b *K8sBackend) SubmitStaged(ctx context.Context, spec *TaskSpec, st *K8sStagingSpec) (*Handle, error) {
	if st == nil {
		return b.Submit(ctx, spec)
	}
	if spec == nil || spec.Image == "" {
		return nil, errors.New("k8s backend: spec.Image is required")
	}
	if st.Image == "" {
		return nil, errors.New("k8s backend: staging image is required")
	}
	// Ordering (W13): Secret → Job → ownerRef patch.
	//   - Secret first, so the pod can never start without its creds (a pod
	//     created before its Secret sits in CreateContainerConfigError, which
	//     Poll would count toward PendingFailAfter).
	//   - The Job's UID only exists after create, so the ownerReference is
	//     patched on afterwards; K8s GC then deletes the Secret with the Job
	//     (TTL or Cancel's Background-propagation delete).
	//   - Any failure after the Secret exists deletes it (and the Job, if the
	//     ownerRef patch failed) — an ephemeral credential object, not audit
	//     data, so no leaked creds and no orphaned un-GC-able Secret.
	job := b.BuildJobStaged(spec, st)
	secret := b.buildStagingSecret(job, st)
	secrets := b.Client.CoreV1().Secrets(b.Namespace)
	if _, err := secrets.Create(ctx, secret, metav1.CreateOptions{}); err != nil {
		return nil, fmt.Errorf("k8s backend: create staging secret: %w", err)
	}
	cleanupSecret := func() {
		// Fresh context: ctx may be the reason we're cleaning up.
		cctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		_ = secrets.Delete(cctx, secret.Name, metav1.DeleteOptions{})
	}
	created, err := b.Client.BatchV1().Jobs(b.Namespace).Create(ctx, job, metav1.CreateOptions{})
	if err != nil {
		cleanupSecret()
		return nil, fmt.Errorf("k8s backend: create job: %w", err)
	}
	// No BlockOwnerDeletion: it would need update on jobs/finalizers under the
	// OwnerReferencesPermissionEnforcement admission plugin, and GC does not need it.
	ownerPatch, _ := json.Marshal(map[string]any{
		"metadata": map[string]any{
			"ownerReferences": []metav1.OwnerReference{{
				APIVersion: "batch/v1",
				Kind:       "Job",
				Name:       created.Name,
				UID:        created.UID,
			}},
		},
	})
	if _, err := secrets.Patch(ctx, secret.Name, types.MergePatchType, ownerPatch, metav1.PatchOptions{}); err != nil {
		cctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		bg := metav1.DeletePropagationBackground
		_ = b.Client.BatchV1().Jobs(b.Namespace).Delete(cctx, created.Name, metav1.DeleteOptions{PropagationPolicy: &bg})
		cancel()
		cleanupSecret()
		return nil, fmt.Errorf("k8s backend: set staging secret owner: %w", err)
	}
	return &Handle{Backend: k8sBackendName, ID: created.Namespace + "/" + created.Name}, nil
}

func isStagedPod(p *corev1.Pod) bool {
	for _, c := range p.Spec.Containers {
		if c.Name == k8sStageOutContainerName {
			return true
		}
	}
	return false
}

func findStatus(sts []corev1.ContainerStatus, name string) *corev1.ContainerStatus {
	for i := range sts {
		if sts[i].Name == name {
			return &sts[i]
		}
	}
	return nil
}

func terminatedDetail(t *corev1.ContainerStateTerminated) string {
	return joinReason(t.Reason, t.Message)
}

// stagedPollVerdict decides a staged pod's status from its per-container
// state. ok=false ⇒ not decisive; Poll falls back to its generic logic
// (Job conditions, pending/unschedulable detection). It also signals
// stage-out (annotation patch) once the task container has terminated.
func (b *K8sBackend) stagedPollVerdict(ctx context.Context, ns string, pods []corev1.Pod) (*TaskStatus, bool) {
	for i := range pods {
		p := &pods[i]
		if !isStagedPod(p) {
			continue
		}
		if in := findStatus(p.Status.InitContainerStatuses, k8sStageInContainerName); in != nil {
			if t := in.State.Terminated; t != nil && t.ExitCode != 0 {
				return &TaskStatus{Phase: TaskFailed, ExitCode: int(t.ExitCode),
					Err: joinReason(stageInFailedErr, joinReason(fmt.Sprintf("stage-in exited %d", t.ExitCode), terminatedDetail(t)))}, true
			}
			if w := in.State.Waiting; w != nil && pendingFailureReasons[w.Reason] &&
				b.clock().Sub(p.CreationTimestamp.Time) >= b.pendingGrace() {
				return &TaskStatus{Phase: TaskFailed, ExitCode: -1,
					Err: joinReason(stageInFailedErr, joinReason(w.Reason, w.Message))}, true
			}
		}
		task := findStatus(p.Status.ContainerStatuses, k8sTaskContainerName)
		if task == nil || task.State.Terminated == nil {
			return nil, false
		}
		tt := task.State.Terminated
		out := findStatus(p.Status.ContainerStatuses, k8sStageOutContainerName)
		outDone := out != nil && out.State.Terminated != nil
		if !outDone {
			b.signalTaskDone(ctx, ns, p, tt.ExitCode)
		}
		if tt.ExitCode != 0 {
			return &TaskStatus{Phase: TaskFailed, ExitCode: int(tt.ExitCode),
				Err: joinReason("container exited", terminatedDetail(tt))}, true
		}
		if !outDone {
			return &TaskStatus{Phase: TaskRunning}, true
		}
		if ot := out.State.Terminated; ot.ExitCode != 0 {
			return &TaskStatus{Phase: TaskFailed, ExitCode: int(ot.ExitCode),
				Err: joinReason(stageOutFailedErr, joinReason(fmt.Sprintf("stage-out exited %d", ot.ExitCode), terminatedDetail(ot)))}, true
		}
		return &TaskStatus{Phase: TaskSucceeded}, true
	}
	return nil, false
}

func (b *K8sBackend) pendingGrace() time.Duration {
	if b.PendingFailAfter > 0 {
		return b.PendingFailAfter
	}
	return DefaultPendingFailAfter
}

// signalTaskDone patches the task-done annotation (best-effort, idempotent).
func (b *K8sBackend) signalTaskDone(ctx context.Context, ns string, p *corev1.Pod, code int32) {
	if _, ok := p.Annotations[K8sTaskDoneAnnotation]; ok {
		return
	}
	patch, _ := json.Marshal(map[string]any{"metadata": map[string]any{
		"annotations": map[string]string{K8sTaskDoneAnnotation: strconv.Itoa(int(code))},
	}})
	_, _ = b.Client.CoreV1().Pods(ns).Patch(ctx, p.Name, types.MergePatchType, patch, metav1.PatchOptions{})
}

// ContainerLogs streams one named container's logs from the Job's pod.
func (b *K8sBackend) ContainerLogs(ctx context.Context, h *Handle, container string) (io.ReadCloser, error) {
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
	pod := pods[0]
	for _, p := range pods[1:] {
		if p.CreationTimestamp.After(pod.CreationTimestamp.Time) {
			pod = p
		}
	}
	return b.Client.CoreV1().Pods(ns).GetLogs(pod.Name, &corev1.PodLogOptions{Container: container}).Stream(ctx)
}

// CollectStagedOutputs reads stage-out's manifest into the reporter's shape.
func (b *K8sBackend) CollectStagedOutputs(ctx context.Context, h *Handle) ([]transport.OutputFile, error) {
	rc, err := b.ContainerLogs(ctx, h, k8sStageOutContainerName)
	if err != nil {
		return nil, fmt.Errorf("%s: read stage-out logs: %w", stageOutFailedErr, err)
	}
	defer rc.Close()
	raw, err := io.ReadAll(io.LimitReader(rc, stageOutLogMaxBytes))
	if err != nil {
		return nil, fmt.Errorf("%s: read stage-out logs: %w", stageOutFailedErr, err)
	}
	// c11-J3b: the SAME logs the manifest parse already read also carry the
	// usage line. Metering only: it never changes the outputs or the error, and
	// a nil usage (no line / bad base64 / bad model) is simply left unset.
	// This is the collect that reads the logs (CollectStagedOutputsScanned
	// delegates here), so it is the one that sets the handle.
	outs, err := ParseStageOutLogs(string(raw))
	if h != nil {
		h.UsageTokens = ParseStageOutUsage(string(raw))
	}
	return outs, err
}

// ParseStageOutLogs turns stage-out logs into outputs. Result JSON with a
// non-empty outputs array wins (Docker readResultJSON rule); otherwise the
// UPLOAD manifest. Never returns nil outputs on success (the API rejects null).
func ParseStageOutLogs(logs string) ([]transport.OutputFile, error) {
	done := false
	var fromResult []transport.OutputFile
	sc := bufio.NewScanner(strings.NewReader(logs))
	sc.Buffer(make([]byte, 64*1024), stageOutLogMaxBytes)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		switch {
		case line == stageOutDoneMarker:
			done = true
		case strings.HasPrefix(line, stageOutResultPrefix) && fromResult == nil:
			rest := strings.TrimPrefix(line, stageOutResultPrefix)
			if i := strings.IndexByte(rest, ':'); i >= 0 {
				rest = rest[i+1:]
			}
			if content, err := base64.StdEncoding.DecodeString(rest); err == nil {
				fromResult = parseResultJSONOutputs(content)
			}
		}
	}
	if !done {
		return nil, fmt.Errorf("%s: stage-out manifest incomplete (no %s marker)", stageOutFailedErr, stageOutDoneMarker)
	}
	if len(fromResult) > 0 {
		return fromResult, nil
	}
	outs := parseUploadManifest(logs)
	if outs == nil {
		outs = []transport.OutputFile{}
	}
	return outs, nil
}

// ParseStageOutUsage (c11-J3b) is the k8s twin of the Docker path's readUsageJSON:
// it scans the SAME stage-out logs for the first stageOutUsagePrefix line, base64-
// decodes it and hands the bytes to executor.go's parseUsageJSON, so the S9 bounds
// (64 KiB cap, counts 0..1e9, model charset gate) are enforced in ONE place. Any
// error - no line, bad base64, bad/missing model, over-cap - yields nil, so usage
// can never fail a task. Only the counts leave the worker; no prompt or response
// text is ever logged. ParseStageOutLogs is untouched: its switch only matches
// stageOutResultPrefix, so this line is ignored there and the outputs and the
// UPLOAD manifest are byte-for-byte unchanged.
func ParseStageOutUsage(logs string) *transport.UsageTokens {
	// Same Scanner buffer settings as ParseStageOutLogs: a usage line is base64
	// (up to 64 KiB) plus the prefix, far past the 8 KiB default line budget.
	sc := bufio.NewScanner(strings.NewReader(logs))
	sc.Buffer(make([]byte, 64*1024), stageOutLogMaxBytes)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if !strings.HasPrefix(line, stageOutUsagePrefix) {
			continue
		}
		// First usage line wins, even when it is unusable (nil): stage-out emits at
		// most one, and a malformed one must not make us scan for a later "good" one.
		content, err := base64.StdEncoding.DecodeString(
			strings.TrimPrefix(line, stageOutUsagePrefix))
		if err != nil {
			return nil
		}
		return parseUsageJSON(content)
	}
	return nil
}

// parseResultJSONOutputs mirrors executor.go readResultJSON's parse.
func parseResultJSONOutputs(content []byte) []transport.OutputFile {
	var result struct {
		Outputs []struct {
			ContentHash   string `json:"content_hash"`
			FileName      string `json:"file_name"`
			FileExtension string `json:"file_extension"`
			SizeBytes     int64  `json:"size_bytes"`
			Location      string `json:"location"`
		} `json:"outputs"`
	}
	if err := json.Unmarshal(content, &result); err != nil || len(result.Outputs) == 0 {
		return nil
	}
	outs := make([]transport.OutputFile, 0, len(result.Outputs))
	for _, o := range result.Outputs {
		hash := o.ContentHash
		if hash == "" && o.Location != "" {
			parts := strings.Split(o.Location, "/")
			hash = parts[len(parts)-1]
		}
		outs = append(outs, transport.OutputFile{
			ContentHash: hash, FileName: o.FileName, FileExtension: o.FileExtension, SizeBytes: o.SizeBytes,
		})
	}
	return outs
}

// ---------------------------------------------------------------------------
// Fortress 3.5 (968a) — K8s-path pre-store scan staging.
//
// 926 (ae6230c55) added the pre-store staging pass to the Docker path, 943 to
// the native path; this is the K8s twin. Today's k8s stage-out uploads DIRECTLY
// to the durable key in-pod, so the API's pre-store consult never saw real
// bytes. Under the staging posture the stage-out container instead uploads to
// the transient `scan-staging/{task_id}/<hash>` prefix (upload_script.go
// stagingUploadParamsFromTask — the SAME contract task_output_scan.py
// _validate_staging_key pins), the worker consults the API with those
// staging_key descriptors, and only an allowed verdict lands the bytes
// durably — via a server-side minio-go CopyObject, so no task bytes ever
// cross the worker wire.
//
// The seam mirrors 943's native one exactly (native_staging.go
// nativeScanConsult / SetNativeScanConsult): the posture decision, the HTTP
// consult and the verdict parsing stay ONE implementation — executor.go
// scanOutputBeforeStore — installed here by the caller (968b) through
// SetK8sScanConsult, never re-derived in Go.
//
// UNSET seam / mode off ⇒ K8sScanStagingActive is false everywhere below:
// K8sStagingUploadCmd returns buildUploadCmd(task) byte-for-byte as today and
// CollectStagedOutputsScanned is exactly b.CollectStagedOutputs — the current
// direct-to-durable path, zero staging hops, zero consults.
// ---------------------------------------------------------------------------

var k8sScanConsult func(ctx context.Context, task *transport.Task,
	staged []transport.OutputFile, uploadPending bool) (bool, string)

// SetK8sScanConsult installs the consult callback (968b wires it once next to
// SetNativeScanConsult / SetOutputScanAuth in main.go). nil restores the
// pre-968 behaviour.
func SetK8sScanConsult(fn func(ctx context.Context, task *transport.Task,
	staged []transport.OutputFile, uploadPending bool) (bool, string)) {
	k8sScanConsult = fn
}

// K8sScanStagingActive is the single gate every staging site below reads: the
// seam must be installed AND the platform-settings posture (the SAME registry
// key the Docker and native paths read, executor.go
// outputScanModeTunableKey) must not be "off".
func K8sScanStagingActive(e *Executor) bool {
	if k8sScanConsult == nil || e == nil {
		return false
	}
	return e.tunables.String(outputScanModeTunableKey, "", "shadow",
		"off", "shadow", "enforce") != "off"
}

// K8sStagingUploadCmd is the staging-aware UploadCmd for K8sStagingFromTask's
// caller (968b sets st.UploadCmd through this instead of buildUploadCmd
// directly). Active ⇒ the staging-pass script (transient prefix, no cap, no
// loan attr: transient blobs are never loan-stamped, 943's rule). Inactive ⇒
// buildUploadCmd(task) UNCHANGED, so the stage-out container's `mc cp` line —
// and every existing golden test of it — is byte-identical.
func K8sStagingUploadCmd(task *transport.Task, e *Executor) string {
	if K8sScanStagingActive(e) {
		return buildUploadScript(stagingUploadParamsFromTask(task))
	}
	return buildUploadCmd(task)
}

// CollectStagedOutputsScanned is the k8s completion entry point: read the
// stage-out manifest with the UNCHANGED CollectStagedOutputs (under the staging
// posture that manifest names the TRANSIENT scan-staging/{task_id}/<hash> keys
// the in-pod stage-out uploaded), then run the consult -> promote sequence.
// Inactive seam/off returns CollectStagedOutputs' result untouched: the current
// direct-to-durable path, byte-for-byte.
//
// See the ordering proof on collectScannedOutputs, the function this wraps.
//
// ORDERING PROOF (the brief's "consult before cleanup on every return path"):
// cleanupNativeStagedOutputs is reached ONLY through the defer registered
// below, inside this function, so Go runs it at function exit — strictly AFTER
// the consult call has returned on every path: the result-JSON blocked
// early-return, the file-outputs blocked early-return, a CopyObject/StatObject
// error return, and the success return. A refusal path never reaches a
// CopyObject, so nothing durable is ever written when the verdict blocks. The
// cleanup is deliberately NOT folded into the stage-out upload itself: that
// would delete the transient blobs in the same pass that created them, before
// the API's HTTP consult could fetch them.
//
// A refusal returns (nil, reason) so the caller reports the task FAILED with
// the scan's reason — the k8s twin of the Docker path's blocked Result
// (executor.go :1281). Retryability matches Docker exactly: main.go reports a
// blocked Run() with shouldRetry=true (the MANAGER decides what an OOM/scan
// failure means for the requeue), and k8s_dispatch.go's output-collection
// failure path is already retryable=true, so the same flag carries.
func CollectStagedOutputsScanned(ctx context.Context, b *K8sBackend, h *Handle,
	task *transport.Task, creds *transport.Credentials, e *Executor) ([]transport.OutputFile, error) {
	outs, err := b.CollectStagedOutputs(ctx, h)
	if err != nil {
		return nil, err
	}
	if !K8sScanStagingActive(e) {
		// Unset seam / mode off: the pre-968 path, byte-for-byte — stage-out
		// already wrote the durable keys and the worker only parses the
		// manifest. Zero staging hops, zero consults, zero cleanup.
		return outs, nil
	}
	if task == nil {
		return nil, errors.New("executor: k8s scan staging: task is required")
	}
	if creds == nil {
		return nil, errors.New("executor: k8s scan staging: credentials are required")
	}
	return collectScannedOutputs(ctx, outs, task, creds, e)
}

// collectScannedOutputs is the consult -> promote half, split out so tests can
// drive it with a manifest of their own (the fake clientset's GetLogs always
// answers "fake logs", so a test cannot feed CollectStagedOutputs a manifest).
// The production sequence is unchanged: CollectStagedOutputsScanned calls this
// with exactly what CollectStagedOutputs parsed.
func collectScannedOutputs(ctx context.Context, outs []transport.OutputFile,
	task *transport.Task, creds *transport.Credentials, e *Executor) ([]transport.OutputFile, error) {
	// The transient prefix exists (the stage-out container ran the staging
	// script), so register the deferred cleanup NOW — best-effort, and the API-
	// side sweep (services/scan_staging_sweep.py, native_staging.go's note) is
	// the follow-up for crash-orphans. A fresh bounded context, not the caller's:
	// k8s_dispatch.go hands output collection a 30s budget the consult and the
	// copies already spend, so a cleanup sharing it would be born expired
	// (native_staging.go's note, verbatim posture).
	//
	// Registered BEFORE the consult so the blocked early-returns below still
	// clean up; defers run at function exit, i.e. strictly after the consult.
	// Belt-and-braces against a nil seam reaching this half (the gate above
	// already excludes it): without a consult there is nothing to promote, so
	// the current direct-to-durable path returns untouched.
	if k8sScanConsult == nil {
		return outs, nil
	}
	cleanupCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	defer func() {
		if cerr := cleanupNativeStagedOutputs(cleanupCtx, task, creds); cerr != nil {
			_ = cerr
		}
	}()

	// Classify by the transient prefix: under the staging posture buildUploadScript
	// prints `UPLOAD,<prefix><hash>,...`, so every FILE descriptor already carries
	// the prefix; a descriptor that does not came from ParseStageOutLogs' result-JSON
	// branch, where stage-out skipped the upload entirely ("result JSON wins",
	// Docker's rule). ParseStageOutLogs returns ONE branch, so the two cases are
	// exclusive.
	underStaging := func(o transport.OutputFile) bool {
		return strings.HasPrefix(o.ContentHash, stagingUploadParamsFromTask(task).Prefix)
	}
	if len(outs) > 0 && !underStaging(outs[0]) {
		// Result-JSON outputs: nothing was staged for them, so this is the
		// metadata-only Step-5 consult (executor.go :1194, uploadPending=false).
		if blocked, reason := k8sScanConsult(ctx, task, outs, false); blocked {
			return nil, fmt.Errorf("%s", reason)
		}
		// Result JSON already names where its bytes live; the worker copies
		// nothing.
		return outs, nil
	}

	// File outputs: normalize each descriptor to its staging key exactly as
	// executor.go :2003 does (the prepend covers a manifest line that printed a
	// bare hash), so the consult carries the staging_key task_output_scan.py
	// _validate_staging_key fetches.
	for i := range outs {
		key := outs[i].ContentHash
		if !strings.HasPrefix(key, stagingUploadParamsFromTask(task).Prefix) {
			key = stagingUploadParamsFromTask(task).Prefix + key
		}
		outs[i].StagingKey = key
	}
	if blocked, reason := k8sScanConsult(ctx, task, outs, true); blocked {
		// Nothing durable was ever written: the refusal is the k8s twin of the
		// Docker path's blocked Result (ISO 27001: a refused object is simply
		// never written, nothing is deleted).
		return nil, fmt.Errorf("%s", reason)
	}

	// Allowed ⇒ promote staging -> durable with a SERVER-SIDE CopyObject: no task
	// bytes ever cross the worker wire, so there is nothing to re-download and
	// verify (the brief's note) — a StatObject size check on the durable key is
	// enough. Creds go through the native path's re-mint margin helper
	// (EnsureCredsFresh + nativeCredRecheckMargin, reused as-is); this seam has
	// no re-mint callback in its signature, so reMint == nil: fresh creds pass
	// through untouched and near-expiry creds are an error rather than a silent
	// use of dead creds (fail-closed; the dispatch reports it retryable=true).
	fresh, err := EnsureCredsFresh(ctx, creds, task.ID, nativeCredRecheckMargin(), nil)
	if err != nil {
		return nil, err
	}
	client, err := s3ClientFor(fresh.MinIO)
	if err != nil {
		return nil, err
	}
	// The durable key honours the wizard_test_run OutputPrefix (uploadScriptParamsFromTask),
	// so a promoted copy lands exactly where the pre-968 direct upload did.
	durable := uploadScriptParamsFromTask(task)
	stagingPrefix := stagingUploadParamsFromTask(task).Prefix
	for i := range outs {
		src := outs[i].StagingKey
		hash := strings.TrimPrefix(src, stagingPrefix)
		dst := durable.Prefix + hash
		// Loan markers: stage-out's `mc cp --attr` (LoanOutputAttr) used to stamp
		// the DURABLE object in-pod; under staging the transient blob is never
		// stamped (943's rule), so the marker moves here. ReplaceMetadata true
		// stamps LoanOutputMetadata(loanID); false preserves the source's
		// metadata (minio-go ignores UserMetadata then).
		loaned := task.Payload.LoanID != ""
		dstOpts := minio.CopyDestOptions{
			Bucket:          durable.Bucket,
			Object:          dst,
			ReplaceMetadata: loaned,
		}
		if loaned {
			dstOpts.UserMetadata = LoanOutputMetadata(task.Payload.LoanID)
		}
		if _, cerr := client.CopyObject(ctx, dstOpts,
			minio.CopySrcOptions{Bucket: durable.Bucket, Object: src}); cerr != nil {
			return nil, fmt.Errorf("k8s scan staging: copy %s -> %s: %w", src, dst, cerr)
		}
		st, serr := client.StatObject(ctx, durable.Bucket, dst, minio.StatObjectOptions{})
		if serr != nil {
			return nil, fmt.Errorf("k8s scan staging: stat durable %s: %w", dst, serr)
		}
		if outs[i].SizeBytes > 0 && st.Size != outs[i].SizeBytes {
			return nil, fmt.Errorf("k8s scan staging: durable %s is %d bytes, manifest said %d",
				outs[i].StagingKey, st.Size, outs[i].SizeBytes)
		}
		// Return the durable descriptor WITHOUT StagingKey — omitempty keeps the
		// /complete wire shape byte-identical to the pre-968 direct upload.
		outs[i].ContentHash = dst
		outs[i].StagingKey = ""
	}
	return outs, nil
}
