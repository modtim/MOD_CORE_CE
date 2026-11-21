// Command egress-smoke is a one-shot harness that exercises the worker's
// per-task egress proxy end to end: it brings up a real proxy via the
// executor's setup path, runs a task container on the proxy's private network
// through it, then tears the proxy down and prints the resulting egress report.
//
// It exists to smoke-test GB.EGR.D (egress proxying) without standing up a full
// task pipeline. It does not build any Executor; it only needs a reachable
// Docker daemon and the egress proxy image.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/executor"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// main only ever calls run() and passes its exit code to os.Exit. Nothing
// else in this file (or anything it calls) may call os.Exit/log.Fatal*: any
// of those unwind past every pending defer, which is exactly what used to
// leak the proxy container, its private network and its /28 subnet whenever
// setup succeeded but the task run failed. Keeping the sole os.Exit here, as
// the very last statement of the program, guarantees run()'s defers (proxy
// teardown, docker client close, signal-context stop) always execute first.
func main() {
	os.Exit(run())
}

func run() int {
	image := flag.String("image", os.Getenv("MOD_EGRESS_PROXY_IMAGE"), "egress proxy image")
	allow := flag.String("allow", os.Getenv("MOD_EGRESS_ALLOW_HOSTS"), "comma-separated allowed egress hosts")
	taskImage := flag.String("task-image", "", "task container image to run the smoke command in")
	cmd := flag.String("cmd", "", "shell command run in the task container via `sh -c`")
	egressNet := flag.String("egress-net", "mod-user-egress", "worker egress network name the proxy is attached to")
	flag.Parse()

	if *image == "" || *taskImage == "" || *cmd == "" {
		fmt.Fprintf(os.Stderr, "usage: egress-smoke -image IMG -task-image TASK -cmd 'sh -c' [-allow a,b] [-egress-net NAME]\n")
		return 2
	}
	if *allow == "" {
		fmt.Fprintln(os.Stderr, "warning: -allow is empty; the proxy will deny all non-loopout egress")
	}
	hosts := splitAllow(*allow)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		log.Printf("docker client: %v", err)
		return 1
	}
	defer cli.Close()

	if _, err := cli.Ping(ctx); err != nil {
		log.Printf("ping docker daemon: %v", err)
		return 1
	}

	h, err := executor.SetupEgressProxyForSmoke(ctx, cli, "egress-smoke", *image, hosts, *egressNet, map[string]string{"mod.smoke": "1"})
	if err != nil {
		log.Printf("setup egress proxy: %v", err)
		return 1
	}
	fmt.Printf("egress proxy handle: net=%s proxy=%s\n", h.Network(), h.ProxyIP())
	defer func() {
		tctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
		defer cancel()
		report := h.TeardownForSmoke(tctx, cli)
		fmt.Printf("egress report (teardown):\n")
		if err := printReport(report); err != nil {
			log.Printf("%v", err)
		}
	}()

	result, err := runContainer(ctx, cli, h, *taskImage, *cmd)
	if err != nil {
		log.Printf("run task container: %v", err)
		return 1
	}
	fmt.Printf("task exit code: %d\n", result.code)
	return result.code
}

type smokeResult struct {
	code int
}

func runContainer(ctx context.Context, cli *client.Client, h *executor.EgressSmokeHandle, image, shellCmd string) (smokeResult, error) {
	resp, err := cli.ContainerCreate(ctx,
		&container.Config{
			Image: image,
			Env:   append([]string{}, h.Env()...),
			Cmd:   []string{"sh", "-c", shellCmd},
		},
		&container.HostConfig{
			DNS:         h.DNS(),
			NetworkMode: container.NetworkMode(h.Network()),
		},
		&network.NetworkingConfig{},
		nil,
		"egress-smoke-task",
	)
	if err != nil {
		return smokeResult{}, fmt.Errorf("create task container: %w", err)
	}
	containerID := resp.ID
	defer func() {
		rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 15*time.Second)
		defer cancel()
		cli.ContainerRemove(rctx, containerID, container.RemoveOptions{Force: true})
	}()

	if err := cli.ContainerStart(ctx, containerID, container.StartOptions{}); err != nil {
		return smokeResult{}, fmt.Errorf("start task container: %w", err)
	}

	statusCh, errCh := cli.ContainerWait(ctx, containerID, container.WaitConditionNotRunning)
	var waitErr error
	select {
	case err := <-errCh:
		if err != nil {
			waitErr = err
		}
	case <-statusCh:
	case <-ctx.Done():
		waitErr = ctx.Err()
	}

	// The task container runs without a TTY, so Docker multiplexes stdout and
	// stderr into one stream with an 8-byte frame header; demux so the two
	// streams interleave in real capture order.
	logs, err := cli.ContainerLogs(ctx, containerID, container.LogsOptions{ShowStdout: true, ShowStderr: true})
	if err == nil {
		defer logs.Close()
		stdout := &strings.Builder{}
		stdcopy.StdCopy(stdout, io.Discard, logs)
		out := stdout.String()
		if out != "" {
			fmt.Printf("--- task output ---\n%s\n", strings.TrimRight(out, "\n"))
		}
	}
	if waitErr != nil {
		return smokeResult{}, fmt.Errorf("wait task container: %w", waitErr)
	}

	return smokeResult{code: inspectExit(ctx, cli, containerID)}, nil
}

// inspectExit reads the container's final exit status via Inspect.
func inspectExit(ctx context.Context, cli *client.Client, containerID string) int {
	info, err := cli.ContainerInspect(ctx, containerID)
	if err != nil || info.State == nil {
		return -1
	}
	return int(info.State.ExitCode)
}

// printReport renders the egress report as indented JSON for the harness output.
func printReport(r *transport.EgressReport) error {
	b, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal egress report: %w", err)
	}
	fmt.Println(string(b))
	return nil
}

// splitAllow turns a comma-separated allow list into a trimmed host slice.
func splitAllow(s string) []string {
	if s == "" {
		return nil
	}
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
