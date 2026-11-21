package executor

// Post-`compose up` network self-heal.
//
// On worker-AlphaBoxEVO-X2 (Docker 29.8.1, compose v5.5.1) the ingress forwarder
// of stack 14 — declared on [mod-stack-isolated (internal), mod-stack-egress] —
// came up attached to ONLY the isolated network. Docker never publishes ports
// for a container whose only network is internal, so :8000 was bound in config
// and dead in practice, with compose reporting success. The spec the API sent
// was correct and the same compose attaches both networks on 29.6.2 / v5.3.1,
// so this is a daemon/compose-side drop the worker cannot prevent — only
// detect and repair.
//
// After every `compose up` the worker compares each service container's
// attached networks with what the compose file declares, `docker network
// connect`s whatever is missing, and restarts a container that publishes ports
// so the port bindings are actually applied. Strictly best-effort: a heal
// failure is logged, never returned — the deploy already succeeded as far as
// compose is concerned, and this must not become a new way for one to fail.

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// expectedAttach is one network a service container must be on, by its real
// (daemon-side) name, with the aliases compose would have given it.
type expectedAttach struct {
	Network string
	Aliases []string
}

// serviceNetPlan is what one compose service's containers should look like.
type serviceNetPlan struct {
	Service  string
	Nets     []expectedAttach
	HasPorts bool
}

