package executor

// add-persistent-stacks §9.2 — compose-runner unit tests. These exercise the
// validation/dispatch logic + the pure helpers WITHOUT needing a docker daemon
// (every case here returns before the `docker compose` shell-out).

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func _run(op, project, compose, fromProject string) error {
	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp: op, StackProject: project, ComposeSpec: compose, MigrateFromProject: fromProject,
	}}
	_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	return err
}

func TestRunStackOp_ValidationErrors(t *testing.T) {
	cases := []struct {
		name, op, project, compose, from, wantSub string
	}{
		{"missing project", "deploy", "", "services: {}", "", "missing stack_project"},
		{"deploy no compose", "deploy", "p", "", "", "missing compose_spec"},
		{"migrate no compose", "migrate", "p", "", "old", "missing compose_spec"},
		{"migrate no from", "migrate", "p", "services: {}", "", "missing migrate_from_project"},
		{"unknown op", "frobnicate", "p", "", "", "unknown stack_op"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := _run(c.op, c.project, c.compose, c.from)
			if err == nil {
				t.Fatalf("expected error for %s, got nil", c.name)
			}
			if !strings.Contains(err.Error(), c.wantSub) {
				t.Fatalf("error %q does not contain %q", err.Error(), c.wantSub)
			}
		})
	}
}

func TestSanitizeProject(t *testing.T) {
	if got := sanitizeProject("modstack7g1"); got != "modstack7g1" {
		t.Fatalf("clean name changed: %q", got)
	}
	if got := sanitizeProject("a/b c:d"); got != "a-b-c-d" {
		t.Fatalf("unsafe chars not replaced: %q", got)
	}
}

func TestTail(t *testing.T) {
	if got := tail("short"); got != "short" {
		t.Fatalf("short string altered: %q", got)
	}
	big := strings.Repeat("x", 5000)
	got := tail(big)
	if len(got) >= 5000 || !strings.HasPrefix(got, "...(truncated)...") {
		t.Fatalf("expected truncated tail, got len=%d prefix=%q", len(got), got[:20])
	}
}

// managed-ai-surfaces-and-stack-ops §9.4 — the rehearsal's safety property is
// that it CANNOT touch the live volume or the running stack. That property has
// to hold structurally, not by the caller passing the right payload, so it is
// asserted on the naming rule itself rather than on a happy-path run (which
// would need a docker daemon).
func TestRehearsalVolumeNameNeverCollidesWithTheLiveVolume(t *testing.T) {
	for _, vol := range []string{
		"modstack7g0_data",
		"modstack7g0_pgdata",
		"x",
		"modrehearsal-modstack7g0_data", // already a scratch name
	} {
		got := rehearsalVolumeName(vol)
		if got == vol {
			t.Fatalf("rehearsal target for %q is the live volume itself", vol)
		}
		if !strings.HasPrefix(got, "modrehearsal-") {
			t.Fatalf("rehearsal target %q is not identifiable as scratch", got)
		}
	}
}

// A rehearsal must never be able to stop the live stack, even if the server
// sends a project. `restore` stops and starts the project; `rehearse` must not
// take that branch at all.
func TestRehearseIgnoresStackProject(t *testing.T) {
	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp:      "rehearse",
		StackProject: "modstack7g0", // would stop the live stack on the restore path
		StackVolume:  "modstack7g0_data",
	}}
	// No MinIO configured in the unit environment, so this fails inside the
	// rehearsal — the point is WHERE it fails. Reaching a compose stop/start
	// would mean the guarantee is only as good as the caller's payload.
	_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err == nil {
		t.Fatal("expected the rehearsal to fail without MinIO creds")
	}
	if !strings.Contains(err.Error(), "rehearsal") {
		t.Fatalf("error %q suggests the rehearsal took a non-rehearsal path", err.Error())
	}
	if strings.Contains(err.Error(), "stop") || strings.Contains(err.Error(), "start") {
		t.Fatalf("rehearsal touched the live project: %q", err.Error())
	}
}

// live-mesh-reenroll — the mesh_reup op re-ups a stack's tailscale sidecar
// against a new login server. MeshAuthkey is a one-shot credential and must
// never surface in LogTail, an error message, or any other logged line, even
// when the invoked process echoes its own argv back on failure (observed
// tailscale behaviour on some usage/error paths).
func TestRunStackOpMeshReup(t *testing.T) {
	const authkey = "tskey-auth-super-secret-value"

	withFakeExec := func(fn func(ctx context.Context, name string, arg ...string) *exec.Cmd) func() {
		orig := execCommandContext
		execCommandContext = fn
		return func() { execCommandContext = orig }
	}

	t.Run("exact argv and failure output is redacted", func(t *testing.T) {
		var gotArgs []string
		restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
			gotArgs = append([]string{name}, arg...)
			// Simulate a failing tailscale that echoes its own argv (including
			// the authkey) into stderr — the exact failure mode the
			// redaction has to survive.
			return exec.CommandContext(ctx, "sh", "-c",
				`echo "tailscale up --login-server=https://mesh.example.com --authkey=`+authkey+` --hostname=w1 --force-reauth: failed" >&2; exit 7`)
		})
		defer restore()

		e := &Executor{}
		task := &transport.Task{Payload: transport.TaskPayload{
			StackOp:         "mesh_reup",
			StackProject:    "modstack7g0",
			MeshService:     "modstack7g0",
			MeshLoginServer: "https://mesh.example.com",
			MeshAuthkey:     authkey,
			MeshHostname:    "w1",
		}}

		var logged []string
		cb := Callbacks{OnLogBatch: func(lines []string) { logged = append(logged, lines...) }}

		res, err := e.runStackOp(context.Background(), task, nil, time.Now(), cb)

		wantArgs := []string{
			"docker", "compose", "-p", "modstack7g0",
			"exec", "-T", "ts-modstack7g0", "tailscale", "up",
			"--login-server=https://mesh.example.com",
			"--authkey=" + authkey,
			"--hostname=w1",
			"--force-reauth",
		}
		if strings.Join(gotArgs, " ") != strings.Join(wantArgs, " ") {
			t.Fatalf("argv mismatch:\n got: %v\nwant: %v", gotArgs, wantArgs)
		}

		if err == nil {
			t.Fatal("expected an error from the simulated tailscale failure")
		}
		if res != nil {
			t.Fatalf("expected nil result on failure, got %+v", res)
		}
		if strings.Contains(err.Error(), authkey) {
			t.Fatalf("authkey leaked into the returned error: %q", err.Error())
		}
		for _, ln := range logged {
			if strings.Contains(ln, authkey) {
				t.Fatalf("authkey leaked into a LogTail/OnLogBatch line: %q", ln)
			}
		}
	})

	t.Run("success output is also redacted", func(t *testing.T) {
		restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
			return exec.CommandContext(ctx, "sh", "-c",
				`echo "reauth ok, authkey=`+authkey+` accepted"; exit 0`)
		})
		defer restore()

		e := &Executor{}
		task := &transport.Task{Payload: transport.TaskPayload{
			StackOp:         "mesh_reup",
			StackProject:    "modstack7g0",
			MeshService:     "modstack7g0",
			MeshLoginServer: "https://mesh.example.com",
			MeshAuthkey:     authkey,
			MeshHostname:    "w1",
		}}
		res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if res == nil || strings.Contains(res.LogTail, authkey) {
			t.Fatalf("authkey leaked into Result.LogTail: %+v", res)
		}
	})

	t.Run("missing mesh_service is an error", func(t *testing.T) {
		called := false
		restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
			called = true
			return exec.CommandContext(ctx, "true")
		})
		defer restore()

		e := &Executor{}
		task := &transport.Task{Payload: transport.TaskPayload{
			StackOp:         "mesh_reup",
			StackProject:    "modstack7g0",
			MeshLoginServer: "https://mesh.example.com",
			MeshAuthkey:     authkey,
			MeshHostname:    "w1",
			// MeshService deliberately omitted.
		}}
		_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
		if err == nil {
			t.Fatal("expected an error for missing mesh_service")
		}
		if !strings.Contains(err.Error(), "mesh_service") {
			t.Fatalf("error %q does not mention mesh_service", err.Error())
		}
		if called {
			t.Fatal("docker/tailscale should never be invoked when mesh_service is missing")
		}
	})
}

