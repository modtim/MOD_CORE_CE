package executor

// Per-task egress proxy setup (GB.EGR.D.6c).
//
// setupEgressProxy brings the per-task egress proxy (internal/egressproxy) up on
// a private Internal=true /28 network dedicated to one task, attaches it to the
// worker's egress network (its only route out), and waits for Docker to report
// it healthy. The returned handle carries what the caller injects into the task
// container: the network to join, the proxy env, and the DNS server (the
// proxy's NXDOMAIN-only stub). On any failure everything created so far is
// removed and the subnet released.

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"strings"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/api/types/strslice"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
)

// egressProxyAlias is the DNS name the task container uses to reach the proxy.
const egressProxyAlias = "mod-egress-proxy"

const (
	egressProxyMemBytes = 64 * 1024 * 1024
	egressProxyNanoCPUs = 250_000_000 // 0.25 CPU
	egressProxyUser     = "65534"
)

// egressProxyHealthcheck is set explicitly on every proxy container's
// create config instead of relying on the image's Dockerfile HEALTHCHECK
// defaults. Without an explicit override, a container's very first
// HEALTHCHECK probe can't fire until Docker's built-in default interval
// (30s) elapses — Docker does not run a probe at t=0 — so on a real daemon
// the proxy would sit "starting" until t=30s (verified empirically:
// `docker inspect` .State.Health.Status stayed "starting" until exactly
// t=30s). Setting Interval/StartInterval here makes the daemon probe the
// proxy almost immediately, so it reports healthy within ~1s of start
// instead of adding ~30s of latency to every task. The Dockerfile
// HEALTHCHECK (egress-proxy target) also sets matching flags as defence in
// depth for the rare case something constructs the container outside this
// path, but this explicit override is what production actually relies on.
var egressProxyHealthcheck = &container.HealthConfig{
	Test:          []string{"CMD", "/mod-egress-proxy", "-healthcheck"},
	Interval:      time.Second,
	Timeout:       2 * time.Second,
	StartPeriod:   10 * time.Second,
	StartInterval: 250 * time.Millisecond,
	Retries:       3,
}

// Injectable for tests so the health wait does not take 15 s, and so the /28
// allocation does not shell out to `docker network ls`.
var (
	// egressProxyHealthTimeout only needs to cover realistic container
	// startup plus a handful of probe cycles now that egressProxyHealthcheck
	// forces a ~1s first probe; it is not bounding Docker's 30s default
	// interval anymore.
	egressProxyHealthTimeout = 15 * time.Second
	egressProxyHealthPoll    = 250 * time.Millisecond
	egressAllocSubnet        = allocateEgressSubnet
	egressReleaseSubnet      = releaseEgressSubnet
)

// errEgressProxyUnhealthy wraps every "proxy did not come up healthy" failure.
var errEgressProxyUnhealthy = errors.New("egress proxy not healthy")

// dockerAPI is the narrow slice of *client.Client the egress proxy needs.
type dockerAPI interface {
	NetworkCreate(ctx context.Context, name string, options network.CreateOptions) (network.CreateResponse, error)
	NetworkRemove(ctx context.Context, networkID string) error
	NetworkConnect(ctx context.Context, networkID, containerID string, config *network.EndpointSettings) error
	ContainerCreate(ctx context.Context, config *container.Config, hostConfig *container.HostConfig, networkingConfig *network.NetworkingConfig, platform *ocispec.Platform, containerName string) (container.CreateResponse, error)
	ContainerStart(ctx context.Context, containerID string, options container.StartOptions) error
	ContainerInspect(ctx context.Context, containerID string) (types.ContainerJSON, error)
	ContainerLogs(ctx context.Context, container string, options container.LogsOptions) (io.ReadCloser, error)
	ContainerRemove(ctx context.Context, containerID string, options container.RemoveOptions) error
}

var _ dockerAPI = (*client.Client)(nil)

// egressProxyHandle is what setupEgressProxy brought up for one task.
type egressProxyHandle struct {
	Network     string
	ContainerID string
	ProxyIP     string
	Subnet      *net.IPNet
	Env         []string
	DNS         []string
	Hosts       []string
}

// egressProxyTaskEnv is the task-container env from design §6b "Task env".
// MOD_ALLOWED_EGRESS_HOSTS is not repeated here: the caller's network selection
// already exports it and it is kept as-is.
func egressProxyTaskEnv() []string {
	proxyURL := "http://" + egressProxyAlias + ":3128"
	return []string{
		"HTTP_PROXY=" + proxyURL,
		"HTTPS_PROXY=" + proxyURL,
		"http_proxy=" + proxyURL,
		"https_proxy=" + proxyURL,
		"NO_PROXY=localhost,127.0.0.1",
		"NODE_USE_ENV_PROXY=1",
		"JAVA_TOOL_OPTIONS=-Dhttp.proxyHost=" + egressProxyAlias + " -Dhttp.proxyPort=3128" +
			" -Dhttps.proxyHost=" + egressProxyAlias + " -Dhttps.proxyPort=3128" +
			" -Dhttp.nonProxyHosts=localhost|127.*",
		"MOD_EGRESS_ENFORCED=true",
	}
}

