package executor

// Per-task egress subnet allocation.
//
// A task's node container is placed on an egress stack network so its
// outbound traffic exits via the site. That network needs a subnet from a
// MOD-owned pool, chosen the same way stack networks are (see stacksubnet.go):
// avoid every live docker-network subnet, every subnet the worker can see on
// its own interfaces, and every subnet currently reserved, then hand out the
// first free /28 from MOD_EGRESS_SUBNET_POOL (default 10.231.0.0/16). A
// different pool from the stack pool keeps per-task egress from ever colliding
// with a stack network the two allocators might otherwise pick.

import (
	"context"
	"fmt"
	"net"
	"os"
	"time"
)

const defaultEgressSubnetPool = "10.231.0.0/16"

// egressSubnetPool is the configured pool (MOD_EGRESS_SUBNET_POOL) or the default.
func egressSubnetPool() *net.IPNet {
	if nets := parseCIDRList(os.Getenv("MOD_EGRESS_SUBNET_POOL")); len(nets) > 0 {
		return nets[0]
	}
	_, n, _ := net.ParseCIDR(defaultEgressSubnetPool)
	return n
}

// allocateEgressSubnet picks a free /28 from the egress pool, avoiding every
// subnet already used by a docker network, every subnet the worker can see on
// its own interfaces/routes, and every subnet currently reserved. The result
// is reserved under subnetMu so a second concurrent caller cannot pick the same
// block.
func allocateEgressSubnet(ctx context.Context) (*net.IPNet, error) {
	existing, err := dockerNetworkSubnets(ctx)
	if err != nil {
		return nil, fmt.Errorf("egress subnet: docker network ls: %v", err)
	}
	avoid := append(hostVisibleSubnets(), parseCIDRList(os.Getenv("MOD_STACK_NET_AVOID"))...)
	for _, subnets := range existing {
		avoid = append(avoid, subnets...)
	}

	subnetMu.Lock()
	defer subnetMu.Unlock()
	now := time.Now()
	for s, t := range subnetReserved {
		if now.Sub(t) > subnetReserveTTL {
			delete(subnetReserved, s)
		} else if _, n, err := net.ParseCIDR(s); err == nil {
			avoid = append(avoid, n)
		}
	}

	n, err := allocateSubnet(egressSubnetPool(), 28, avoid)
	if err != nil {
		return nil, err
	}
	subnetReserved[n.String()] = now
	return n, nil
}

// releaseEgressSubnet drops the reservation for a subnet previously handed out
// by allocateEgressSubnet. It is a no-op for a subnet that is not reserved.
func releaseEgressSubnet(n *net.IPNet) {
	if n == nil {
		return
	}
	subnetMu.Lock()
	defer subnetMu.Unlock()
	delete(subnetReserved, n.String())
}