func withFakeExec(fn func(ctx context.Context, name string, arg ...string) *exec.Cmd) func() {
	orig := execCommandContext
	execCommandContext = fn
	return func() { execCommandContext = orig }
}

// D. On 2026-09-28 the combined `compose logs --tail 200` for stack 30 was
// entirely filled by the chattiest container (llama-server), hiding nginx's
// own log lines — the ones that actually explained the failure. The `logs`
// op must return a status table plus a fair, per-service-capped breakdown so
// no single service can crowd the others out.
func TestRunStackOpLogsPerServiceBreakdown(t *testing.T) {
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		full := strings.Join(arg, " ")
		switch {
		case strings.Contains(full, "ps -a --services"):
			return exec.CommandContext(ctx, "sh", "-c", `printf 'lb\nnoisy\nquiet\n'`)
		case strings.HasSuffix(full, "ps -a"):
			return exec.CommandContext(ctx, "sh", "-c", `printf 'NAME  STATUS\nlb    Exited (1)\n'`)
		case strings.Contains(full, "logs --tail 200 --no-color --timestamps noisy"):
			return exec.CommandContext(ctx, "sh", "-c", `yes noisyline | head -c 20000`)
		case strings.Contains(full, "logs --tail 200 --no-color --timestamps quiet"):
			return exec.CommandContext(ctx, "sh", "-c", `printf 'quiet started ok\n'`)
		case strings.Contains(full, "logs --tail 200 --no-color --timestamps lb"):
			return exec.CommandContext(ctx, "sh", "-c", `printf 'lb exited 1: bind: address in use\n'`)
		}
		return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected argv: `+full+`" >&2; exit 1`)
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{StackOp: "logs", StackProject: "modstack30g0"}}
	res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	out := res.LogTail
	if !strings.Contains(out, "lb    Exited (1)") {
		t.Fatalf("missing compose ps -a status table:\n%s", out)
	}
	if !strings.Contains(out, "=== lb ===") || !strings.Contains(out, "=== noisy ===") || !strings.Contains(out, "=== quiet ===") {
		t.Fatalf("missing per-service section headers:\n%s", out)
	}
	if !strings.Contains(out, "quiet started ok") {
		t.Fatalf("quiet service's own output was hidden by the noisy one:\n%s", out)
	}
	if !strings.Contains(out, "lb exited 1: bind: address in use") {
		t.Fatalf("lb's own output — the actually useful line — was hidden:\n%s", out)
	}
	if strings.Count(out, "noisyline") >= 20000/len("noisyline ") {
		t.Fatalf("noisy service was not capped to its own budget")
	}
	if len(out) > stackLogsOverallCap+4096 {
		t.Fatalf("result exceeds the overall cap: %d bytes", len(out))
	}
}

// When the project has no services here and the combined `logs` call errors
// or comes back empty (never deployed on this host, or already torn down),
// the op must say so plainly — that is a legitimate result, not a failure,
// and the old empty-string answer read as "stack logs don't work".
func TestRunStackOpLogsNoContainersPlainMessage(t *testing.T) {
	cases := map[string]string{
		"combined logs errors": `echo "no such project" >&2; exit 1`,
		"combined logs empty":  `exit 0`,
	}
	for name, logsSh := range cases {
		t.Run(name, func(t *testing.T) {
			restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
				full := strings.Join(arg, " ")
				switch {
				case strings.Contains(full, "ps -a --services"):
					return exec.CommandContext(ctx, "sh", "-c", `exit 0`) // empty output, project unknown here
				case strings.Contains(full, " logs "):
					return exec.CommandContext(ctx, "sh", "-c", logsSh)
				}
				return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected argv: `+full+`" >&2; exit 1`)
			})
			defer restore()

			e := &Executor{}
			task := &transport.Task{Payload: transport.TaskPayload{StackOp: "logs", StackProject: "modstack-gone"}}
			res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			want := "no containers for project modstack-gone on this host (never deployed here, or removed)"
			if !strings.Contains(res.LogTail, want) {
				t.Fatalf("expected the plain no-containers message %q, got:\n%s", want, res.LogTail)
			}
		})
	}
}

