//go:build !windows

package executor

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// fakeWslcScript is a POSIX-shell stand-in for wslc.exe (design doc §4b:
// the backend is tested against the CLI surface, so the fake implements
// the same verbs the backend shells out to: run, container list, container
// inspect, container stop, container rm). State lives under
// $FAKE_WSLC_STATE so every invocation sees the same container table.
//
// Like the real (attached) `wslc run`, `run` blocks for the container's
// lifetime and streams the container's stdout to its own stdout (here: one
// "wslc-fake stdout of <image>" line); it then settles the container to
// exited with $FAKE_WSLC_EXIT (default 0) after $FAKE_WSLC_RUN_SLEEP
// (default 0.1s). The fake honors the confirmed flag set only (no
// --log-driver/--log-opt; --gpus presence-only) and prints one
// "id name" line per container for `container list` (the backend parses
// the list output defensively).
const fakeWslcScript = `#!/bin/sh
state="$FAKE_WSLC_STATE"
mkdir -p "$state"
[ -f "$state/invocations" ] || : > "$state/invocations"
line="inv "
for a in "$@"; do line="$line[$a]"; done
printf '%s\n' "$line" >> "$state/invocations"

# --- container table ------------------------------------------------------
# rows: "id name state exitcode"
table="$state/containers"
[ -f "$table" ] || : > "$table"
lookup() { awk -v n="$1" '$2 == n { print; found=1 } END { if (!found) exit 1 }' "$table"; }

case "$1" in
login)
  # GP.17f fake: wslc login <host> --username <u> --password-stdin.
  # Records argv (the password must NEVER appear here) and, when
  # --password-stdin is present, records what arrived on stdin (one line).
  shift
  host=""
  username=""
  password_stdin=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --username) username="$2"; shift 2; continue ;;
      --password) shift 2; continue ;;
      --password-stdin) password_stdin=1; shift; continue ;;
      *) host="$1"; shift; continue ;;
    esac
  done
  if [ -n "$FAKE_WSLC_LOGIN_FAIL" ]; then
    echo "wslc: login: denied" >&2
    exit "${FAKE_WSLC_LOGIN_FAIL}"
  fi
  if [ "$password_stdin" = 1 ]; then
    read -r stdin_line
    echo "login-stdin:$stdin_line" >> "$state/login_stdin"
  fi
  echo "login $host user=$username" >> "$state/login_events"
  echo "Login Succeeded"
  exit 0
  ;;
run)
  # docker-like: run [--flag [val]]... IMAGE CMD...
  shift # drop "run"
  name=""; image=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --name) name="$2"; shift 2; continue ;;
      --network) shift 2; continue ;;
      -e|--cpus|--memory) shift 2; continue ;;
      --rm|--gpus) shift; continue ;;
    esac
    break
  done
  # remaining args: IMAGE CMD...
  [ $# -gt 0 ] || { echo "wslc: run: missing image" >&2; exit 64; }
  image="$1"; shift
  [ -n "$name" ] || { echo "wslc: run: missing --name" >&2; exit 64; }
  cid="fake-$(echo "$name" | tr -dc 'a-z0-9')"
  echo "$cid $name running 0" > "$table"
  # Stream the container's stdout to the run process's stdout (the backend
  # tees it to the task log).
  echo "wslc-fake stdout of $image"
  sleep "${FAKE_WSLC_RUN_SLEEP:-0.1}"
  code="${FAKE_WSLC_EXIT:-0}"
  sed -i "s/ running 0$/ exited $code/" "$table"
  exit "$code"
  ;;
container)
  sub="$2"
  case "$sub" in
  list)
    # The backend calls: container list (confirmed verb spelling) and
    # parses the output defensively, so emit plain "id name" rows.
    while read -r id nm st ec; do
      [ -n "$id" ] || continue
      echo "$id $nm"
    done < "$table"
    ;;
  inspect)
    row="$(lookup "$3" 2>/dev/null)" || { echo "Error: no such container: $3" >&2; exit 1; }
    set -- $row
    cat <<EOF
[{"Id":"$1","State":{"Status":"$3","ExitCode":$4}}]
EOF
    ;;
  stop)
    row="$(lookup "$3" 2>/dev/null)" || { echo "Error: no such container: $3" >&2; exit 1; }
    set -- $row
    echo "$1 $2 exited 137" > "$table"
    echo "$1"
    ;;
  rm)
    row="$(lookup "$3" 2>/dev/null)" || { echo "Error: no such container: $3" >&2; exit 1; }
    set -- $row
    grep -v "^$1 " "$table" > "$table.tmp" && mv "$table.tmp" "$table"
    echo "$1"
    ;;
  *)
    echo "wslc: unknown container subcommand: $sub" >&2
    exit 64
    ;;
  esac
  ;;
*)
  echo "wslc: unknown verb: $1" >&2
  exit 64
  ;;
esac
`

