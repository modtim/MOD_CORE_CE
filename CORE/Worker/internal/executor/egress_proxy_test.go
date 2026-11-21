package executor

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/pkg/stdcopy"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
)

// fakeEgressDocker is a recording dockerAPI for setupEgressProxy tests.
type fakeEgressDocker struct {
	calls   []string
	netOpts network.CreateOptions
	cfg     *container.Config
	hc      *container.HostConfig
	nc      *network.NetworkingConfig
	health  string // "" = no health reported yet

	logsBody string // bytes returned by ContainerLogs
	logsErr  error  // error returned by ContainerLogs
	logOpts  container.LogsOptions
}

func (f *fakeEgressDocker) NetworkCreate(_ context.Context, name string, o network.CreateOptions) (network.CreateResponse, error) {
	f.calls = append(f.calls, "NetworkCreate "+name)
	f.netOpts = o
	return network.CreateResponse{ID: "net-" + name}, nil
}

func (f *fakeEgressDocker) NetworkRemove(_ context.Context, id string) error {
	f.calls = append(f.calls, "NetworkRemove "+id)
	return nil
}

func (f *fakeEgressDocker) NetworkConnect(_ context.Context, netID, cid string, _ *network.EndpointSettings) error {
	f.calls = append(f.calls, "NetworkConnect "+netID+" "+cid)
	return nil
}

func (f *fakeEgressDocker) ContainerCreate(_ context.Context, cfg *container.Config, hc *container.HostConfig, nc *network.NetworkingConfig, _ *ocispec.Platform, name string) (container.CreateResponse, error) {
	f.calls = append(f.calls, "ContainerCreate "+name)
	f.cfg, f.hc, f.nc = cfg, hc, nc
	return container.CreateResponse{ID: "cid-proxy"}, nil
}

func (f *fakeEgressDocker) ContainerStart(_ context.Context, id string, _ container.StartOptions) error {
	f.calls = append(f.calls, "ContainerStart "+id)
	return nil
}

func (f *fakeEgressDocker) ContainerInspect(_ context.Context, id string) (types.ContainerJSON, error) {
	st := &types.ContainerState{Status: "running", Running: true}
	if f.health != "" {
		st.Health = &types.Health{Status: f.health}
	}
	return types.ContainerJSON{
		ContainerJSONBase: &types.ContainerJSONBase{ID: id, State: st},
		NetworkSettings: &types.NetworkSettings{Networks: map[string]*network.EndpointSettings{
			"mod-egr-task1234": {IPAddress: "10.231.0.2"},
		}},
	}, nil
}

func (f *fakeEgressDocker) ContainerLogs(_ context.Context, id string, o container.LogsOptions) (io.ReadCloser, error) {
	f.logOpts = o
	f.calls = append(f.calls, "ContainerLogs "+id)
	if f.logsErr != nil {
		return nil, f.logsErr
	}
	// A non-TTY container's logs arrive multiplexed, exactly as the daemon
	// sends them; frame the fixture so Teardown's demux is exercised.
	var framed bytes.Buffer
	if _, err := stdcopy.NewStdWriter(&framed, stdcopy.Stdout).Write([]byte(f.logsBody)); err != nil {
		return nil, err
	}
	return io.NopCloser(&framed), nil
}

func (f *fakeEgressDocker) ContainerRemove(_ context.Context, id string, o container.RemoveOptions) error {
	if !o.Force {
		return errors.New("expected force remove")
	}
	f.calls = append(f.calls, "ContainerRemove "+id)
	return nil
}

// stubEgressSubnet swaps in a fixed /28 (or an error) and records releases.
func stubEgressSubnet(t *testing.T, allocErr error) *[]string {
	t.Helper()
	released := []string{}
	oldAlloc, oldRel := egressAllocSubnet, egressReleaseSubnet
	oldTimeout, oldPoll := egressProxyHealthTimeout, egressProxyHealthPoll
	egressAllocSubnet = func(context.Context) (*net.IPNet, error) {
		if allocErr != nil {
			return nil, allocErr
		}
		_, n, _ := net.ParseCIDR("10.231.0.0/28")
		return n, nil
	}
	egressReleaseSubnet = func(n *net.IPNet) { released = append(released, n.String()) }
	egressProxyHealthTimeout = 50 * time.Millisecond
	egressProxyHealthPoll = 5 * time.Millisecond
	t.Cleanup(func() {
		egressAllocSubnet, egressReleaseSubnet = oldAlloc, oldRel
		egressProxyHealthTimeout, egressProxyHealthPoll = oldTimeout, oldPoll
	})
	return &released
}