// 2026-09-29 — crash-looping mesh sidecars (tailscale x509 spam) filled the
// whole "logs" result on the Dell/R9000 hosts and pushed the real app
// services (lb, vllm-0) out of the 48KB tail. The sections must be ordered
// infrastructure-FIRST so the app services sit at the end — and tailN keeps
// the end — and the sidecars must get their own, smaller, per-service cap.
func TestRunStackOpLogsSidecarsFirstAndCapped(t *testing.T) {
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		full := strings.Join(arg, " ")
		switch {
		case strings.Contains(full, "ps -a --services"):
			return exec.CommandContext(ctx, "sh", "-c", `printf 'tsegress-vllm-0\nts-vllm-0-ingress\nts-vllm-0-net\nvllm-0\n'`)
		case strings.HasSuffix(full, "ps -a"):
			return exec.CommandContext(ctx, "sh", "-c", `printf 'NAME  STATUS\nvllm-0  Exited (1)\n'`)
		case strings.Contains(full, "logs --tail 200 --no-color --timestamps tsegress-vllm-0"):
			return exec.CommandContext(ctx, "sh", "-c", `yes "x509: certificate signed by unknown authority" | head -c 50000`)
		case strings.Contains(full, "logs --tail 200 --no-color --timestamps ts-vllm-0-ingress"):
			return exec.CommandContext(ctx, "sh", "-c", `yes "x509: certificate signed by unknown authority" | head -c 50000`)
		case strings.Contains(full, "logs --tail 200 --no-color --timestamps ts-vllm-0-net"):
			return exec.CommandContext(ctx, "sh", "-c", `yes "x509: certificate signed by unknown authority" | head -c 50000`)
		case strings.Contains(full, "logs --tail 200 --no-color --timestamps vllm-0"):
			return exec.CommandContext(ctx, "sh", "-c", `printf 'vllm-0 out of memory: killed\n'`)
		}
		return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected argv: `+full+`" >&2; exit 1`)
	})
	defer restore()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{StackOp: "logs", StackProject: "modstack31g0"}}
	res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	out := res.LogTail

	if !strings.Contains(out, "vllm-0 out of memory: killed") {
		t.Fatalf("app service output was crowded out by the sidecar spam:\n%s", out)
	}
	// App sections must come AFTER the infrastructure sections.
	appAt := strings.Index(out, "=== vllm-0 ===")
	for _, side := range []string{"=== tsegress-vllm-0 ===", "=== ts-vllm-0-ingress ===", "=== ts-vllm-0-net ==="} {
		sideAt := strings.Index(out, side)
		if sideAt < 0 || appAt < 0 || sideAt >= appAt {
			t.Fatalf("sidecar %q must be ordered before the app section (side=%d app=%d):\n%s", side, sideAt, appAt, out)
		}
	}
	// The sidecars were 50KB of spam each; their sections must be capped to
	// the sidecar budget, not the full per-service budget.
	const spamLine = "x509: certificate signed by unknown authority"
	if n := strings.Count(out, spamLine); n*30 > 3*perSidecarLogsCap {
		t.Fatalf("sidecar spam exceeds what the three capped budgets (%d bytes each) allow: %d lines",
			perSidecarLogsCap, n)
	}
	if len(out) > stackLogsOverallCap+4096 {
		t.Fatalf("result exceeds the overall cap: %d bytes", len(out))
	}
}

// 2026-09-29 — stack 34 redeploy failed with "network b4060e77… not found":
// the stopped containers still pinned an external network that had been
// deleted and recreated under a NEW id, and `compose up` on unchanged config
// does not recreate them. The deploy must detect that specific failure and
// retry exactly once with --force-recreate.
func TestIsStaleNetworkErr(t *testing.T) {
	cases := []struct {
		name, out string
		err       error
		want      bool
	}{
		{"daemon network-not-found in output",
			"Error response from daemon: network b4060e77abc not found",
			errors.New(`exit status 1`), true},
		{"network-not-found split across output and error",
			"Error response from daemon: network b4060e77abc",
			errors.New(`not found`), true},
		{"unrelated daemon error",
			"Error response from daemon: port is already allocated",
			errors.New(`exit status 1`), false},
		{"not-found about something else",
			"Error response from daemon: container xyz not found",
			errors.New(`exit status 1`), false},
		{"network created is success output, not an error",
			"Network foo_created", nil, false},
		{"success with network chatter, no error",
			"Network modstack34g0_default  created", nil, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := isStaleNetworkErr(c.out, c.err); got != c.want {
				t.Fatalf("isStaleNetworkErr(%q, %v) = %v, want %v", c.out, c.err, got, c.want)
			}
		})
	}
}

// 2026-10-02 — stack 29: a dropped compose service left its mesh sidecar
// attached to the project network, so `compose up --remove-orphans` failed on
// network removal with "has active endpoints". The predicate must match that
// failure and stay disjoint from the stale-network case.
func TestIsActiveEndpointsNetworkErr(t *testing.T) {
	cases := []struct {
		name, out string
		err       error
		want      bool
	}{
		{"live stack 29 failure",
			`Error response from daemon: error while removing network: network modstack29g0_mod-stack-egress has active endpoints (name:"modstack29g0-ts-lb-net-1" id:"9c1f0a")`,
			errors.New(`exit status 1`), true},
		{"active-endpoints split across output and error",
			"Error response from daemon: error while removing network: network modstack29g0_mod-stack-egress",
			errors.New(`has active endpoints`), true},
		{"case-insensitive match",
			"NETWORK modstack29g0_mod-stack-egress HAS ACTIVE ENDPOINTS",
			errors.New(`exit status 1`), true},
		{"stale-network error belongs to isStaleNetworkErr, not this",
			"Error response from daemon: network b4060e77abc not found",
			errors.New(`exit status 1`), false},
		{"unrelated daemon error",
			"Error response from daemon: port is already allocated",
			errors.New(`exit status 1`), false},
		{"success output, no error",
			"Container modstack29g0-ts-lb-net-1  started", nil, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := isActiveEndpointsNetworkErr(c.out, c.err); got != c.want {
				t.Fatalf("isActiveEndpointsNetworkErr(%q, %v) = %v, want %v", c.out, c.err, got, c.want)
			}
		})
	}
}

