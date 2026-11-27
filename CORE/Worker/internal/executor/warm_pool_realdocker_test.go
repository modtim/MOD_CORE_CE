//go:tag warmbench

package executor

// KEEP-WARM-SLICE-1 PART 3: the REAL-DOCKER isolation proof behind build tag
// `warmbench` (run on cpu4, NOT xeon, via the golang:1.25 image; every
// resource this test creates is named kw4-* or carries the kw4inst instance
// label and is removed by the test's own cleanup — no prune, nothing else on
// the host is touched). It drives the SAME pool functions the worker uses
// (warmAcquire / warmRunExec / warmFinishCall) against a real daemon and
// proves the four cross-call properties the fake engine can only assert
// about the CALLS:
//   1. a file written in call 1 is GONE in call 2 (pool volume wiped);
//   2. call 1's env is invisible to call 2 — in `env` of a new exec AND in
//      /proc/1/environ (the keep-alive init: zero create-time env, R3);
//   3. a file in /tmp from call 1 is GONE in call 2 (tmpfs wipe, L-1);
//   4. a background process started in call 1 does NOT survive into call 2
//      (the pid-delta check RETIRES the container, L-7).
// Run: go test -tags warmbench -count=1 -run WarmReal ./internal/executor

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/api/types/volume"
	"github.com/docker/docker/client"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// warmRealAvailable skips the test unless a daemon answers (the tag is opt-in;
// a missing daemon is a SKIP, never a false red).
func warmRealAvailable() bool {
	var cli, err = client.NewClientWithOpts(client.FromEnv)
	if err != nil { return false }
	var ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var _, perr = cli.Ping(ctx)
	if perr != nil { return false }
	return true
}

