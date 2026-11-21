package executor

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
)

func TestK8sStatusMapping(t *testing.T) {
	cases := []struct {
		name      string
		in        *TaskStatus
		status    string
		reason    string
		oom, retr bool
		code      int
	}{
		{"nil", nil, NodeRunRunning, "", false, false, 0},
		{"running", &TaskStatus{Phase: TaskRunning}, NodeRunRunning, "", false, false, 0},
		{"succeeded", &TaskStatus{Phase: TaskSucceeded}, NodeRunCompleted, "", false, false, 0},
		{"oom", &TaskStatus{Phase: TaskFailed, ExitCode: 137, Err: "BackoffLimitExceeded: Job has reached the specified backoff limit: OOMKilled"}, NodeRunFailed, ReasonOOMKilled, true, false, 137},
		{"bare137", &TaskStatus{Phase: TaskFailed, ExitCode: 137, Err: ""}, NodeRunFailed, "", true, false, 137},
		{"imagepull", &TaskStatus{Phase: TaskFailed, ExitCode: -1, Err: "ImagePullBackOff: Back-off pulling image"}, NodeRunFailed, ReasonImagePull, false, false, -1},
		{"errimagepull", &TaskStatus{Phase: TaskFailed, ExitCode: -1, Err: "ErrImagePull: nope"}, NodeRunFailed, ReasonImagePull, false, false, -1},
		{"unsched", &TaskStatus{Phase: TaskFailed, ExitCode: -1, Err: "Unschedulable: 0/3 nodes available"}, NodeRunFailed, ReasonUnschedulable, false, true, -1},
		{"deadline", &TaskStatus{Phase: TaskFailed, ExitCode: -1, Err: "DeadlineExceeded: Job was active longer than specified deadline"}, NodeRunFailed, ReasonDeadlineExceeded, false, false, -1},
		{"notfound", &TaskStatus{Phase: TaskFailed, ExitCode: -1, Err: "job not found (deleted or TTL-expired)"}, NodeRunFailed, ReasonJobNotFound, false, true, -1},
		{"exit3", &TaskStatus{Phase: TaskFailed, ExitCode: 3, Err: "container exited: Error"}, NodeRunFailed, ReasonContainerExit, false, false, 3},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			u := MapTaskStatus(c.in)
			if u.Status != c.status || u.Reason != c.reason || u.OOMKilled != c.oom || u.ShouldRetry != c.retr || u.ExitCode != c.code {
				t.Fatalf("got %+v", u)
			}
			if u.Status == NodeRunFailed {
				if !strings.Contains(u.ErrorMessage, c.reason) {
					t.Errorf("message %q lacks reason %q", u.ErrorMessage, c.reason)
				}
				if c.code >= 0 && !strings.Contains(u.ErrorMessage, "exit code") {
					t.Errorf("message %q lacks exit code", u.ErrorMessage)
				}
			} else if u.ErrorMessage != "" {
				t.Errorf("non-failed status carries message %q", u.ErrorMessage)
			}
		})
	}
}

func TestK8sWithTaskIDCopiesEnv(t *testing.T) {
	orig := &TaskSpec{Image: "img", Env: map[string]string{"A": "1"}}
	out := WithTaskID(orig, "42")
	if out.Env["MOD_TASK_ID"] != "42" || out.Env["A"] != "1" || out.Image != "img" {
		t.Fatalf("bad spec %+v", out)
	}
	if _, ok := orig.Env["MOD_TASK_ID"]; ok {
		t.Fatal("original env mutated")
	}
	if taskIDFromEnv(out.Env) != "42" {
		t.Fatal("K8sBackend would not see the task id")
	}
	if WithTaskID(nil, "7").Env["MOD_TASK_ID"] != "7" {
		t.Fatal("nil spec not handled")
	}
}

// scriptedBackend returns a fixed sequence of Poll results.
type scriptedBackend struct {
	seq []*TaskStatus
	err []error
	i   int
}