// The deploy op's stale-network recovery: the FIRST `up -d --remove-orphans`
// fails with "network … not found", the retry carries --force-recreate and
// succeeds, and the result is exactly what a first-try success would produce
// (including healStackNetworks). A NON-network failure must not retry.
func TestRunStackOpDeployStaleNetworkForceRecreate(t *testing.T) {
	// Minimal spec: no top-level networks (so ensureExternalNetworks never
	// touches the real docker CLI), no GPU assignment, no weights, no model
	// cache mounts — the only daemon interaction on this path is dockerCLI.
	const spec = "services:\n  app:\n    image: busybox\n"

	t.Run("network-not-found triggers one force-recreate retry", func(t *testing.T) {
		var upCalls [][]string
		restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
			full := strings.Join(arg, " ")
			if strings.Contains(full, "compose") && strings.Contains(full, "up -d") {
				upCalls = append(upCalls, arg)
				if len(upCalls) == 1 {
					return exec.CommandContext(ctx, "sh", "-c",
						`echo "Error response from daemon: network b4060e77abc not found" >&2; exit 1`)
				}
				return exec.CommandContext(ctx, "sh", "-c", `echo "recreated ok"`)
			}
			return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected argv: `+full+`" >&2; exit 1`)
		})
		defer restore()

		origDocker := dockerCLI
		dockerCLI = func(_ context.Context, args ...string) (string, error) { return "", nil }
		defer func() { dockerCLI = origDocker }()

		var logged []string
		cb := Callbacks{OnLogBatch: func(lines []string) { logged = append(logged, lines...) }}

		e := &Executor{}
		task := &transport.Task{Payload: transport.TaskPayload{
			StackOp: "deploy", StackProject: "modstack34g0", ComposeSpec: spec,
		}}
		res, err := e.runStackOp(context.Background(), task, nil, time.Now(), cb)
		if err != nil {
			t.Fatalf("deploy should have recovered via force-recreate, got: %v", err)
		}
		if res == nil || !strings.Contains(res.LogTail, "recreated ok") {
			t.Fatalf("expected the retry's success output in LogTail, got: %+v", res)
		}
		if len(upCalls) != 2 {
			t.Fatalf("expected exactly 2 compose up calls (first + one retry), got %d: %v", len(upCalls), upCalls)
		}
		first := strings.Join(upCalls[0], " ")
		second := strings.Join(upCalls[1], " ")
		if !strings.Contains(first, "up -d --remove-orphans") || strings.Contains(first, "--force-recreate") {
			t.Fatalf("first call was not the plain up: %q", first)
		}
		if !strings.Contains(second, "up -d --remove-orphans --force-recreate") {
			t.Fatalf("retry did not carry --force-recreate: %q", second)
		}
		sawNotice := false
		for _, ln := range logged {
			if strings.Contains(strings.ToLower(ln), "stale network") {
				sawNotice = true
			}
		}
		if !sawNotice {
			t.Fatalf("expected a stale-network log line in OnLogBatch, got: %v", logged)
		}
	})

	t.Run("unrelated failure is returned as-is without retry", func(t *testing.T) {
		var upCalls int
		restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
			full := strings.Join(arg, " ")
			if strings.Contains(full, "compose") && strings.Contains(full, "up -d") {
				upCalls++
				return exec.CommandContext(ctx, "sh", "-c",
					`echo "Error response from daemon: port is already allocated" >&2; exit 1`)
			}
			return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected argv: `+full+`" >&2; exit 1`)
		})
		defer restore()

		origDocker := dockerCLI
		dockerCLI = func(_ context.Context, args ...string) (string, error) { return "", nil }
		defer func() { dockerCLI = origDocker }()

		e := &Executor{}
		task := &transport.Task{Payload: transport.TaskPayload{
			StackOp: "deploy", StackProject: "modstack34g0", ComposeSpec: spec,
		}}
		_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
		if err == nil {
			t.Fatal("expected the compose up failure to be returned")
		}
		if !strings.Contains(err.Error(), "compose up failed") {
			t.Fatalf("error %q does not keep the existing wrapping style", err.Error())
		}
		if upCalls != 1 {
			t.Fatalf("expected exactly 1 compose up call (no retry), got %d", upCalls)
		}
	})
}

// isMeshSidecar must catch every sidecar naming shape the platform injects
// and none of the app services that merely contain those substrings.
func TestRunStackOpTeardownRetainKeepsVolumes(t *testing.T) {
	// GA.ASW.8 — `data_policy="retain"` must NOT destroy the stack's data: the
	// compose `down` runs WITHOUT the `-v` flag, so the project's named volumes
	// (and their contents) survive. Absent / "erase" keeps `down -v`.
	withFakeExec := func(fn func(ctx context.Context, name string, arg ...string) *exec.Cmd) func() {
		orig := execCommandContext
		execCommandContext = fn
		return func() { execCommandContext = orig }
	}

	runTeardown := func(policy string) []string {
		var gotArgs []string
		restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
			gotArgs = append([]string{name}, arg...)
			return exec.CommandContext(ctx, "true")
		})
		defer restore()

		e := &Executor{}
		task := &transport.Task{Payload: transport.TaskPayload{
			StackOp:         "teardown",
			StackProject:    "modstack29g0",
			StackDataPolicy: policy,
		}}
		_, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
		if err != nil {
			t.Fatalf("teardown with policy %q failed: %v", policy, err)
		}
		return gotArgs
	}

	retainArgs := runTeardown("retain")
	if !strings.Contains(strings.Join(retainArgs, " "), "down --remove-orphans") {
		t.Fatalf("retain teardown must still run compose down, got: %v", retainArgs)
	}
	if strings.Contains(strings.Join(retainArgs, " "), " -v") {
		t.Fatalf("retain teardown must NOT pass -v (that deletes the volumes), got: %v", retainArgs)
	}

	eraseArgs := runTeardown("erase")
	if !strings.HasSuffix(strings.Join(eraseArgs, " "), " -v") {
		t.Fatalf("erase teardown must pass -v (reclaim volumes), got: %v", eraseArgs)
	}

	// absent policy keeps the pre-existing reclaim behaviour
	legacyArgs := runTeardown("")
	if !strings.HasSuffix(strings.Join(legacyArgs, " "), " -v") {
		t.Fatalf("absent data_policy must keep `down -v`, got: %v", legacyArgs)
	}
}

func TestIsMeshSidecar(t *testing.T) {
	for _, svc := range []string{"ts-lb", "ts-vllm-0", "ts-vllm-0-ingress", "ts-vllm-0-net", "tsegress-lb", "ingress-lb"} {
		if !isMeshSidecar(svc) {
			t.Fatalf("%q must be recognised as a mesh sidecar", svc)
		}
	}
	for _, svc := range []string{"lb", "vllm-0", "nginx", "modstack31g0-ts"} {
		if isMeshSidecar(svc) {
			t.Fatalf("%q is an app service, not a sidecar", svc)
		}
	}
}