// newTestWslcBackend builds a WslcBackend pointed at a fake wslc script in
// t.TempDir, and returns the backend, its scratch root, and the fake's
// invocation log (re-readable at any time).
func newTestWslcBackend(t *testing.T) (*WslcBackend, string, string) {
	t.Helper()
	dir := t.TempDir()
	script := filepath.Join(dir, "wslc-fake")
	if err := os.WriteFile(script, []byte(fakeWslcScript), 0755); err != nil {
		t.Fatalf("writing fake wslc: %v", err)
	}
	state := filepath.Join(dir, "state")
	scratch := filepath.Join(dir, "scratch")
	if err := os.MkdirAll(scratch, 0755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FAKE_WSLC_STATE", state)
	return NewWslcBackend(script, scratch), scratch, filepath.Join(state, "invocations")
}

func wslcInvocations(t *testing.T, path string) []string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var lines []string
	for _, l := range strings.Split(string(data), "\n") {
		if l != "" {
			lines = append(lines, l)
		}
	}
	return lines
}

func waitWslc(t *testing.T, b *WslcBackend, h *Handle, timeout time.Duration) *TaskStatus {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		st, err := b.Poll(context.Background(), h)
		if err != nil {
			t.Fatalf("Poll: %v", err)
		}
		if st.Phase != TaskRunning {
			return st
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("task %s still running after %s", h.ID, timeout)
	return nil
}

// TestWslcArgMapping verifies the TaskSpec → `wslc run` argv mapping.
func TestWslcArgMapping(t *testing.T) {
	b, _, inv := newTestWslcBackend(t)
	spec := &TaskSpec{
		Image:  "reg.local/tenant/img:v1",
		Env:    map[string]string{"MOD_TASK_ID": "tmap", "FOO": "bar baz", "NEEDS_INTERNET": ""},
		Cmd:    []string{"python", "/app/run.py", "--opt"},
		GPU:    GPURequest{Count: 1},
		Egress: EgressDefaultDeny,
		Resources: ResourceLimits{
			CPUCores: 2.5,
			MemoryMB: 1024,
		},
	}
	if _, err := b.Submit(context.Background(), spec); err != nil {
		t.Fatalf("Submit: %v", err)
	}
	st := waitWslc(t, b, &Handle{Backend: "wslc", ID: "tmap"}, 5*time.Second)
	if st.Phase != TaskSucceeded {
		t.Fatalf("expected succeeded, got %+v", st)
	}

	var runLine string
	for _, l := range wslcInvocations(t, inv) {
		if strings.HasPrefix(l, "inv [run]") {
			runLine = l
		}
	}
	if runLine == "" {
		t.Fatal("no `wslc run` invocation recorded")
	}
	mustContain := []string{
		"[--name][mod-tmap]",
		"[--rm]",
		"[--network][none]",
		"[-e][FOO=bar baz]",
		"[-e][NEEDS_INTERNET=]",
		"[-e][MOD_TASK_ID=tmap]",
		"[--gpus]",
		"[--cpus][2.5]",
		"[--memory][1024M]",
		"[reg.local/tenant/img:v1]",
		"[python]",
		"[/app/run.py]",
		"[--opt]",
	}
	for _, want := range mustContain {
		if !strings.Contains(runLine, want) {
			t.Errorf("run argv missing %s in:\n%s", want, runLine)
		}
	}
	// GP.17b: wslc has no --log-driver/--log-opt, so neither may appear.
	for _, banned := range []string{"[--log-driver]", "[--log-opt]"} {
		if strings.Contains(runLine, banned) {
			t.Errorf("run argv must not contain %s in:\n%s", banned, runLine)
		}
	}

	// GPU-less, zero-value egress, no resources: no --gpu/cpus/memory
	// flags, and zero-value egress still maps to --network none.
	b2, _, inv2 := newTestWslcBackend(t)
	spec2 := &TaskSpec{
		Image: "img2",
		Env:   map[string]string{"MOD_TASK_ID": "tmap2"},
		Cmd:   []string{"echo", "hi"},
	}
	if _, err := b2.Submit(context.Background(), spec2); err != nil {
		t.Fatalf("Submit2: %v", err)
	}
	waitWslc(t, b2, &Handle{Backend: "wslc", ID: "tmap2"}, 5*time.Second)
	for _, l := range wslcInvocations(t, inv2) {
		if strings.HasPrefix(l, "inv [run]") {
			for _, banned := range []string{"[--gpus]", "[--cpus]", "[--memory]"} {
				if strings.Contains(l, banned) {
					t.Errorf("unexpected %s for zero-value spec in:\n%s", banned, l)
				}
			}
			if !strings.Contains(l, "[--network][none]") {
				t.Errorf("zero-value egress must map to --network none:\n%s", l)
			}
		}
	}
}

// TestWslcSubmitPollSuccess covers submit → running → succeeded.
func TestWslcSubmitPollSuccess(t *testing.T) {
	b, _, _ := newTestWslcBackend(t)
	spec := &TaskSpec{
		Image:  "img:ok",
		Env:    map[string]string{"MOD_TASK_ID": "tsucc"},
		Cmd:    []string{"true"},
		Egress: EgressDefaultDeny,
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	if h.Backend != "wslc" || h.ID != "tsucc" {
		t.Fatalf("unexpected handle %+v", h)
	}
	st := waitWslc(t, b, h, 8*time.Second)
	if st.Phase != TaskSucceeded {
		t.Fatalf("expected succeeded, got %+v", st)
	}
	if st.ExitCode != 0 {
		t.Errorf("expected exit 0, got %d", st.ExitCode)
	}
	// Poll is idempotent after settling.
	st2, err := b.Poll(context.Background(), h)
	if err != nil {
		t.Fatalf("re-Poll: %v", err)
	}
	if st2.Phase != TaskSucceeded {
		t.Errorf("re-Poll expected succeeded, got %+v", st2)
	}
}

// TestWslcNonZeroExit covers container exit code propagation to failed.
func TestWslcNonZeroExit(t *testing.T) {
	b, _, _ := newTestWslcBackend(t)
	t.Setenv("FAKE_WSLC_EXIT", "42")
	spec := &TaskSpec{
		Image:  "img:fail",
		Env:    map[string]string{"MOD_TASK_ID": "tfail"},
		Cmd:    []string{"false"},
		Egress: EgressDefaultDeny,
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	st := waitWslc(t, b, h, 8*time.Second)
	if st.Phase != TaskFailed {
		t.Fatalf("expected failed, got %+v", st)
	}
	if st.ExitCode != 42 {
		t.Errorf("expected exit code 42, got %d", st.ExitCode)
	}
}

// TestWslcLogs verifies Logs() returns the container's captured output
// (the watch tees the attached run's stdout into <id>/task.log, and the
// fake streams "wslc-fake stdout of <image>" on stdout) and keeps working
// after the run has exited.
func TestWslcLogs(t *testing.T) {
	b, scratch, _ := newTestWslcBackend(t)
	spec := &TaskSpec{
		Image:  "img:logs",
		Env:    map[string]string{"MOD_TASK_ID": "tlogs"},
		Cmd:    []string{"echo", "hello from wslc"},
		Egress: EgressDefaultDeny,
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	st := waitWslc(t, b, h, 8*time.Second)
	if st.Phase != TaskSucceeded {
		t.Fatalf("expected succeeded, got %+v", st)
	}

	rc, err := b.Logs(context.Background(), h)
	if err != nil {
		t.Fatalf("Logs: %v", err)
	}
	data, err := io.ReadAll(rc)
	rc.Close()
	if err != nil {
		t.Fatalf("ReadAll: %v", err)
	}
	if !strings.Contains(string(data), "wslc-fake stdout of img:logs") {
		t.Errorf("logs missing expected content: %q", data)
	}

	rc2, err := b.Logs(context.Background(), h)
	if err != nil {
		t.Fatalf("Logs after exit: %v", err)
	}
	rc2.Close()
	if _, err := os.Stat(filepath.Join(scratch, "tlogs", "task.log")); err != nil {
		t.Errorf("task log file missing: %v", err)
	}
}

// TestWslcCancel covers the hard-evict path: cancel a still-running task,
// verify it settles to failed, and that the container is stopped+removed.
func TestWslcCancel(t *testing.T) {
	b, _, inv := newTestWslcBackend(t)
	// Long-lived container: the fake `run` blocks this long, so the task
	// is TaskRunning when Cancel lands.
	t.Setenv("FAKE_WSLC_RUN_SLEEP", "10")
	spec := &TaskSpec{
		Image:  "img:cancel",
		Env:    map[string]string{"MOD_TASK_ID": "tcancel"},
		Cmd:    []string{"sleep", "30"},
		Egress: EgressDefaultDeny,
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	// Wait until the task is running AND the container is registered in
	// the fake's table (the watch persists the learned ID to
	// container.json, which only happens once `wslc container list` shows
	// the container — i.e. the table has the row).
	cidPath := filepath.Join(b.ScratchRoot, "tcancel", "container.json")
	deadline := time.Now().Add(5 * time.Second)
	for {
		st, perr := b.Poll(context.Background(), h)
		if perr != nil {
			t.Fatalf("Poll: %v", perr)
		}
		if _, err := os.Stat(cidPath); err == nil && st.Phase == TaskRunning {
			break
		}
		if st.Phase != TaskRunning || time.Now().After(deadline) {
			_, idErr := os.Stat(cidPath)
			t.Fatalf("task did not reach running with a registered container (phase=%v, id file: %v)", st.Phase, idErr)
		}
		time.Sleep(20 * time.Millisecond)
	}

	if err := b.Cancel(context.Background(), h); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	// Cancel is idempotent.
	if err := b.Cancel(context.Background(), h); err != nil {
		t.Fatalf("Cancel (2nd): %v", err)
	}

	st := waitWslc(t, b, h, 8*time.Second)
	if st.Phase != TaskFailed {
		t.Fatalf("expected failed after cancel, got %+v", st)
	}

	var sawStop, sawRm bool
	for _, l := range wslcInvocations(t, inv) {
		if strings.HasPrefix(l, "inv [container][stop][mod-tcancel]") {
			sawStop = true
		}
		if strings.HasPrefix(l, "inv [container][rm][mod-tcancel]") {
			sawRm = true
		}
	}
	if !sawStop {
		t.Error("Cancel did not issue `wslc container stop`")
	}
	if !sawRm {
		t.Error("Cancel did not issue `wslc container rm`")
	}
}

// TestWslcEgressRefuse (GP.17g) verifies user-egress and allowlist tasks
// are REFUSED at Submit with a clear "needs a docker-capable worker" error
// — and that the fake wslc binary is never invoked (fail-closed: the task
// never starts on a network the backend cannot police).
func TestWslcEgressRefuse(t *testing.T) {
	b, _, inv := newTestWslcBackend(t)
	for _, egress := range []EgressPolicy{EgressUserEgress, EgressAllowlist} {
		spec := &TaskSpec{
			Image:       "img:egress",
			Env:         map[string]string{"MOD_TASK_ID": "teg-" + string(egress)},
			Cmd:         []string{"true"},
			Egress:      egress,
			EgressHosts: []string{"example.com"},
		}
		_, err := b.Submit(context.Background(), spec)
		if err == nil {
			t.Fatalf("Submit with egress %q should fail", egress)
		}
		if !strings.Contains(err.Error(), "cannot be enforced") {
			t.Errorf("error should say the policy cannot be enforced, got: %v", err)
		}
		if !strings.Contains(err.Error(), "docker-capable worker") {
			t.Errorf("error should point at docker-capable workers, got: %v", err)
		}
		if got := wslcInvocations(t, inv); len(got) != 0 {
			t.Errorf("fake wslc must NOT be invoked for refused egress %q, got invocations: %v", egress, got)
		}
	}
}

// TestWslcEgressForcedProxy (GP.17g) verifies the explicit escape hatch:
// with ForceEgressProxy set on the backend, a user-egress task runs
// Bridged with HTTP(S)_PROXY/NO_PROXY env injected. This is
// proxy-forcing, not enforcement (design doc §3.1).
func TestWslcEgressForcedProxy(t *testing.T) {
	b, _, inv := newTestWslcBackend(t)
	b.ForceEgressProxy = "http://198.51.100.1:8888"
	spec := &TaskSpec{
		Image:  "img:proxy",
		Env:    map[string]string{"MOD_TASK_ID": "teproxy"},
		Cmd:    []string{"true"},
		Egress: EgressUserEgress,
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit with forced proxy: %v", err)
	}
	if st := waitWslc(t, b, h, 5*time.Second); st.Phase != TaskSucceeded {
		t.Fatalf("expected succeeded, got %+v", st)
	}
	var runLine string
	for _, l := range wslcInvocations(t, inv) {
		if strings.HasPrefix(l, "inv [run]") {
			runLine = l
		}
	}
	if runLine == "" {
		t.Fatal("no `wslc run` invocation recorded")
	}
	for _, want := range []string{
		"[--network][bridged]",
		"[-e][HTTP_PROXY=http://198.51.100.1:8888]",
		"[-e][HTTPS_PROXY=http://198.51.100.1:8888]",
		"[-e][NO_PROXY=localhost,127.0.0.1]",
	} {
		if !strings.Contains(runLine, want) {
			t.Errorf("run argv missing %s in:\n%s", want, runLine)
		}
	}
	if strings.Contains(runLine, "[--network][none]") {
		t.Errorf("forced-proxy task must not run None:\n%s", runLine)
	}
}

// TestWslcEgressUnknown (GP.17g) verifies an unknown policy string fails
// closed to None — it is never mapped to Bridged/unrestricted.
func TestWslcEgressUnknown(t *testing.T) {
	b, _, inv := newTestWslcBackend(t)
	spec := &TaskSpec{
		Image:  "img:unk",
		Env:    map[string]string{"MOD_TASK_ID": "teunk"},
		Cmd:    []string{"true"},
		Egress: EgressPolicy("mystery-mode"),
	}
	h, err := b.Submit(context.Background(), spec)
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	if st := waitWslc(t, b, h, 5*time.Second); st.Phase != TaskSucceeded {
		t.Fatalf("expected succeeded, got %+v", st)
	}
	for _, l := range wslcInvocations(t, inv) {
		if strings.HasPrefix(l, "inv [run]") {
			if !strings.Contains(l, "[--network][none]") {
				t.Errorf("unknown egress must fail closed to none:\n%s", l)
			}
		}
	}
}

// TestWslcBadHandleAndSpec mirrors the native backend's defensive checks.
func TestWslcBadHandleAndSpec(t *testing.T) {
	b, _, _ := newTestWslcBackend(t)
	ctx := context.Background()

	if _, err := b.Submit(ctx, nil); err == nil {
		t.Error("Submit(nil) should fail")
	}
	if _, err := b.Submit(ctx, &TaskSpec{Image: "img", Cmd: []string{"true"}}); err == nil {
		t.Error("Submit without MOD_TASK_ID should fail")
	}
	if _, err := b.Submit(ctx, &TaskSpec{Env: map[string]string{"MOD_TASK_ID": "x"}, Cmd: []string{"true"}}); err == nil {
		t.Error("Submit without Image should fail")
	}
	if _, err := b.Submit(ctx, &TaskSpec{Env: map[string]string{"MOD_TASK_ID": "x"}, Image: "img"}); err == nil {
		t.Error("Submit without Cmd should fail")
	}

	// Duplicate submit while the first is still tracked must fail.
	spec := &TaskSpec{
		Image:  "img:dup",
		Env:    map[string]string{"MOD_TASK_ID": "tdup"},
		Cmd:    []string{"true"},
		Egress: EgressDefaultDeny,
	}
	if _, err := b.Submit(ctx, spec); err != nil {
		t.Fatalf("Submit dup-1: %v", err)
	}
	if _, err := b.Submit(ctx, spec); err == nil {
		t.Error("duplicate Submit should fail")
	}
	waitWslc(t, b, &Handle{Backend: "wslc", ID: "tdup"}, 5*time.Second)

	// Wrong-backend handles are rejected on every call.
	bh := &Handle{Backend: "docker", ID: "tdup"}
	if _, err := b.Poll(ctx, bh); err == nil {
		t.Error("Poll with wrong backend should fail")
	}
	if rc, err := b.Logs(ctx, bh); err == nil {
		rc.Close()
		t.Error("Logs with wrong backend should fail")
	}
	if err := b.Cancel(ctx, bh); err == nil {
		t.Error("Cancel with wrong backend should fail")
	}
	// Unknown same-backend handles are rejected too.
	uk := &Handle{Backend: "wslc", ID: "nope"}
	if _, err := b.Poll(ctx, uk); err == nil {
		t.Error("Poll with unknown task should fail")
	}
	if _, err := b.Logs(ctx, uk); err == nil {
		t.Error("Logs with unknown task should fail")
	}
	if err := b.Cancel(ctx, uk); err == nil {
		t.Error("Cancel with unknown task should fail")
	}
}

// TestWslcBinaryPathInjection verifies the injectable binary path is what
// actually runs (the fake is only reachable through injection — the
// default C:\ path does not exist on linux).
func TestWslcBinaryPathInjection(t *testing.T) {
	def := NewWslcBackend("", t.TempDir())
	if def.BinaryPath != wslcDefaultBinary {
		t.Errorf("empty binary path should default to %q, got %q", wslcDefaultBinary, def.BinaryPath)
	}

	b, _, inv := newTestWslcBackend(t)
	if _, err := os.Stat(b.BinaryPath); err != nil {
		t.Fatalf("injected binary does not exist: %v", err)
	}
	spec := &TaskSpec{
		Image:  "img:inj",
		Env:    map[string]string{"MOD_TASK_ID": "tinj"},
		Cmd:    []string{"true"},
		Egress: EgressDefaultDeny,
	}
	if _, err := b.Submit(context.Background(), spec); err != nil {
		t.Fatalf("Submit: %v", err)
	}
	waitWslc(t, b, &Handle{Backend: "wslc", ID: "tinj"}, 5*time.Second)
	if len(wslcInvocations(t, inv)) == 0 {
		t.Error("fake wslc was never invoked — binary injection not honored")
	}
}

// --- GP.17f: registry login (wslc login <host> --username u --password-stdin)

// wslcTaskWithRegistryAuth builds a minimal spec carrying the given
// platform/tenant creds.
func wslcTaskWithRegistryAuth(id, image string, platform, tenant transport.HarborCredentials) *TaskSpec {
	return &TaskSpec{
		Image:  image,
		Env:    map[string]string{"MOD_TASK_ID": id},
		Cmd:    []string{"true"},
		Egress: EgressDefaultDeny,
		RegistryAuth: TaskRegistryAuth{
			Platform: platform,
			Tenant:   tenant,
		},
	}
}

// TestWslcRegistryLoginCachesPerSession verifies a login is issued once for
// the first task and NOT repeated for a second task with the same
// (registry host, username) — the session-scoped cache.
func TestWslcRegistryLoginCachesPerSession(t *testing.T) {
	b, _, inv := newTestWslcBackend(t)
	creds := transport.HarborCredentials{
		Registry: "https://stage-registry.example.com/",
		Username: "robot$platform",
		Password: "s3cr3t-password",
	}
	for _, id := range []string{"treg1", "treg2"} {
		spec := wslcTaskWithRegistryAuth(id, "stage-registry.example.com/proj/img:v1", creds, transport.HarborCredentials{})
		if _, err := b.Submit(context.Background(), spec); err != nil {
			t.Fatalf("Submit %s: %v", id, err)
		}
		waitWslc(t, b, &Handle{Backend: "wslc", ID: id}, 5*time.Second)
	}
	logins := 0
	for _, l := range wslcInvocations(t, inv) {
		if strings.HasPrefix(l, "inv [login]") {
			logins++
		}
	}
	if logins != 1 {
		t.Fatalf("expected exactly 1 login invocation for 2 same-cred tasks, got %d", logins)
	}
}

// TestWslcRegistryLoginPasswordOnStdinNotArgv verifies the password reaches
// the login process via STDIN only: it appears in the fake's captured
// stdin file and NOWHERE in the invocation argv log or debug log.
func TestWslcRegistryLoginPasswordOnStdinNotArgv(t *testing.T) {
	b, _, inv := newTestWslcBackend(t)
	const password = "hunter2-not-in-argv"
	creds := transport.HarborCredentials{
		Registry: "stage-registry.example.com",
		Username: "robot$platform",
		Password: password,
	}
	spec := wslcTaskWithRegistryAuth("tregstdin", "stage-registry.example.com/proj/img:v1", creds, transport.HarborCredentials{})
	if _, err := b.Submit(context.Background(), spec); err != nil {
		t.Fatalf("Submit: %v", err)
	}
	waitWslc(t, b, &Handle{Backend: "wslc", ID: "tregstdin"}, 5*time.Second)

	// The login invocation must exist and carry host + --username +
	// --password-stdin, but never the password.
	var loginLine string
	for _, l := range wslcInvocations(t, inv) {
		if strings.HasPrefix(l, "inv [login]") {
			loginLine = l
		}
	}
	if loginLine == "" {
		t.Fatal("no `wslc login` invocation recorded")
	}
	for _, want := range []string{
		"[login]", "[stage-registry.example.com]",
		"[--username][robot$platform]", "[--password-stdin]",
	} {
		if !strings.Contains(loginLine, want) {
			t.Errorf("login argv missing %s in:\n%s", want, loginLine)
		}
	}
	if strings.Contains(loginLine, password) {
		t.Errorf("password must not appear on argv:\n%s", loginLine)
	}

	// The password DID arrive on stdin (fake records it).
	stdinData, err := os.ReadFile(filepath.Join(filepath.Dir(inv), "login_stdin"))
	if err != nil {
		t.Fatalf("fake never captured login stdin: %v", err)
	}
	if !strings.Contains(string(stdinData), password) {
		t.Errorf("password missing from login stdin capture (redacted check: contains %q)", "hunter2-not-in-argv")
	}

	// And the backend's own debug log never contains the password either.
	debugData, _ := os.ReadFile(filepath.Join(b.ScratchRoot, "wslc-debug.log"))
	if strings.Contains(string(debugData), password) {
		t.Errorf("password leaked into the wslc debug log")
	}
}

// TestWslcRegistryLoginSkippedWithoutCreds verifies no login happens when
// the task's credential bundle carries no registry creds (public image).
func TestWslcRegistryLoginSkippedWithoutCreds(t *testing.T) {
	b, _, inv := newTestWslcBackend(t)
	spec := &TaskSpec{
		Image:  "public.example.com/proj/img:v1",
		Env:    map[string]string{"MOD_TASK_ID": "tregnone"},
		Cmd:    []string{"true"},
		Egress: EgressDefaultDeny,
	}
	if _, err := b.Submit(context.Background(), spec); err != nil {
		t.Fatalf("Submit: %v", err)
	}
	waitWslc(t, b, &Handle{Backend: "wslc", ID: "tregnone"}, 5*time.Second)
	for _, l := range wslcInvocations(t, inv) {
		if strings.HasPrefix(l, "inv [login]") {
			t.Errorf("login must not be invoked without registry creds:\n%s", l)
		}
	}
}

// TestWslcRegistryLoginTenantPick verifies tenant images pick the TENANT
// creds (not platform) — the same PickRegistryAuth the docker path uses.
func TestWslcRegistryLoginTenantPick(t *testing.T) {
	b, _, inv := newTestWslcBackend(t)
	platform := transport.HarborCredentials{
		Registry: "ghcr.io",
		Username: "admin",
		Password: "platform-pw",
	}
	tenant := transport.HarborCredentials{
		Registry: "example.com",
		Username: "robot$tenant-1",
		Password: "tenant-pw",
	}
	spec := wslcTaskWithRegistryAuth("tregtenant", "example.com/tenant-1/img:v1", platform, tenant)
	if _, err := b.Submit(context.Background(), spec); err != nil {
		t.Fatalf("Submit: %v", err)
	}
	waitWslc(t, b, &Handle{Backend: "wslc", ID: "tregtenant"}, 5*time.Second)
	var loginLine string
	for _, l := range wslcInvocations(t, inv) {
		if strings.HasPrefix(l, "inv [login]") {
			loginLine = l
		}
	}
	if loginLine == "" {
		t.Fatal("no login recorded")
	}
	if !strings.Contains(loginLine, "[example.com]") {
		t.Errorf("login must target the tenant registry:\n%s", loginLine)
	}
	if !strings.Contains(loginLine, "[--username][robot$tenant-1]") {
		t.Errorf("login must use the tenant robot user:\n%s", loginLine)
	}
	if strings.Contains(loginLine, "[admin]") {
		t.Errorf("login must NOT use platform creds for a tenant image:\n%s", loginLine)
	}
}

// TestWslcRegistryLoginFailureSurfacesHostError verifies a login failure
// fails the task (Submit returns an error, no run started) with an error
// naming the registry host and never the password.
func TestWslcRegistryLoginFailureSurfacesHostError(t *testing.T) {
	b, _, inv := newTestWslcBackend(t)
	t.Setenv("FAKE_WSLC_LOGIN_FAIL", "1")
	const password = "definitely-not-logged"
	creds := transport.HarborCredentials{
		Registry: "stage-registry.example.com",
		Username: "robot$platform",
		Password: password,
	}
	spec := wslcTaskWithRegistryAuth("tregfail", "stage-registry.example.com/proj/img:v1", creds, transport.HarborCredentials{})
	_, err := b.Submit(context.Background(), spec)
	if err == nil {
		t.Fatal("Submit must fail when the registry login fails")
	}
	msg := err.Error()
	if !strings.Contains(msg, "stage-registry.example.com") {
		t.Errorf("login failure must name the registry host, got: %q", msg)
	}
	if strings.Contains(msg, password) {
		t.Errorf("login failure must never contain the password, got: %q", msg)
	}
	for _, l := range wslcInvocations(t, inv) {
		if strings.HasPrefix(l, "inv [run]") {
			t.Errorf("no run may start after a login failure:\n%s", l)
		}
	}
}

// TestWslcRegistryLoginCacheClearedOnStop verifies ClearRegistryLogins
// (the session-stop hook) forces a re-login for the next task.
func TestWslcRegistryLoginCacheClearedOnStop(t *testing.T) {
	b, _, inv := newTestWslcBackend(t)
	creds := transport.HarborCredentials{
		Registry: "stage-registry.example.com",
		Username: "robot$platform",
		Password: "pw-1",
	}
	for _, id := range []string{"tregh1", "tregh2"} {
		spec := wslcTaskWithRegistryAuth(id, "stage-registry.example.com/proj/img:v1", creds, transport.HarborCredentials{})
		if _, err := b.Submit(context.Background(), spec); err != nil {
			t.Fatalf("Submit %s: %v", id, err)
		}
		waitWslc(t, b, &Handle{Backend: "wslc", ID: id}, 5*time.Second)
	}
	countLogins := func() int {
		n := 0
		for _, l := range wslcInvocations(t, inv) {
			if strings.HasPrefix(l, "inv [login]") {
				n++
			}
		}
		return n
	}
	if got := countLogins(); got != 1 {
		t.Fatalf("expected 1 login before stop, got %d", got)
	}

	// Session stop: the NOAH session manager calls this on StopSession.
	b.ClearRegistryLogins()

	spec := wslcTaskWithRegistryAuth("tregh3", "stage-registry.example.com/proj/img:v1", creds, transport.HarborCredentials{})
	if _, err := b.Submit(context.Background(), spec); err != nil {
		t.Fatalf("Submit after stop: %v", err)
	}
	waitWslc(t, b, &Handle{Backend: "wslc", ID: "tregh3"}, 5*time.Second)
	if got := countLogins(); got != 2 {
		t.Fatalf("expected a re-login after ClearRegistryLogins, got %d logins total", got)
	}
}
