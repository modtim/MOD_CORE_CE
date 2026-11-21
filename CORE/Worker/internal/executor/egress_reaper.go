package executor

import (
	"context"
	"strings"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/network"
)

// egress networks created by setupEgressProxy live in the mod-egr-* family and
// carry sidecarLabel + this worker's sidecar identity, exactly like the sidecar
// containers they back. The proxy container itself is named
// mod-egr-proxy-<taskID8>, sharing the task-id8 suffix that is also embedded in
// the network name — which is what lets the reaper derive the proxy's name from
// the network's alone.
const (
	egressNetPrefix   = "mod-egr-"
	egressProxyPrefix = "mod-egr-proxy-"
)

// shouldPruneEgressNetwork is the pure policy for a mod-egr-* network that
// carries sidecarLabel. It mirrors shouldPruneLabeled / classifyOwner from
// executor.go: ownership is the primary discriminator (foreign owners are
// provably abandoned), and age is only a backstop — a live network a task is
// still attached to is never reclaimed, regardless of owner.
//
// `attached` is the number of containers on the network EXCLUDING the proxy
// itself, which the caller subtracts (see PruneEgressNetworks). It is only ever
// consulted on the age-backstop branch, where even one attached container keeps
// the network pinned.
func shouldPruneEgressNetwork(name string, labels map[string]string, selfWorker, selfInstance string, age time.Duration, attached int) bool {
	// Only ever touch a network this sidecar system recognizes. A mod-egr-*
	// network without sidecarLabel is not one we manage and is left to its
	// creator regardless of age.
	if !strings.HasPrefix(name, egressNetPrefix) || labels[sidecarLabel] == "" {
		return false
	}
	owner := classifyOwner(labels, selfWorker, selfInstance)
	// Our own earlier incarnation that died mid-task: provably abandoned, and
	// nothing else will ever remove it.
	if owner == ownerOrphan {
		return true
	}
	// Ours-live or another worker's on a shared host (which may be mid-task,
	// exactly as shouldPruneLabeled treats a foreign running sidecar): only the
	// age backstop applies, and a live attachment keeps the network pinned.
	return age > sidecarStuckAfter() && attached == 0
}

// egressProxyName reconstructs the proxy container name from its network name.
// Both share the task-id8 suffix stamped by setupEgressProxy.
func egressProxyName(netName string) string {
	return egressProxyPrefix + strings.TrimPrefix(netName, egressNetPrefix)
}

// containerMatchesExact reports whether a container's name list contains
// proxyName (docker surfaces names with a leading slash).
func containerMatchesExact(names []string, proxyName string) bool {
	for _, n := range names {
		if n == proxyName || n == "/"+proxyName {
			return true
		}
	}
	return false
}

// PruneEgressNetworks removes orphaned per-task egress networks from a previous
// worker incarnation. It lists the mod-egr-* networks carrying sidecarLabel,
// applies shouldPruneEgressNetwork, and for each prunable network removes its
// proxy container first (forceful) before removing the network itself. The
// proxy name is derived from the network name, so no per-network container scan
// is needed.
//
// It never removes a network that still carries containers other than its own
// proxy, and it logs-and-continues on any docker error — a flaky socket must
// not abort the sweep or crash the worker. Returns the number of networks
// removed.
func (e *Executor) PruneEgressNetworks(ctx context.Context) int {
	pruned := 0

	// List only the networks this sidecar system owns. The daemon filters
	// server-side by label, so this returns O(managed networks) rather than
	// O(all host networks) and never surfaces unrelated user networks.
	listCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
	nets, err := e.docker.NetworkList(listCtx, network.ListOptions{
		Filters: filters.NewArgs(filters.Arg("label", sidecarLabel)),
	})
	cancel()
	if err != nil {
		e.log.Warn("prune egress networks: list failed", "err", err)
		return pruned
	}

	for _, n := range nets {
		// n.Containers includes the proxy itself; subtract it so a live proxy
		// on an otherwise-abandoned network doesn't count as an attachment.
		attached := len(n.Containers) - 1
		if attached < 0 {
			attached = 0
		}

		if !shouldPruneEgressNetwork(n.Name, n.Labels, e.workerName, e.instanceID, time.Since(n.Created), attached) {
			continue
		}
		if e.removeEgressNetwork(ctx, n.ID, n.Name) {
			pruned++
		}
	}

	if pruned > 0 {
		e.log.Warn("reclaimed orphaned egress networks from a previous worker",
			"count", pruned, "worker", e.workerName)
	}
	return pruned
}

// removeEgressNetwork force-removes a network's proxy container, then the
// network. Removing the proxy first is required: NetworkRemove refuses a
// non-empty network, and the proxy is what holds it attached.
//
func (e *Executor) removeEgressNetwork(ctx context.Context, id, netName string) bool {
	rmCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	e.removeEgressProxy(rmCtx, egressProxyName(netName))

	if err := e.docker.NetworkRemove(rmCtx, id); err != nil {
		e.log.Debug("prune egress: network remove failed", "net", netName, "err", err)
		return false
	}
	return true
}

// removeEgressProxy finds and force-removes the proxy container for a network,
// matching by exact name so a name filter (which is a substring match) never
// removes an unrelated container. Best-effort: a missing proxy is not an error
// because a proxy-less network is already removable.
func (e *Executor) removeEgressProxy(ctx context.Context, proxyName string) {
	rmCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()

	listCtx, lcancel := context.WithTimeout(rmCtx, 10*time.Second)
	defer lcancel()
	list, err := e.docker.ContainerList(listCtx, container.ListOptions{
		All: true,
		Filters: filters.NewArgs(
			filters.Arg("name", proxyName),
		),
	})
	if err != nil {
		e.log.Debug("prune egress: list proxies failed", "proxy", proxyName, "err", err)
		return
	}

	for _, c := range list {
		if !containerMatchesExact(c.Names, proxyName) {
			continue
		}
		if err := e.docker.ContainerRemove(rmCtx, c.ID, container.RemoveOptions{Force: true}); err != nil {
			e.log.Debug("prune egress: remove proxy failed", "proxy", proxyName, "err", err)
		}
	}
}