// 1131 — GPU2 stack 111 and HP both failed compose-up with "open
// /run/nvidia-persistenced/socket: no such file".
func TestIsNvidiaPersistencedSocketErr(t *testing.T) {
	cases := []struct {
		name, out string
		err       error
		want      bool
	}{
		{"live GPU2 stack 111 failure",
			"open /run/nvidia-persistenced/socket: no such file or directory",
			errors.New(`exit status 1`), true},
		{"split across output and error",
			"nvidia container runtime error:",
			errors.New(`could not open /run/nvidia-persistenced/socket`), true},
		{"case-insensitive match",
			"OPEN /RUN/NVIDIA-PERSISTENCED/SOCKET: NO SUCH FILE",
			errors.New(`exit status 1`), true},
		{"stale-network error belongs to isStaleNetworkErr, not this",
			"Error response from daemon: network b4060e77abc not found",
			errors.New(`exit status 1`), false},
		{"unrelated daemon error",
			"Error response from daemon: port is already allocated",
			errors.New(`exit status 1`), false},
		{"success output, no error",
			"Container modstack111g0-app-1  started", nil, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := isNvidiaPersistencedSocketErr(c.out, c.err); got != c.want {
				t.Fatalf("isNvidiaPersistencedSocketErr(%q, %v) = %v, want %v", c.out, c.err, got, c.want)
			}
		})
	}
}

// 1131 — nvidia-smi's own error text distinguishes a reboot-required NVML
// driver/library mismatch from a merely-stopped persistenced daemon.
func TestIsNVMLDriverMismatch(t *testing.T) {
	cases := []struct {
		name, out string
		want      bool
	}{
		{"classic NVML failure",
			"Failed to initialize NVML: Driver/library version mismatch", true},
		{"case-insensitive match",
			"FAILED TO INITIALIZE NVML: DRIVER/LIBRARY VERSION MISMATCH", true},
		{"healthy nvidia-smi output",
			"Mon Oct 12 00:00:00 2026\n+---...GPU-Util...", false},
		{"empty output (binary missing)", "", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := isNVMLDriverMismatch(c.out); got != c.want {
				t.Fatalf("isNVMLDriverMismatch(%q) = %v, want %v", c.out, got, c.want)
			}
		})
	}
}

// 1131 — the deploy op's persistenced-socket recovery: nvidia-smi decides
// whether a reboot is required (no retry, warning surfaced for the
// heartbeat) or persistenced just needs restarting (one retry, like the
// stale-network/active-endpoints ladder cases).
func TestRunStackOpDeployPersistencedSocketRecovery(t *testing.T) {
	const spec = "services:\n  app:\n    image: busybox\n"
	const socketErr = `open /run/nvidia-persistenced/socket: no such file or directory`

	t.Run("NVML mismatch returns original error without retry or restart", func(t *testing.T) {
		defer clearGPUDriverMismatchWarning()
		var upCalls, smiCalls int
		restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
			switch name {
			case "docker":
				full := strings.Join(arg, " ")
				if strings.Contains(full, "compose") && strings.Contains(full, "up -d") {
					upCalls++
					return exec.CommandContext(ctx, "sh", "-c",
						`echo "`+socketErr+`" >&2; exit 1`)
				}
				return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected docker argv: `+full+`" >&2; exit 1`)
			case "nvidia-smi":
				smiCalls++
				return exec.CommandContext(ctx, "sh", "-c",
					`echo "Failed to initialize NVML: Driver/library version mismatch" >&2; exit 1`)
			case "systemctl":
				t.Fatalf("systemctl must not be called when NVML reports a mismatch (reboot required, not retry)")
				return exec.CommandContext(ctx, "true")
			default:
				return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected command: `+name+`" >&2; exit 1`)
			}
		})
		defer restore()

		origDocker := dockerCLI
		dockerCLI = func(_ context.Context, args ...string) (string, error) { return "", nil }
		defer func() { dockerCLI = origDocker }()

		var logged []string
		cb := Callbacks{OnLogBatch: func(lines []string) { logged = append(logged, lines...) }}

		e := &Executor{}
		task := &transport.Task{Payload: transport.TaskPayload{
			StackOp: "deploy", StackProject: "modstack111g0", ComposeSpec: spec,
		}}
		_, err := e.runStackOp(context.Background(), task, nil, time.Now(), cb)
		if err == nil {
			t.Fatal("expected the compose up failure to be returned unchanged")
		}
		if !strings.Contains(err.Error(), "compose up failed") {
			t.Fatalf("error %q does not keep the existing wrapping style", err.Error())
		}
		if upCalls != 1 {
			t.Fatalf("expected exactly 1 compose up call (no retry on reboot-required), got %d", upCalls)
		}
		if smiCalls != 1 {
			t.Fatalf("expected exactly 1 nvidia-smi probe, got %d", smiCalls)
		}
		warnings := GPUWarnings()
		if len(warnings) != 1 || warnings[0] != "gpu_driver_mismatch: reboot required" {
			t.Fatalf("expected GPUWarnings to carry the mismatch warning, got: %v", warnings)
		}
		sawNotice := false
		for _, ln := range logged {
			if strings.Contains(strings.ToLower(ln), "driver/library version mismatch") {
				sawNotice = true
			}
		}
		if !sawNotice {
			t.Fatalf("expected a mismatch log line in OnLogBatch, got: %v", logged)
		}
	})

	t.Run("healthy NVML restarts persistenced and retries successfully", func(t *testing.T) {
		defer clearGPUDriverMismatchWarning()
		var upCalls, systemctlCalls int
		restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
			switch name {
			case "docker":
				full := strings.Join(arg, " ")
				if strings.Contains(full, "compose") && strings.Contains(full, "up -d") {
					upCalls++
					if upCalls == 1 {
						return exec.CommandContext(ctx, "sh", "-c",
							`echo "`+socketErr+`" >&2; exit 1`)
					}
					return exec.CommandContext(ctx, "sh", "-c", `echo "recreated ok"`)
				}
				return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected docker argv: `+full+`" >&2; exit 1`)
			case "nvidia-smi":
				return exec.CommandContext(ctx, "sh", "-c", `echo "nvidia-smi healthy, no mismatch"`)
			case "systemctl":
				systemctlCalls++
				return exec.CommandContext(ctx, "sh", "-c", `echo "started"`)
			default:
				return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected command: `+name+`" >&2; exit 1`)
			}
		})
		defer restore()

		origDocker := dockerCLI
		dockerCLI = func(_ context.Context, args ...string) (string, error) { return "", nil }
		defer func() { dockerCLI = origDocker }()

		e := &Executor{}
		task := &transport.Task{Payload: transport.TaskPayload{
			StackOp: "deploy", StackProject: "modstack111g0", ComposeSpec: spec,
		}}
		res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
		if err != nil {
			t.Fatalf("deploy should have recovered via persistenced restart, got: %v", err)
		}
		if res == nil || !strings.Contains(res.LogTail, "recreated ok") {
			t.Fatalf("expected the retry's success output in LogTail, got: %+v", res)
		}
		if upCalls != 2 {
			t.Fatalf("expected exactly 2 compose up calls (first + one retry), got %d", upCalls)
		}
		if systemctlCalls != 1 {
			t.Fatalf("expected exactly 1 systemctl start nvidia-persistenced call, got %d", systemctlCalls)
		}
		if warnings := GPUWarnings(); len(warnings) != 0 {
			t.Fatalf("healthy NVML must not leave a GPU warning behind, got: %v", warnings)
		}
	})
}