// composeProjectName mirrors compose's project-name normalisation (lowercase,
// only [a-z0-9_-]) — the name it prefixes networks with and stamps on the
// com.docker.compose.project label.
func composeProjectName(project string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(project) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '-' || r == '_' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// expectedStackNetworks computes, per service, the networks its containers must
// be attached to. A top-level `name:` is used verbatim, an `external` network
// by its key, anything else as `<project>_<key>`. A service with no `networks:`
// gets compose's implicit `default`. Services with `network_mode` (host,
// service:ts-x, none, ...) are skipped — they have no attachments of their own.
func expectedStackNetworks(composeYAML, project string) ([]serviceNetPlan, error) {
	var doc struct {
		Services map[string]struct {
			Networks    interface{}   `yaml:"networks"`
			NetworkMode string        `yaml:"network_mode"`
			Ports       []interface{} `yaml:"ports"`
		} `yaml:"services"`
		Networks map[string]struct {
			Name     string      `yaml:"name"`
			External interface{} `yaml:"external"`
		} `yaml:"networks"`
	}
	if err := yaml.Unmarshal([]byte(composeYAML), &doc); err != nil {
		return nil, err
	}
	proj := composeProjectName(project)
	resolve := func(key string) string {
		if n, ok := doc.Networks[key]; ok {
			if name := strings.TrimSpace(n.Name); name != "" {
				return name
			}
			switch v := n.External.(type) {
			case nil:
			case bool:
				if v {
					return key
				}
			default: // legacy {external: {name: x}} mapping
				if m, ok := v.(map[string]interface{}); ok {
					if s, ok := m["name"].(string); ok && strings.TrimSpace(s) != "" {
						return strings.TrimSpace(s)
					}
				}
				return key
			}
		}
		return proj + "_" + key
	}

	names := make([]string, 0, len(doc.Services))
	for name := range doc.Services {
		names = append(names, name)
	}
	sort.Strings(names)

	var plans []serviceNetPlan
	for _, name := range names {
		svc := doc.Services[name]
		if strings.TrimSpace(svc.NetworkMode) != "" {
			continue
		}
		keys := serviceNetworkKeys(svc.Networks)
		if svc.Networks == nil {
			keys = []string{"default"}
		}
		sort.Strings(keys)
		plan := serviceNetPlan{Service: name, HasPorts: len(svc.Ports) > 0}
		for _, key := range keys {
			aliases := []string{name}
			if m, ok := svc.Networks.(map[string]interface{}); ok {
				if cfg, ok := m[key].(map[string]interface{}); ok {
					if al, ok := cfg["aliases"].([]interface{}); ok {
						for _, a := range al {
							if s, ok := a.(string); ok && strings.TrimSpace(s) != "" && s != name {
								aliases = append(aliases, s)
							}
						}
					}
				}
			}
			plan.Nets = append(plan.Nets, expectedAttach{Network: resolve(key), Aliases: aliases})
		}
		plans = append(plans, plan)
	}
	return plans, nil
}

// missingNetworks returns the expected attachments absent from `attached`
// (the daemon-side network names a container is on). Used only where the
// richer staleOrMissing check (network ID + aliases) is not available.
func missingNetworks(expected []expectedAttach, attached []string) []expectedAttach {
	have := make(map[string]bool, len(attached))
	for _, n := range attached {
		have[n] = true
	}
	var out []expectedAttach
	for _, e := range expected {
		if !have[e.Network] {
			out = append(out, e)
		}
	}
	return out
}

// ctrNetAttach is one docker network endpoint on a container, as reported by
// `docker inspect -f {{json .NetworkSettings.Networks}}`.
type ctrNetAttach struct {
	NetworkID string   `json:"NetworkID"`
	Aliases   []string `json:"Aliases"`
	// DNSNames is the newer field some Docker/compose versions populate
	// instead of (or in addition to) Aliases — it also includes the
	// container's own name/short ID, not just compose aliases.
	DNSNames []string `json:"DNSNames"`
}

func (a ctrNetAttach) hasAlias(want string) bool {
	for _, x := range a.Aliases {
		if x == want {
			return true
		}
	}
	for _, x := range a.DNSNames {
		if x == want {
			return true
		}
	}
	return false
}

// staleAttach is one expected attachment a container needs (re)connecting
// for, with why.
type staleAttach struct {
	expectedAttach
	Reason       string // "missing" | "stale network id" | "missing alias"
	WasConnected bool   // true if already on the network (needs disconnect first)
}

// staleOrMissing compares a container's live per-network attachments against
// what it is expected to have. A network the daemon reassigned a new ID to
// (deleted and recreated — see stacksubnet.go's redeploy fix) looks
// "attached" by name but is not the same network any more, and Docker's
// embedded DNS on it has never heard of this container's service name; a
// network missing an expected alias has the same symptom. Both need a
// disconnect+reconnect, not just a connect.
func staleOrMissing(expected []expectedAttach, attached map[string]ctrNetAttach, liveIDs map[string]string) []staleAttach {
	var out []staleAttach
	for _, e := range expected {
		got, ok := attached[e.Network]
		if !ok {
			out = append(out, staleAttach{expectedAttach: e, Reason: "missing"})
			continue
		}
		if liveID := liveIDs[e.Network]; liveID != "" && got.NetworkID != "" && got.NetworkID != liveID {
			out = append(out, staleAttach{expectedAttach: e, Reason: "stale network id", WasConnected: true})
			continue
		}
		for _, want := range e.Aliases {
			if !got.hasAlias(want) {
				out = append(out, staleAttach{expectedAttach: e, Reason: "missing alias", WasConnected: true})
				break
			}
		}
	}
	return out
}

// dockerNetworkIDs resolves the live daemon-side IDs of the given network
// names, best-effort: a name that does not (yet) exist is simply absent from
// the result rather than failing the whole call.
func dockerNetworkIDs(ctx context.Context, names []string) map[string]string {
	out := map[string]string{}
	if len(names) == 0 {
		return out
	}
	args := append([]string{"network", "inspect", "-f", "{{.Name}}|{{.Id}}"}, names...)
	raw, _ := dockerCLI(ctx, args...)
	for _, line := range strings.Split(raw, "\n") {
		name, id, ok := strings.Cut(strings.TrimSpace(line), "|")
		if ok && name != "" && id != "" {
			out[name] = id
		}
	}
	return out
}

// dockerCLI runs `docker <args...>` and returns combined output. A seam so
// tests can fake the daemon.
var dockerCLI = func(ctx context.Context, args ...string) (string, error) {
	out, err := execCommandContext(ctx, "docker", args...).CombinedOutput()
	return string(out), err
}

// healStackNetworks reconnects any declared network a stack container is
// missing, restarting port-publishing containers it had to fix. Returns the
// log lines it produced (also streamed through cb.OnLogBatch).
func (e *Executor) healStackNetworks(ctx context.Context, cb Callbacks, project, composeYAML string) []string {
	var lines []string
	logf := func(format string, a ...interface{}) {
		lines = append(lines, fmt.Sprintf(format, a...))
	}
	defer func() {
		if cb.OnLogBatch != nil && len(lines) > 0 {
			cb.OnLogBatch(lines)
		}
	}()

	plans, err := expectedStackNetworks(composeYAML, project)
	if err != nil {
		logf("network self-heal: skipped, cannot parse compose: %v", err)
		return lines
	}
	proj := composeProjectName(project)

	netSet := map[string]bool{}
	for _, plan := range plans {
		for _, n := range plan.Nets {
			netSet[n.Network] = true
		}
	}
	netNames := make([]string, 0, len(netSet))
	for n := range netSet {
		netNames = append(netNames, n)
	}
	sort.Strings(netNames)
	liveIDs := dockerNetworkIDs(ctx, netNames)

	for _, plan := range plans {
		out, err := dockerCLI(ctx, "ps", "-a", "-q", "--no-trunc",
			"--filter", "label=com.docker.compose.project="+proj,
			"--filter", "label=com.docker.compose.service="+plan.Service)
		if err != nil {
			logf("network self-heal: cannot list containers for %s: %v", plan.Service, err)
			continue
		}
		for _, id := range strings.Fields(out) {
			raw, err := dockerCLI(ctx, "inspect", "-f",
				"{{.Name}}|{{.State.Running}}|{{json .NetworkSettings.Networks}}", id)
			if err != nil {
				logf("network self-heal: cannot inspect %s: %v", shortID(id), err)
				continue
			}
			ctrName, running, attached, err := parseInspectNetworks(raw)
			if err != nil {
				logf("network self-heal: cannot read networks of %s: %v", shortID(id), err)
				continue
			}
			stale := staleOrMissing(plan.Nets, attached, liveIDs)
			if len(stale) == 0 {
				continue
			}
			fixed := 0
			for _, m := range stale {
				if m.WasConnected {
					// Reconnecting without disconnecting first errors "already
					// exists" and neither refreshes the endpoint's network ID
					// nor adds the missing alias.
					if dout, derr := dockerCLI(ctx, "network", "disconnect", "-f", m.Network, id); derr != nil {
						logf("network self-heal: %s disconnect from %s (%s) FAILED: %v %s",
							ctrName, m.Network, m.Reason, derr, strings.TrimSpace(dout))
						continue
					}
				}
				args := []string{"network", "connect"}
				for _, a := range m.Aliases {
					args = append(args, "--alias", a)
				}
				args = append(args, m.Network, id)
				if cout, err := dockerCLI(ctx, args...); err != nil {
					logf("network self-heal: %s was %s on %s; reconnect FAILED: %v %s",
						ctrName, m.Reason, m.Network, err, strings.TrimSpace(cout))
					continue
				}
				fixed++
				logf("network self-heal: %s was %s on %s; repaired (reconnected with alias(es) %s)",
					ctrName, m.Reason, m.Network, strings.Join(m.Aliases, ", "))
			}
			// Port bindings are applied at start; a container that was running
			// with only an internal network needs a restart to publish them.
			if fixed > 0 && plan.HasPorts && running {
				if rout, err := dockerCLI(ctx, "restart", id); err != nil {
					logf("network self-heal: restart of %s FAILED: %v %s", ctrName, err, strings.TrimSpace(rout))
				} else {
					logf("network self-heal: restarted %s so its published ports apply", ctrName)
				}
			}
		}
	}
	return lines
}

// parseInspectNetworks parses "<name>|<running>|<networks json>" — the
// networks json is `.NetworkSettings.Networks`, one ctrNetAttach per network
// name the container is attached to.
func parseInspectNetworks(raw string) (name string, running bool, attached map[string]ctrNetAttach, err error) {
	parts := strings.SplitN(strings.TrimSpace(raw), "|", 3)
	if len(parts) != 3 {
		return "", false, nil, fmt.Errorf("unexpected inspect output %q", raw)
	}
	if err := json.Unmarshal([]byte(parts[2]), &attached); err != nil {
		return "", false, nil, err
	}
	return strings.TrimPrefix(parts[0], "/"), parts[1] == "true", attached, nil
}

func shortID(id string) string {
	if len(id) > 12 {
		return id[:12]
	}
	return id
}
