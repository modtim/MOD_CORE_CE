package executor

// MAC.1.s12 — the darwin native-confinement POLICY, exercised on any runner.
//
// The decisions live in native_confine_policy.go (untagged) and take the OS
// name as an argument, so the darwin policy is unit-testable on linux without
// a Mac. The build-tagged twins only apply the decision.

import (
	"errors"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

func specFor(tenantID int) *TaskSpec {
	return &TaskSpec{
		TenantID:      tenantID,
		NativeCommand: "true",
	}
}

// --- posture honesty -------------------------------------------------------

func TestNativeConfinementPostureDarwinIsUnsupported(t *testing.T) {
	// The whole point of MAC.1.s12: a macOS native worker must never be
	// reported as confined. It has a Credential uid drop but no Pdeathsig,
	// no CAP_CHOWN gate, and sandbox-exec is deprecated and unused.
	if got := nativeConfinementPosture("darwin"); got != darwinConfinementUnsupported {
		t.Fatalf("darwin posture must report %q, got %q", darwinConfinementUnsupported, got)
	}
}

func TestNativeConfinementPostureLinuxNamesLayer(t *testing.T) {
	if got := nativeConfinementPosture("linux"); got != "pdeathsig" {
		t.Fatalf("linux posture must name its layer, got %q", got)
	}
}

func TestNativeConfinementPostureUnknownOSFailsClosed(t *testing.T) {
	// Anything that is not a layer we verified reports "unsupported" — never
	// a guessed layer name.
	// (a helper, not an inline literal: keeps the range clause unambiguous)
	for _, osName := range unknownOSNames() {
		if got := nativeConfinementPosture(osName); got != darwinConfinementUnsupported {
			t.Fatalf("os %q must report %q, got %q", osName, darwinConfinementUnsupported, got)
		}
	}
}

// --- darwin tenant gate ----------------------------------------------------

func TestNativeDarwinTenantGateRefusesTenantCode(t *testing.T) {
	err := nativeTenantCodeGate("darwin", specFor(7), nil)
	if err == nil {
		t.Fatal("a darwin native worker must refuse tenant-supplied code by default")
	}
	// The existing sentinel is preserved (errors.Is), so dispatchNativeTask
	// still fails the task without pointless retries.
	if !errors.Is(err, ErrNativeTenantRefused) {
		t.Fatalf("refusal must wrap ErrNativeTenantRefused, got %v", err)
	}
	msg := err.Error()
	if !strings.Contains(msg, "macOS") || !strings.Contains(msg, "TENANT_OWNED") {
		t.Fatalf("darwin refusal must name the macOS/TENANT_OWNED reason, got %q", msg)
	}
	if !strings.Contains(msg, "sandbox-exec is deprecated") {
		t.Fatalf("darwin refusal must state sandbox-exec is not used, got %q", msg)
	}
}

func TestNativeDarwinTenantGatePlatformTaskAllowed(t *testing.T) {
	// Trusted curated recipes are platform/system tasks (TenantID == 0): they
	// run on darwin, that is the whole point of a macOS native worker.
	if err := nativeTenantCodeGate("darwin", specFor(0), nil); err != nil {
		t.Fatalf("platform task must run on darwin, got %v", err)
	}
	if err := nativeTenantCodeGate("darwin", nil, nil); err != nil {
		t.Fatalf("nil spec must not be a tenant refusal, got %v", err)
	}
}

func TestNativeDarwinTenantGateBYOBoxAllowed(t *testing.T) {
	// The TENANT_OWNED/BYO posture is the existing registry setting — no new
	// trust model.
	tun := tunables.New()
	tun.Apply(map[string]interface{}{nativeAllowUnsandboxedTenantCodeKey: true})
	if err := nativeTenantCodeGate("darwin", specFor(42), tun); err != nil {
		t.Fatalf("tenant-owned/BYO worker must allow tenant code, got %v", err)
	}
}

func TestNativeDarwinTenantGateFalseTunableStillRefused(t *testing.T) {
	tun := tunables.New()
	tun.Apply(map[string]interface{}{nativeAllowUnsandboxedTenantCodeKey: false})
	err := nativeTenantCodeGate("darwin", specFor(42), tun)
	if !errors.Is(err, ErrNativeTenantRefused) {
		t.Fatalf("explicit-false tunable must still refuse, got %v", err)
	}
}

// --- darwin uid choice -----------------------------------------------------

func TestNativeDarwinDropUIDUsesDedicatedUserOnlyWhenRoot(t *testing.T) {
	tun := tunables.New()
	tun.Apply(map[string]interface{}{darwinDropUIDKey: 61001})

	// Root worker + configured dedicated user -> the recipe runs as that uid.
	if got := nativeDropUIDForOS("darwin", tun, 0); got != 61001 {
		t.Fatalf("root darwin worker must run the recipe as uid 61001, got %d", got)
	}
	// A non-root macOS worker cannot setuid and is already unprivileged: the
	// recipe runs as the worker's own user (0), never a bogus drop.
	if got := nativeDropUIDForOS("darwin", tun, 1000); got != 0 {
		t.Fatalf("non-root darwin worker must run as its own user, got %d", got)
	}
	// No dedicated user configured -> the worker's own user, not uid 0 drop.
	empty := tunables.New()
	if got := nativeDropUIDForOS("darwin", empty, 0); got != 0 {
		t.Fatalf("unset dedicated user must mean run as the worker, got %d", got)
	}
	// A nil store (nothing pushed) is the same fallback, fail-closed.
	if got := nativeDropUIDForOS("darwin", nil, 0); got != 0 {
		t.Fatalf("nil tunables store must mean run as the worker, got %d", got)
	}
}

func TestNativeDarwinDropUIDPure(t *testing.T) {
	// The pure decision itself, so the darwin wrapper's guard is testable.
	if got := darwinDropUID(61001, 0); got != 61001 {
		t.Fatalf("root + configured uid must drop, got %d", got)
	}
	if got := darwinDropUID(61001, 1000); got != 0 {
		t.Fatalf("non-root worker must not drop, got %d", got)
	}
	if got := darwinDropUID(0, 0); got != 0 {
		t.Fatalf("no configured uid must mean no drop, got %d", got)
	}
}

func TestNativeDropUIDLinuxKeepsExistingKey(t *testing.T) {
	// Linux behaviour is unchanged by MAC.1.s12: worker.native_drop_uid only.
	tun := tunables.New()
	tun.Apply(map[string]interface{}{nativeDropUIDKey: 4242, darwinDropUIDKey: 61001})
	if got := nativeDropUIDForOS("linux", tun, 0); got != 4242 {
		t.Fatalf("linux must read worker.native_drop_uid, got %d", got)
	}
	if got := nativeDropUIDForOS("linux", tun, 1000); got != 0 {
		t.Fatalf("non-root linux worker must not drop, got %d", got)
	}
}

func TestNativeDropUIDWindowsHasNoUID(t *testing.T) {
	tun := tunables.New()
	tun.Apply(map[string]interface{}{nativeDropUIDKey: 4242, darwinDropUIDKey: 61001})
	if got := nativeDropUIDForOS("windows", tun, 0); got != 0 {
		t.Fatalf("windows has no uid concept here, got %d", got)
	}
}

// unknownOSNames are the OSes that are not a confinement layer MOD verified.
func unknownOSNames() []string { return []string{"windows", "plan9", ""} }