// 1360 — transient post-boot error retry tests for compose up.
// These exercise isMountErr (pure function), composeUpWithRetryImpl (mock docker
// via composeCmdRedactedHook), and the deploy-path retry integration via _run.

func TestIsMountErr(t *testing.T) {
	// Known mount error patterns from 1360.
	mountCases := []string{
		"ERROR: for my-container  Cannot start service: bind source path '/var/lib/mod-secrets/ca.pem' does not exist",
		"error mounting '/var/lib/mod-secrets/ca.pem' to rootfs at '/etc/ssl/certs/ca.pem': \"no such file or directory\"",
		"OCI runtime create failed: container_init.go:72: mount /var/lib/mod-secrets/ca.pem -> /etc/ssl/certs/ca.pem: no such file or directory: unknown",
		"Cannot start service ollama: bind source path /models does not exist",
		"error mounting /run/user/1000/secret.pem to /run/secrets/secret: no such file or directory",
	}
	for _, tc := range mountCases {
		if !isMountErr(tc, fmt.Errorf("operation failed")) {
			t.Errorf("expected isMountErr(true) for: %s", tc)
		}
	}

	// Non-mount error patterns must not match.
	nonMountCases := []string{
		"OCI runtime create failed: exec: \"python\": executable file not found in $PATH",
		"ERROR: for my-container  Cannot start service: port 8080 is already in use",
		"network modnet_abc123 not found",
		"Error response from daemon: could not apply seccomp profile",
		"network modnet has active endpoints",
		"open /run/nvidia-persistenced/socket: no such file",
		"failed to solve: failed to compute cache key: invalid reference format",
	}
	for _, tc := range nonMountCases {
		if isMountErr(tc, fmt.Errorf("operation failed")) {
			t.Errorf("expected isMountErr(false) for: %s", tc)
		}
	}

	// nil error must never match.
	if isMountErr("some output", nil) {
		t.Error("expected isMountErr(false) for nil error")
	}

	// OCI runtime without mount signal should not match.
	if isMountErr("OCI runtime create failed: exec: \"python\": executable file not found in $PATH", fmt.Errorf("runtime error")) {
		t.Error("OCI runtime without mount signal should not be isMountErr")
	}
}

// TestComposeUpWithRetry_MountErrorBackoff tests that a transient mount error
// triggers retries with exponential backoff, and the final success is returned.
func TestComposeUpWithRetry_MountErrorBackoff(t *testing.T) {
	orig := maxComposeUpRetrySeconds
	maxComposeUpRetrySeconds = 2 // cap backoff at 2s for test speed
	defer func() { maxComposeUpRetrySeconds = orig }()

	callNum := 0
	e := &Executor{}
	e.composeCmdRedactedHook = func(ctx context.Context, cb Callbacks, project, composePath string, secrets, extraEnv []string, args ...string) (string, error) {
		callNum++
		// First 3 calls fail with a mount error; 4th succeeds.
		if callNum <= 3 {
			return "", fmt.Errorf("bind source path '/var/lib/mod-secrets/ca.pem' does not exist")
		}
		return "stack started ok", nil
	}

	var logged []string
	cb := Callbacks{OnLogBatch: func(lines []string) { logged = append(logged, lines...) }}

	out, err := composeUpWithRetryImpl(context.Background(), cb, e, "teststack", "/tmp/docker-compose.yml", nil, nil, false)

	if err != nil {
		t.Fatalf("expected success after retries, got: %v", err)
	}
	if out != "stack started ok" {
		t.Fatalf("unexpected output: %q", out)
	}
	if callNum != 4 {
		t.Fatalf("expected 4 compose up calls (3 failures + 1 success), got %d", callNum)
	}

	// Check that retry messages were logged.
	foundRetry := false
	for _, l := range logged {
		if strings.Contains(l, "mount error") && strings.Contains(l, "retrying") {
			foundRetry = true
			break
		}
	}
	if !foundRetry {
		t.Fatalf("expected retry log line, got: %v", logged)
	}
}

// TestComposeUpWithRetry_NonTransientFailsImmediately tests that non-transient
// errors (e.g., permission denied, invalid config) do NOT trigger retries.
func TestComposeUpWithRetry_NonTransientFailsImmediately(t *testing.T) {
	e := &Executor{}
	e.composeCmdRedactedHook = func(ctx context.Context, cb Callbacks, project, composePath string, secrets, extraEnv []string, args ...string) (string, error) {
		return "", fmt.Errorf("permission denied: cannot connect to Docker daemon")
	}

	var logged []string
	cb := Callbacks{OnLogBatch: func(lines []string) { logged = append(logged, lines...) }}

	_, err := composeUpWithRetryImpl(context.Background(), cb, e, "teststack", "/tmp/docker-compose.yml", nil, nil, false)

	if err == nil {
		t.Fatal("expected an error for non-transient failure")
	}
	if !strings.Contains(err.Error(), "permission denied") {
		t.Fatalf("unexpected error: %v", err)
	}

	// Should NOT have any retry log lines.
	for _, l := range logged {
		if strings.Contains(l, "retrying") {
			t.Fatalf("non-transient error should not trigger retry, but got log: %s", l)
		}
	}
}