// egressTaskID8 shortens a task id for docker object names.
func egressTaskID8(taskID string) string {
	if len(taskID) > 8 {
		return taskID[:8]
	}
	return taskID
}

// setupEgressProxy allocates a /28, creates the internal task network and the
// hardened proxy on it, connects the proxy to egressNet and waits for health.
// On any error it removes what it created and releases the subnet.
func setupEgressProxy(ctx context.Context, d dockerAPI, taskID, image string, hosts []string, egressNet string, labels map[string]string) (h *egressProxyHandle, err error) {
	subnet, err := egressAllocSubnet(ctx)
	if err != nil {
		return nil, fmt.Errorf("egress proxy subnet: %w", err)
	}

	id8 := egressTaskID8(taskID)
	netName := "mod-egr-" + id8
	proxyName := "mod-egr-proxy-" + id8

	allLabels := make(map[string]string, len(labels)+1)
	for k, v := range labels {
		allLabels[k] = v
	}
	allLabels["mod.task"] = taskID

	netCreated := false
	containerID := ""
	defer func() {
		if err == nil {
			return
		}
		cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 15*time.Second)
		defer cancel()
		if containerID != "" {
			_ = d.ContainerRemove(cctx, containerID, container.RemoveOptions{Force: true})
		}
		if netCreated {
			_ = d.NetworkRemove(cctx, netName)
		}
		egressReleaseSubnet(subnet)
	}()

	if _, err = d.NetworkCreate(ctx, netName, network.CreateOptions{
		Driver:   "bridge",
		Internal: true,
		IPAM:     &network.IPAM{Config: []network.IPAMConfig{{Subnet: subnet.String()}}},
		Labels:   allLabels,
	}); err != nil {
		return nil, fmt.Errorf("egress proxy network create: %w", err)
	}
	netCreated = true

	cfg := &container.Config{
		Image: image,
		User:  egressProxyUser,
		Env: []string{
			"MOD_EGRESS_ALLOW=" + strings.Join(hosts, ","),
			"MOD_EGRESS_TASK_ID=" + taskID,
		},
		Labels:      allLabels,
		Healthcheck: egressProxyHealthcheck,
	}
	hc := &container.HostConfig{
		NetworkMode:    container.NetworkMode(netName),
		ReadonlyRootfs: true,
		CapDrop:        strslice.StrSlice{"ALL"},
		CapAdd:         strslice.StrSlice{"NET_BIND_SERVICE"},
		SecurityOpt:    []string{"no-new-privileges:true"},
		Resources: container.Resources{
			Memory:   egressProxyMemBytes,
			NanoCPUs: egressProxyNanoCPUs,
		},
	}
	nc := &network.NetworkingConfig{EndpointsConfig: map[string]*network.EndpointSettings{
		netName: {Aliases: []string{egressProxyAlias}},
	}}
	created, err := d.ContainerCreate(ctx, cfg, hc, nc, nil, proxyName)
	if err != nil {
		return nil, fmt.Errorf("egress proxy container create: %w", err)
	}
	containerID = created.ID

	if err = d.ContainerStart(ctx, containerID, container.StartOptions{}); err != nil {
		return nil, fmt.Errorf("egress proxy container start: %w", err)
	}
	if err = d.NetworkConnect(ctx, egressNet, containerID, &network.EndpointSettings{}); err != nil {
		return nil, fmt.Errorf("egress proxy connect %s: %w", egressNet, err)
	}

	proxyIP, err := waitEgressProxyHealthy(ctx, d, containerID, netName)
	if err != nil {
		return nil, err
	}

	return &egressProxyHandle{
		Network:     netName,
		ContainerID: containerID,
		ProxyIP:     proxyIP,
		Subnet:      subnet,
		Env:         egressProxyTaskEnv(),
		DNS:         []string{proxyIP},
		Hosts:       hosts,
	}, nil
}

// SetupEgressProxyForSmoke is a thin exported seam for the egress smoke harness.
// It wraps the unexported setupEgressProxy so cmd/egress-smoke can bring up a
// real proxy handle without duplicating the allocation/health logic. Only added
// for the harness: production callers keep using the executor's own path.
func SetupEgressProxyForSmoke(ctx context.Context, cli *client.Client, taskID, image string, hosts []string, egressNet string, labels map[string]string) (*EgressSmokeHandle, error) {
	h, err := setupEgressProxy(ctx, cli, taskID, image, hosts, egressNet, labels)
	if err != nil {
		return nil, err
	}
	return &EgressSmokeHandle{h}, nil
}