func (s *scriptedBackend) Submit(context.Context, *TaskSpec) (*Handle, error) { return nil, nil }
func (s *scriptedBackend) Logs(context.Context, *Handle) (io.ReadCloser, error) {
	return nil, nil
}
func (s *scriptedBackend) Cancel(context.Context, *Handle) error { return nil }
func (s *scriptedBackend) Poll(context.Context, *Handle) (*TaskStatus, error) {
	i := s.i
	if i >= len(s.seq) {
		i = len(s.seq) - 1
	}
	s.i++
	var err error
	if i < len(s.err) {
		err = s.err[i]
	}
	if err != nil {
		return nil, err
	}
	return s.seq[i], nil
}

func TestK8sPollUntilDoneEmitsOnChangeOnly(t *testing.T) {
	run := &TaskStatus{Phase: TaskRunning}
	b := &scriptedBackend{
		seq: []*TaskStatus{run, run, nil, run, {Phase: TaskFailed, ExitCode: 137, Err: "OOMKilled"}},
		err: []error{nil, nil, errors.New("transient"), nil, nil},
	}
	var got []string
	u, err := PollUntilDone(context.Background(), b, &Handle{Backend: "k8s", ID: "ns/j"}, time.Millisecond,
		func(u NodeRunUpdate) { got = append(got, u.Status) })
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(got, ",") != "running,failed" {
		t.Fatalf("updates = %v", got)
	}
	if !u.OOMKilled || u.Status != NodeRunFailed {
		t.Fatalf("final = %+v", u)
	}
}

func TestK8sPollUntilDoneCtxCancel(t *testing.T) {
	b := &scriptedBackend{seq: []*TaskStatus{{Phase: TaskRunning}}}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	u, err := PollUntilDone(ctx, b, &Handle{}, time.Millisecond, nil)
	if !errors.Is(err, context.DeadlineExceeded) || u.Status != NodeRunRunning {
		t.Fatalf("u=%+v err=%v", u, err)
	}
}

func TestBackendSelectDockerDefault(t *testing.T) {
	for _, cfg := range []*config.Config{nil, {}, {ExecutorBackend: config.BackendDocker}} {
		b, err := NewBackendFromConfig(cfg, nil, nil)
		if err != nil {
			t.Fatal(err)
		}
		if _, ok := b.(*DockerBackend); !ok {
			t.Fatalf("want DockerBackend, got %T", b)
		}
	}
}

func TestBackendSelectK8sInvalidKubeconfigErrors(t *testing.T) {
	t.Setenv("KUBERNETES_SERVICE_HOST", "")
	cfg := &config.Config{ExecutorBackend: config.BackendK8s, KubeconfigPath: "/nonexistent/kubeconfig"}
	b, err := NewBackendFromConfig(cfg, nil, nil)
	if err == nil || b != nil {
		t.Fatalf("expected error and nil backend, got %T / %v", b, err)
	}
}

func TestBackendSelectK8sValidKubeconfig(t *testing.T) {
	t.Setenv("KUBERNETES_SERVICE_HOST", "")
	t.Setenv("MOD_K8S_NAMESPACE", "tasks-x")
	p := filepath.Join(t.TempDir(), "kubeconfig")
	kc := `apiVersion: v1
kind: Config
clusters:
- cluster: {server: "https://127.0.0.1:6443"}
  name: c
contexts:
- context: {cluster: c, user: u}
  name: ctx
current-context: ctx
users:
- name: u
  user: {token: abc}
`
	if err := os.WriteFile(p, []byte(kc), 0o600); err != nil {
		t.Fatal(err)
	}
	b, err := NewBackendFromConfig(&config.Config{ExecutorBackend: config.BackendK8s, KubeconfigPath: p}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	kb, ok := b.(*K8sBackend)
	if !ok {
		t.Fatalf("want K8sBackend, got %T", b)
	}
	if kb.Namespace != "tasks-x" {
		t.Fatalf("namespace = %q", kb.Namespace)
	}
}
