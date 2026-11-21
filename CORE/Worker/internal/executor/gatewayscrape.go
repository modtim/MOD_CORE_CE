// stack-ha-reconciliation group R 27.5c: LLM-gateway scrape.
//
// A stack that fronts its serving replicas with the SGLang model gateway
// (image `lmsysorg/sgl-model-gateway:*`) exposes two scrape surfaces:
//
//   * GET :8080/workers  — a JSON list of {url, is_healthy, load, ...} replicas.
//   * GET :29000/metrics — a Prometheus text dump.
//
// The worker's job is narrow: fetch the raw payloads and forward them in the
// heartbeat. ALL parsing happens on the manager (services/llm_gateway_metrics).
// The worker does no more than a json.Unmarshal of /workers so the heartbeat
// ships a parsed object rather than a string.
//
// Best-effort throughout. A scrape that fails, times out, or finds no gateway
// simply omits the `gateway` key; it must never fail the observation, delay the
// heartbeat past its budget, or panic.

package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/docker/docker/api/types"
)

// gatewayImageMarker is the substring that identifies the gateway container by
// image. The manager pins `lmsysorg/sgl-model-gateway:v0.3.2`, but matching on
// the repo keeps a version bump from silently breaking the scrape.
const gatewayImageMarker = "sgl-model-gateway"

// Container ports the gateway binds. 8080 is published by the stack; 29000 is
// the in-container Prometheus port and is normally NOT published, so it is only
// reachable via the container's bridge IP — metrics therefore degrades to absent
// on the published-port fallback, which is the documented "send only workers" case.
const (
	gatewayWorkersContainerPort = 8080
	gatewayMetricsContainerPort = 29000
	gatewayHostFallbackIP       = "127.0.0.1"
)

// Timeouts. A single call must not hang the heartbeat, and the whole per-project
// scrape (both endpoints, possibly across two candidate addresses) is capped by
// the pass budget so a fleet of wedged gateways cannot stack their costs.
const (
	gatewayCallTimeout = 2 * time.Second
	gatewayPassBudget  = 4 * time.Second
)

// gatewayMaxBody caps the response size so a misbehaving gateway cannot blow up
// the heartbeat payload or pin memory. 1 MiB is far above a realistic
// /workers list or /metrics dump for a serving stack.
const gatewayMaxBody = 1 << 20

// gatewayHTTPDo performs one GET and returns the (size-capped) body. It is a
// func var, not a method, so tests can redirect it at httptest servers without
// constructing an Executor. The default performs a real request with a hard
// per-call timeout bound by ctx.
var gatewayHTTPDo = func(ctx context.Context, url string) ([]byte, error) {
	client := &http.Client{Timeout: gatewayCallTimeout}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("gateway %s returned %d", url, resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, gatewayMaxBody))
}

// isGatewayImage reports whether a container image is the LLM serving gateway.
func isGatewayImage(image string) bool {
	return strings.Contains(strings.ToLower(image), gatewayImageMarker)
}

// gatewayCandidate is one host:port pair set the gateway might be listening on.
type gatewayCandidate struct {
	host        string
	workersPort uint16
	metricsPort uint16 // 0 => metrics not reachable via this candidate
}

func (c gatewayCandidate) workersURL() string {
	return "http://" + net.JoinHostPort(c.host, strconv.Itoa(int(c.workersPort))) + "/workers"
}

func (c gatewayCandidate) metricsURL() string {
	return "http://" + net.JoinHostPort(c.host, strconv.Itoa(int(c.metricsPort))) + "/metrics"
}