func hasStr(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

func TestEgressProxySetupHappyPath(t *testing.T) {
	released := stubEgressSubnet(t, nil)
	f := &fakeEgressDocker{health: types.Healthy}
	labels := map[string]string{"mod.worker": "w1"}

	h, err := setupEgressProxy(context.Background(), f, "task1234-abcd", "mod/egress-proxy:1",
		[]string{"api.example.com", "*.hf.co"}, "mod-user-egress", labels)
	if err != nil {
		t.Fatalf("setupEgressProxy: %v", err)
	}
	if len(*released) != 0 {
		t.Errorf("subnet released on success: %v", *released)
	}

	// Network: internal, pinned subnet, caller labels + mod.task.
	if !f.netOpts.Internal || f.netOpts.IPAM == nil || f.netOpts.IPAM.Config[0].Subnet != "10.231.0.0/28" {
		t.Errorf("network opts = %+v", f.netOpts)
	}
	if f.netOpts.Labels["mod.task"] != "task1234-abcd" || f.netOpts.Labels["mod.worker"] != "w1" {
		t.Errorf("network labels = %v", f.netOpts.Labels)
	}
	if _, ok := labels["mod.task"]; ok {
		t.Error("caller's label map was mutated")
	}

	// Proxy container env + hardening.
	if !hasStr(f.cfg.Env, "MOD_EGRESS_ALLOW=api.example.com,*.hf.co") || !hasStr(f.cfg.Env, "MOD_EGRESS_TASK_ID=task1234-abcd") {
		t.Errorf("proxy env = %v", f.cfg.Env)
	}
	if f.cfg.User != "65534" {
		t.Errorf("user = %q", f.cfg.User)
	}
	hc := f.hc
	if !hc.ReadonlyRootfs || !hasStr(hc.CapDrop, "ALL") || !hasStr(hc.CapAdd, "NET_BIND_SERVICE") || len(hc.CapAdd) != 1 {
		t.Errorf("rootfs/caps = %v %v %v", hc.ReadonlyRootfs, hc.CapDrop, hc.CapAdd)
	}
	if !hasStr(hc.SecurityOpt, "no-new-privileges:true") {
		t.Errorf("security opts = %v", hc.SecurityOpt)
	}
	if hc.Memory != 64*1024*1024 || hc.NanoCPUs != 250_000_000 {
		t.Errorf("resources = %d / %d", hc.Memory, hc.NanoCPUs)
	}
	if len(hc.Binds) != 0 || len(hc.Mounts) != 0 {
		t.Errorf("proxy must have no binds/mounts: %v %v", hc.Binds, hc.Mounts)
	}
	ep := f.nc.EndpointsConfig["mod-egr-task1234"]
	if ep == nil || !hasStr(ep.Aliases, "mod-egress-proxy") {
		t.Errorf("endpoint config = %+v", f.nc.EndpointsConfig)
	}

	// Connected to the egress net after start.
	if !hasStr(f.calls, "NetworkConnect mod-user-egress cid-proxy") {
		t.Errorf("calls = %v", f.calls)
	}

	// Handle.
	if h.Network != "mod-egr-task1234" || h.ContainerID != "cid-proxy" || h.ProxyIP != "10.231.0.2" {
		t.Errorf("handle = %+v", h)
	}
	if h.Subnet.String() != "10.231.0.0/28" {
		t.Errorf("subnet = %v", h.Subnet)
	}
	if len(h.DNS) != 1 || h.DNS[0] != "10.231.0.2" {
		t.Errorf("DNS = %v", h.DNS)
	}
	for _, want := range []string{
		"HTTP_PROXY=http://mod-egress-proxy:3128",
		"HTTPS_PROXY=http://mod-egress-proxy:3128",
		"http_proxy=http://mod-egress-proxy:3128",
		"https_proxy=http://mod-egress-proxy:3128",
		"NO_PROXY=localhost,127.0.0.1",
		"NODE_USE_ENV_PROXY=1",
		"MOD_EGRESS_ENFORCED=true",
	} {
		if !hasStr(h.Env, want) {
			t.Errorf("task env missing %q: %v", want, h.Env)
		}
	}
	javaOK := false
	for _, e := range h.Env {
		if strings.HasPrefix(e, "JAVA_TOOL_OPTIONS=") && strings.Contains(e, "-Dhttps.proxyHost=mod-egress-proxy") &&
			strings.Contains(e, "-Dhttp.nonProxyHosts=localhost|127.*") {
			javaOK = true
		}
	}
	if !javaOK {
		t.Errorf("JAVA_TOOL_OPTIONS missing/incomplete: %v", h.Env)
	}
}

func TestEgressProxySetupHealthTimeoutCleansUp(t *testing.T) {
	released := stubEgressSubnet(t, nil)
	f := &fakeEgressDocker{health: types.Starting}

	h, err := setupEgressProxy(context.Background(), f, "task1234-abcd", "img", []string{"a.com"}, "mod-user-egress", nil)
	if err == nil || h != nil {
		t.Fatalf("want error, got h=%v err=%v", h, err)
	}
	if !errors.Is(err, errEgressProxyUnhealthy) {
		t.Errorf("err = %v, want errEgressProxyUnhealthy", err)
	}
	if !hasStr(f.calls, "ContainerRemove cid-proxy") || !hasStr(f.calls, "NetworkRemove mod-egr-task1234") {
		t.Errorf("cleanup calls missing: %v", f.calls)
	}
	if len(*released) != 1 || (*released)[0] != "10.231.0.0/28" {
		t.Errorf("released = %v", *released)
	}
}

func TestEgressProxySetupUnhealthyFailsFast(t *testing.T) {
	stubEgressSubnet(t, nil)
	egressProxyHealthTimeout = time.Hour // must not wait for the deadline
	f := &fakeEgressDocker{health: types.Unhealthy}

	if _, err := setupEgressProxy(context.Background(), f, "task1234", "img", nil, "egr", nil); !errors.Is(err, errEgressProxyUnhealthy) {
		t.Fatalf("err = %v", err)
	}
}

func TestEgressProxySetupSubnetExhausted(t *testing.T) {
	released := stubEgressSubnet(t, errors.New("no free /28 in 10.231.0.0/16"))
	f := &fakeEgressDocker{health: types.Healthy}

	_, err := setupEgressProxy(context.Background(), f, "task1234", "img", nil, "egr", nil)
	if err == nil || !strings.Contains(err.Error(), "subnet") {
		t.Fatalf("err = %v", err)
	}
	if len(f.calls) != 0 {
		t.Errorf("docker touched after subnet failure: %v", f.calls)
	}
	if len(*released) != 0 {
		t.Errorf("released = %v", *released)
	}
}

// LogLine JSON-lines like the real proxy emits: egress_allow / egress_deny /
// dns_deny (the latter two carry host/port/reason; dns_deny lines from the DNS
// stub omit reason so aggregateProxyLog falls back to the event name).
const egressProxyLogFixture = `{"ev":"egress_allow","host":"api.example.com","port":443}
{"ev":"egress_deny","host":"blocked.test","port":80,"reason":"allowlist"}
{"ev":"egress_deny","host":"blocked.test","port":80,"reason":"allowlist"}
{"ev":"dns_deny","host":"cve.shodan.io","reason":"dns_deny"}
`

func TestEgressProxyTeardownAggregatesAndRemoves(t *testing.T) {
	stubEgressSubnet(t, nil)
	f := &fakeEgressDocker{health: types.Healthy, logsBody: egressProxyLogFixture}
	labels := map[string]string{}

	h, err := setupEgressProxy(context.Background(), f, "task1234", "img",
		[]string{"api.example.com"}, "mod-user-egress", labels)
	if err != nil {
		t.Fatalf("setup: %v", err)
	}

	report := h.Teardown(context.Background(), f)

	// Aggregation passes through: allowed host present, denials counted + sorted.
	if report.Mode != "proxy" || !hasStr(report.AllowedHosts, "api.example.com") {
		t.Errorf("report mode/allowed = %q %v", report.Mode, report.AllowedHosts)
	}
	if len(report.Denied) != 2 {
		t.Fatalf("denied = %v, want 2 tuples", report.Denied)
	}
	// blocked.test denied twice must sort above the single dns_deny.
	if report.Denied[0].Host != "blocked.test" || report.Denied[0].Count != 2 {
		t.Errorf("top denied = %+v, want blocked.test/2", report.Denied[0])
	}
	if report.Denied[1].Host != "cve.shodan.io" || report.Denied[1].Reason != "dns_deny" {
		t.Errorf("second denied = %+v", report.Denied[1])
	}

	// Best-effort teardown ran and logged no errors.
	if !hasStr(f.calls, "ContainerRemove cid-proxy") {
		t.Errorf("container not force-removed: %v", f.calls)
	}
	if !hasStr(f.calls, "NetworkRemove mod-egr-task1234") {
		t.Errorf("network not removed: %v", f.calls)
	}
}

func TestEgressProxyTeardownRemovesEvenWhenLogsErrors(t *testing.T) {
	stubEgressSubnet(t, nil)
	f := &fakeEgressDocker{health: types.Healthy, logsErr: errors.New("daemon went away")}

	h, err := setupEgressProxy(context.Background(), f, "task1234", "img",
		[]string{"api.example.com"}, "mod-user-egress", nil)
	if err != nil {
		t.Fatalf("setup: %v", err)
	}

	// A report is still returned (hosts known even without the log stream),
	// and the container/network/subnet are torn down regardless.
	report := h.Teardown(context.Background(), f)
	if report == nil || len(report.AllowedHosts) == 0 || report.Denied != nil {
		t.Errorf("best-effort report = %+v", report)
	}
	if !hasStr(f.calls, "ContainerLogs cid-proxy") || !hasStr(f.calls, "ContainerRemove cid-proxy") {
		t.Errorf("logs/remove calls missing: %v", f.calls)
	}
	if !hasStr(f.calls, "NetworkRemove mod-egr-task1234") {
		t.Errorf("network remove missing: %v", f.calls)
	}
}

// TestEgressProxyHealthTimeoutClearsDockerDefaultProbeDelay guards against a
// regression to relying on the egress-proxy image's Dockerfile HEALTHCHECK
// defaults. On a real Docker daemon, a container with no explicit
// Interval/StartInterval on its create config gets Docker's built-in 30s
// default interval and never runs a probe at t=0 — confirmed against the
// live daemon (docker inspect .State.Health.Status stayed "starting" until
// exactly t=30s). setupEgressProxy must set an explicit fast Healthcheck
// (egressProxyHealthcheck) on the container it creates so the daemon's first
// probe fires almost immediately instead. This asserts the create config the
// fake dockerAPI captured carries that override, not just that
// egressProxyHealthTimeout is long enough to tolerate the slow path.
func TestEgressProxyHealthTimeoutClearsDockerDefaultProbeDelay(t *testing.T) {
	stubEgressSubnet(t, nil)
	f := &fakeEgressDocker{health: types.Healthy}

	h, err := setupEgressProxy(context.Background(), f, "task1234-abcd", "img",
		[]string{"a.com"}, "mod-user-egress", nil)
	if err != nil {
		t.Fatalf("setupEgressProxy: %v", err)
	}
	t.Cleanup(func() { h.Teardown(context.Background(), f) })

	hc := f.cfg.Healthcheck
	if hc == nil {
		t.Fatal("container create config carries no Healthcheck override; the proxy " +
			"will inherit Docker's 30s default first-probe delay on a real daemon")
	}
	wantTest := []string{"CMD", "/mod-egress-proxy", "-healthcheck"}
	if len(hc.Test) != len(wantTest) {
		t.Errorf("Healthcheck.Test = %v, want %v", hc.Test, wantTest)
	} else {
		for i := range wantTest {
			if hc.Test[i] != wantTest[i] {
				t.Errorf("Healthcheck.Test = %v, want %v", hc.Test, wantTest)
				break
			}
		}
	}
	if hc.Interval <= 0 || hc.Interval > 2*time.Second {
		t.Errorf("Healthcheck.Interval = %s, want a fast (<=2s) interval", hc.Interval)
	}
	if hc.StartInterval <= 0 || hc.StartInterval > time.Second {
		t.Errorf("Healthcheck.StartInterval = %s, want a fast (<=1s) start interval", hc.StartInterval)
	}
	if hc.StartPeriod <= 0 {
		t.Errorf("Healthcheck.StartPeriod = %s, want > 0 so early failed probes don't flap unhealthy", hc.StartPeriod)
	}
	if hc.Timeout <= 0 {
		t.Errorf("Healthcheck.Timeout = %s, want > 0", hc.Timeout)
	}
	if hc.Retries <= 0 {
		t.Errorf("Healthcheck.Retries = %d, want > 0", hc.Retries)
	}

	// egressProxyHealthTimeout no longer needs to clear Docker's 30s default
	// probe delay (the explicit Healthcheck above makes the first probe fire
	// in ~1s), so it should be a modest bound, not tens of seconds of
	// per-task startup latency.
	if egressProxyHealthTimeout > 20*time.Second {
		t.Errorf("egressProxyHealthTimeout = %s is too long now that the proxy sets an "+
			"explicit fast Healthcheck; every task pays this as startup latency on failure",
			egressProxyHealthTimeout)
	}
}