func TestWarmRealIsolationAcrossCalls(t *testing.T) {
	if !warmRealAvailable() { t.Skip("no docker daemon for warmbench") }
	var cli, err = client.NewClientWithOpts(client.FromEnv)
	if err != nil { t.Fatal(err) }

	// Throwaway isolated network for the create-time posture; removed at the
	// end. The name is kw4-prefixed so it is recognisable as this test's.
	var netName = "kw4-warm-net"
	var nctx, ncancel = context.WithTimeout(context.Background(), 15*time.Second)
	defer ncancel()
	var netResp, nerr = cli.NetworkCreate(nctx, netName, network.CreateOptions{Driver: "bridge"})
	if nerr != nil { t.Fatal("network create: ", nerr) }

	var logs bytes.Buffer
	var store = tunables.New()
	store.Apply(map[string]interface{}{warmPoolEnabledTunableKey: true,
		warmPoolMaxContainersTunableKey: 64})
	var e = &Executor{docker: cli, workerName: "kw4lane", instanceID: "kw4inst",
		network: netName, log: slog.New(slog.NewTextHandler(&logs, nil)), tunables: store}

	// Cleanup: remove EVERY warm-pool resource carrying THIS test's instance
	// label, plus the network. Label-scoped: nothing else on the host is touched.
	t.Cleanup(func() {
		var cctx, ccancel = context.WithTimeout(context.Background(), 60*time.Second)
		defer ccancel()
		var list container.ListOptions
		list.All = true
		list.Filters = filters.NewArgs(filters.Arg("label", warmPoolLabel))
		var cs, lerr = cli.ContainerList(cctx, list)
		if lerr == nil {
			for _, c := range cs {
				if c.Labels[sidecarInstanceLabel] != "kw4inst" { continue }
				cli.ContainerRemove(cctx, c.ID, container.RemoveOptions{Force: true})
			}
		}
		var vs, verr = cli.VolumeList(cctx, volume.ListOptions{
			Filters: filters.NewArgs(filters.Arg("label", warmPoolLabel))})
		if verr == nil {
			for _, v := range vs.Volumes {
				if v.Labels[sidecarInstanceLabel] != "kw4inst" { continue }
				cli.VolumeRemove(cctx, v.Name, true)
			}
		}
		cli.NetworkRemove(cctx, netResp.ID)
	})

	// The command strings are built as variables FIRST: this parser does not
	// accept multi-line `+` concatenation inside a composite literal.
	var call1Cmd = "mkdir -p /data/input/nest; "+
		"echo marker1 > /data/input/marker1.txt; "+
		"echo nested1 > /data/input/nest/deep1.txt; "+
		"echo dot1 > /data/input/.dotfile1; "+
		"ln -s /data/input/marker1.txt /data/input/link1; "+
		"echo tmpfile1 > /tmp/tmpfile1.txt; "+
		"echo tmpdot1 > /tmp/.tmpdot1; "+
		"echo hello"
	var p = transport.TaskPayload{
		NodeKind: "kw4.bench", ProcessingImage: "alpine:latest",
		Cmd: []string{"sh", "-c", call1Cmd},
		ContainerEnv: map[string]string{"INPUT_DIR": "/data/input", "OUTPUT_DIR": "/data/output"},
	}
	var pol = &transport.WarmPolicy{IdleSeconds: 300, MaxMemoryMB: 512, MaxCalls: 5, Protocol: "exec"}
	var creds1 = &transport.Credentials{MinIO: transport.MinIOCredentials{
		Endpoint: "http://minio:9000", AccessKey: "AKkw4", SecretKey: "SKkw4",
		SessionToken: "STkw4"}, ModelCacheToken: "MCkw4"}
	var env1 = e.warmExecEnv(p, creds1, "alpine:latest")
	// call 1's env carries a marker the node could print; it must NEVER be
	// visible to call 2 (neither a new exec's env nor the init's /proc/1).
	env1 = append(env1, "KW4_CALL1_ENV=secret-from-call-1")

	var a, aerr = e.warmAcquire(context.Background(), 9001, 901, "alpine:latest", p, pol, netName, env1)
	if aerr != nil || a == nil { t.Fatal("warmAcquire: ", aerr) }

	// ---- call 1: writes files (nested dir, dotfile, symlink, /tmp), prints -
	var tail1, code1, oom1, rerr1 = e.warmRunExec(context.Background(), a, p, env1, 60*time.Second, Callbacks{})
	if rerr1 != nil || code1 != 0 || oom1 { t.Fatal("call 1: ", rerr1, " code=", code1) }
	if !strings.Contains(tail1, "hello") { t.Fatal("call 1 log tail missing its own output: ", tail1) }
	var ferr1 error
	if ferr1 = e.warmFinishCall(a); ferr1 != nil { t.Fatal("finish call 1: ", ferr1) }

	// ---- call 2: SAME key must REUSE the container, and see NOTHING ------
	var b, berr = e.warmAcquire(context.Background(), 9002, 901, "alpine:latest", p, pol, netName, env1)
	if berr != nil || b == nil { t.Fatal("re-acquire: ", berr) }
	if b.containerID != a.containerID {
		t.Fatal("the pool must REUSE the container for the same key: ", a.containerID, " vs ", b.containerID)
	}
	// The probe: every call-1 artifact must be gone (regular file, nested dir
	// file, dotfile, symlink, /tmp file, /tmp dotfile); call 1's env marker
	// must be absent from a NEW exec's `env` AND from /proc/1/environ; the
	// per-task creds must never reach the create-time env (/proc/1/environ).
	var probeCmd = "found=0; "+
		"for f in /data/input/marker1.txt /data/input/nest/deep1.txt /data/input/.dotfile1 /data/input/link1 /tmp/tmpfile1.txt /tmp/.tmpdot1; do "+
		"  if [ -e \"$f\" ] || [ -L \"$f\" ]; then echo \"LEAK_FILE $f\"; found=1; fi; done; "+
		"if env | grep -q KW4_CALL1_ENV; then echo LEAK_ENV; found=1; fi; "+
		"if tr '\\0' '\\n' < /proc/1/environ | grep -q KW4_CALL1_ENV; then echo LEAK_PROC1_ENV; found=1; fi; "+
		"if tr '\\0' '\\n' < /proc/1/environ | grep -q MINIO_ACCESS_KEY; then echo LEAK_PROC1_CREDS; found=1; fi; "+
		"if tr '\\0' '\\n' < /proc/1/environ | grep -q API_URL; then echo LEAK_PROC1_APIURL; found=1; fi; "+
		"if [ \"$found\" = 0 ]; then echo CLEAN; fi"
	var probe = transport.TaskPayload{
		NodeKind: "kw4.bench", ProcessingImage: "alpine:latest",
		Cmd: []string{"sh", "-c", probeCmd},
		ContainerEnv: map[string]string{},
	}
	var env2 = e.warmExecEnv(probe, creds1, "alpine:latest")
	var tail2, code2, oom2, rerr2 = e.warmRunExec(context.Background(), b, probe, env2, 60*time.Second, Callbacks{})
	if rerr2 != nil || oom2 { t.Fatal("probe exec: ", rerr2, " oom=", oom2) }
	if !strings.Contains(tail2, "CLEAN") || strings.Contains(tail2, "LEAK") {
		t.Fatal("cross-call leak in the reused container:\n", tail2)
	}
	if code2 != 0 { t.Fatal("probe exit code: ", code2) }
	var ferr2 error
	if ferr2 = e.warmFinishCall(b); ferr2 != nil { t.Fatal("finish probe call: ", ferr2) }

	// ---- call 3: a background process must NOT survive into call 4 -------
	var p3 = transport.TaskPayload{
		NodeKind: "kw4.bench", ProcessingImage: "alpine:latest",
		Cmd: []string{"sh", "-c", "sleep 300 & echo started"},
		ContainerEnv: map[string]string{"INPUT_DIR": "/data/input", "OUTPUT_DIR": "/data/output"},
	}
	// SAME env as calls 1-2 (the env hash must match the entry's, or the pool
	// retires it — which is correct behaviour, just not what this leg tests).
	var env3 = env1
	var c3, cerr = e.warmAcquire(context.Background(), 9003, 901, "alpine:latest", p3, pol, netName, env3)
	if cerr != nil || c3 == nil { t.Fatal("acquire call 3: ", cerr) }
	var tail3, code3, oom3, rerr3 = e.warmRunExec(context.Background(), c3, p3, env3, 60*time.Second, Callbacks{})
	if rerr3 != nil || code3 != 0 || oom3 { t.Fatal("call 3: ", rerr3, " code=", code3, " oom=", oom3) }
	if !strings.Contains(tail3, "started") { t.Fatal("call 3 log tail: ", tail3) }
	// the pid-delta check must RETIRE the container (a stray sleep survived).
	var ferr3 error
	if ferr3 = e.warmFinishCall(c3); ferr3 == nil {
		t.Fatal("a detached process must fail the finish (L-7 pid delta)")
	}
	// call 4 must run in a DIFFERENT container (the old one is gone), and the
	// retired container must be GONE from the daemon.
	var ictx, icancel = context.WithTimeout(context.Background(), 15*time.Second)
	defer icancel()
	var ci, cierr = cli.ContainerInspect(ictx, c3.containerID)
	if cierr == nil && ci.State.Running {
		t.Fatal("the retired container is still running on the daemon")
	}
	var d4, derr = e.warmAcquire(context.Background(), 9004, 901, "alpine:latest", p3, pol, netName, env3)
	if derr != nil || d4 == nil { t.Fatal("acquire call 4: ", derr) }
	if d4.containerID == c3.containerID {
		t.Fatal("the retired container was handed to call 4")
	}
	var tail4, code4, oom4, rerr4 = e.warmRunExec(context.Background(), d4, p3, env3, 60*time.Second, Callbacks{})
	if rerr4 != nil || code4 != 0 || oom4 { t.Fatal("call 4: ", rerr4, " code=", code4, " oom=", oom4) }
	if !strings.Contains(tail4, "started") { t.Fatal("call 4 log tail: ", tail4) }
	var ferr4 error
	if ferr4 = e.warmFinishCall(d4); ferr4 == nil {
		t.Fatal("call 4 also detached a process; the finish must fail again")
	}
	println("warmbench OK: reuse + clean probe + stray-process retire + container removed")
}
