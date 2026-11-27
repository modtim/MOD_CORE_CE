package executor

// STACK-HEALTH-FINISH-1126 — real-docker leg for collectServiceStates (F-1 of
// STACK-HEALTH-STATUS-BUILD). A throwaway managed project (modstack991126g0 —
// the managed shape isManagedStackContainer requires; the run is throwaway and
// fully removed by t.Cleanup) with three services:
//   looper  — command exit 1, restart always  -> the daemon crash-loops it:
//             reported BAD on a beat carrying crash evidence (running with
//             RestartCount>0, or restarting, or exited non-zero)
//   sleeper — sleep 600, restart "no"         -> running, no healthcheck: NOT bad
//   oneshot — exit 0, restart "no"            -> exited 0, finished job: NOT bad
// (owner rule: a one-shot that exited 0 with restart no is healthy).
// The test drives the REAL collectServiceStates with a real docker client
// (*client.Client satisfies stackPolicyDocker — stackloop.go pins that at
// `var _ stackPolicyDocker = (*client.Client)(nil)`), so the Inspect shape the
// earlier lane could not verify is now proven against a live daemon.
// Skipped unless STACKHEALTH_DOCKER=1 and a daemon is reachable; the lane run
// mounts the host docker CLI + compose cli-plugin + socket into golang:1.25
// (same wiring as stackobserve_sidecarheal_docker_test.go's SELFHEAL_DOCKER leg).

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/client"
)

const realHealthProject = "modstack991126g0"

func healthComposeArgv(dir string) []string {
	return []string{"docker", "compose", "-p", realHealthProject,
		"-f", filepath.Join(dir, "compose.yml")}
}

func healthUp(dir string) (string, error) {
	argv := healthComposeArgv(dir)
	out, err := exec.Command(argv[0], argv[1], argv[2], argv[3], argv[4], argv[5], "up", "-d").CombinedOutput()
	return string(out), err
}

func healthDown(dir string) (string, error) {
	argv := healthComposeArgv(dir)
	out, err := exec.Command(argv[0], argv[1], argv[2], argv[3], argv[4], argv[5],
		"down", "-v", "--remove-orphans").CombinedOutput()
	return string(out), err
}

func listHealthContainers(cli *client.Client) ([]types.Container, error) {
	return cli.ContainerList(context.Background(), container.ListOptions{
		All:     true,
		Filters: filters.NewArgs(filters.Arg("label", composeProjectLabel+"="+realHealthProject)),
	})
}

func findState(states []ServiceState, service string) ServiceState {
	for _, st := range states {
		if st.Service == service {
		 return st
		}
	}
	return ServiceState{Service: "-", State: "absent"}
}

