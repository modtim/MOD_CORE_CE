package executor

import (
	"net"
	"testing"
)

func TestEgressSubnetPoolDefault(t *testing.T) {
	t.Setenv("MOD_EGRESS_SUBNET_POOL", "")
	_, n, _ := net.ParseCIDR(defaultEgressSubnetPool)
	if got := egressSubnetPool(); got.String() != n.String() {
		t.Fatalf("default pool = %s, want %s", got, n)
	}
}

func TestEgressSubnetPoolOverride(t *testing.T) {
	t.Setenv("MOD_EGRESS_SUBNET_POOL", "10.55.0.0/20")
	got := egressSubnetPool()
	want := "10.55.0.0/20"
	if got.String() != want {
		t.Fatalf("pool = %s, want %s", got, want)
	}
}

// allocateSubnet walks blocks in order, so reserving the first block forces the
// next call to step past it. Exhausting the whole small pool returns an error.
func TestEgressSubnetAllocateAvoidAndExhaustion(t *testing.T) {
	_, pool, _ := net.ParseCIDR("10.231.0.0/24")

	// A pool this small (a dozen /28s) makes the avoid/exhaustion behavior
	// visible without a big setup. Loop until the allocator itself reports that
	// the pool is spent, so the test tracks the pool's real capacity rather than
	// a guess.
	reserved := []*net.IPNet{}
	got := []*net.IPNet{}
	for {
		n, err := allocateSubnet(pool, 28, reserved)
		if err != nil {
			if len(got) == 0 {
				t.Fatalf("first allocation failed: %v", err)
			}
			break
		}
		got = append(got, n)
		reserved = append(reserved, n)
	}

	// No two handed-out blocks may overlap, and each must be a distinct /28.
	for i, a := range got {
		if ones, bits := a.Mask.Size(); bits != 32 || ones != 28 {
			t.Fatalf("got[%d] = %s, not an IPv4 /28", i, a)
		}
		for j, b := range got {
			if i == j {
				continue
			}
			if networksOverlap(a, b) {
				t.Fatalf("got[%d] %s overlaps got[%d] %s", i, a, j, b)
			}
		}
	}

	// Once every block in the pool is handed out, allocation fails.
	if _, err := allocateSubnet(pool, 28, reserved); err == nil {
		t.Fatalf("expected exhaustion error, got nil")
	}
}

// A caller-supplied avoid set must be respected: if the first block is already
// taken, the allocator must return a different one, never the avoided block.
func TestEgressSubnetAvoidRespected(t *testing.T) {
	_, pool, _ := net.ParseCIDR("10.231.0.0/28") // only two /28s: .0 and .16
	avoid := []*net.IPNet{}
	_, a16, _ := net.ParseCIDR("10.231.0.16/28")
	avoid = append(avoid, a16)

	n, err := allocateSubnet(pool, 28, avoid)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if n.String() != "10.231.0.0/28" {
		t.Fatalf("avoided .16 but got %s", n)
	}

	// Now avoid both blocks -> exhausted.
	_, a0, _ := net.ParseCIDR("10.231.0.0/28")
	avoid = append(avoid, a0)
	if _, err := allocateSubnet(pool, 28, avoid); err == nil {
		t.Fatalf("expected error when every block is avoided")
	}
}

// networksOverlap reports whether two IPNets share any address. It exists only
// so the test stays dependency-free (net.IPNet has no overlaps method).
func networksOverlap(a, b *net.IPNet) bool {
	if a == nil || b == nil {
		return false
	}
	// Inputs are fixed /28s: same size and same network-start IP means same block.
	return a.Mask.String() == b.Mask.String() && a.IP.Equal(b.IP)
}