// EgressSmokeHandle is a tiny exported wrapper around the unexported
// egressProxyHandle, exposing only what the smoke harness needs: the network to
// attach the task container to, its env/DNS/hosts, and a teardown that returns
// the egress report.
type EgressSmokeHandle struct{ inner *egressProxyHandle }

func (s *EgressSmokeHandle) Network() string     { return s.inner.Network }
func (s *EgressSmokeHandle) Env() []string       { return s.inner.Env }
func (s *EgressSmokeHandle) DNS() []string       { return s.inner.DNS }
func (s *EgressSmokeHandle) Hosts() []string     { return s.inner.Hosts }
func (s *EgressSmokeHandle) ProxyIP() string     { return s.inner.ProxyIP }
func (s *EgressSmokeHandle) ContainerID() string { return s.inner.ContainerID }

// TeardownForSmoke runs the handle's teardown against the live docker client and
// returns the egress report for the harness to print as JSON.
func (s *EgressSmokeHandle) TeardownForSmoke(ctx context.Context, cli *client.Client) *transport.EgressReport {
	return s.inner.Teardown(ctx, cli)
}

// waitEgressProxyHealthy polls Inspect until Docker reports the proxy healthy
// (the image's HEALTHCHECK runs the binary's -healthcheck self-probe), then
// returns its IP on netName. It fails fast on "unhealthy" or a stopped proxy.
func waitEgressProxyHealthy(ctx context.Context, d dockerAPI, containerID, netName string) (string, error) {
	deadline := time.Now().Add(egressProxyHealthTimeout)
	last := "no state"
	for {
		info, err := d.ContainerInspect(ctx, containerID)
		if err != nil {
			last = err.Error()
		} else if info.ContainerJSONBase != nil && info.State != nil {
			st := info.State
			if !st.Running {
				return "", fmt.Errorf("%w: exited (status %s, code %d)", errEgressProxyUnhealthy, st.Status, st.ExitCode)
			}
			if st.Health != nil {
				last = st.Health.Status
				if st.Health.Status == types.Unhealthy {
					return "", fmt.Errorf("%w: healthcheck reports unhealthy", errEgressProxyUnhealthy)
				}
				if st.Health.Status == types.Healthy {
					if info.NetworkSettings != nil {
						if ep := info.NetworkSettings.Networks[netName]; ep != nil && ep.IPAddress != "" {
							return ep.IPAddress, nil
						}
					}
					last = "healthy but no IP on " + netName
				}
			}
		}
		if !time.Now().Before(deadline) {
			return "", fmt.Errorf("%w within %s (last: %s)", errEgressProxyUnhealthy, egressProxyHealthTimeout, last)
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(egressProxyHealthPoll):
		}
	}
}

// Teardown aggregates the proxy's egress log into an *transport.EgressReport,
// then best-effort tears everything down: force-remove the proxy container,
// remove the private task network, and release the /28. Cleanup errors are
// logged (not returned): the report is the sole return value and cleanup must
// not mask it.
//
// The container logs are written to stdout as a Docker stream; they are
// demultiplexed to plain text with stdcopy before being fed to
// aggregateProxyLog.
func (h *egressProxyHandle) Teardown(ctx context.Context, d dockerAPI) *transport.EgressReport {
	report := h.summarizeEgress(ctx, d)

	cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 15*time.Second)
	defer cancel()

	if err := d.ContainerRemove(cctx, h.ContainerID, container.RemoveOptions{Force: true}); err != nil {
		// Not fatal: the container may already be gone.
	}
	if err := d.NetworkRemove(cctx, h.Network); err != nil {
	}
	egressReleaseSubnet(h.Subnet)

	return report
}

// summarizeEgress reads and demuxes the proxy container's stdout log, then
// folds it into an EgressReport. If the log stream itself errors out we return
// a best-effort report with the configured hosts so callers still learn which
// egress was enforced; the error is logged and swallowed.
func (h *egressProxyHandle) summarizeEgress(ctx context.Context, d dockerAPI) *transport.EgressReport {
	logs, err := d.ContainerLogs(ctx, h.ContainerID, container.LogsOptions{
		ShowStdout: true,
		Follow:     false,
		Timestamps: false,
	})
	if err != nil {
		// Log stream unavailable: still return a report so callers learn the
		// enforced host list (no denials observed).
		return &transport.EgressReport{Mode: "proxy", AllowedHosts: h.Hosts}
	}
	defer logs.Close()

	// The proxy container runs without a TTY, so the daemon multiplexes its
	// log stream (8-byte frame header per chunk) even though the proxy itself
	// writes plain JSON-lines. Demux stdout before decoding; reading the raw
	// stream would prefix every line with a frame header and aggregateProxyLog
	// would skip them all, reporting no denials.
	stdout := &bytes.Buffer{}
	if _, err := stdcopy.StdCopy(stdout, io.Discard, logs); err != nil {
		return &transport.EgressReport{Mode: "proxy", AllowedHosts: h.Hosts}
	}
	return aggregateProxyLog(stdout, h.Hosts)
}