func TestCollectServiceStatesRealDockerLooperBadSleeperHealthyOneShotNotBad(t *testing.T) {
	if os.Getenv("STACKHEALTH_DOCKER") != "1" {
	 t.Skip("STACKHEALTH_DOCKER=1 required (real daemon; gpu5 lane run)")
	}
	if out, err := exec.Command("docker", "version", "--format", "{{.Server.Version}}").CombinedOutput(); err != nil {
	 t.Skipf("no docker daemon reachable: %v %s", err, string(out))
	}

	 tmpdir := os.Getenv("TMPDIR")
	 if tmpdir == "" {
	 tmpdir = "/tmp"
	 }
	 _ = os.Setenv("TMPDIR", tmpdir) // healthUp's compose runs inherit it
	 dir := filepath.Join(tmpdir, realHealthProject)
	 if err := os.MkdirAll(dir, 0o700); err != nil {
	 t.Fatalf("mkdir %s: %v", dir, err)
	 }
	const yml = `services:
  looper:
    image: alpine:3.20
    command: ["sh", "-c", "exit 1"]
    restart: always
  sleeper:
    image: alpine:3.20
    command: ["sh", "-c", "sleep 600"]
    restart: "no"
  oneshot:
    image: alpine:3.20
    command: ["sh", "-c", "exit 0"]
    restart: "no"
`
	if err := os.WriteFile(filepath.Join(dir, "compose.yml"), []byte(yml), 0o600); err != nil {
	 t.Fatalf("write compose.yml: %v", err)
	}
	t.Cleanup(func() {
		// The lane run owns cleanup: containers + the project network down,
		// fixture dir removed. Nothing else on this host is touched.
		healthDown(dir)
		_ = os.RemoveAll(dir)
	})

	 if out, err := healthUp(dir); err != nil {
	 t.Fatalf("compose up: %v\n%s", err, out)
	 }

	 cli, err := client.NewClientWithOpts(client.FromEnv)
	 if err != nil {
	 t.Fatalf("docker client: %v", err)
	 }
	 defer cli.Close()

	 // The daemon's restart cycle has transient words (starting/backoff) that
	 // serviceIsBad deliberately does NOT condemn (stackDownCount owns "should
	 // run but does not"); so the looper is proven by AT LEAST one bad beat
	 // carrying crash evidence, never by "every beat".
	 var looperBad, sleeperUp, oneshotDone bool
	 var looperDesc, sleeperDesc, oneshotDesc string
	 for i := 0; i < 80; i++ {
	 containers, lerr := listHealthContainers(cli)
	 if lerr != nil {
	 t.Fatalf("container list: %v", lerr)
	 }
	 obs := &StackObservation{Project: realHealthProject}
	 byProject := map[string]*StackObservation{realHealthProject: obs}
	 // The REAL observation-path function, real client, real daemon.
	 collectServiceStates(context.Background(), cli, containers, byProject)

	 looper := findState(obs.ServiceStates, "looper")
	 sleeper := findState(obs.ServiceStates, "sleeper")
	 oneshot := findState(obs.ServiceStates, "oneshot")

	 if looper.Service == "looper" && looper.Bad {
	 // A bad looper beat must carry real crash evidence, not a guess.
	 if !(looper.Restarts > 0 || looper.Exit != 0 ||
	 looper.State == "restarting" || looper.State == "dead" ||
	 looper.State == "paused") {
	 t.Fatalf("looper read bad with no crash evidence: state=%s exit=%d restarts=%d",
	 looper.State, looper.Exit, looper.Restarts)
	 }
	 looperBad = true
	 looperDesc = looper.State + "/exit=" + strconv.Itoa(looper.Exit) +
	 "/restarts=" + strconv.Itoa(looper.Restarts)
	 }
	 if sleeper.Service == "sleeper" {
	 if sleeper.Bad {
	 t.Fatalf("healthy sleeper read bad: state=%s health=%s restarts=%d",
	 sleeper.State, sleeper.Health, sleeper.Restarts)
	 }
	 if sleeper.State == "running" {
	 sleeperUp = true
	 sleeperDesc = "running/health=" + sleeper.Health +
	 "/restarts=" + strconv.Itoa(sleeper.Restarts)
	 }
	 }
	 if oneshot.Service == "oneshot" && oneshot.State == "exited" {
	 if oneshot.Bad {
	 t.Fatalf("one-shot exited-0 read bad: exit=%d restarts=%d",
	 oneshot.Exit, oneshot.Restarts)
	 }
	 if oneshot.Exit != 0 {
	 t.Fatalf("one-shot exit code read %d, expected 0", oneshot.Exit)
	 }
	 oneshotDone = true
	 oneshotDesc = "exited/exit=" + strconv.Itoa(oneshot.Exit) +
	 "/restarts=" + strconv.Itoa(oneshot.Restarts)
	 }

	 if looperBad && sleeperUp && oneshotDone {
	 t.Logf("realdocker: looper BAD (%s) sleeper NOT-bad (%s) oneshot NOT-bad (%s)",
	 looperDesc, sleeperDesc, oneshotDesc)
	 return
	 }
	 time.Sleep(500 * time.Millisecond)
	 if i == 79 {
	 t.Fatalf("postures never all seen: looperBad=%v(%s) sleeperUp=%v(%s) oneshotDone=%v(%s)",
	 looperBad, looperDesc, sleeperUp, sleeperDesc, oneshotDone, oneshotDesc)
	 }
	 }
}