// gatewayCandidates resolves, in priority order, the addresses to dial for a
// gateway container.
//
//  1. The container's bridge IP (from ContainerInspect) + the container's own
//     ports. This is the primary path: bridge IPs are routable from a host
//     process, and it also reaches an embedded (container) worker that shares a
//     network with the stack.
//  2. The host-published port for 8080, dialed on 127.0.0.1. This is the
//     fallback for a host-process worker when the IP is unavailable; the
//     metrics port is usually not published, so it degrades to workers-only.
func gatewayCandidates(c types.Container, containerIP string) []gatewayCandidate {
	var out []gatewayCandidate

	if ip := strings.TrimSpace(containerIP); ip != "" {
		out = append(out, gatewayCandidate{
			host:        ip,
			workersPort: gatewayWorkersContainerPort,
			metricsPort: gatewayMetricsContainerPort,
		})
	}

	var pubWorkers, pubMetrics uint16
	haveWorkers := false
	for _, p := range c.Ports {
		switch p.PrivatePort {
		case gatewayWorkersContainerPort:
			if p.PublicPort > 0 {
				pubWorkers = p.PublicPort
				haveWorkers = true
			}
		case gatewayMetricsContainerPort:
			pubMetrics = p.PublicPort
		}
	}
	if haveWorkers {
		out = append(out, gatewayCandidate{
			host:        gatewayHostFallbackIP,
			workersPort: pubWorkers,
			metricsPort: pubMetrics, // may be 0 => metrics skipped
		})
	}
	return out
}

// benchIngressCandidates resolves dial targets for a container that has no
// sgl-model-gateway image (bug 1543 — a Strata coder lane, or any other
// non-gr-llm-serving stack, serves its OpenAI-compatible surface directly, not
// behind a dedicated gateway container). Unlike gatewayCandidates, it cannot
// assume a fixed well-known port — a gr-llm-serving gateway always binds 8080,
// but an arbitrary stack's own ingress port varies — so it reads the
// container's own published port mapping instead.
//
// Same two-step priority as gatewayCandidates: the container's bridge IP with
// its own container-side port first (reachable from a host-process worker and
// an embedded one alike), the host-published port on 127.0.0.1 second.
func benchIngressCandidates(c types.Container, containerIP string) []benchTarget {
	var out []benchTarget
	ip := strings.TrimSpace(containerIP)
	seen := map[uint16]bool{}
	for _, p := range c.Ports {
		if p.PublicPort == 0 || p.PrivatePort == 0 {
			continue
		}
		if ip != "" && !seen[p.PrivatePort] {
			out = append(out, benchTarget{host: ip, port: p.PrivatePort})
			seen[p.PrivatePort] = true
		}
	}
	for _, p := range c.Ports {
		if p.PublicPort == 0 {
			continue
		}
		out = append(out, benchTarget{host: gatewayHostFallbackIP, port: p.PublicPort})
	}
	return out
}

// scrapeGatewayEndpoints fetches /workers (required) and /metrics (best-effort)
// from the first candidate that answers /workers, and returns the raw payload
// shaped for the heartbeat:
//
//	{"workers": <parsed JSON object/array, or raw string on unparse>, "metrics": "<text>"}
//
// It returns nil when no candidate serves /workers, so the caller omits the
// `gateway` key. It never returns an error: any failure is a silent omission.
func scrapeGatewayEndpoints(ctx context.Context, candidates []gatewayCandidate) map[string]interface{} {
	if len(candidates) == 0 {
		return nil
	}
	passCtx, cancel := context.WithTimeout(ctx, gatewayPassBudget)
	defer cancel()

	var workersBody []byte
	var served gatewayCandidate
	found := false
	for _, cand := range candidates {
		body, err := gatewayHTTPDo(passCtx, cand.workersURL())
		if err != nil {
			continue // try the next candidate
		}
		workersBody = body
		served = cand
		found = true
		break
	}
	if !found {
		return nil
	}

	gw := map[string]interface{}{}
	var parsed interface{}
	if err := json.Unmarshal(workersBody, &parsed); err == nil {
		gw["workers"] = parsed
	} else {
		// The manager also accepts a JSON string; keep the raw text rather than
		// dropping the payload because one endpoint emitted non-object JSON.
		gw["workers"] = string(workersBody)
	}

	if served.metricsPort > 0 {
		if mbody, err := gatewayHTTPDo(passCtx, served.metricsURL()); err == nil {
			gw["metrics"] = string(mbody)
		}
		// metrics down -> leave the key absent (workers only).
	}
	return gw
}