// TestComposeUpWithRetry_StaleNetworkImmediateRetry tests that the stale-network
// case retries immediately with --force-recreate (no backoff delay).
func TestComposeUpWithRetry_StaleNetworkImmediateRetry(t *testing.T) {
	callNum := 0
	e := &Executor{}
	e.composeCmdRedactedHook = func(ctx context.Context, cb Callbacks, project, composePath string, secrets, extraEnv []string, args ...string) (string, error) {
		callNum++
		if callNum == 1 {
			return "", fmt.Errorf("network modnet_abc123 not found")
		}
		// Second call: verify --force-recreate was used.
		for _, a := range args {
			if a == "--force-recreate" {
				return "stack started ok", nil
			}
		}
		return "", fmt.Errorf("containers still using stale network")
	}

	var logged []string
	cb := Callbacks{OnLogBatch: func(lines []string) { logged = append(logged, lines...) }}

	out, err := composeUpWithRetryImpl(context.Background(), cb, e, "teststack", "/tmp/docker-compose.yml", nil, nil, false)

	if err != nil {
		t.Fatalf("expected success after stale-network retry, got: %v", err)
	}
	if out != "stack started ok" {
		t.Fatalf("unexpected output: %q", out)
	}
	if callNum != 2 {
		t.Fatalf("expected exactly 2 compose up calls, got %d", callNum)
	}
	foundStaleLog := false
	for _, l := range logged {
		if strings.Contains(l, "stale network reference") {
			foundStaleLog = true
			break
		}
	}
	if !foundStaleLog {
		t.Fatalf("expected stale-network log message, got: %v", logged)
	}
}

// TestComposeUpWithRetry_NvidiaPersistencedFailsImmediately tests that the
// nvidia-persistenced socket error is NOT retried — it returns immediately.
func TestComposeUpWithRetry_NvidiaPersistencedFailsImmediately(t *testing.T) {
	callNum := 0
	e := &Executor{}
	e.composeCmdRedactedHook = func(ctx context.Context, cb Callbacks, project, composePath string, secrets, extraEnv []string, args ...string) (string, error) {
		callNum++
		return "", fmt.Errorf("open /run/nvidia-persistenced/socket: no such file or directory")
	}

	_, err := composeUpWithRetryImpl(context.Background(), Callbacks{}, e, "teststack", "/tmp/docker-compose.yml", nil, nil, false)

	if err == nil {
		t.Fatal("expected error for nvidia-persistenced failure")
	}
	if callNum != 1 {
		t.Fatalf("expected exactly 1 call (no retry for persistenced), got %d", callNum)
	}
}

// TestComposeUpWithRetry_ActiveEndpointsFailsImmediately tests that the
// active-endpoints network error is NOT retried inside composeUpWithRetry.
func TestComposeUpWithRetry_ActiveEndpointsFailsImmediately(t *testing.T) {
	callNum := 0
	e := &Executor{}
	e.composeCmdRedactedHook = func(ctx context.Context, cb Callbacks, project, composePath string, secrets, extraEnv []string, args ...string) (string, error) {
		callNum++
		return "", fmt.Errorf("driver failed programming external endpoint on network modnet: Network modnet has active endpoints")
	}

	_, err := composeUpWithRetryImpl(context.Background(), Callbacks{}, e, "teststack", "/tmp/docker-compose.yml", nil, nil, false)

	if err == nil {
		t.Fatal("expected error for active-endpoints failure")
	}
	if callNum != 1 {
		t.Fatalf("expected exactly 1 call (no retry for active endpoints), got %d", callNum)
	}
}

// TestComposeUpWithRetry_MaxRetriesExhausted tests that after all retries
// are exhausted, the error is returned with the last output.
func TestComposeUpWithRetry_MaxRetriesExhausted(t *testing.T) {
	orig := maxComposeUpRetries
	maxComposeUpRetries = 2 // 2 retries after the initial attempt
	defer func() { maxComposeUpRetries = orig }()

	callNum := 0
	e := &Executor{}
	e.composeCmdRedactedHook = func(ctx context.Context, cb Callbacks, project, composePath string, secrets, extraEnv []string, args ...string) (string, error) {
		callNum++
		return "", fmt.Errorf("bind source path '/secrets/ca.pem' does not exist")
	}

	_, err := composeUpWithRetryImpl(context.Background(), Callbacks{}, e, "teststack", "/tmp/docker-compose.yml", nil, nil, false)

	if err == nil {
		t.Fatal("expected error after all retries exhausted")
	}
	if callNum != 3 {
		t.Fatalf("expected 3 calls (1 initial + 2 retries), got %d", callNum)
	}
}

// TestDeployStackRunner_MountErrorSucceedsOnRetry exercises the full deploy path
// through _run("deploy", ...) with a compose spec that has no external networks,
// mocking docker compose to fail with a mount error twice then succeed.
func TestDeployStackRunner_MountErrorSucceedsOnRetry(t *testing.T) {
	orig := maxComposeUpRetries
	maxComposeUpRetries = 2
	origRetrySeconds := maxComposeUpRetrySeconds
	maxComposeUpRetrySeconds = 0 // no sleep in tests
	defer func() {
		maxComposeUpRetries = orig
		maxComposeUpRetrySeconds = origRetrySeconds
	}()

	spec := `
services:
  myapp:
    image: nginx:latest
    ports:
      - "8080:80"
`

	composeCallCount := 0
	withFakeExec := func(fn func(ctx context.Context, name string, arg ...string) *exec.Cmd) func() {
		orig := execCommandContext
		execCommandContext = fn
		return func() { execCommandContext = orig }
	}

	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		full := strings.Join(arg, " ")
		switch {
		case strings.Contains(full, "compose") && strings.Contains(full, "up"):
			composeCallCount++
			if composeCallCount <= 2 {
				return exec.CommandContext(ctx, "sh", "-c",
					`echo "ERROR: Cannot start service myapp: bind source path '/var/lib/mod-secrets/ca.pem' does not exist" >&2; exit 1`)
			}
			return exec.CommandContext(ctx, "sh", "-c", `echo "Started myapp"; exit 0`)
		case strings.Contains(full, "compose") && strings.Contains(full, "ps"):
			// healStackNetworks calls docker compose ps — return empty.
			return exec.CommandContext(ctx, "sh", "-c", `exit 0`)
		}
		return exec.CommandContext(ctx, "sh", "-c", `exit 0`)
	})
	defer restore()

	err := _run("deploy", "teststack34", spec, "")

	if err != nil {
		t.Fatalf("expected deploy to succeed after retries, got: %v", err)
	}
	if composeCallCount != 3 {
		t.Fatalf("expected exactly 3 compose up calls, got %d", composeCallCount)
	}
}

// TestDeployStackRunner_NonTransientFailsImmediately tests that the deploy path
// does NOT retry on non-transient errors.
func TestDeployStackRunner_NonTransientFailsImmediately(t *testing.T) {
	orig := maxComposeUpRetries
	maxComposeUpRetries = 2
	origRetrySeconds := maxComposeUpRetrySeconds
	maxComposeUpRetrySeconds = 0
	defer func() {
		maxComposeUpRetries = orig
		maxComposeUpRetrySeconds = origRetrySeconds
	}()

	spec := `
services:
  myapp:
    image: nginx:latest
`

	composeCallCount := 0
	withFakeExec := func(fn func(ctx context.Context, name string, arg ...string) *exec.Cmd) func() {
		orig := execCommandContext
		execCommandContext = fn
		return func() { execCommandContext = orig }
	}

	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		full := strings.Join(arg, " ")
		switch {
		case strings.Contains(full, "compose") && strings.Contains(full, "up"):
			composeCallCount++
			return exec.CommandContext(ctx, "sh", "-c",
				`echo "ERROR: invalid compose config: service \"myapp\" has neither of these fields: image, build" >&2; exit 1`)
		case strings.Contains(full, "compose") && strings.Contains(full, "ps"):
			return exec.CommandContext(ctx, "sh", "-c", `exit 0`)
		}
		return exec.CommandContext(ctx, "sh", "-c", `exit 0`)
	})
	defer restore()

	err := _run("deploy", "teststack99", spec, "")

	if err == nil {
		t.Fatal("expected deploy to fail on non-transient error")
	}
	if composeCallCount != 1 {
		t.Fatalf("expected exactly 1 call (no retry for non-transient), got %d", composeCallCount)
	}
	if !strings.Contains(err.Error(), "invalid compose config") {
		t.Fatalf("error should contain the invalid config message, got: %v", err)
	}
}

// TestDeployStackRunner_StaleNetworkSucceedsOnForceRecreate tests the deploy path
// with a stale-network error that succeeds on the immediate --force-recreate retry.
func TestDeployStackRunner_StaleNetworkSucceedsOnForceRecreate(t *testing.T) {
	orig := maxComposeUpRetries
	maxComposeUpRetries = 2
	origRetrySeconds := maxComposeUpRetrySeconds
	maxComposeUpRetrySeconds = 0
	defer func() {
		maxComposeUpRetries = orig
		maxComposeUpRetrySeconds = origRetrySeconds
	}()

	spec := `
services:
  myapp:
    image: nginx:latest
`

	composeCallCount := 0
	withFakeExec := func(fn func(ctx context.Context, name string, arg ...string) *exec.Cmd) func() {
		orig := execCommandContext
		execCommandContext = fn
		return func() { execCommandContext = orig }
	}

	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		full := strings.Join(arg, " ")
		switch {
		case strings.Contains(full, "compose") && strings.Contains(full, "up"):
			composeCallCount++
			if composeCallCount == 1 {
				return exec.CommandContext(ctx, "sh", "-c",
					`echo "network modnet_abc123 not found" >&2; exit 1`)
			}
			if strings.Contains(full, "--force-recreate") {
				return exec.CommandContext(ctx, "sh", "-c", `echo "recreated"; exit 0`)
			}
			return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected args"; exit 1`)
		case strings.Contains(full, "compose") && strings.Contains(full, "ps"):
			return exec.CommandContext(ctx, "sh", "-c", `exit 0`)
		}
		return exec.CommandContext(ctx, "sh", "-c", `exit 0`)
	})
	defer restore()

	err := _run("deploy", "teststack55", spec, "")

	if err != nil {
		t.Fatalf("expected deploy to succeed after stale-network force-recreate, got: %v", err)
	}
	if composeCallCount != 2 {
		t.Fatalf("expected 2 compose up calls, got %d", composeCallCount)
	}
}

// TestRunStackOpDeployActiveEndpointsTeardownRetry guards the 2026-10-02
// stack-29 fix: composeUpWithRetry classifies "network has active endpoints"
// as non-retryable and returns immediately, but runStackOp must still tear
// the project down (no -v) and retry once at that layer. The 1360 refactor
// that introduced composeUpWithRetry initially dropped this recovery path
// entirely — this test pins it in place.
func TestRunStackOpDeployActiveEndpointsTeardownRetry(t *testing.T) {
	const spec = "services:\n  app:\n    image: busybox\n"

	var upCalls [][]string
	var downCalls [][]string
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		full := strings.Join(arg, " ")
		switch {
		case strings.Contains(full, "compose") && strings.Contains(full, "up -d"):
			upCalls = append(upCalls, arg)
			if len(upCalls) == 1 {
				return exec.CommandContext(ctx, "sh", "-c",
					`echo "Error response from daemon: Network modstack34g0_default has active endpoints" >&2; exit 1`)
			}
			return exec.CommandContext(ctx, "sh", "-c", `echo "recreated ok"`)
		case strings.Contains(full, "compose") && strings.Contains(full, "down"):
			downCalls = append(downCalls, arg)
			return exec.CommandContext(ctx, "sh", "-c", `echo "torn down"`)
		}
		return exec.CommandContext(ctx, "sh", "-c", `echo "unexpected argv: `+full+`" >&2; exit 1`)
	})
	defer restore()

	origDocker := dockerCLI
	dockerCLI = func(_ context.Context, args ...string) (string, error) { return "", nil }
	defer func() { dockerCLI = origDocker }()

	e := &Executor{}
	task := &transport.Task{Payload: transport.TaskPayload{
		StackOp: "deploy", StackProject: "modstack34g0", ComposeSpec: spec,
	}}
	res, err := e.runStackOp(context.Background(), task, nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("deploy should have recovered via teardown + retry, got: %v", err)
	}
	if res == nil || !strings.Contains(res.LogTail, "recreated ok") {
		t.Fatalf("expected the retry's success output in LogTail, got: %+v", res)
	}
	if len(upCalls) != 2 {
		t.Fatalf("expected exactly 2 compose up calls (first + one retry), got %d: %v", len(upCalls), upCalls)
	}
	if len(downCalls) != 1 {
		t.Fatalf("expected exactly 1 compose down call (teardown before retry), got %d: %v", len(downCalls), downCalls)
	}
	for _, a := range downCalls[0] {
		if a == "-v" {
			t.Fatalf("teardown must never pass -v (volumes must be preserved): %v", downCalls[0])
		}
	}
}
